"""Tests for episodes.py -- the durable early_momentum_v3 episode lifecycle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution import episodes


def _mock_conn(
    fetchone_results: list[Any] | None = None,
    fetchall_results: list[list[Any]] | None = None,
    rowcounts: list[int] | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """Same shape as test_journal.py's _mock_conn, extended with fetchall
    (episodes.py's list-returning functions use dict_row + fetchall)."""
    cur = AsyncMock()
    if rowcounts is not None:
        rc_iter = iter(rowcounts)

        async def _execute(*_args: object, **_kwargs: object) -> None:
            cur.rowcount = next(rc_iter)

        cur.execute = AsyncMock(side_effect=_execute)
    else:
        cur.execute = AsyncMock()
    if fetchone_results is not None:
        cur.fetchone = AsyncMock(side_effect=fetchone_results)
    if fetchall_results is not None:
        cur.fetchall = AsyncMock(side_effect=fetchall_results)

    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=cur)
    cur_cm.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_cm)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn, cur


def _episode_row(**overrides: object) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    row = {
        "episode_id": uuid.uuid4(),
        "strategy_id": 1,
        "contract_sha256": b"0" * 32,
        "source_exchange": "binance",
        "source_native_id": "BEATUSDT",
        "exchange": "bybit",
        "native_market_id": "BEATUSDT",
        "execution_symbol": "BEAT/USDT:USDT",
        "execution_identity_key": "exec-key",
        "source_identity_key": "src-key",
        "cluster_key": "BEAT",
        "ceiling": 100.0,
        "features": {},
        "armed_at": now,
        "expires_at": now + timedelta(minutes=60),
        "status": "armed",
        "terminal_reason": None,
        "claim_token": None,
        "claimed_at": None,
        "claim_expires_at": None,
        "claim_attempts": 0,
    }
    row.update(overrides)
    return row


async def test_create_episode_returns_episode_on_success() -> None:
    conn, cur = _mock_conn(fetchone_results=[_episode_row()])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        episode = await episodes.create_episode(
            "postgresql://x",
            strategy_id=1,
            contract_sha256=b"0" * 32,
            source_exchange="binance",
            source_native_id="BEATUSDT",
            exchange="bybit",
            native_market_id="BEATUSDT",
            execution_symbol="BEAT/USDT:USDT",
            execution_identity_key="exec-key",
            source_identity_key="src-key",
            cluster_key="BEAT",
            ceiling=100.0,
            features={},
            ttl_seconds=3600,
        )
    assert episode is not None
    assert episode.status == "armed"
    assert episode.exchange == "bybit"
    query = cur.execute.call_args.args[0]
    assert "ON CONFLICT (exchange, native_market_id) WHERE status IN ('armed', 'claimed')" in query


async def test_create_episode_returns_none_on_live_instrument_conflict() -> None:
    conn, _cur = _mock_conn(fetchone_results=[None])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        episode = await episodes.create_episode(
            "postgresql://x",
            strategy_id=1,
            contract_sha256=b"0" * 32,
            source_exchange="binance",
            source_native_id="BEATUSDT",
            exchange="bybit",
            native_market_id="BEATUSDT",
            execution_symbol="BEAT/USDT:USDT",
            execution_identity_key="exec-key",
            source_identity_key="src-key",
            cluster_key="BEAT",
            ceiling=100.0,
            features={},
            ttl_seconds=3600,
        )
    assert episode is None


async def test_create_episode_db_error_returns_none_not_raises() -> None:
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        episode = await episodes.create_episode(
            "postgresql://x",
            strategy_id=1,
            contract_sha256=b"0" * 32,
            source_exchange="binance",
            source_native_id="BEATUSDT",
            exchange="bybit",
            native_market_id="BEATUSDT",
            execution_symbol="BEAT/USDT:USDT",
            execution_identity_key="exec-key",
            source_identity_key="src-key",
            cluster_key="BEAT",
            ceiling=100.0,
            features={},
            ttl_seconds=3600,
        )
    assert episode is None


async def test_create_rejected_episode_records_reason() -> None:
    conn, cur = _mock_conn()
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        recorded = await episodes.create_rejected_episode(
            "postgresql://x",
            strategy_id=1,
            contract_sha256=b"0" * 32,
            source_exchange="binance",
            source_native_id="BEATUSDT",
            exchange="bybit",
            native_market_id="",
            ceiling=100.0,
            features={},
            reason=episodes.REASON_IDENTITY_UNRESOLVED,
        )
    assert recorded is True
    query, params = cur.execute.call_args.args
    assert params["reason"] == "identity_unresolved"
    # native_market_id falls back to source_native_id when resolution never
    # got far enough to produce an execution-side id.
    assert params["native_market_id"] == "BEATUSDT"
    # Dedup: a still-disqualified candidate must not insert a fresh
    # 'rejected' row on every scanner tick -- the insert is atomically
    # gated by WHERE NOT EXISTS on the same (source_exchange,
    # source_native_id, reason) within the dedup window (colleague review).
    assert "WHERE NOT EXISTS" in query
    assert params["dedup_window_seconds"] == episodes._REJECTED_EPISODE_DEDUP_WINDOW_SECONDS


async def test_create_rejected_episode_dedup_window_is_configurable() -> None:
    conn, cur = _mock_conn()
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        await episodes.create_rejected_episode(
            "postgresql://x",
            strategy_id=1,
            contract_sha256=b"0" * 32,
            source_exchange="binance",
            source_native_id="BEATUSDT",
            exchange="bybit",
            native_market_id="",
            ceiling=100.0,
            features={},
            reason=episodes.REASON_IDENTITY_UNRESOLVED,
            dedup_window_seconds=60,
        )
    params = cur.execute.call_args.args[1]
    assert params["dedup_window_seconds"] == 60


async def test_claim_episode_sql_has_the_corrected_reclaim_predicate() -> None:
    """Regression (colleague review): expires_at > now() must gate BOTH the
    armed and the claimed-and-expired branches, or a genuinely expired
    episode's stale claim could be reclaimed forever instead of terminating."""
    conn, cur = _mock_conn(fetchone_results=[_episode_row(status="claimed")])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await episodes.claim_episode("postgresql://x", episode_id="e1")

    assert outcome.claimed is True
    assert outcome.claim_token is not None
    query = cur.execute.call_args.args[0]
    assert "AND expires_at > now()" in query
    assert "AND (" in query
    assert "status = 'armed'" in query
    assert "status = 'claimed' AND claim_expires_at < now()" in query


async def test_claim_episode_returns_not_claimed_on_no_row() -> None:
    conn, _cur = _mock_conn(fetchone_results=[None])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await episodes.claim_episode("postgresql://x", episode_id="e1")
    assert outcome.claimed is False
    assert outcome.episode is None
    assert outcome.claim_token is None


async def test_terminate_episode_post_claim_is_guarded_by_claim_token() -> None:
    conn, cur = _mock_conn(rowcounts=[1])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        terminated = await episodes.terminate_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            reason=episodes.REASON_INSUFFICIENT_DEPTH,
            status="rejected",
        )
    assert terminated is True
    query, params = cur.execute.call_args.args
    assert "claim_token = %(claim_token)s" in query
    assert params["claim_token"] == "tok-1"  # noqa: S105


async def test_terminate_episode_pre_claim_is_guarded_by_armed_status() -> None:
    conn, cur = _mock_conn(rowcounts=[1])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        terminated = await episodes.terminate_episode(
            "postgresql://x",
            episode_id="e1",
            reason=episodes.REASON_IDENTITY_UNRESOLVED,
        )
    assert terminated is True
    query = cur.execute.call_args.args[0]
    assert "status = 'armed'" in query
    assert "claim_token" not in query


async def test_reap_overdue_terminates_the_three_dead_cases() -> None:
    """A claimed-and-expired row UNDER the attempts cap must never be
    reaped by the infra-failure branch -- it's what list_actionable's
    reclaim branch is for. But a claimed row whose overall expires_at has
    also passed (window expired while claimed) must always be reaped,
    regardless of claim_attempts -- otherwise it hangs forever, since
    list_actionable's reclaim branch no longer picks up expired-window rows
    either."""
    conn, cur = _mock_conn(rowcounts=[3, 2, 1])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        summary = await episodes.reap_overdue("postgresql://x", max_claim_attempts=5)

    assert summary.expired_armed == 3
    assert summary.expired_while_claimed == 2
    assert summary.infrastructure_failed_claims == 1
    first_query, _first_params = cur.execute.call_args_list[0].args
    assert "status = 'armed' AND expires_at < now()" in first_query
    second_query, _second_params = cur.execute.call_args_list[1].args
    assert "status = 'claimed' AND expires_at < now()" in second_query
    third_query, third_params = cur.execute.call_args_list[2].args
    assert "claim_attempts >= %(max_attempts)s" in third_query
    assert "expires_at > now()" in third_query
    assert third_params["max_attempts"] == 5


async def test_list_actionable_covers_armed_and_reclaimable_claimed() -> None:
    conn, cur = _mock_conn(fetchall_results=[[_episode_row(), _episode_row(status="claimed")]])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        rows = await episodes.list_actionable("postgresql://x", max_claim_attempts=5)

    assert len(rows) == 2
    query, params = cur.execute.call_args.args
    assert "status = 'armed' AND expires_at > now()" in query
    # The claimed branch must also require expires_at > now() -- a claimed
    # episode whose overall window has expired is reap_overdue's job
    # (expired_while_claimed), never list_actionable's to keep re-caching.
    assert (
        "status = 'claimed'\n"
        "       AND expires_at > now()\n"
        "       AND claim_expires_at < now()\n"
        "       AND claim_attempts < %(max_attempts)s" in query
    )
    assert params["max_attempts"] == 5


async def test_list_actionable_db_error_returns_empty_list() -> None:
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        rows = await episodes.list_actionable("postgresql://x")
    assert rows == []


async def test_resolve_routes_batch_unresolved_when_ambiguous_or_missing() -> None:
    conn, cur = _mock_conn(
        fetchall_results=[
            [
                {
                    "source_native_id": "AAAUSDT",
                    "source_identity_key": "src-a",
                    "execution_native_id": "AAAUSDT",
                    "execution_identity_key": "exec-a",
                    "cluster_key": "AAA",
                },
                # BBBUSDT: two confirmed matches -> ambiguous -> None
                {
                    "source_native_id": "BBBUSDT",
                    "source_identity_key": "src-b1",
                    "execution_native_id": "BBBUSDT",
                    "execution_identity_key": "exec-b1",
                    "cluster_key": "BBB",
                },
                {
                    "source_native_id": "BBBUSDT",
                    "source_identity_key": "src-b2",
                    "execution_native_id": "BBBUSDT",
                    "execution_identity_key": "exec-b2",
                    "cluster_key": "BBB",
                },
            ]
        ]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        result = await episodes.resolve_routes_batch(
            "postgresql://x",
            source_exchange="binance",
            source_native_ids=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
            execution_exchange="bybit",
        )

    assert result["AAAUSDT"] is not None
    assert result["AAAUSDT"].execution_native_id == "AAAUSDT"
    assert result["BBBUSDT"] is None  # ambiguous
    assert result["CCCUSDT"] is None  # missing entirely
    query = cur.execute.call_args.args[0]
    assert "native_market_id = ANY(%(source_native_ids)s)" in query


async def test_resolve_routes_batch_empty_input_short_circuits() -> None:
    result = await episodes.resolve_routes_batch(
        "postgresql://x",
        source_exchange="binance",
        source_native_ids=[],
        execution_exchange="bybit",
    )
    assert result == {}


async def test_route_still_confirmed_true_and_false() -> None:
    conn_true, _cur = _mock_conn(fetchone_results=[(1,)])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn_true)):
        assert (
            await episodes.route_still_confirmed(
                "postgresql://x", cluster_key="BEAT", exchange="bybit", native_market_id="BEATUSDT"
            )
            is True
        )

    conn_false, _cur = _mock_conn(fetchone_results=[None])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn_false)):
        assert (
            await episodes.route_still_confirmed(
                "postgresql://x", cluster_key="BEAT", exchange="bybit", native_market_id="BEATUSDT"
            )
            is False
        )


async def test_identity_snapshot_age_none_when_no_snapshot_ever_captured() -> None:
    conn, _cur = _mock_conn(fetchone_results=[(None,)])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        age = await episodes.identity_snapshot_age_seconds("postgresql://x", exchange="bybit")
    assert age is None


async def test_identity_snapshot_age_returns_seconds() -> None:
    conn, _cur = _mock_conn(fetchone_results=[(3600.0,)])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        age = await episodes.identity_snapshot_age_seconds("postgresql://x", exchange="bybit")
    assert age == 3600.0


async def test_within_rearm_cooldown_true_and_false() -> None:
    conn_recent, _cur = _mock_conn(fetchone_results=[(1,)])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn_recent)):
        assert (
            await episodes.within_rearm_cooldown(
                "postgresql://x",
                exchange="bybit",
                native_market_id="BEATUSDT",
                cooldown_seconds=1800,
            )
            is True
        )

    conn_none, _cur = _mock_conn(fetchone_results=[None])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn_none)):
        assert (
            await episodes.within_rearm_cooldown(
                "postgresql://x",
                exchange="bybit",
                native_market_id="BEATUSDT",
                cooldown_seconds=1800,
            )
            is False
        )


async def test_within_rearm_cooldown_fails_closed_on_db_error() -> None:
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        blocked = await episodes.within_rearm_cooldown(
            "postgresql://x", exchange="bybit", native_market_id="BEATUSDT", cooldown_seconds=1800
        )
    assert blocked is True


async def test_health_metrics_shape() -> None:
    conn, _cur = _mock_conn(
        fetchone_results=[
            {
                "overdue_armed": 2,
                "expired_claims": 1,
                "identity_stale_rejections_last_hour": 3,
                "oldest_overdue_armed_age_seconds": 20.0,
                "oldest_expired_claim_age_seconds": 45.5,
                "oldest_overdue_age_seconds": 45.5,
            }
        ]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        metrics = await episodes.health_metrics("postgresql://x")
    assert metrics == {
        "overdue_armed": 2,
        "expired_claims": 1,
        "identity_stale_rejections_last_hour": 3,
        "oldest_overdue_armed_age_seconds": 20.0,
        "oldest_expired_claim_age_seconds": 45.5,
        "oldest_overdue_age_seconds": 45.5,
    }


async def test_health_metrics_empty_overdue_returns_none_ages_not_zero() -> None:
    """The two per-status ages must be None (not 0.0) when their count is
    zero -- distinct from a genuine 0-second-old reading, and distinct from
    "couldn't measure". Only oldest_overdue_age_seconds (kept for backward
    compatibility) still defaults to 0.0."""
    conn, _cur = _mock_conn(
        fetchone_results=[
            {
                "overdue_armed": 0,
                "expired_claims": 0,
                "identity_stale_rejections_last_hour": 0,
                "oldest_overdue_armed_age_seconds": None,
                "oldest_expired_claim_age_seconds": None,
                "oldest_overdue_age_seconds": None,
            }
        ]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        metrics = await episodes.health_metrics("postgresql://x")
    assert metrics["oldest_overdue_armed_age_seconds"] is None
    assert metrics["oldest_expired_claim_age_seconds"] is None
    assert metrics["oldest_overdue_age_seconds"] == 0.0


async def test_identity_health_flags_stale_and_unknown_ages() -> None:
    async def _fake_age(_db_url: str, *, exchange: str) -> float | None:
        return None if exchange == "bybit" else 3600.0

    with patch("schurfer_execution.episodes.identity_snapshot_age_seconds", side_effect=_fake_age):
        result = await episodes.identity_health(
            "postgresql://x", exchanges=["binance", "bybit"], max_age_hours=6.0
        )

    assert result["binance"] == {"age_seconds": 3600.0, "stale": False}
    assert result["bybit"] == {"age_seconds": None, "stale": True}


async def test_health_metrics_db_error_returns_none_values_not_raises() -> None:
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        metrics = await episodes.health_metrics("postgresql://x")
    assert metrics["overdue_armed"] is None
    assert metrics["expired_claims"] is None
    assert metrics["oldest_overdue_armed_age_seconds"] is None
    assert metrics["oldest_expired_claim_age_seconds"] is None
    assert metrics["oldest_overdue_age_seconds"] is None
