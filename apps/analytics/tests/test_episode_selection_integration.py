"""Real-Postgres coverage for the order the episode's decision is chosen in.

The unit tests above assert what the Python does with rows. This asserts what
the database returns, because the defect lived entirely in the SQL: filtering to
decisions with a completed outcome and only then applying "first opened, else
earliest" is a different rule, and no test that supplied its own rows could see
it.

Skips when the local migrated development Postgres is unavailable, following
this package's own convention, unless REQUIRE_INTEGRATION_DB=1.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.episode_selection import episode_decision_query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_STRATEGY = "test_episode_selection_v1"
_SINCE = datetime(2026, 3, 1, tzinfo=UTC)
_UNTIL = datetime(2026, 3, 2, tzinfo=UTC)
_HORIZON = 60


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


_INSERT_DECISION = text("""
    INSERT INTO app.trade_decisions
        (decision_id, pump_event_id, base, exchange, ts, action, reason,
         strategy_version, features)
    VALUES (:decision_id, :pump_event_id, :base, 'bybit', :ts, :action, 'test',
            :strategy_version, :features)
""")

_INSERT_OUTCOME = text("""
    INSERT INTO app.trade_decision_outcomes
        (decision_id, horizon_minutes, status, short_return_pct)
    VALUES (:decision_id, :horizon, :status, :short_return_pct)
""")


def _features(age_minutes: float) -> str:
    return (
        '{"signal": {"components": {"pump_age": '
        f'{{"value": {age_minutes / 60}, "points": 0, "max": 2, "note": ""}}}}}}'
    )


@pytest.mark.asyncio
async def test_the_episode_keeps_its_own_decision_when_that_outcome_is_unresolved() -> None:
    """The exact shape a colleague reproduced: an episode whose first
    `opened_paper` decision is unresolved and whose later `skipped` decision is
    complete. The episode must come back represented by the opened one, with a
    null outcome, rather than by the skipped one at a different age."""
    engine = await _connect_or_skip()
    episode_id = None
    opened_id, skipped_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            episode_id = (
                await connection.execute(
                    text("""
                        INSERT INTO app.pump_events (base, exchange, first_seen_at, last_seen_at)
                        VALUES ('EPISODESEL', 'bybit', :ts, :ts) RETURNING id
                    """),
                    {"ts": _SINCE},
                )
            ).scalar_one()

            # 0.6 minutes old, opened, outcome still unresolved.
            await connection.execute(
                _INSERT_DECISION,
                {
                    "decision_id": opened_id,
                    "pump_event_id": episode_id,
                    "base": "EPISODESEL",
                    "ts": _SINCE + timedelta(minutes=1),
                    "action": "opened_paper",
                    "strategy_version": _STRATEGY,
                    "features": _features(0.6),
                },
            )
            await connection.execute(
                _INSERT_OUTCOME,
                {
                    "decision_id": opened_id,
                    "horizon": _HORIZON,
                    "status": "pending",
                    "short_return_pct": None,
                },
            )
            # 6 minutes old, skipped, outcome complete. The tempting substitute.
            await connection.execute(
                _INSERT_DECISION,
                {
                    "decision_id": skipped_id,
                    "pump_event_id": episode_id,
                    "base": "EPISODESEL",
                    "ts": _SINCE + timedelta(minutes=7),
                    "action": "skipped",
                    "strategy_version": _STRATEGY,
                    "features": _features(6.0),
                },
            )
            await connection.execute(
                _INSERT_OUTCOME,
                {
                    "decision_id": skipped_id,
                    "horizon": _HORIZON,
                    "status": "complete",
                    "short_return_pct": 3.0,
                },
            )

        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(episode_decision_query("short_return_pct")),
                        {
                            "strategies": [_STRATEGY],
                            "since": _SINCE,
                            "until": _UNTIL,
                            "horizon": _HORIZON,
                        },
                    )
                )
                .mappings()
                .all()
            )

        assert len(rows) == 1, "one row per episode"
        row = rows[0]
        assert (
            row["decision_id"] == opened_id
        ), "the opened decision represents the episode even though it is unresolved"
        assert row["short_return_pct"] is None, "and it carries no outcome, rather than another's"
        assert row["components"]["pump_age"]["value"] == pytest.approx(0.01)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM app.trade_decision_outcomes WHERE decision_id = ANY(:ids)"),
                {"ids": [opened_id, skipped_id]},
            )
            await connection.execute(
                text("DELETE FROM app.trade_decisions WHERE strategy_version = :v"),
                {"v": _STRATEGY},
            )
            if episode_id is not None:
                await connection.execute(
                    text("DELETE FROM app.pump_events WHERE id = :id"), {"id": episode_id}
                )
        await engine.dispose()
