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

import json
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


# Column lists match the models in packages/journal rather than a guess. Two
# rounds of CI were spent learning that: the first version invented
# `app.pump_events.exchange`, and the second hand-built a JSON string one closing
# brace short. Every NOT NULL column is supplied explicitly, because only
# first_seen_at and last_seen_at carry server defaults -- every other `default=`
# in the models is Python-side and does nothing for a raw INSERT.
_INSERT_EPISODE = text("""
    INSERT INTO app.pump_events
        (base, episode, miss_count, first_seen_at, last_seen_at,
         peak_pct, last_pct, exchanges)
    VALUES (:base, 1, 0, :ts, :ts, 50, 40, '[]'::jsonb)
    RETURNING id
""")

_INSERT_DECISION = text("""
    INSERT INTO app.trade_decisions
        (decision_id, pump_event_id, base, exchange, ts, action, reason,
         strategy_version, features, created_at)
    VALUES (:decision_id, :pump_event_id, :base, 'bybit', :ts, :action, 'test',
            :strategy_version, CAST(:features AS jsonb), :ts)
""")

_INSERT_OUTCOME = text("""
    INSERT INTO app.trade_decision_outcomes
        (decision_id, horizon_minutes, resolver_version, timeframe_minutes,
         status, short_return_pct, bars_count, expected_bars, attempt_count,
         resolved_at)
    VALUES (:decision_id, :horizon, :resolver_version, 5, :status, :short_return_pct,
            0, 0, 1, :ts)
""")


def _features(age_minutes: float) -> str:
    """Serialized rather than hand-written. The hand-written version was one
    closing brace short, which Postgres reported and no local test could."""
    return json.dumps(
        {
            "signal": {
                "components": {
                    "pump_age": {
                        "value": age_minutes / 60,
                        "points": 0,
                        "max": 2,
                        "note": "",
                    }
                }
            }
        }
    )


async def test_episode_selection_rejects_partial_and_alternate_resolver_outcomes() -> None:
    """The reproduced substitution shape, plus both integrity hazards.

    The first opened decision has a partial requested-resolver outcome and a
    complete result from another resolver, while a later skipped decision is
    complete. The episode must keep the opened decision with a null exact
    outcome, without substitution or duplication.
    """
    engine = await _connect_or_skip()
    episode_id = None
    opened_id, skipped_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            episode_id = (
                await connection.execute(_INSERT_EPISODE, {"base": "EPISODESEL", "ts": _SINCE})
            ).scalar_one()

            # 0.6 minutes old, opened. Its requested-resolver outcome is partial,
            # while a second resolver claims a complete result. Neither may be
            # substituted into an exact forward_v1 read.
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
                    "resolver_version": "forward_v1",
                    "status": "partial",
                    "short_return_pct": 9.0,
                    "ts": _SINCE,
                },
            )
            await connection.execute(
                _INSERT_OUTCOME,
                {
                    "decision_id": opened_id,
                    "horizon": _HORIZON,
                    "resolver_version": "other_v2",
                    "status": "complete",
                    "short_return_pct": 99.0,
                    "ts": _SINCE,
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
                    "resolver_version": "forward_v1",
                    "status": "complete",
                    "short_return_pct": 3.0,
                    "ts": _SINCE,
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
                            "resolver_version": "forward_v1",
                        },
                    )
                )
                .mappings()
                .all()
            )

        assert len(rows) == 1, "one row per episode"
        row = rows[0]
        # str() on both sides: the column is uuid, and psycopg returns a UUID
        # object regardless of what was passed in. Comparing the two types
        # directly fails on a pair that is in fact equal.
        assert str(row["decision_id"]) == str(
            opened_id
        ), "the opened decision represents the episode even though it is unresolved"
        assert (
            row["short_return_pct"] is None
        ), "partial and alternate-resolver outcomes stay coverage, not exact evidence"
        assert row["components"]["pump_age"]["value"] == pytest.approx(0.01)
        # The substitution this whole change exists to prevent.
        assert str(row["decision_id"]) != str(skipped_id)
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
