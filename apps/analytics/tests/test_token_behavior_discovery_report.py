from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import duckdb
import pytest
from schurfer_analytics.challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerFormalResult,
    ChallengerInference,
    InferenceReadiness,
    PairedInference,
    StrategyInference,
)
from schurfer_analytics.clustered_inference import BootstrapEstimate
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import (
    DEFAULT_REPLAY_HORIZONS,
    ReplayDecision,
    ReplayEpisode,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.token_behavior_descriptors import (
    ONE_DAY_MS,
    DailyBar,
    RecoveryResult,
)
from schurfer_analytics.token_behavior_discovery_report import (
    CANDIDATE_HIGH_VOLATILITY,
    CANDIDATE_NO_PRIOR_SPIKE,
    CANDIDATE_VARIANT_KEYS,
    CANDIDATE_YOUNG_LISTING,
    MIN_CHANGED_TRADES,
    TOKEN_BEHAVIOR_DATASET_CONTENT_FINGERPRINT,
    TOKEN_BEHAVIOR_DATASET_RUN_ID,
    TOKEN_BEHAVIOR_DATASET_SINCE,
    TOKEN_BEHAVIOR_DATASET_UNTIL_EXCLUSIVE,
    TOKEN_BEHAVIOR_STRATEGY_VERSIONS,
    CandidateReadiness,
    DescriptorSet,
    FrozenThresholds,
    InsufficientDescriptorDataError,
    TokenHistoryContext,
    WeekConcentration,
    _candidate_high_volatility,
    _candidate_readiness,
    _candidate_slow_recovery,
    _candidate_young_listing,
    _final_verdict,
    _is_canonical_run,
    _last_week_robustness,
    _verify_manifest,
    _week_concentration,
    build_token_behavior_discovery_report,
    evaluate_token_behavior_episode,
    freeze_thresholds,
    in_scope_episodes,
    load_token_history_index,
    render_json,
    render_markdown,
)
from schurfer_analytics.token_behavior_discovery_report import (
    TokenBehaviorEpisodeResult as Result,
)
from schurfer_analytics.virtual_market import DecisionMarketPath
from schurfer_analytics.virtual_strategy import DEFAULT_COSTS, MarketPath, expected_path_bounds

if TYPE_CHECKING:
    from pathlib import Path

T0 = TOKEN_BEHAVIOR_DATASET_SINCE
DECISION_TS = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
DAY0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _day_bar(days_before_day0: int, close: float, high: float | None = None) -> DailyBar:
    ts_ms = int(DAY0.timestamp() * 1000) - days_before_day0 * ONE_DAY_MS
    return DailyBar(ts_ms=ts_ms, close=close, high=high or close)


def _manifest(**overrides: object) -> dict[str, object]:
    inner: dict[str, object] = {
        "run_id": TOKEN_BEHAVIOR_DATASET_RUN_ID,
        "dataset_content_fingerprint": TOKEN_BEHAVIOR_DATASET_CONTENT_FINGERPRINT,
        "dataset_ready": True,
    }
    inner.update(overrides)
    return {"manifest": inner, "results": []}


# --- _verify_manifest --------------------------------------------------------


def test_verify_manifest_accepts_the_frozen_contract() -> None:
    _verify_manifest(_manifest())  # must not raise


def test_verify_manifest_rejects_run_id_drift() -> None:
    with pytest.raises(ValueError, match="run_id"):
        _verify_manifest(_manifest(run_id="some-other-run"))


def test_verify_manifest_rejects_fingerprint_drift() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        _verify_manifest(_manifest(dataset_content_fingerprint="0" * 64))


def test_verify_manifest_rejects_dataset_not_ready() -> None:
    with pytest.raises(ValueError, match="dataset_ready"):
        _verify_manifest(_manifest(dataset_ready=False))


# --- load_token_history_index (real DuckDB/Parquet I/O) ----------------------


def _write_bars_parquet(path: Path) -> str:
    """Writes a real bars.parquet and returns its sha256 hex digest, so
    callers can populate the manifest's own parquet_sha256 field truthfully."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("CREATE TABLE bars (ts_ms BIGINT, close DOUBLE, high DOUBLE)")
        connection.execute(
            "INSERT INTO bars VALUES (?, ?, ?), (?, ?, ?)",
            [
                int(DAY0.timestamp() * 1000) - 2 * ONE_DAY_MS,
                100.0,
                101.0,
                int(DAY0.timestamp() * 1000) - ONE_DAY_MS,
                102.0,
                103.0,
            ],
        )
        connection.execute(
            "COPY bars TO ? (FORMAT PARQUET)",
            [path.as_posix()],
        )
    finally:
        connection.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_load_token_history_index_reads_bars_and_onboarded_at(tmp_path: Path) -> None:
    checksum = _write_bars_parquet(tmp_path / "binance" / "ERA" / "bars.parquet")
    manifest = _manifest()
    manifest["results"] = [
        {
            "publishable": True,
            "parquet_relative_path": "binance/ERA/bars.parquet",
            "parquet_sha256": checksum,
            "decisions": [
                {"pump_event_id": 42, "onboarded_at": "2026-01-01T00:00:00Z"},
            ],
        }
    ]
    (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest))

    index = load_token_history_index(tmp_path)

    assert 42 in index
    context = index[42]
    assert len(context.bars) == 2
    assert context.onboarded_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_load_token_history_index_skips_non_publishable_results(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["results"] = [
        {
            "publishable": False,
            "parquet_relative_path": None,
            "decisions": [{"pump_event_id": 7, "onboarded_at": "2026-01-01T00:00:00Z"}],
        }
    ]
    (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest))

    index = load_token_history_index(tmp_path)

    assert index == {}


def test_load_token_history_index_rejects_parquet_checksum_mismatch(tmp_path: Path) -> None:
    _write_bars_parquet(tmp_path / "binance" / "ERA" / "bars.parquet")
    manifest = _manifest()
    manifest["results"] = [
        {
            "publishable": True,
            "parquet_relative_path": "binance/ERA/bars.parquet",
            "parquet_sha256": "0" * 64,  # deliberately wrong
            "decisions": [{"pump_event_id": 42, "onboarded_at": "2026-01-01T00:00:00Z"}],
        }
    ]
    (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest))

    with pytest.raises(ValueError, match="checksum mismatch"):
        load_token_history_index(tmp_path)


def test_load_token_history_index_rejects_missing_parquet_checksum(tmp_path: Path) -> None:
    _write_bars_parquet(tmp_path / "binance" / "ERA" / "bars.parquet")
    manifest = _manifest()
    manifest["results"] = [
        {
            "publishable": True,
            "parquet_relative_path": "binance/ERA/bars.parquet",
            "decisions": [{"pump_event_id": 42, "onboarded_at": "2026-01-01T00:00:00Z"}],
        }
    ]
    (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest))

    with pytest.raises(ValueError, match="missing parquet_sha256"):
        load_token_history_index(tmp_path)


def test_load_token_history_index_rejects_fingerprint_drift(tmp_path: Path) -> None:
    manifest = _manifest(dataset_content_fingerprint="0" * 64)
    (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest))

    with pytest.raises(ValueError, match="fingerprint"):
        load_token_history_index(tmp_path)


# --- freeze_thresholds --------------------------------------------------------


def _descriptor(
    *,
    volatility: float | None = 0.1,
    recovery: RecoveryResult | None = None,
    listing_age: float = 10.0,
    prior_spike_count_90d: int | None = 0,
) -> DescriptorSet:
    return DescriptorSet(
        prior_spike_count_90d=prior_spike_count_90d,
        historical_volatility_30d=volatility,
        recovery=recovery or RecoveryResult(status="no_prior_spike"),
        listing_age_days=listing_age,
    )


def test_freeze_thresholds_computes_medians_over_resolved_values_only() -> None:
    sample = [
        _descriptor(volatility=0.1, listing_age=10.0, recovery=RecoveryResult("no_prior_spike")),
        _descriptor(volatility=0.3, listing_age=20.0, recovery=RecoveryResult("no_prior_spike")),
        _descriptor(volatility=None, listing_age=30.0, recovery=RecoveryResult("recovered", 5.0)),
        _descriptor(volatility=0.5, listing_age=40.0, recovery=RecoveryResult("recovered", 15.0)),
    ]
    thresholds = freeze_thresholds(sample)
    assert thresholds.volatility_median == 0.3  # median of {0.1, 0.3, 0.5}
    assert thresholds.volatility_sample_size == 3
    assert thresholds.recovery_median_days == 10.0  # median of {5.0, 15.0}
    assert thresholds.recovery_sample_size == 2
    assert thresholds.listing_age_median_days == 25.0  # median of all 4
    assert thresholds.listing_age_sample_size == 4


def test_freeze_thresholds_raises_when_no_volatility_resolved() -> None:
    sample = [_descriptor(volatility=None)]
    with pytest.raises(InsufficientDescriptorDataError, match="insufficient"):
        freeze_thresholds(sample)


def test_freeze_thresholds_raises_when_no_recovered_episode() -> None:
    sample = [_descriptor(recovery=RecoveryResult("not_yet_recovered_by_decision"))]
    with pytest.raises(InsufficientDescriptorDataError, match="insufficient"):
        freeze_thresholds(sample)


def test_freeze_thresholds_empty_sample_raises() -> None:
    with pytest.raises(InsufficientDescriptorDataError, match="insufficient"):
        freeze_thresholds([])


# --- candidate gating functions -----------------------------------------------

_THRESHOLDS = FrozenThresholds(
    volatility_median=0.2,
    recovery_median_days=10.0,
    listing_age_median_days=30.0,
    volatility_sample_size=10,
    recovery_sample_size=10,
    listing_age_sample_size=10,
)


def test_candidate_no_prior_spike_true_when_zero() -> None:
    from schurfer_analytics.token_behavior_discovery_report import _candidate_no_prior_spike

    assert _candidate_no_prior_spike(_descriptor(prior_spike_count_90d=0)) is True


def test_candidate_no_prior_spike_false_when_nonzero() -> None:
    from schurfer_analytics.token_behavior_discovery_report import _candidate_no_prior_spike

    assert _candidate_no_prior_spike(_descriptor(prior_spike_count_90d=2)) is False


def test_candidate_no_prior_spike_none_when_unresolved() -> None:
    from schurfer_analytics.token_behavior_discovery_report import _candidate_no_prior_spike

    assert _candidate_no_prior_spike(_descriptor(prior_spike_count_90d=None)) is None


def test_candidate_high_volatility_above_median() -> None:
    assert _candidate_high_volatility(_descriptor(volatility=0.3), _THRESHOLDS) is True


def test_candidate_high_volatility_at_or_below_median_is_false() -> None:
    assert _candidate_high_volatility(_descriptor(volatility=0.2), _THRESHOLDS) is False


def test_candidate_high_volatility_none_when_unresolved() -> None:
    assert _candidate_high_volatility(_descriptor(volatility=None), _THRESHOLDS) is None


def test_candidate_slow_recovery_excluded_when_no_prior_spike() -> None:
    result = _candidate_slow_recovery(
        _descriptor(recovery=RecoveryResult("no_prior_spike")), _THRESHOLDS
    )
    assert result is None


def test_candidate_slow_recovery_excluded_when_missing_reference_price() -> None:
    result = _candidate_slow_recovery(
        _descriptor(recovery=RecoveryResult("missing_reference_price")), _THRESHOLDS
    )
    assert result is None


def test_candidate_slow_recovery_true_when_above_median() -> None:
    result = _candidate_slow_recovery(
        _descriptor(recovery=RecoveryResult("recovered", recovered_in_days=15.0)), _THRESHOLDS
    )
    assert result is True


def test_candidate_slow_recovery_false_when_at_or_below_median() -> None:
    result = _candidate_slow_recovery(
        _descriptor(recovery=RecoveryResult("recovered", recovered_in_days=10.0)), _THRESHOLDS
    )
    assert result is False


def test_candidate_slow_recovery_censored_short_observation_is_unresolved() -> None:
    # Not yet recovered, but only observed for less time than the threshold
    # -- genuinely too early to know, must not be treated as a signal.
    result = _candidate_slow_recovery(
        _descriptor(
            recovery=RecoveryResult("not_yet_recovered_by_decision", observed_for_days=5.0)
        ),
        _THRESHOLDS,
    )
    assert result is None


def test_candidate_slow_recovery_censored_long_observation_is_a_definitive_signal() -> None:
    # Observed at least as long as the threshold without recovering --
    # this IS a real slow-recovery signal, not unresolved.
    result = _candidate_slow_recovery(
        _descriptor(
            recovery=RecoveryResult("not_yet_recovered_by_decision", observed_for_days=10.0)
        ),
        _THRESHOLDS,
    )
    assert result is True


def test_candidate_young_listing_below_median() -> None:
    assert _candidate_young_listing(_descriptor(listing_age=10.0), _THRESHOLDS) is True


def test_candidate_young_listing_at_or_above_median_is_false() -> None:
    assert _candidate_young_listing(_descriptor(listing_age=30.0), _THRESHOLDS) is False


# --- evaluate_token_behavior_episode ------------------------------------------


def _decision(
    row_id: int,
    *,
    score: int,
    ts: datetime = DECISION_TS,
    pump_event_id: int = 42,
    decision_id: str | None = None,
) -> ReplayDecision:
    return ReplayDecision(
        row_id=row_id,
        decision_id=decision_id or f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=pump_event_id,
        event_base="ERA",
        event_first_seen_at=ts,
        event_closed_at=ts + timedelta(hours=7),
        ts=ts,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="measurement",
        score=score,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {
                "computed_at": ts.timestamp(),
                "components": {
                    name: {"value": 1.0, "points": score // 5, "max": 2, "note": ""}
                    for name in (
                        "pump_age",
                        "price_extent",
                        "oi_trend",
                        "funding_rate",
                        "retrace_from_peak",
                    )
                },
                "data_quality": {"oi": True, "funding": True},
            },
            "config": {
                "score_threshold": 6,
                "require_market_quality": True,
                "signal_position_usd": 50,
            },
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 2},
            "ask_impact_bps": {"100": 3},
            "quality": {"allowed": True, "depth_target_usd": 100},
        },
        outcomes=(
            ReplayOutcome(
                horizon_minutes=480,
                status="complete",
                anchor_exchange="binance",
                source_exchange="binance",
                entry_price=100,
                forward_price=90,
                mfe_pct=10,
                mae_pct=0,
                short_return_pct=10,
                coverage_ratio=1,
            ),
        ),
    )


def _score6_decision(row_id: int, **kwargs: object) -> ReplayDecision:
    # 5 components x 2 points ("//5" above only works for multiples of 5;
    # score=10 -> 2 points each -> sum 10). Use omitted-component trick
    # instead: keep components summing to exactly `score`.
    return _decision(row_id, score=6, **kwargs)  # type: ignore[arg-type]


def _episode(*decisions: ReplayDecision, pump_event_id: int = 42) -> ReplayEpisode:
    return ReplayEpisode(pump_event_id, "ERA", "base:ERA", decisions, ())


def _complete_path(decision: ReplayDecision) -> MarketPath:
    start_ms, end_ms = expected_path_bounds(decision)
    candles = tuple(
        Candle(
            timestamp,
            100 if timestamp == start_ms else 90,
            100 if timestamp == start_ms else 90,
            90,
            90,
            1,
        )
        for timestamp in range(start_ms, end_ms, TIMEFRAME_MS)
    )
    return MarketPath(
        pump_event_id=42,
        exchange=decision.exchange,
        base=decision.base,
        status="complete",
        candles=candles,
    )


_RESOLVED_THRESHOLDS = FrozenThresholds(
    volatility_median=0.5,
    recovery_median_days=10.0,
    listing_age_median_days=30.0,
    volatility_sample_size=10,
    recovery_sample_size=10,
    listing_age_sample_size=10,
)


def test_evaluate_not_triggered_is_cash_for_every_candidate() -> None:
    decision = _decision(1, score=2)
    episode = _episode(decision)
    result = evaluate_token_behavior_episode(episode, {}, {}, _RESOLVED_THRESHOLDS, DEFAULT_COSTS)
    assert result.baseline_triggered is False
    assert all(value == 0.0 for value in result.candidate_returns.values())


def test_evaluate_selection_unresolved_propagates() -> None:
    decision = _score6_decision(1)
    broken = replace(decision, score=None)
    episode = _episode(broken)
    result = evaluate_token_behavior_episode(episode, {}, {}, _RESOLVED_THRESHOLDS, DEFAULT_COSTS)
    assert result.baseline_triggered is False
    assert result.error is not None
    assert all(value is None for value in result.candidate_returns.values())


def test_evaluate_missing_token_history_leaves_baseline_resolved_but_candidates_unresolved() -> (
    None
):
    # Missing token-history bars must never shrink the baseline's own
    # resolved sample -- the baseline strategy doesn't depend on
    # token-history at all. Only the candidates become unresolved.
    decision = _score6_decision(1)
    episode = _episode(decision)
    path_by_decision = {decision.decision_id or "": _complete_path(decision)}
    result = evaluate_token_behavior_episode(
        episode, path_by_decision, {}, _RESOLVED_THRESHOLDS, DEFAULT_COSTS
    )
    assert result.baseline_triggered is True
    assert result.baseline_net_return_pct is not None
    assert result.descriptors is None
    assert all(value is None for value in result.candidate_returns.values())
    assert result.error is not None


def test_evaluate_complete_resolution_applies_all_four_candidate_gates() -> None:
    decision = _score6_decision(1)
    episode = _episode(decision)
    path_by_decision = {decision.decision_id or "": _complete_path(decision)}
    # Dense, unbroken daily coverage well past the 90-day lookback, flat
    # price throughout (no spike ever triggers) -- makes coverage_ok and
    # prior_spike_count_90d==0 unambiguous.
    bars = tuple(_day_bar(days, 100.0) for days in range(91, 0, -1))
    token_history = {42: TokenHistoryContext(bars=bars, onboarded_at=T0 - timedelta(days=1))}
    result = evaluate_token_behavior_episode(
        episode, path_by_decision, token_history, _RESOLVED_THRESHOLDS, DEFAULT_COSTS
    )
    assert result.baseline_triggered is True
    assert result.descriptors is not None
    assert set(result.candidate_gates) == set(CANDIDATE_VARIANT_KEYS)
    # No prior spikes recorded in this flat-price history -> no_prior_spike
    # candidate should gate to cash (True), and young listing (age ~1 day,
    # well under the 30-day median) should also gate to cash.
    assert result.candidate_gates[CANDIDATE_NO_PRIOR_SPIKE] is True
    assert result.candidate_returns[CANDIDATE_NO_PRIOR_SPIKE] == 0.0
    assert result.candidate_gates[CANDIDATE_YOUNG_LISTING] is True
    assert result.candidate_returns[CANDIDATE_YOUNG_LISTING] == 0.0


def test_evaluate_kept_candidate_matches_baseline_return() -> None:
    decision = _score6_decision(1)
    episode = _episode(decision)
    path_by_decision = {decision.decision_id or "": _complete_path(decision)}
    bars = tuple(_day_bar(days, 100.0) for days in range(91, 0, -1))
    token_history = {42: TokenHistoryContext(bars=bars, onboarded_at=T0 - timedelta(days=400))}
    result = evaluate_token_behavior_episode(
        episode, path_by_decision, token_history, _RESOLVED_THRESHOLDS, DEFAULT_COSTS
    )
    # Old listing (well above median) -> young_listing candidate keeps the
    # baseline trade unchanged.
    assert result.candidate_gates[CANDIDATE_YOUNG_LISTING] is False
    assert result.candidate_returns[CANDIDATE_YOUNG_LISTING] == result.baseline_net_return_pct


# --- in_scope_episodes -------------------------------------------------------


def test_in_scope_episodes_excludes_pump_events_outside_the_frozen_instrument_set() -> None:
    # The baseline itself is scoped to the frozen dataset's 47 instruments
    # (token_behavior_descriptors.py's module docstring). An eligible
    # episode whose pump_event_id has no token-history entry must be
    # excluded from the report's population entirely, not merely left with
    # unresolved candidates.
    in_scope_decision = _score6_decision(1, pump_event_id=42)
    out_of_scope_decision = _score6_decision(2, pump_event_id=99)
    dataset = build_replay_dataset(
        [in_scope_decision, out_of_scope_decision],
        _filters(),
    )
    token_history = {
        42: TokenHistoryContext(bars=_flat_bars(), onboarded_at=T0 - timedelta(days=400))
    }
    scoped = in_scope_episodes(dataset, token_history)
    assert {episode.pump_event_id for episode in scoped} == {42}


def test_in_scope_episodes_empty_when_no_overlap() -> None:
    decision = _score6_decision(1, pump_event_id=42)
    dataset = build_replay_dataset([decision], _filters())
    scoped = in_scope_episodes(dataset, {})
    assert scoped == ()


# --- _week_concentration -------------------------------------------------------


def _week_result(week: str, *, triggered: bool = True) -> Result:
    return Result(
        pump_event_id=1,
        cluster_key="base:ERA",
        base="ERA",
        week_key=week,
        decision_id="d",
        decision_ts=None,
        baseline_triggered=triggered,
        baseline_net_return_pct=1.0,
        descriptors=None,
        candidate_gates={},
        candidate_returns={},
        trade=None,
    )


def test_week_concentration_empty_when_nothing_triggered() -> None:
    concentration = _week_concentration((_week_result("2026-W31", triggered=False),))
    assert concentration.distinct_weeks == 0
    assert concentration.largest_week_pct is None


def test_week_concentration_computes_largest_share() -> None:
    results = (
        _week_result("2026-W31"),
        _week_result("2026-W31"),
        _week_result("2026-W31"),
        _week_result("2026-W32"),
    )
    concentration = _week_concentration(results)
    assert concentration.distinct_weeks == 2
    assert concentration.largest_week_pct == pytest.approx(75.0)


# --- _candidate_readiness -------------------------------------------------------


def _readiness_result(
    *, gate: bool | None, base: str, baseline_return: float | None = 1.0
) -> Result:
    return Result(
        pump_event_id=1,
        cluster_key=f"base:{base}",
        base=base,
        week_key="2026-W31",
        decision_id="d",
        decision_ts=None,
        baseline_triggered=True,
        baseline_net_return_pct=baseline_return,
        descriptors=None,
        candidate_gates={CANDIDATE_NO_PRIOR_SPIKE: gate},
        candidate_returns={CANDIDATE_NO_PRIOR_SPIKE: 0.0 if gate else baseline_return},
        trade=None,
    )


def test_candidate_readiness_materiality_requires_both_trades_and_assets() -> None:
    population = tuple(
        _readiness_result(gate=True, base=f"COIN{i}") for i in range(MIN_CHANGED_TRADES)
    )
    readiness = _candidate_readiness(CANDIDATE_NO_PRIOR_SPIKE, population)
    assert readiness.changed_trades == MIN_CHANGED_TRADES
    assert readiness.changed_assets == MIN_CHANGED_TRADES
    assert readiness.materiality_ok is True


def test_candidate_readiness_fails_materiality_when_concentrated_in_one_asset() -> None:
    population = tuple(_readiness_result(gate=True, base="ERA") for _ in range(MIN_CHANGED_TRADES))
    readiness = _candidate_readiness(CANDIDATE_NO_PRIOR_SPIKE, population)
    assert readiness.changed_trades == MIN_CHANGED_TRADES
    assert readiness.changed_assets == 1
    assert readiness.materiality_ok is False


def test_candidate_readiness_resolved_pct_excludes_none_gates() -> None:
    population = (
        _readiness_result(gate=True, base="A"),
        _readiness_result(gate=None, base="B"),
    )
    readiness = _candidate_readiness(CANDIDATE_NO_PRIOR_SPIKE, population)
    assert readiness.resolved_count == 1
    assert readiness.resolved_pct == pytest.approx(50.0)


def test_candidate_readiness_profit_factor_none_when_no_losses() -> None:
    # gate=True -> cash (0.0) for every episode: no losing episodes at all,
    # so profit_factor's own convention returns None (undefined ratio),
    # which must still count as "ok" (trivially satisfies > 1).
    population = tuple(_readiness_result(gate=True, base=f"COIN{i}") for i in range(3))
    readiness = _candidate_readiness(CANDIDATE_NO_PRIOR_SPIKE, population)
    assert readiness.profit_factor is None
    assert readiness.profit_factor_ok is True


def test_candidate_readiness_profit_factor_fails_below_one() -> None:
    # gate=False -> kept, same as baseline: mix of a small gain and a much
    # larger loss gives PF well under 1.
    population = (
        _readiness_result(gate=False, base="A", baseline_return=1.0),
        _readiness_result(gate=False, base="B", baseline_return=-5.0),
    )
    readiness = _candidate_readiness(CANDIDATE_NO_PRIOR_SPIKE, population)
    assert readiness.profit_factor == pytest.approx(1.0 / 5.0)
    assert readiness.profit_factor_ok is False


def test_candidate_readiness_profit_factor_passes_above_one() -> None:
    population = (
        _readiness_result(gate=False, base="A", baseline_return=5.0),
        _readiness_result(gate=False, base="B", baseline_return=-1.0),
    )
    readiness = _candidate_readiness(CANDIDATE_NO_PRIOR_SPIKE, population)
    assert readiness.profit_factor == pytest.approx(5.0)
    assert readiness.profit_factor_ok is True


def test_candidate_readiness_profit_factor_excludes_unresolved_returns() -> None:
    # gate=False (kept) but the baseline trade itself never resolved -- a
    # descriptor-resolved episode whose RETURN is still None must not crash
    # or silently become a zero inside the profit-factor computation.
    population = (
        _readiness_result(gate=False, base="A", baseline_return=None),
        _readiness_result(gate=False, base="B", baseline_return=5.0),
    )
    readiness = _candidate_readiness(CANDIDATE_NO_PRIOR_SPIKE, population)
    assert readiness.resolved_count == 2  # both gate-resolved
    assert readiness.profit_factor is None  # only one return value, no losses
    assert readiness.profit_factor_ok is True


# --- _last_week_robustness -------------------------------------------------------


def _robustness_result(week: str, *, baseline: float | None, challenger: float | None) -> Result:
    return Result(
        pump_event_id=1,
        cluster_key="base:ERA",
        base="ERA",
        week_key=week,
        decision_id="d",
        decision_ts=None,
        baseline_triggered=True,
        baseline_net_return_pct=baseline,
        descriptors=None,
        candidate_gates={CANDIDATE_NO_PRIOR_SPIKE: False},
        candidate_returns={CANDIDATE_NO_PRIOR_SPIKE: challenger},
        trade=None,
    )


def test_last_week_robustness_no_data_when_population_empty() -> None:
    assert _last_week_robustness(CANDIDATE_NO_PRIOR_SPIKE, ()) == "no_data"


def test_last_week_robustness_positive_when_delta_and_expectancy_both_positive() -> None:
    population = (
        _robustness_result("2026-W31", baseline=-2.0, challenger=1.0),
        _robustness_result("2026-W32", baseline=-2.0, challenger=1.0),
    )
    # Only the LAST week (W32) matters.
    assert _last_week_robustness(CANDIDATE_NO_PRIOR_SPIKE, population) == "positive"


def test_last_week_robustness_not_positive_when_expectancy_negative() -> None:
    population = (_robustness_result("2026-W31", baseline=1.0, challenger=-1.0),)
    assert _last_week_robustness(CANDIDATE_NO_PRIOR_SPIKE, population) == "not_positive"


def test_last_week_robustness_insufficient_data_when_last_week_all_unresolved() -> None:
    population = (
        _robustness_result("2026-W31", baseline=1.0, challenger=1.0),
        _robustness_result("2026-W32", baseline=None, challenger=None),
    )
    assert _last_week_robustness(CANDIDATE_NO_PRIOR_SPIKE, population) == "insufficient_data"


# --- _is_canonical_run -------------------------------------------------------


def _filters(**overrides: object) -> ReplayFilters:
    from schurfer_analytics.outcomes import RESOLVER_VERSION

    base: dict[str, object] = {
        "since": TOKEN_BEHAVIOR_DATASET_SINCE,
        "until": TOKEN_BEHAVIOR_DATASET_UNTIL_EXCLUSIVE,
        "strategy_versions": TOKEN_BEHAVIOR_STRATEGY_VERSIONS,
        "resolver_version": RESOLVER_VERSION,
        "required_horizons": DEFAULT_REPLAY_HORIZONS,
        "allow_fallback": False,
    }
    base.update(overrides)
    return ReplayFilters(**base)  # type: ignore[arg-type]


def test_is_canonical_run_true_for_registered_defaults() -> None:
    assert _is_canonical_run(_filters(), DEFAULT_COSTS) is True


def test_is_canonical_run_false_when_fallback_allowed() -> None:
    assert _is_canonical_run(_filters(allow_fallback=True), DEFAULT_COSTS) is False


def test_is_canonical_run_false_when_costs_overridden() -> None:
    overridden = replace(
        DEFAULT_COSTS, taker_fee_bps_per_side=DEFAULT_COSTS.taker_fee_bps_per_side + 1
    )
    assert _is_canonical_run(_filters(), overridden) is False


def test_is_canonical_run_false_when_strategy_versions_overridden() -> None:
    assert _is_canonical_run(_filters(strategy_versions=("other_v1",)), DEFAULT_COSTS) is False


def test_is_canonical_run_false_when_required_horizons_overridden() -> None:
    # A non-standard horizon set changes which episodes are ELIGIBLE at all
    # (decision_exclusion_reasons' missing_outcome:{horizon} check) --
    # letting that silently pass as "canonical" would let a non-standard
    # configuration produce a real nomination verdict.
    assert _is_canonical_run(_filters(required_horizons=(240,)), DEFAULT_COSTS) is False


# --- _structural_candidate_gap_count ----------------------------------------


def _gap_result(*, baseline: float | None, gates: dict[str, bool | None]) -> Result:
    return Result(
        pump_event_id=1,
        cluster_key="base:ERA",
        base="ERA",
        week_key="2026-W31",
        decision_id="d",
        decision_ts=None,
        baseline_triggered=True,
        baseline_net_return_pct=baseline,
        descriptors=None,
        candidate_gates=gates,
        candidate_returns={},
        trade=None,
    )


def test_structural_candidate_gap_count_counts_resolved_baseline_with_a_none_gate() -> None:
    from schurfer_analytics.token_behavior_discovery_report import (
        _structural_candidate_gap_count,
    )

    population = (
        _gap_result(baseline=1.0, gates={CANDIDATE_NO_PRIOR_SPIKE: None}),
        _gap_result(baseline=1.0, gates=dict.fromkeys(CANDIDATE_VARIANT_KEYS, True)),
    )
    assert _structural_candidate_gap_count(population) == 1


def test_structural_candidate_gap_count_excludes_unresolved_baseline() -> None:
    # A baseline that itself failed to resolve is a different, zero-
    # tolerance signal -- must NOT be folded into the structural allowance.
    from schurfer_analytics.token_behavior_discovery_report import (
        _structural_candidate_gap_count,
    )

    population = (_gap_result(baseline=None, gates={CANDIDATE_NO_PRIOR_SPIKE: None}),)
    assert _structural_candidate_gap_count(population) == 0


# --- _final_verdict -------------------------------------------------------


def _empty_inference(status: str) -> ChallengerInference:
    readiness = InferenceReadiness(
        status=status,
        eligible_episodes=10,
        formal_sample_episodes=10,
        formal_sample_clusters=5,
        baseline_resolved=10,
        completely_paired_episodes=10,
    )
    return ChallengerInference(
        inference_version="v",
        bootstrap_version="v",
        holm_version="v",
        seed_derivation="v",
        settings=DEFAULT_INFERENCE_SETTINGS,
        readiness=readiness,
        formal_sample_event_ids=(),
        cluster_concentration=(),
        baseline=None,
        challengers=(),
    )


def _ready_inference(verdict: str) -> ChallengerInference:
    readiness = InferenceReadiness(
        status="formal_sample_ready",
        eligible_episodes=100,
        formal_sample_episodes=100,
        formal_sample_clusters=30,
        baseline_resolved=100,
        completely_paired_episodes=100,
    )
    estimate = BootstrapEstimate(
        episodes=100, clusters=30, point_estimate=1.0, lower_bound=0.1, upper_bound=2.0
    )
    strategy = StrategyInference(
        strategy_key=CANDIDATE_NO_PRIOR_SPIKE,
        estimate=estimate,
        verdict="evidence_of_edge",
        minimum_leave_one_cluster_out_pct=0.5,
        leave_one_cluster_out=(),
    )
    paired = PairedInference(
        variant_key=CANDIDATE_NO_PRIOR_SPIKE,
        estimate=estimate,
        holm_rank=1,
        raw_p_value=0.01,
        holm_adjusted_p_value=0.01,
        holm_critical_alpha=0.05,
        holm_rejected=True,
        familywise_confidence_level=0.95,
        familywise_lower_bound=0.1,
        familywise_upper_bound=2.0,
    )
    result = ChallengerFormalResult(
        variant_key=CANDIDATE_NO_PRIOR_SPIKE, strategy=strategy, paired=paired, verdict=verdict
    )
    return ChallengerInference(
        inference_version="v",
        bootstrap_version="v",
        holm_version="v",
        seed_derivation="v",
        settings=DEFAULT_INFERENCE_SETTINGS,
        readiness=readiness,
        formal_sample_event_ids=(),
        cluster_concentration=(),
        baseline=strategy,
        challengers=(result,),
    )


_READY_WEEKS = WeekConcentration(distinct_weeks=3, largest_week_pct=40.0)
_GOOD_READINESS = {
    CANDIDATE_NO_PRIOR_SPIKE: CandidateReadiness(
        variant_key=CANDIDATE_NO_PRIOR_SPIKE,
        resolved_count=90,
        resolved_pct=90.0,
        changed_trades=15,
        changed_assets=10,
        materiality_ok=True,
        profit_factor=2.0,
        profit_factor_ok=True,
    )
}


def test_final_verdict_not_canonical_is_sensitivity_only() -> None:
    verdict, nominated = _final_verdict(
        canonical_run=False,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_ready_inference("shadow_candidate"),
        candidate_readiness=_GOOD_READINESS,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("sensitivity_only_no_promotion", None)


def test_final_verdict_insufficient_data_when_too_few_episodes() -> None:
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=10,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_ready_inference("shadow_candidate"),
        candidate_readiness=_GOOD_READINESS,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("insufficient_data", None)


def test_final_verdict_insufficient_data_when_too_few_clusters() -> None:
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=5,
        week_concentration=_READY_WEEKS,
        inference=_ready_inference("shadow_candidate"),
        candidate_readiness=_GOOD_READINESS,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("insufficient_data", None)


def test_final_verdict_insufficient_data_when_one_week_dominates() -> None:
    concentrated = WeekConcentration(distinct_weeks=3, largest_week_pct=80.0)
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=concentrated,
        inference=_ready_inference("shadow_candidate"),
        candidate_readiness=_GOOD_READINESS,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("insufficient_data", None)


def test_final_verdict_insufficient_data_when_shared_inference_gate_not_cleared() -> None:
    # This report's own (looser) gates pass, but challenger_inference's own
    # shared, stricter family-wide gate has not been cleared -- inference
    # readiness is "directional_only" and challengers is empty. Must be
    # "insufficient_data", never "no_separation" (there is no statistical
    # result to report "no separation" from at all).
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_empty_inference("directional_only"),
        candidate_readiness=_GOOD_READINESS,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("insufficient_data", None)


def test_final_verdict_candidate_requires_all_readiness_gates_together() -> None:
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_ready_inference("shadow_candidate"),
        candidate_readiness=_GOOD_READINESS,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("candidate", CANDIDATE_NO_PRIOR_SPIKE)


def _two_candidate_inference(
    *, holm_p_value_no_prior_spike: float, holm_p_value_high_volatility: float
) -> ChallengerInference:
    """Two candidates BOTH clearing shadow_candidate, with distinct Holm-
    adjusted p-values -- for the tie-break test. Holm-Bonferroni bounds the
    family-wise error rate; it does not cap the number of rejections at
    one, so this is a legitimate state to handle, not an invariant
    violation."""
    readiness = InferenceReadiness(
        status="formal_sample_ready",
        eligible_episodes=100,
        formal_sample_episodes=100,
        formal_sample_clusters=30,
        baseline_resolved=100,
        completely_paired_episodes=100,
    )
    estimate = BootstrapEstimate(
        episodes=100, clusters=30, point_estimate=1.0, lower_bound=0.1, upper_bound=2.0
    )
    challengers = []
    for variant_key, holm_p in (
        (CANDIDATE_NO_PRIOR_SPIKE, holm_p_value_no_prior_spike),
        (CANDIDATE_HIGH_VOLATILITY, holm_p_value_high_volatility),
    ):
        strategy = StrategyInference(
            strategy_key=variant_key,
            estimate=estimate,
            verdict="evidence_of_edge",
            minimum_leave_one_cluster_out_pct=0.5,
            leave_one_cluster_out=(),
        )
        paired = PairedInference(
            variant_key=variant_key,
            estimate=estimate,
            holm_rank=1,
            raw_p_value=holm_p,
            holm_adjusted_p_value=holm_p,
            holm_critical_alpha=0.05,
            holm_rejected=True,
            familywise_confidence_level=0.95,
            familywise_lower_bound=0.1,
            familywise_upper_bound=2.0,
        )
        challengers.append(
            ChallengerFormalResult(
                variant_key=variant_key,
                strategy=strategy,
                paired=paired,
                verdict="shadow_candidate",
            )
        )
    return ChallengerInference(
        inference_version="v",
        bootstrap_version="v",
        holm_version="v",
        seed_derivation="v",
        settings=DEFAULT_INFERENCE_SETTINGS,
        readiness=readiness,
        formal_sample_event_ids=(),
        cluster_concentration=(),
        baseline=challengers[0].strategy,
        challengers=tuple(challengers),
    )


def test_final_verdict_breaks_ties_deterministically_by_lowest_holm_p_value() -> None:
    both_good_readiness = {
        CANDIDATE_NO_PRIOR_SPIKE: _GOOD_READINESS[CANDIDATE_NO_PRIOR_SPIKE],
        CANDIDATE_HIGH_VOLATILITY: CandidateReadiness(
            variant_key=CANDIDATE_HIGH_VOLATILITY,
            resolved_count=90,
            resolved_pct=90.0,
            changed_trades=15,
            changed_assets=10,
            materiality_ok=True,
            profit_factor=2.0,
            profit_factor_ok=True,
        ),
    }
    both_positive_robustness = {
        CANDIDATE_NO_PRIOR_SPIKE: "positive",
        CANDIDATE_HIGH_VOLATILITY: "positive",
    }
    # no_prior_spike has the stronger (lower) Holm-adjusted p-value -- must win.
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_two_candidate_inference(
            holm_p_value_no_prior_spike=0.01, holm_p_value_high_volatility=0.04
        ),
        candidate_readiness=both_good_readiness,
        last_week_robustness=both_positive_robustness,
    )
    assert (verdict, nominated) == ("candidate", CANDIDATE_NO_PRIOR_SPIKE)

    # Reversed p-values -> the other candidate must win instead, proving
    # this is a real comparison, not a fixed first-in-list pick.
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_two_candidate_inference(
            holm_p_value_no_prior_spike=0.04, holm_p_value_high_volatility=0.01
        ),
        candidate_readiness=both_good_readiness,
        last_week_robustness=both_positive_robustness,
    )
    assert (verdict, nominated) == ("candidate", CANDIDATE_HIGH_VOLATILITY)


def test_final_verdict_no_separation_when_shadow_candidate_but_materiality_fails() -> None:
    weak_readiness = {
        CANDIDATE_NO_PRIOR_SPIKE: CandidateReadiness(
            variant_key=CANDIDATE_NO_PRIOR_SPIKE,
            resolved_count=90,
            resolved_pct=90.0,
            changed_trades=2,
            changed_assets=1,
            materiality_ok=False,
            profit_factor=2.0,
            profit_factor_ok=True,
        )
    }
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_ready_inference("shadow_candidate"),
        candidate_readiness=weak_readiness,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("no_separation", None)


def test_final_verdict_no_separation_when_profit_factor_fails() -> None:
    losing_readiness = {
        CANDIDATE_NO_PRIOR_SPIKE: CandidateReadiness(
            variant_key=CANDIDATE_NO_PRIOR_SPIKE,
            resolved_count=90,
            resolved_pct=90.0,
            changed_trades=15,
            changed_assets=10,
            materiality_ok=True,
            profit_factor=0.5,
            profit_factor_ok=False,
        )
    }
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_ready_inference("shadow_candidate"),
        candidate_readiness=losing_readiness,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("no_separation", None)


def test_final_verdict_no_separation_when_last_week_not_positive() -> None:
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_ready_inference("shadow_candidate"),
        candidate_readiness=_GOOD_READINESS,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "not_positive"},
    )
    assert (verdict, nominated) == ("no_separation", None)


def test_final_verdict_no_separation_when_statistically_inconclusive() -> None:
    verdict, nominated = _final_verdict(
        canonical_run=True,
        formal_population_size=100,
        formal_cluster_count=30,
        week_concentration=_READY_WEEKS,
        inference=_ready_inference("inconclusive"),
        candidate_readiness=_GOOD_READINESS,
        last_week_robustness={CANDIDATE_NO_PRIOR_SPIKE: "positive"},
    )
    assert (verdict, nominated) == ("no_separation", None)


# --- build_token_behavior_discovery_report (small end-to-end smoke test) -----


def test_build_report_rejects_a_non_frozen_cohort_window() -> None:
    filters = _filters(until=TOKEN_BEHAVIOR_DATASET_UNTIL_EXCLUSIVE + timedelta(days=1))
    dataset = build_replay_dataset([], filters)
    with pytest.raises(ValueError, match="frozen dataset cohort window"):
        build_token_behavior_discovery_report(
            dataset,
            filters,
            {},
            (),
            generated_at=datetime.now(UTC),
            code_revision="deadbeef",
            working_tree_dirty=False,
        )


def test_build_report_rejects_duplicate_market_paths() -> None:
    filters = _filters()
    dataset = build_replay_dataset([], filters)
    decision = _score6_decision(1)
    path = _complete_path(decision)
    duplicate_paths = (
        DecisionMarketPath(decision.decision_id or "x", path),
        DecisionMarketPath(decision.decision_id or "x", path),
    )
    with pytest.raises(ValueError, match="duplicate market paths"):
        build_token_behavior_discovery_report(
            dataset,
            filters,
            {},
            duplicate_paths,
            generated_at=datetime.now(UTC),
            code_revision="deadbeef",
            working_tree_dirty=False,
        )


def _flat_bars(days_of_history: int = 100) -> tuple[DailyBar, ...]:
    return tuple(_day_bar(days, 100.0) for days in range(days_of_history, 0, -1))


def _bars_with_one_recovered_spike() -> tuple[DailyBar, ...]:
    """Dense daily coverage with exactly one spike-and-recovery episode ten
    days before the decision, so freeze_thresholds has at least one
    "recovered" observation to take a median over."""
    return (
        *(_day_bar(d, 100.0) for d in range(100, 10, -1)),
        _day_bar(10, 138.0, high=140.0),  # high/prev_close(100)-1 = 0.40 -> qualifies
        _day_bar(9, 130.0),
        _day_bar(8, 120.0),
        _day_bar(7, 112.0),
        _day_bar(6, 106.0),
        _day_bar(5, 102.0),
        _day_bar(4, 100.0),
        _day_bar(3, 98.0),
        _day_bar(2, 95.0),  # within +-10% of pre_spike_close(100) -> recovered
        _day_bar(1, 94.0),
    )


def test_build_report_small_sample_is_insufficient_data() -> None:
    # A handful of real, resolvable episodes -- correct end-to-end wiring
    # through build_replay_dataset -> descriptors -> threshold freezing ->
    # challenger_inference, but nowhere near the frozen readiness gates.
    # Must land on "insufficient_data", not crash and not fabricate a
    # candidate.
    # Anchored on DAY0/DECISION_TS (not T0) -- the synthetic bars built by
    # _day_bar/_flat_bars/_bars_with_one_recovered_spike are all relative to
    # DAY0, and a decision timestamped near T0 (weeks earlier) would see
    # those bars as being in the future, silently changing what "known"
    # history looks like.
    decisions = [
        _decision(1, score=6, ts=DECISION_TS, pump_event_id=42),
        _decision(2, score=6, ts=DECISION_TS + timedelta(hours=1), pump_event_id=43),
        _decision(3, score=6, ts=DECISION_TS + timedelta(hours=2), pump_event_id=44),
    ]
    filters = _filters()
    dataset = build_replay_dataset(decisions, filters)
    paths = tuple(
        DecisionMarketPath(decision.decision_id or "", _complete_path(decision))
        for decision in decisions
    )
    token_history = {
        42: TokenHistoryContext(
            bars=_bars_with_one_recovered_spike(), onboarded_at=T0 - timedelta(days=400)
        ),
        43: TokenHistoryContext(bars=_flat_bars(), onboarded_at=T0 - timedelta(days=400)),
        44: TokenHistoryContext(bars=_flat_bars(), onboarded_at=T0 - timedelta(days=400)),
    }

    report = build_token_behavior_discovery_report(
        dataset,
        filters,
        token_history,
        paths,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
    )
    assert report.final_verdict == "insufficient_data"
    assert report.nominated_candidate is None
    assert report.eligible_episodes == 3
    assert report.out_of_scope_episodes == 0  # all 3 pump_event_ids covered
    assert report.formal_population_size == 3
    assert report.formal_sample_size == 3  # well under FORMAL_EPISODES, all counted


def test_build_report_excludes_out_of_scope_episodes_from_the_population() -> None:
    # A 4th episode whose pump_event_id has no token-history entry at all --
    # must be excluded from the population entirely, not counted as a
    # descriptor-unresolved formal_population member.
    decisions = [
        _decision(1, score=6, ts=DECISION_TS, pump_event_id=42),
        _decision(2, score=6, ts=DECISION_TS + timedelta(hours=1), pump_event_id=99),
    ]
    filters = _filters()
    dataset = build_replay_dataset(decisions, filters)
    paths = tuple(
        DecisionMarketPath(decision.decision_id or "", _complete_path(decision))
        for decision in decisions
    )
    token_history = {
        42: TokenHistoryContext(
            bars=_bars_with_one_recovered_spike(), onboarded_at=T0 - timedelta(days=400)
        ),
    }

    report = build_token_behavior_discovery_report(
        dataset,
        filters,
        token_history,
        paths,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
    )
    assert report.eligible_episodes == 2
    assert report.out_of_scope_episodes == 1
    assert report.formal_population_size == 1
    assert all(result.pump_event_id != 99 for result in report.episode_results)


def test_build_report_gracefully_degrades_when_thresholds_cannot_be_frozen() -> None:
    # Only flat, spike-free bars -- no "recovered" episode ever exists, so
    # freeze_thresholds cannot compute a recovery median. Must produce an
    # insufficient_data report, never an unhandled crash.
    decisions = [_decision(1, score=6, ts=DECISION_TS, pump_event_id=42)]
    filters = _filters()
    dataset = build_replay_dataset(decisions, filters)
    paths = tuple(
        DecisionMarketPath(decision.decision_id or "", _complete_path(decision))
        for decision in decisions
    )
    token_history = {
        42: TokenHistoryContext(bars=_flat_bars(), onboarded_at=T0 - timedelta(days=400)),
    }

    report = build_token_behavior_discovery_report(
        dataset,
        filters,
        token_history,
        paths,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
    )
    assert report.final_verdict == "insufficient_data"
    assert report.nominated_candidate is None
    assert report.manifest.thresholds is None
    assert report.formal_population_size == 0
    assert report.candidate_readiness == ()
    assert report.episode_results == ()
    # Funnel counts collected before the freeze attempt are still honest.
    assert report.eligible_episodes == 1
    assert report.out_of_scope_episodes == 0
    # Must still render without crashing in both formats.
    render_json(report)
    render_markdown(report)
