from __future__ import annotations

from datetime import datetime, timedelta

from schurfer_analytics.replay import ReplayDataset, ReplayDecision, ReplayEpisode
from schurfer_analytics.token_history_identity_preflight_report import (
    TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE,
    ExchangeReadinessRow,
    HistoryWindowRow,
    IdentityRecord,
    InstrumentSummary,
    ReadinessRow,
    TokenHistoryPreflightManifest,
    TokenHistoryPreflightReport,
    _history_window_bucket,
    _identity_fingerprint,
    _instrument_summaries,
    _select_baseline_decisions,
    identity_readiness,
    pump_event_sources_statement,
    render_markdown,
)
from schurfer_journal.models import PumpEventSource
from sqlalchemy.dialects import postgresql

T0 = TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE


def _sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )


# --- pump_event_sources_statement (pure, no DB) ------------------------------


def test_pump_event_sources_statement_scopes_by_event_ids() -> None:
    sql = _sql(pump_event_sources_statement([1, 2, 3]))
    assert "pump_event_sources.event_id IN (1, 2, 3)" in sql
    for column in (
        "market_id",
        "identity_key",
        "unified_symbol",
        "market_type",
        "base_asset",
        "quote_asset",
        "settle_asset",
        "onboarded_at",
        "identity_conflict",
    ):
        assert f"pump_event_sources.{column}" in sql
    # Deliberately not scoped to a single exchange in SQL: matching happens
    # per-decision in Python, since one event can have a row per exchange.
    assert "pump_event_sources.exchange =" not in sql


# --- identity_readiness (pure) ------------------------------------------


def _source(
    *,
    identity_key: str | None = "key-1",
    unified_symbol: str | None = "ERA/USDT:USDT",
    market_type: str | None = "swap",
    base_asset: str | None = "ERA",
    quote_asset: str | None = "USDT",
    settle_asset: str | None = "USDT",
    onboarded_at: datetime | None = T0 - timedelta(days=200),
    identity_conflict: bool = False,
) -> PumpEventSource:
    return PumpEventSource(
        event_id=1,
        exchange="binance",
        market_id="market-1",
        identity_key=identity_key,
        unified_symbol=unified_symbol,
        market_type=market_type,
        base_asset=base_asset,
        quote_asset=quote_asset,
        settle_asset=settle_asset,
        onboarded_at=onboarded_at,
        identity_conflict=identity_conflict,
    )


def test_identity_readiness_no_source_row() -> None:
    readiness, days = identity_readiness(None, "ERA", T0)
    assert readiness == "no_source_row"
    assert days is None


def test_identity_readiness_identity_conflict_fails_closed() -> None:
    readiness, days = identity_readiness(_source(identity_conflict=True), "ERA", T0)
    assert readiness == "identity_conflict"
    assert days is None


def test_identity_readiness_missing_identity_key() -> None:
    readiness, days = identity_readiness(_source(identity_key=None), "ERA", T0)
    assert readiness == "missing_identity"
    assert days is None


def test_identity_readiness_missing_unified_symbol() -> None:
    readiness, days = identity_readiness(_source(unified_symbol=None), "ERA", T0)
    assert readiness == "missing_identity"
    assert days is None


def test_identity_readiness_not_swap() -> None:
    readiness, days = identity_readiness(_source(market_type="spot"), "ERA", T0)
    assert readiness == "not_swap"
    assert days is None


def test_identity_readiness_base_mismatch() -> None:
    readiness, days = identity_readiness(_source(base_asset="OTHER"), "ERA", T0)
    assert readiness == "base_mismatch"
    assert days is None


def test_identity_readiness_base_match_is_case_insensitive() -> None:
    readiness, _days = identity_readiness(_source(base_asset="era"), "ERA", T0)
    assert readiness == "identity_ready"


def test_identity_readiness_quote_not_usdt() -> None:
    readiness, days = identity_readiness(_source(quote_asset="USDC"), "ERA", T0)
    assert readiness == "quote_not_usdt"
    assert days is None


def test_identity_readiness_settle_not_usdt() -> None:
    readiness, days = identity_readiness(_source(settle_asset="USDC"), "ERA", T0)
    assert readiness == "settle_not_usdt"
    assert days is None


def test_identity_readiness_missing_onboarded_at() -> None:
    readiness, days = identity_readiness(_source(onboarded_at=None), "ERA", T0)
    assert readiness == "missing_onboarded_at"
    assert days is None


def test_identity_readiness_naive_onboarded_at_fails_closed() -> None:
    # A naive datetime (no tzinfo) must never be silently guessed as UTC:
    # fail closed with its own reason instead.
    naive = datetime(2026, 1, 1)
    readiness, days = identity_readiness(_source(onboarded_at=naive), "ERA", T0)
    assert readiness == "invalid_onboarded_at_timezone"
    assert days is None


def test_identity_readiness_onboarded_after_decision_fails_closed() -> None:
    source = _source(onboarded_at=T0 + timedelta(days=1))
    readiness, days = identity_readiness(source, "ERA", T0)
    assert readiness == "onboarded_at_after_decision"
    assert days is None


def test_identity_readiness_onboarded_exactly_at_decision_fails_closed() -> None:
    source = _source(onboarded_at=T0)
    readiness, days = identity_readiness(source, "ERA", T0)
    assert readiness == "onboarded_at_after_decision"
    assert days is None


def test_identity_readiness_ready_computes_available_days() -> None:
    source = _source(onboarded_at=T0 - timedelta(days=200))
    readiness, days = identity_readiness(source, "ERA", T0)
    assert readiness == "identity_ready"
    assert days == 200


# --- _history_window_bucket ---------------------------------------------


def test_history_window_bucket_under_90d() -> None:
    assert _history_window_bucket(10) == "under_90d"
    assert _history_window_bucket(89) == "under_90d"


def test_history_window_bucket_at_least_90d() -> None:
    assert _history_window_bucket(90) == "at_least_90d"
    assert _history_window_bucket(364) == "at_least_90d"


def test_history_window_bucket_at_least_365d() -> None:
    assert _history_window_bucket(365) == "at_least_365d"
    assert _history_window_bucket(1000) == "at_least_365d"


# --- _identity_fingerprint -------------------------------------------------


def test_identity_fingerprint_is_stable_for_identical_input() -> None:
    sources = {(1, "binance"): _source()}
    assert _identity_fingerprint(sources) == _identity_fingerprint(sources)


def test_identity_fingerprint_changes_when_identity_conflict_flips() -> None:
    clean = {(1, "binance"): _source(identity_conflict=False)}
    conflicted = {(1, "binance"): _source(identity_conflict=True)}
    assert _identity_fingerprint(clean) != _identity_fingerprint(conflicted)


def test_identity_fingerprint_changes_when_onboarded_at_is_filled_in_later() -> None:
    before = {(1, "binance"): _source(onboarded_at=None)}
    after = {(1, "binance"): _source(onboarded_at=T0 - timedelta(days=10))}
    assert _identity_fingerprint(before) != _identity_fingerprint(after)


# --- _instrument_summaries -------------------------------------------------


def _record(
    *,
    pump_event_id: int,
    exchange: str = "binance",
    base: str = "ERA",
    readiness: str = "identity_ready",
    identity_key: str | None = "key-1",
    unified_symbol: str | None = "ERA/USDT:USDT",
    available_history_days: int | None = 200,
) -> IdentityRecord:
    return IdentityRecord(
        pump_event_id=pump_event_id,
        base=base,
        exchange=exchange,
        decision_ts=T0,
        readiness=readiness,
        identity_key=identity_key,
        unified_symbol=unified_symbol,
        available_history_days=available_history_days,
    )


def test_instrument_summaries_groups_by_exchange_and_identity_key() -> None:
    records = [
        _record(pump_event_id=1, available_history_days=100),
        _record(pump_event_id=2, available_history_days=300),
    ]
    summaries = _instrument_summaries(records)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.decisions == 2
    assert summary.min_available_history_days == 100
    assert summary.max_available_history_days == 300


def test_instrument_summaries_excludes_non_ready_records() -> None:
    records = [_record(pump_event_id=1, readiness="identity_conflict", identity_key=None)]
    assert _instrument_summaries(records) == ()


def test_instrument_summaries_separates_by_exchange() -> None:
    records = [
        _record(pump_event_id=1, exchange="binance"),
        _record(pump_event_id=2, exchange="bitget"),
    ]
    summaries = _instrument_summaries(records)
    assert len(summaries) == 2
    assert {summary.exchange for summary in summaries} == {"binance", "bitget"}


# --- _select_baseline_decisions (pure, over ReplayDataset) -------------------


def _components(points: tuple[int, ...]) -> dict[str, object]:
    names = ("pump_age", "price_extent", "oi_trend", "funding_rate", "retrace_from_peak")
    return {
        name: {"value": 1.0, "points": score, "max": 2, "note": ""}
        for name, score in zip(names, points, strict=True)
    }


def _decision(row_id: int, points: tuple[int, ...], *, exchange: str = "binance") -> ReplayDecision:
    ts = T0 + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=T0,
        event_closed_at=T0 + timedelta(hours=7),
        ts=ts,
        base="ERA",
        exchange=exchange,
        action="skipped",
        reason="measurement",
        score=sum(points),
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {
                "computed_at": ts.timestamp(),
                "components": _components(points),
                "data_quality": {"oi": True, "funding": True},
            },
            "config": {"require_market_quality": True, "signal_position_usd": 50},
        },
        liquidity={"status": "sampled", "quality": {"allowed": True, "depth_target_usd": 100}},
        outcomes=(),
    )


def _episode(*decisions: ReplayDecision) -> ReplayEpisode:
    return ReplayEpisode(42, "ERA", "base:ERA", decisions, ())


def test_select_baseline_decisions_keeps_score_6_and_above() -> None:
    decision = _decision(1, (2, 2, 1, 1, 1))  # score 7
    dataset = ReplayDataset(
        decisions=(decision,),
        episodes=(_episode(decision),),
        unassigned_decisions=(),
        unassigned_reasons=(),
        input_fingerprint="x",
    )
    selected = _select_baseline_decisions(dataset)
    assert len(selected) == 1
    pump_event_id, base, picked = selected[0]
    assert pump_event_id == 42
    assert base == "ERA"
    assert picked.decision_id == decision.decision_id


def test_select_baseline_decisions_excludes_below_threshold() -> None:
    decision = _decision(1, (1, 1, 1, 1, 1))  # score 5, below score_6
    dataset = ReplayDataset(
        decisions=(decision,),
        episodes=(_episode(decision),),
        unassigned_decisions=(),
        unassigned_reasons=(),
        input_fingerprint="x",
    )
    assert _select_baseline_decisions(dataset) == []


# --- render smoke test ---------------------------------------------------


def test_render_markdown_smoke() -> None:
    report = TokenHistoryPreflightReport(
        manifest=TokenHistoryPreflightManifest(
            protocol_version="v",
            replay_engine_version="v",
            replay_query_version="v",
            report_version="v",
            code_revision="abc123",
            working_tree_dirty=False,
            generated_at=T0,
            dataset_since=T0,
            dataset_until_exclusive=T0 + timedelta(days=1),
            decision_fingerprint="deadbeef",
            identity_fingerprint="cafef00d",
            input_fingerprint="combined123",
            strategy_versions=("pump_short_v1_market_quality",),
            resolver_version="v1",
            required_horizons=(60, 480),
            fallback_allowed=False,
            history_window_days=(90, 365),
        ),
        eligible_episodes=10,
        excluded_episodes=2,
        input_exclusion_reasons=(ReadinessRow("no_market_path", 2),),
        replay_eligible_baseline_decisions=5,
        readiness_distribution=(
            ReadinessRow("identity_ready", 3),
            ReadinessRow("no_source_row", 2),
        ),
        readiness_by_exchange=(ExchangeReadinessRow("binance", 3, 2),),
        unique_ready_instruments=2,
        history_window_distribution=(
            HistoryWindowRow("at_least_365d", 2),
            HistoryWindowRow("under_90d", 1),
        ),
        median_available_history_days=200.0,
        instruments=(InstrumentSummary("binance", "key-1", "ERA/USDT:USDT", "ERA", 2, 100, 300),),
        records=(
            IdentityRecord(
                42, "ERA", "binance", T0, "identity_ready", "key-1", "ERA/USDT:USDT", 200
            ),
        ),
    )
    text = render_markdown(report)
    assert "Token-Behavior-History Identity Preflight" in text
    assert "DB-only preflight" in text
    assert "abc123" in text
    assert "identity_ready" in text
    assert "200.0 days" in text
    assert "Sampling frame for step 2" in text
    assert "ERA/USDT:USDT" in text
