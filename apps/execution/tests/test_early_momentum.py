"""Tests for early_momentum.py -- the v4 episode-lifecycle orchestration.

episodes.py itself is unit-tested in test_episodes.py; these tests focus on
how early_momentum.py wires the scanner/trigger loops around it (which
episodes.* calls happen in what order, and with what arguments), plus v4's
input-quality gating (window evidence -> validate -> signal) and the
worker-heartbeat wiring around both loops.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution import early_momentum, episodes
from schurfer_execution.execution_intent import (
    DisabledBroker,
    ExecutionResult,
    ExecutionStatus,
    PaperBroker,
    TradingMode,
)
from schurfer_execution.journal import OpenTradeOutcome
from schurfer_execution.supervisor import WorkerReadinessGate
from schurfer_execution.symbols import ExecutionInstrument
from schurfer_execution.worker_health import WorkerHeartbeat
from schurfer_market_quality import SeriesIdentity, WindowQualityEvidence, WindowQualityResult


def _open_gate() -> WorkerReadinessGate:
    return WorkerReadinessGate(set())


async def _empty_scan_iter(_pattern: str) -> Any:
    # A for-loop over an empty tuple, not a return-then-yield, so vulture's
    # "unreachable code after return" check doesn't trip on this -- same
    # idiom as test_monitor.py's _async_iter, specialized to zero items.
    for item in ():
        yield item


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


def _scanner_row(**overrides: object) -> dict[str, Any]:
    """A raw _SQL_SCANNER result row that is quality-clean AND
    signal-qualifying by default -- individual tests override just the
    field(s) they care about."""
    now = datetime.now(tz=UTC)
    row: dict[str, Any] = {
        "exchange": "binance",
        "market_type": "linear",
        "symbol": "BEATUSDT",
        "window_start": now - timedelta(minutes=120),
        "window_end": now,
        "raw_row_count": 121,
        "distinct_bucket_count": 121,
        "max_gap_seconds": 60.0,
        "capture_versions": ["v1"],
        "universe_versions": ["uv1"],
        "price_complete_count": 121,
        "trades_complete_count": 121,
        "oi_complete_count": 121,
        "first_oi_event_at": now - timedelta(minutes=120),
        "latest_oi_event_at": now - timedelta(seconds=30),
        "unbackfilled_gap_minutes_sum": 0,
        "has_future_timestamp": False,
        "has_invalid_price": False,
        "has_invalid_open_interest": False,
        "has_duplicate_bucket": False,
        "oi_start": 1000.0,
        "oi_latest": 1100.0,  # +10% growth, above the 5% signal threshold
        "price_max": 1.0,
        "price_min": 0.99,  # ~1% range, below the 3% signal threshold
        "buy_vol": 600.0,
        "sell_vol": 100.0,  # net positive taker flow
    }
    row.update(overrides)
    return row


def _evidence(**overrides: object) -> WindowQualityEvidence:
    now = datetime.now(tz=UTC)
    fields: dict[str, object] = {
        "identity": SeriesIdentity(exchange="binance", market_type="linear", symbol="BEATUSDT"),
        "window_start": now - timedelta(minutes=120),
        "window_end": now,
        "raw_row_count": 121,
        "distinct_bucket_count": 121,
        "max_gap_seconds": 60.0,
        "latest_bucket_start": now,
        "capture_versions": ("v1",),
        "universe_versions": ("uv1",),
        "price_complete_count": 121,
        "trades_complete_count": 121,
        "oi_complete_count": 121,
        "first_oi_event_at": now - timedelta(minutes=120),
        "latest_oi_event_at": now - timedelta(seconds=30),
        "unbackfilled_gap_minutes_sum": 0,
        "has_future_timestamp": False,
        "has_invalid_price": False,
        "has_invalid_open_interest": False,
        "has_duplicate_bucket": False,
    }
    fields.update(overrides)
    return WindowQualityEvidence(**fields)  # type: ignore[arg-type]


def _quality_result(**overrides: object) -> WindowQualityResult:
    return WindowQualityResult(evidence=_evidence(), reasons=())  # type: ignore[arg-type]


def _signal(*, qualified: bool = True) -> early_momentum.EarlyMomentumSignalResult:
    features = early_momentum.EarlyMomentumSignalFeatures(
        oi_growth_pct=0.10,
        price_range_pct=0.01,
        net_taker_flow_usd=500.0,
        ceiling=100.0,
        bucket_start=datetime.now(tz=UTC),
    )
    return early_momentum.EarlyMomentumSignalResult(features=features, qualified=qualified)


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


def test_contract_payload_includes_the_quality_policy() -> None:
    quality = early_momentum._CONTRACT_PAYLOAD["scanner"]["quality"]
    assert quality == early_momentum.EARLY_MOMENTUM_V4_QUALITY_POLICY.to_canonical_dict()
    assert quality["required_bucket_count"] == 121


def test_strategy_version_and_watch_prefix_are_v4() -> None:
    assert early_momentum._STRATEGY_VERSION == "4"
    assert early_momentum._WATCH_KEY_PREFIX == "market:early_momentum:v4:watch:"


def test_contract_hash_matches_the_pinned_checked_literal() -> None:
    """The whole point of pinning a literal: a silent edit to any of
    _CONTRACT_PAYLOAD's inputs must fail the import, not mint a new hash
    that every subsequently-armed episode starts using unnoticed
    (feat/early-momentum-prospective-cohort-v1, colleague review,
    2026-08-25)."""
    assert early_momentum.CONTRACT_SHA256_HEX == early_momentum._EXPECTED_CONTRACT_SHA256_HEX


def test_verify_contract_hash_pinned_passes_on_a_match() -> None:
    early_momentum._verify_contract_hash_pinned("abc", "abc", strategy_version="4")


def test_verify_contract_hash_pinned_raises_on_any_mismatch() -> None:
    with pytest.raises(RuntimeError, match="contract changed without an explicit version"):
        early_momentum._verify_contract_hash_pinned("abc", "def", strategy_version="4")


def test_changing_any_contract_input_changes_the_hash() -> None:
    """One parameter at a time: exit_params, size_usd, leverage, signal
    thresholds, and the quality policy each independently change
    CONTRACT_SHA256 -- required regression coverage
    (feat/early-momentum-prospective-cohort-v1)."""
    import copy
    import hashlib
    import json

    base_hash = hashlib.sha256(
        json.dumps(early_momentum._CONTRACT_PAYLOAD, sort_keys=True).encode()
    ).digest()

    mutations: list[dict[str, Any]] = []
    exit_params_changed = copy.deepcopy(early_momentum._CONTRACT_PAYLOAD)
    exit_params_changed["exit_params"] = {
        **exit_params_changed["exit_params"],
        "take_profit_pct": 5.0,
    }
    mutations.append(exit_params_changed)

    size_changed = copy.deepcopy(early_momentum._CONTRACT_PAYLOAD)
    size_changed["size_usd"] = 200.0
    mutations.append(size_changed)

    leverage_changed = copy.deepcopy(early_momentum._CONTRACT_PAYLOAD)
    leverage_changed["leverage"] = 10
    mutations.append(leverage_changed)

    signal_changed = copy.deepcopy(early_momentum._CONTRACT_PAYLOAD)
    signal_changed["signal"] = {**signal_changed["signal"], "oi_growth_min_pct": 0.10}
    mutations.append(signal_changed)

    quality_changed = copy.deepcopy(early_momentum._CONTRACT_PAYLOAD)
    quality_changed["scanner"] = {
        "quality": {**quality_changed["scanner"]["quality"], "required_bucket_count": 999}
    }
    mutations.append(quality_changed)

    for mutated_payload in mutations:
        mutated_hash = hashlib.sha256(json.dumps(mutated_payload, sort_keys=True).encode()).digest()
        assert mutated_hash != base_hash


def test_contract_id_is_a_short_readable_prefix_of_the_full_hash() -> None:
    expected_prefix = f"early_momentum_v{early_momentum._STRATEGY_VERSION}_"
    assert early_momentum.CONTRACT_ID.startswith(expected_prefix)
    assert early_momentum.CONTRACT_ID.endswith(early_momentum.CONTRACT_SHA256_HEX[:12])


def test_prospective_runtime_policy_accepts_only_the_frozen_effective_config() -> None:
    cfg = _cfg(identity_snapshot_max_age_hours=720.0)
    early_momentum.validate_prospective_runtime_policy(cfg, trading_mode="paper")
    assert (
        early_momentum.PROSPECTIVE_RUNTIME_POLICY_SHA256_HEX
        == "720888b733bc097d53071b26edd5b85b4bb6dcc295a386fc1dc6590f9a2888d8"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("early_momentum_rearm_cooldown_seconds", 1),
        ("identity_snapshot_max_age_hours", 6.0),
        ("liquidity_depth_multiplier", 3.0),
        ("max_spread_bps", 25.0),
        ("max_liquidity_impact_bps", 25.0),
    ),
)
def test_prospective_runtime_policy_rejects_any_selection_drift(field: str, value: object) -> None:
    overrides: dict[str, object] = {"identity_snapshot_max_age_hours": 720.0, field: value}
    cfg = _cfg(**overrides)
    with pytest.raises(RuntimeError, match="runtime policy drifted"):
        early_momentum.validate_prospective_runtime_policy(cfg, trading_mode="paper")


def test_prospective_runtime_policy_rejects_non_paper_mode() -> None:
    cfg = _cfg(identity_snapshot_max_age_hours=720.0)
    with pytest.raises(RuntimeError, match="runtime policy drifted"):
        early_momentum.validate_prospective_runtime_policy(cfg, trading_mode="shadow")


# --- input-quality evidence and signal ---


def test_row_to_evidence_maps_every_field() -> None:
    row = _scanner_row()
    evidence = early_momentum._row_to_evidence(row)
    assert evidence.identity.exchange == "binance"
    assert evidence.identity.market_type == "linear"
    assert evidence.identity.symbol == "BEATUSDT"
    assert evidence.raw_row_count == 121
    assert evidence.distinct_bucket_count == 121
    assert evidence.capture_versions == ("v1",)
    assert evidence.universe_versions == ("uv1",)
    assert evidence.has_duplicate_bucket is False


def test_row_to_evidence_handles_null_version_arrays() -> None:
    # psycopg can hand back NULL (None) for array_agg over an empty group
    # in edge cases -- must not crash, just an empty tuple.
    row = _scanner_row(capture_versions=None, universe_versions=None)
    evidence = early_momentum._row_to_evidence(row)
    assert evidence.capture_versions == ()
    assert evidence.universe_versions == ()


def test_clean_row_passes_quality_validation() -> None:
    from schurfer_market_quality import validate

    evidence = early_momentum._row_to_evidence(_scanner_row())
    result = validate(
        evidence, early_momentum.EARLY_MOMENTUM_V4_QUALITY_POLICY, evaluated_at=datetime.now(tz=UTC)
    )
    assert result.qualified is True


def test_compute_signal_qualifies_on_oi_growth_and_tight_range() -> None:
    result = early_momentum._compute_signal(_scanner_row())
    assert result.qualified is True
    assert result.features.oi_growth_pct == pytest.approx(0.10)


def test_compute_signal_rejects_when_oi_growth_too_low() -> None:
    row = _scanner_row(oi_start=1000.0, oi_latest=1020.0)  # only +2%
    result = early_momentum._compute_signal(row)
    assert result.qualified is False


def test_compute_signal_rejects_when_price_range_too_wide() -> None:
    row = _scanner_row(price_max=1.05, price_min=0.95)  # ~10% range
    result = early_momentum._compute_signal(row)
    assert result.qualified is False


def test_compute_signal_rejects_when_net_taker_flow_is_negative() -> None:
    row = _scanner_row(buy_vol=100.0, sell_vol=600.0)
    result = early_momentum._compute_signal(row)
    assert result.qualified is False


def test_compute_signal_does_not_qualify_a_genuinely_sharp_move_off() -> None:
    """No magnitude-based sanity filter anywhere -- a real, large OI jump
    in an otherwise-clean window must still qualify (colleague review:
    never reject based on how big the move is, only on data trustworthiness)."""
    row = _scanner_row(oi_start=1000.0, oi_latest=2000.0)  # +100%
    result = early_momentum._compute_signal(row)
    assert result.qualified is True


def test_compute_signal_handles_zero_oi_start_without_crashing() -> None:
    row = _scanner_row(oi_start=0.0, oi_latest=100.0)
    result = early_momentum._compute_signal(row)
    assert result.qualified is False


def test_episode_features_contains_full_evidence_and_signal() -> None:
    quality_result = _quality_result()
    signal = _signal()
    features = early_momentum._episode_features(quality_result, signal)
    assert features["quality_policy_version"] == early_momentum._QUALITY_POLICY_HASH
    assert features["bucket_count"] == 121
    assert features["distinct_bucket_count"] == 121
    assert features["capture_version"] == "v1"
    assert features["universe_version"] == "uv1"
    assert features["quality_reasons"] == []
    assert features["oi_growth_pct"] == pytest.approx(10.0)
    assert features["price_range_pct"] == pytest.approx(1.0)


def test_episode_features_records_quality_reasons_when_rejected() -> None:
    from schurfer_market_quality import WindowQualityReason

    rejected = WindowQualityResult(evidence=_evidence(), reasons=(WindowQualityReason.STALE_OI,))
    features = early_momentum._episode_features(rejected, _signal())
    assert features["quality_reasons"] == ["stale_oi"]


def test_tally_rejection_counters_increments_every_reason() -> None:
    from schurfer_market_quality import WindowQualityReason

    counters = early_momentum._new_scanner_counters()
    early_momentum._tally_rejection_counters(
        counters, (WindowQualityReason.GAP, WindowQualityReason.STALE_OI)
    )
    assert counters["rejected_gap"] == 1
    assert counters["rejected_stale_oi"] == 1
    assert counters["rejected_incomplete"] == 0


def test_tally_rejection_counters_folds_capability_reasons_into_one_bucket() -> None:
    from schurfer_market_quality import WindowQualityReason

    counters = early_momentum._new_scanner_counters()
    early_momentum._tally_rejection_counters(
        counters,
        (
            WindowQualityReason.INCOMPLETE_PRICE,
            WindowQualityReason.INCOMPLETE_TRADES,
            WindowQualityReason.INCOMPLETE_OI,
        ),
    )
    assert counters["rejected_incomplete"] == 3


# --- scanner ---


async def test_scanner_disabled_without_db_url() -> None:
    rdb = MagicMock()
    with patch("schurfer_execution.early_momentum._scan_once", new_callable=AsyncMock) as scan:
        await early_momentum.run_early_momentum_scanner(rdb, _cfg(db_url=None))
    scan.assert_not_awaited()


async def test_run_early_momentum_scanner_writes_heartbeat_via_track_tick() -> None:
    rdb = MagicMock()
    rdb.set = AsyncMock()
    with (
        patch(
            "schurfer_execution.early_momentum._scan_once",
            AsyncMock(return_value={"candidates_found": 2}),
        ),
        patch(
            "schurfer_execution.early_momentum.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await early_momentum.run_early_momentum_scanner(rdb, _cfg())

    assert rdb.set.await_count == 2
    for call in rdb.set.await_args_list:
        assert call.args[0] == early_momentum._SCANNER_HEARTBEAT_KEY
    started = WorkerHeartbeat.from_json(rdb.set.await_args_list[0].args[1])
    completed = WorkerHeartbeat.from_json(rdb.set.await_args_list[1].args[1])
    assert started.state == "started"
    assert completed.state == "completed"
    assert completed.counters == {"candidates_found": 2}


async def test_run_early_momentum_scanner_writes_failed_heartbeat_and_keeps_looping() -> None:
    rdb = MagicMock()
    rdb.set = AsyncMock()
    with (
        patch(
            "schurfer_execution.early_momentum._scan_once",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(
            "schurfer_execution.early_momentum.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await early_momentum.run_early_momentum_scanner(rdb, _cfg())

    completed = WorkerHeartbeat.from_json(rdb.set.await_args_list[1].args[1])
    assert completed.state == "failed"
    assert completed.last_error == "boom"


async def test_run_early_momentum_trigger_writes_heartbeat_via_track_tick() -> None:
    rdb = MagicMock()
    rdb.set = AsyncMock()
    with (
        patch("schurfer_execution.early_momentum._trigger_tick", new_callable=AsyncMock),
        patch(
            "schurfer_execution.early_momentum.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await early_momentum.run_early_momentum_trigger({}, rdb, _cfg(), PaperBroker(_open_gate()))

    assert rdb.set.await_count == 2
    for call in rdb.set.await_args_list:
        assert call.args[0] == early_momentum._TRIGGER_HEARTBEAT_KEY
    completed = WorkerHeartbeat.from_json(rdb.set.await_args_list[1].args[1])
    assert completed.state == "completed"


async def test_scan_once_returns_explanatory_counters_for_a_mixed_batch() -> None:
    rdb = AsyncMock()
    cfg = _cfg()
    clean_row = _scanner_row(symbol="CLEANUSDT")
    gap_row = _scanner_row(symbol="GAPUSDT", max_gap_seconds=200.0)
    incomplete_row = _scanner_row(symbol="INCUSDT", price_complete_count=100)

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
            AsyncMock(return_value=60.0),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.resolve_routes_batch",
            AsyncMock(return_value={"CLEANUSDT": _route()}),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.within_rearm_cooldown",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.create_episode",
            AsyncMock(return_value=None),
        ),
    ):
        cur = AsyncMock()
        cur.execute = AsyncMock()
        cur.fetchall = AsyncMock(return_value=[clean_row, gap_row, incomplete_row])
        cur.fetchone = AsyncMock(return_value={"now": datetime.now(tz=UTC)})
        cur_cm = MagicMock()
        cur_cm.__aenter__ = AsyncMock(return_value=cur)
        cur_cm.__aexit__ = AsyncMock(return_value=False)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur_cm)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        mock_connect.return_value = conn

        counters = await early_momentum._scan_once(rdb, cfg)

    assert counters["symbols_total"] == 3
    assert counters["quality_ready"] == 1
    assert counters["candidates_found"] == 1
    assert counters["rejected_gap"] == 1
    assert counters["rejected_incomplete"] == 1


async def test_scan_once_judges_freshness_against_the_database_clock_not_the_app_clock() -> None:
    """bucket-lag/OI-staleness must be judged against Postgres's own now(),
    never the execution app's local clock -- otherwise ordinary NTP drift
    between the two hosts shows up as false staleness or masks real
    staleness (colleague review). A window_end deliberately years away
    from the real wall clock, evaluated against a mocked DB now() that
    sits exactly at window_end (zero real lag), must still qualify --
    proving the DB-fetched value is what's actually used."""
    rdb = AsyncMock()
    cfg = _cfg()
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    row = _scanner_row(
        symbol="CLOCKUSDT",
        window_end=long_ago,
        latest_oi_event_at=long_ago - timedelta(seconds=30),
    )

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
            AsyncMock(return_value=60.0),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.resolve_routes_batch",
            AsyncMock(return_value={"CLOCKUSDT": _route()}),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.within_rearm_cooldown",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.create_episode",
            AsyncMock(return_value=None),
        ),
    ):
        cur = AsyncMock()
        cur.execute = AsyncMock()
        cur.fetchall = AsyncMock(return_value=[row])
        cur.fetchone = AsyncMock(return_value={"now": long_ago})  # DB "now" == window_end
        cur_cm = MagicMock()
        cur_cm.__aenter__ = AsyncMock(return_value=cur)
        cur_cm.__aexit__ = AsyncMock(return_value=False)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur_cm)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        mock_connect.return_value = conn

        counters = await early_momentum._scan_once(rdb, cfg)

    assert counters["rejected_stale_bucket"] == 0
    assert counters["rejected_stale_oi"] == 0
    assert counters["quality_ready"] == 1


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
        cur.fetchall = AsyncMock(return_value=[_scanner_row()])
        cur.fetchone = AsyncMock(return_value={"now": datetime.now(tz=UTC)})
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
            row=_scanner_row(),
            quality_result=_quality_result(),
            signal=_signal(),
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
            row=_scanner_row(),
            quality_result=_quality_result(),
            signal=_signal(),
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
            row=_scanner_row(),
            quality_result=_quality_result(),
            signal=_signal(),
            strategy_id=1,
            source_exchange="binance",
            route=_route(),
            catalog_stale=False,
        )

    create.assert_awaited_once()
    kw = create.call_args.kwargs
    assert kw["contract_sha256"] == early_momentum.CONTRACT_SHA256
    assert kw["execution_symbol"] is None  # not known without a live exchange client
    assert kw["features"]["quality_policy_version"] == early_momentum._QUALITY_POLICY_HASH
    rdb.set.assert_awaited_once()
    key, payload = rdb.set.call_args.args[:2]
    assert key == "market:early_momentum:v4:watch:e1"
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
            row=_scanner_row(),
            quality_result=_quality_result(),
            signal=_signal(),
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
    rdb.scan_iter = _empty_scan_iter

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
        await early_momentum._trigger_tick(
            {"bybit": _exchange()}, rdb, _cfg(), PaperBroker(_open_gate())
        )

    reap.assert_awaited_once()
    listed.assert_awaited_once()
    reconcile.assert_awaited_once()


async def test_trigger_tick_repairs_missing_watch_cache_from_actionable() -> None:
    rdb = MagicMock()
    rdb.exists = AsyncMock(return_value=False)  # cache entry missing
    rdb.set = AsyncMock()
    rdb.scan_iter = _empty_scan_iter
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
        await early_momentum._trigger_tick(
            {"bybit": _exchange()}, rdb, _cfg(), PaperBroker(_open_gate())
        )

    rdb.set.assert_awaited_once()
    assert rdb.set.call_args.args[0] == "market:early_momentum:v4:watch:e1"


# --- breakout handling ---


async def test_check_breakout_disabled_never_claims_or_terminates_an_episode() -> None:
    """EARLY_MOMENTUM_MODE=disabled must stop before any episode is ever
    claimed -- claiming and then mis-terminating it as
    "infrastructure_failure" would misrepresent a deliberate operator
    choice as a real production incident and burn the claim lease for
    nothing (colleague review, P1)."""
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
            new_callable=AsyncMock,
        ) as find_open,
        patch(
            "schurfer_execution.early_momentum.episodes.claim_episode", new_callable=AsyncMock
        ) as claim,
        patch(
            "schurfer_execution.early_momentum.episodes.terminate_episode",
            new_callable=AsyncMock,
        ) as terminate,
    ):
        await early_momentum._check_breakout(
            ex, rdb, _cfg(), DisabledBroker(), cached=cached, tickers=tickers
        )

    find_open.assert_not_awaited()
    claim.assert_not_awaited()
    terminate.assert_not_awaited()


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
        await early_momentum._check_breakout(
            ex, rdb, _cfg(), PaperBroker(_open_gate()), cached=cached, tickers=tickers
        )

    terminate.assert_awaited_once()
    kw = terminate.call_args.kwargs
    assert kw["reason"] == episodes.REASON_ALREADY_OPEN
    assert kw["status"] == episodes.STATUS_SUPPRESSED
    claim.assert_not_awaited()
    rdb.delete.assert_awaited_once_with("market:early_momentum:v4:watch:e1")


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

    await early_momentum._check_breakout(
        ex, rdb, _cfg(), PaperBroker(_open_gate()), cached=cached, tickers=tickers
    )

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
        await early_momentum._check_breakout(
            ex, rdb, _cfg(), PaperBroker(_open_gate()), cached=cached, tickers=tickers
        )

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
        await early_momentum._check_breakout(
            ex, rdb, _cfg(), PaperBroker(_open_gate()), cached=cached, tickers=tickers
        )

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
        await early_momentum._check_breakout(
            ex, rdb, _cfg(), PaperBroker(_open_gate()), cached=cached, tickers=tickers
        )

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
            PaperBroker(_open_gate()),
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
            PaperBroker(_open_gate()),
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
    assert setup_context["strategy"] == "early_momentum_v4"
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
            PaperBroker(_open_gate()),
            instrument=_instrument(),
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            source_exchange="binance",
            source_native_id="BEATUSDT",
            ceiling=100.0,
        )

    terminate.assert_awaited_once()
    assert terminate.call_args.kwargs["reason"] == episodes.REASON_INFRASTRUCTURE_FAILURE


class _FakeShadowBroker:
    """A broker stub returning SHADOW_RECORDED without going through the real
    ShadowBroker (that write path has its own dedicated tests in
    test_execution_intent.py) -- this file only needs to check that
    _quote_and_open reads the status correctly."""

    mode = TradingMode.SHADOW

    async def open(self, intent: Any, *, cfg: Any, rdb: Any) -> ExecutionResult:
        return ExecutionResult(mode=self.mode, status=ExecutionStatus.SHADOW_RECORDED)


async def test_quote_and_open_shadow_recorded_suppresses_not_infra_failure() -> None:
    """EARLY_MOMENTUM_MODE=shadow: evidence was written on purpose -- must
    terminate STATUS_SUPPRESSED/REASON_SHADOW_RECORDED, never
    STATUS_REJECTED/REASON_INFRASTRUCTURE_FAILURE (that reason is reserved
    for an actual broker failure)."""
    rdb = AsyncMock()
    ex = _exchange()
    ex.fetch_order_book = AsyncMock(return_value=_healthy_book())
    with patch(
        "schurfer_execution.early_momentum.episodes.terminate_episode",
        new_callable=AsyncMock,
    ) as terminate:
        await early_momentum._quote_and_open(
            ex,
            rdb,
            _cfg(),
            _FakeShadowBroker(),
            instrument=_instrument(),
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            source_exchange="binance",
            source_native_id="BEATUSDT",
            ceiling=100.0,
        )

    terminate.assert_awaited_once()
    assert terminate.call_args.kwargs["reason"] == episodes.REASON_SHADOW_RECORDED
    assert terminate.call_args.kwargs["status"] == episodes.STATUS_SUPPRESSED


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
            PaperBroker(_open_gate()),
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
        await early_momentum._check_breakout(
            ex, rdb, _cfg(), PaperBroker(_open_gate()), cached=cached, tickers=tickers
        )

    release.assert_awaited_once()


def test_no_update_statement_anywhere_ever_touches_contract_sha256() -> None:
    """One episode cannot change contract (feat/early-momentum-prospective-
    cohort-v1, colleague-required regression): `contract_sha256` is written
    exactly once, at arm time (INSERT), and never again -- a static source
    scan over every raw SQL UPDATE statement in episodes.py and journal.py,
    the only two modules that ever touch app.early_momentum_episodes, is a
    stronger regression guard here than a single behavioral test could be:
    it catches ANY future UPDATE that mentions the column, not just the
    specific code paths one test happens to exercise."""
    import re

    from schurfer_execution import journal

    for module in (episodes, journal):
        source = inspect.getsource(module)
        for match in re.finditer(r"UPDATE\s+[\w.]+\s+SET\s+(.*?)WHERE", source, re.DOTALL):
            set_clause = match.group(1)
            assert (
                "contract_sha256" not in set_clause
            ), f"{module.__name__} has an UPDATE that touches contract_sha256: {set_clause!r}"


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
