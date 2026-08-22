"""Tests for early_momentum.py -- the v3 episode-lifecycle orchestration.

episodes.py itself is unit-tested in test_episodes.py; these tests focus on
how early_momentum.py wires the scanner/trigger loops around it (which
episodes.* calls happen in what order, and with what arguments).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest
from schurfer_execution import early_momentum, episodes
from schurfer_execution.journal import OpenTradeOutcome
from schurfer_execution.symbols import ExecutionInstrument


def _cfg(**overrides: object) -> MagicMock:
    cfg = MagicMock(
        db_url="postgresql://x",
        liquidity_depth_multiplier=2.0,
        max_spread_bps=50.0,
        max_liquidity_impact_bps=50.0,
        early_momentum_rearm_cooldown_seconds=1800,
        identity_snapshot_max_age_hours=6.0,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _episode(**overrides: object) -> episodes.Episode:
    now = datetime.now(tz=UTC)
    fields = {
        "episode_id": "e1",
        "strategy_id": 1,
        "contract_sha256": early_momentum.CONTRACT_SHA256,
        "source_exchange": "binance",
        "source_native_id": "BEATUSDT",
        "exchange": "bybit",
        "native_market_id": "BEATUSDT",
        "execution_symbol": None,
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
    fields.update(overrides)
    return episodes.Episode(**fields)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> dict[str, Any]:
    row = {
        "exchange": "binance",
        "symbol": "BEATUSDT",
        "bucket_start": datetime.now(tz=UTC),
        "close_price": 1.0,
        "open_interest": 1050.0,
        "oi_start_2h": 1000.0,
        "price_max_2h": 1.0,
        "price_min_2h": 0.98,
    }
    row.update(overrides)
    return row


def _route() -> episodes.BatchRoute:
    return episodes.BatchRoute(
        source_native_id="BEATUSDT",
        source_identity_key="src-key",
        execution_native_id="BEATUSDT",
        execution_identity_key="exec-key",
        cluster_key="BEAT",
    )


def _exchange() -> MagicMock:
    ex = MagicMock()
    ex.id = "bybit"
    ex.markets = {
        "BEAT/USDT:USDT": {
            "id": "BEATUSDT",
            "symbol": "BEAT/USDT:USDT",
            "base": "BEAT",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        }
    }
    return ex


# --- contract hash ---


def test_contract_sha256_is_deterministic_and_32_bytes() -> None:
    assert isinstance(early_momentum.CONTRACT_SHA256, bytes)
    assert len(early_momentum.CONTRACT_SHA256) == 32
    # Recomputing from the same fixed payload must produce the identical
    # digest -- no non-deterministic ordering/float formatting involved.
    import hashlib
    import json

    recomputed = hashlib.sha256(
        json.dumps(early_momentum._CONTRACT_PAYLOAD, sort_keys=True).encode()
    ).digest()
    assert recomputed == early_momentum.CONTRACT_SHA256


# --- scanner ---


async def test_scanner_disabled_without_db_url() -> None:
    rdb = MagicMock()
    with patch("schurfer_execution.early_momentum._scan_once", new_callable=AsyncMock) as scan:
        await early_momentum.run_early_momentum_scanner(rdb, _cfg(db_url=None))
    scan.assert_not_awaited()


async def test_scan_once_rejects_when_identity_catalog_is_stale() -> None:
    rdb = AsyncMock()
    cfg = _cfg()
    with (
        patch(
            "schurfer_execution.early_momentum.psycopg.AsyncConnection.connect",
            new_callable=AsyncMock,
        ) as mock_connect,
        patch(
            "schurfer_execution.early_momentum.journal.ensure_strategy",
            AsyncMock(return_value=1),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.identity_snapshot_age_seconds",
            AsyncMock(return_value=999_999.0),  # far older than max_age_hours
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.resolve_routes_batch",
            AsyncMock(return_value={"BEATUSDT": _route()}),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.create_rejected_episode",
            new_callable=AsyncMock,
        ) as create_rejected,
        patch(
            "schurfer_execution.early_momentum.episodes.create_episode", new_callable=AsyncMock
        ) as create,
    ):
        cur = AsyncMock()
        cur.execute = AsyncMock()
        cur.fetchall = AsyncMock(return_value=[_candidate()])
        cur_cm = MagicMock()
        cur_cm.__aenter__ = AsyncMock(return_value=cur)
        cur_cm.__aexit__ = AsyncMock(return_value=False)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur_cm)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        mock_connect.return_value = conn

        await early_momentum._scan_once(rdb, cfg)

    create_rejected.assert_awaited_once()
    assert create_rejected.call_args.kwargs["reason"] == episodes.REASON_IDENTITY_CATALOG_STALE
    create.assert_not_awaited()


async def test_scan_once_rejects_when_route_unresolved() -> None:
    rdb = AsyncMock()
    with (
        patch("schurfer_execution.early_momentum.psycopg.AsyncConnection.connect"),
        patch(
            "schurfer_execution.early_momentum.journal.ensure_strategy",
            AsyncMock(return_value=1),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.identity_snapshot_age_seconds",
            AsyncMock(return_value=60.0),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.resolve_routes_batch",
            AsyncMock(return_value={"BEATUSDT": None}),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.create_rejected_episode",
            new_callable=AsyncMock,
        ) as create_rejected,
    ):
        await early_momentum._process_candidate(
            rdb,
            _cfg(),
            candidate=_candidate(),
            strategy_id=1,
            source_exchange="binance",
            route=None,
            catalog_stale=False,
        )

    create_rejected.assert_awaited_once()
    assert create_rejected.call_args.kwargs["reason"] == episodes.REASON_IDENTITY_UNRESOLVED


async def test_process_candidate_rejects_within_rearm_cooldown() -> None:
    rdb = AsyncMock()
    with (
        patch(
            "schurfer_execution.early_momentum.episodes.within_rearm_cooldown",
            AsyncMock(return_value=True),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.create_rejected_episode",
            new_callable=AsyncMock,
        ) as create_rejected,
        patch(
            "schurfer_execution.early_momentum.episodes.create_episode", new_callable=AsyncMock
        ) as create,
    ):
        await early_momentum._process_candidate(
            rdb,
            _cfg(),
            candidate=_candidate(),
            strategy_id=1,
            source_exchange="binance",
            route=_route(),
            catalog_stale=False,
        )

    create_rejected.assert_awaited_once()
    assert create_rejected.call_args.kwargs["reason"] == episodes.REASON_REARM_COOLDOWN
    create.assert_not_awaited()


async def test_process_candidate_arms_episode_and_writes_watch_cache() -> None:
    rdb = AsyncMock()
    ep = _episode()
    with (
        patch(
            "schurfer_execution.early_momentum.episodes.within_rearm_cooldown",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.create_episode",
            AsyncMock(return_value=ep),
        ) as create,
    ):
        await early_momentum._process_candidate(
            rdb,
            _cfg(),
            candidate=_candidate(),
            strategy_id=1,
            source_exchange="binance",
            route=_route(),
            catalog_stale=False,
        )

    create.assert_awaited_once()
    kw = create.call_args.kwargs
    assert kw["contract_sha256"] == early_momentum.CONTRACT_SHA256
    assert kw["execution_symbol"] is None  # not known without a live exchange client
    rdb.set.assert_awaited_once()
    key, payload = rdb.set.call_args.args[:2]
    assert key == "market:early_momentum:v3:watch:e1"
    import json

    stored = json.loads(payload)
    assert stored["episode_id"] == "e1"
    assert stored["native_market_id"] == "BEATUSDT"


async def test_process_candidate_writes_nothing_on_live_instrument_conflict() -> None:
    """create_episode returning None means the immutable-WATCH rule already
    fired (another live episode watches this instrument) -- no cache write."""
    rdb = AsyncMock()
    with (
        patch(
            "schurfer_execution.early_momentum.episodes.within_rearm_cooldown",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.create_episode",
            AsyncMock(return_value=None),
        ),
    ):
        await early_momentum._process_candidate(
            rdb,
            _cfg(),
            candidate=_candidate(),
            strategy_id=1,
            source_exchange="binance",
            route=_route(),
            catalog_stale=False,
        )

    rdb.set.assert_not_awaited()


# --- trigger tick: reap + reconciliation ---


async def test_trigger_tick_reaps_and_lists_actionable_even_with_no_watch_keys() -> None:
    rdb = MagicMock()
    rdb.exists = AsyncMock(return_value=True)

    async def _scan_iter(_pattern: str) -> AsyncIterator[bytes]:
        return
        yield  # pragma: no cover - makes this an async generator with 0 items

    rdb.scan_iter = _scan_iter

    with (
        patch(
            "schurfer_execution.early_momentum.episodes.reap_overdue", new_callable=AsyncMock
        ) as reap,
        patch(
            "schurfer_execution.early_momentum.episodes.list_actionable",
            AsyncMock(return_value=[]),
        ) as listed,
        patch(
            "schurfer_execution.early_momentum.paper.reconcile_missing_positions",
            new_callable=AsyncMock,
        ) as reconcile,
    ):
        await early_momentum._trigger_tick({"bybit": _exchange()}, rdb, _cfg())

    reap.assert_awaited_once()
    listed.assert_awaited_once()
    reconcile.assert_awaited_once()


async def test_trigger_tick_repairs_missing_watch_cache_from_actionable() -> None:
    rdb = MagicMock()
    rdb.exists = AsyncMock(return_value=False)  # cache entry missing
    rdb.set = AsyncMock()

    async def _scan_iter(_pattern: str) -> AsyncIterator[bytes]:
        return
        yield  # pragma: no cover

    rdb.scan_iter = _scan_iter
    ep = _episode()

    with (
        patch("schurfer_execution.early_momentum.episodes.reap_overdue", new_callable=AsyncMock),
        patch(
            "schurfer_execution.early_momentum.episodes.list_actionable",
            AsyncMock(return_value=[ep]),
        ),
        patch(
            "schurfer_execution.early_momentum.paper.reconcile_missing_positions",
            new_callable=AsyncMock,
        ),
    ):
        await early_momentum._trigger_tick({"bybit": _exchange()}, rdb, _cfg())

    rdb.set.assert_awaited_once()
    assert rdb.set.call_args.args[0] == "market:early_momentum:v3:watch:e1"


# --- breakout handling ---


async def test_check_breakout_terminates_suppressed_when_already_open() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    ex.markets["BEAT/USDT:USDT"]["active"] = True
    tickers = {"BEAT/USDT:USDT": {"last": 101.0}}
    cached = {
        "episode_id": "e1",
        "ceiling": 100.0,
        "native_market_id": "BEATUSDT",
        "source_exchange": "binance",
        "source_native_id": "BEATUSDT",
    }

    with (
        patch(
            "schurfer_execution.early_momentum.journal.find_open_trade_id",
            AsyncMock(return_value=42),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.terminate_episode",
            new_callable=AsyncMock,
        ) as terminate,
        patch(
            "schurfer_execution.early_momentum.episodes.claim_episode", new_callable=AsyncMock
        ) as claim,
    ):
        await early_momentum._check_breakout(ex, rdb, _cfg(), cached=cached, tickers=tickers)

    terminate.assert_awaited_once()
    kw = terminate.call_args.kwargs
    assert kw["reason"] == episodes.REASON_ALREADY_OPEN
    assert kw["status"] == episodes.STATUS_SUPPRESSED
    claim.assert_not_awaited()
    rdb.delete.assert_awaited_once_with("market:early_momentum:v3:watch:e1")


async def test_check_breakout_below_ceiling_does_nothing() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    tickers = {"BEAT/USDT:USDT": {"last": 99.0}}  # below ceiling
    cached = {
        "episode_id": "e1",
        "ceiling": 100.0,
        "native_market_id": "BEATUSDT",
        "source_exchange": "binance",
        "source_native_id": "BEATUSDT",
    }

    await early_momentum._check_breakout(ex, rdb, _cfg(), cached=cached, tickers=tickers)

    rdb.delete.assert_not_awaited()


async def test_check_breakout_noop_when_claim_fails() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    tickers = {"BEAT/USDT:USDT": {"last": 101.0}}
    cached = {
        "episode_id": "e1",
        "ceiling": 100.0,
        "native_market_id": "BEATUSDT",
        "source_exchange": "binance",
        "source_native_id": "BEATUSDT",
    }

    with (
        patch(
            "schurfer_execution.early_momentum.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.claim_episode",
            AsyncMock(
                return_value=episodes.ClaimOutcome(claimed=False, episode=None, claim_token=None)
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.paper.reserve_position", new_callable=AsyncMock
        ) as reserve,
    ):
        await early_momentum._check_breakout(ex, rdb, _cfg(), cached=cached, tickers=tickers)

    reserve.assert_not_awaited()


async def test_check_breakout_terminates_route_invalidated() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    tickers = {"BEAT/USDT:USDT": {"last": 101.0}}
    cached = {
        "episode_id": "e1",
        "ceiling": 100.0,
        "native_market_id": "BEATUSDT",
        "source_exchange": "binance",
        "source_native_id": "BEATUSDT",
    }
    ep = _episode(status="claimed", claim_token="tok-1")  # noqa: S106

    with (
        patch(
            "schurfer_execution.early_momentum.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.claim_episode",
            AsyncMock(
                return_value=episodes.ClaimOutcome(claimed=True, episode=ep, claim_token="tok-1")  # noqa: S106
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.set_execution_symbol",
            new_callable=AsyncMock,
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.route_still_confirmed",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.terminate_episode",
            new_callable=AsyncMock,
        ) as terminate,
        patch(
            "schurfer_execution.early_momentum.paper.reserve_position", new_callable=AsyncMock
        ) as reserve,
    ):
        await early_momentum._check_breakout(ex, rdb, _cfg(), cached=cached, tickers=tickers)

    terminate.assert_awaited_once()
    assert terminate.call_args.kwargs["reason"] == episodes.REASON_ROUTE_INVALIDATED
    reserve.assert_not_awaited()


async def test_check_breakout_terminates_suppressed_on_reservation_conflict() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    tickers = {"BEAT/USDT:USDT": {"last": 101.0}}
    cached = {
        "episode_id": "e1",
        "ceiling": 100.0,
        "native_market_id": "BEATUSDT",
        "source_exchange": "binance",
        "source_native_id": "BEATUSDT",
    }
    ep = _episode(status="claimed", claim_token="tok-1")  # noqa: S106

    with (
        patch(
            "schurfer_execution.early_momentum.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.claim_episode",
            AsyncMock(
                return_value=episodes.ClaimOutcome(claimed=True, episode=ep, claim_token="tok-1")  # noqa: S106
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.set_execution_symbol",
            new_callable=AsyncMock,
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.route_still_confirmed",
            AsyncMock(return_value=True),
        ),
        patch(
            "schurfer_execution.early_momentum.paper.reserve_position",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.early_momentum.paper.release_reservation",
            new_callable=AsyncMock,
        ) as release,
        patch(
            "schurfer_execution.early_momentum.episodes.terminate_episode",
            new_callable=AsyncMock,
        ) as terminate,
    ):
        await early_momentum._check_breakout(ex, rdb, _cfg(), cached=cached, tickers=tickers)

    terminate.assert_awaited_once()
    kw = terminate.call_args.kwargs
    assert kw["reason"] == episodes.REASON_POSITION_EXISTS
    assert kw["status"] == episodes.STATUS_SUPPRESSED
    # Reservation was never acquired -- nothing to release.
    release.assert_not_awaited()


# --- quote and open ---


def _healthy_book() -> dict[str, Any]:
    return {"bids": [[99.9, 1000.0]], "asks": [[100.1, 1000.0]]}


async def test_quote_and_open_terminates_on_market_quality_gate_failure() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    ex.fetch_order_book = AsyncMock(
        return_value={"bids": [[99.9, 1000.0]], "asks": [[100.1, 0.001]]}
    )

    with (
        patch(
            "schurfer_execution.early_momentum.episodes.terminate_episode",
            new_callable=AsyncMock,
        ) as terminate,
        patch(
            "schurfer_execution.early_momentum.paper.open_paper_for_episode",
            new_callable=AsyncMock,
        ) as open_paper,
    ):
        await early_momentum._quote_and_open(
            ex,
            rdb,
            _cfg(),
            instrument=_instrument(),
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            source_exchange="binance",
            source_native_id="BEATUSDT",
            ceiling=100.0,
        )

    terminate.assert_awaited_once()
    assert terminate.call_args.kwargs["reason"] == episodes.REASON_INSUFFICIENT_DEPTH
    open_paper.assert_not_awaited()


async def test_quote_and_open_calls_open_paper_for_episode_with_full_context() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    ex.fetch_order_book = AsyncMock(return_value=_healthy_book())

    with patch(
        "schurfer_execution.early_momentum.paper.open_paper_for_episode",
        AsyncMock(
            return_value=OpenTradeOutcome(
                trade_id=42, created=True, recovered=False, claim_valid=True
            )
        ),
    ) as open_paper:
        await early_momentum._quote_and_open(
            ex,
            rdb,
            _cfg(),
            instrument=_instrument(),
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            source_exchange="binance",
            source_native_id="BEATUSDT",
            ceiling=100.0,
        )

    open_paper.assert_awaited_once()
    kw = open_paper.call_args.kwargs
    assert kw["episode_id"] == "e1"
    assert kw["claim_token"] == "tok-1"  # noqa: S105
    assert kw["entry_idempotency_key"] == "e1:entry:base"
    assert kw["side"] == "long"
    setup_context = kw["setup_context"]
    assert setup_context["strategy"] == "early_momentum_v3"
    assert setup_context["episode_id"] == "e1"
    assert setup_context["entry_price_includes_impact"] is True


async def test_quote_and_open_terminates_infra_failure_when_open_fails_but_claim_valid() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    ex.fetch_order_book = AsyncMock(return_value=_healthy_book())
    with (
        patch(
            "schurfer_execution.early_momentum.paper.open_paper_for_episode",
            AsyncMock(
                return_value=OpenTradeOutcome(
                    trade_id=None, created=False, recovered=False, claim_valid=True
                )
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.terminate_episode",
            new_callable=AsyncMock,
        ) as terminate,
    ):
        await early_momentum._quote_and_open(
            ex,
            rdb,
            _cfg(),
            instrument=_instrument(),
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            source_exchange="binance",
            source_native_id="BEATUSDT",
            ceiling=100.0,
        )

    terminate.assert_awaited_once()
    assert terminate.call_args.kwargs["reason"] == episodes.REASON_INFRASTRUCTURE_FAILURE


async def test_quote_and_open_does_not_terminate_when_claim_already_invalid() -> None:
    """A reclaimed/expired claim under us has already moved on -- our own
    stale token can't terminate it, and shouldn't try."""
    rdb = AsyncMock()
    ex = _exchange()
    ex.fetch_order_book = AsyncMock(return_value=_healthy_book())
    with (
        patch(
            "schurfer_execution.early_momentum.paper.open_paper_for_episode",
            AsyncMock(
                return_value=OpenTradeOutcome(
                    trade_id=None, created=False, recovered=False, claim_valid=False
                )
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.terminate_episode",
            new_callable=AsyncMock,
        ) as terminate,
    ):
        await early_momentum._quote_and_open(
            ex,
            rdb,
            _cfg(),
            instrument=_instrument(),
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            source_exchange="binance",
            source_native_id="BEATUSDT",
            ceiling=100.0,
        )

    terminate.assert_not_awaited()


async def test_check_breakout_releases_reservation_even_when_quote_and_open_raises() -> None:
    rdb = AsyncMock()
    ex = _exchange()
    tickers = {"BEAT/USDT:USDT": {"last": 101.0}}
    cached = {
        "episode_id": "e1",
        "ceiling": 100.0,
        "native_market_id": "BEATUSDT",
        "source_exchange": "binance",
        "source_native_id": "BEATUSDT",
    }
    ep = _episode(status="claimed", claim_token="tok-1")  # noqa: S106

    with (
        patch(
            "schurfer_execution.early_momentum.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.claim_episode",
            AsyncMock(
                return_value=episodes.ClaimOutcome(claimed=True, episode=ep, claim_token="tok-1")  # noqa: S106
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.set_execution_symbol",
            new_callable=AsyncMock,
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.route_still_confirmed",
            AsyncMock(return_value=True),
        ),
        patch(
            "schurfer_execution.early_momentum.paper.reserve_position",
            AsyncMock(return_value=True),
        ),
        patch(
            "schurfer_execution.early_momentum.paper.release_reservation",
            new_callable=AsyncMock,
        ) as release,
        patch(
            "schurfer_execution.early_momentum._quote_and_open",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await early_momentum._check_breakout(ex, rdb, _cfg(), cached=cached, tickers=tickers)

    release.assert_awaited_once()


def _instrument() -> ExecutionInstrument:
    return ExecutionInstrument(
        exchange="bybit",
        symbol="BEAT/USDT:USDT",
        native_market_id="BEATUSDT",
        base="BEAT",
        quote="USDT",
        settle="USDT",
        market_type="swap",
    )
