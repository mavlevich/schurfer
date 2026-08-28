"""Real-Postgres coverage for pump_recurrence_integrity_repository.py.

Follows the repository integration-test convention in this package: skips
when the local migrated development Postgres is unavailable, unless
REQUIRE_INTEGRATION_DB=1 is set (CI sets this so a broken/unprovisioned
Postgres service fails the build loudly instead of this test silently
skipping and the run still going green -- mirrors
apps/execution/tests/test_episodes_integration.py's `_connect_or_skip`
convention).

Colleague review (2026-08-28) asked for a repository-level integration test
covering: an event with zero `pump_event_sources` rows (the P1 #2 finding --
`identity_observations_statement`'s inner join makes it invisible to that
query outright, and `build_report`'s coverage accounting depends on
`episodes_statement` independently returning every event so the gap can be
detected), timezone mapping, and the repeatable-read transaction actually
returning both result sets from one snapshot.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.pump_recurrence_integrity_report import PumpRecurrenceIntegrityFilters
from schurfer_analytics.pump_recurrence_integrity_repository import (
    PumpRecurrenceIntegrityRepository,
)
from schurfer_journal.models import PumpEvent, PumpEventSource
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unreachable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


async def test_connect_or_skip_raises_when_require_integration_db_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI-enforcement path itself: with REQUIRE_INTEGRATION_DB=1, an
    unreachable Postgres must fail the test, never silently skip it."""

    class _FailingConnection:
        async def __aenter__(self) -> _FailingConnection:
            raise OSError("connection refused")

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _FailingEngine:
        def connect(self) -> _FailingConnection:
            return _FailingConnection()

        async def dispose(self) -> None:
            return None

    monkeypatch.setenv("REQUIRE_INTEGRATION_DB", "1")
    monkeypatch.setattr(
        sys.modules[__name__],
        "create_async_engine",
        lambda *_args, **_kwargs: _FailingEngine(),
    )
    with pytest.raises(RuntimeError, match="REQUIRE_INTEGRATION_DB"):
        await _connect_or_skip()


async def test_load_returns_every_event_even_without_source_rows() -> None:
    """Two events in the filter window: one with a `pump_event_sources` row,
    one with none. `episodes_statement` must return both (it has no join);
    `identity_observations_statement` must return an observation only for
    the one that has a source row -- the gap between the two result sets is
    exactly what `build_report` turns into `events_without_source_observations`.
    """
    engine = await _connect_or_skip()
    run_id = uuid.uuid4().hex
    base_with_source = f"TESTWS{run_id[:8]}".upper()
    base_without_source = f"TESTNS{run_id[:8]}".upper()
    first_seen = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    event_with_source_id: int | None = None
    event_without_source_id: int | None = None
    try:
        async with engine.begin() as connection:
            event_with_source_id = (
                await connection.execute(
                    insert(PumpEvent)
                    .values(
                        base=base_with_source,
                        episode=1,
                        first_seen_at=first_seen,
                        last_seen_at=first_seen + timedelta(minutes=5),
                        peak_pct=42.0,
                        last_pct=30.0,
                        exchanges=["gate"],
                    )
                    .returning(PumpEvent.id)
                )
            ).scalar_one()
            event_without_source_id = (
                await connection.execute(
                    insert(PumpEvent)
                    .values(
                        base=base_without_source,
                        episode=1,
                        first_seen_at=first_seen,
                        last_seen_at=first_seen + timedelta(minutes=5),
                        peak_pct=17.0,
                        last_pct=10.0,
                        exchanges=["mexc"],
                    )
                    .returning(PumpEvent.id)
                )
            ).scalar_one()
            await connection.execute(
                insert(PumpEventSource).values(
                    event_id=event_with_source_id,
                    exchange="gate",
                    symbol=f"{base_with_source}/USDT:USDT",
                    identity_key=f"gate:{base_with_source}/USDT:USDT",
                    unified_symbol=f"{base_with_source}/USDT:USDT",
                    base_asset=base_with_source,
                    identity_conflict=False,
                    first_change_pct=10.0,
                    last_change_pct=30.0,
                    peak_change_pct=42.0,
                )
            )

        # Wraps the same `engine` this test uses for cleanup below rather than
        # `PumpRecurrenceIntegrityRepository.from_url(...)` -- deliberately
        # never call `repository.close()` (it disposes the engine), so the
        # cleanup DELETE below still has a live connection to run on.
        repository = PumpRecurrenceIntegrityRepository(engine)
        episodes, identity_observations = await repository.load(
            PumpRecurrenceIntegrityFilters(
                since=first_seen - timedelta(minutes=1),
                until=first_seen + timedelta(minutes=1),
            )
        )

        episode_event_ids = {episode.event_id for episode in episodes}
        assert event_with_source_id in episode_event_ids
        assert event_without_source_id in episode_event_ids

        identity_event_ids = {observation.event_id for observation in identity_observations}
        assert event_with_source_id in identity_event_ids
        assert event_without_source_id not in identity_event_ids

        # Timezone mapping: values round-trip as UTC-aware datetimes.
        loaded = next(episode for episode in episodes if episode.event_id == event_with_source_id)
        assert loaded.first_seen_at.tzinfo is not None
        assert loaded.first_seen_at == first_seen
    finally:
        async with engine.begin() as connection:
            event_ids = tuple(
                event_id
                for event_id in (event_with_source_id, event_without_source_id)
                if event_id is not None
            )
            if event_ids:
                await connection.execute(delete(PumpEvent).where(PumpEvent.id.in_(event_ids)))
        await engine.dispose()
