from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from schurfer_analytics.ohlcv import IncompleteFetchError
from schurfer_analytics.token_history_identity_preflight_report import (
    IdentityRecord,
    InstrumentSummary,
    ReadinessRow,
    TokenHistoryPreflightManifest,
)
from schurfer_analytics.token_history_ohlcv_sample_report import (
    MAX_SAMPLE_INSTRUMENTS,
    MAX_SAMPLE_WINDOW_DAYS,
    SampleCandidate,
    _fetch_instrument_sample,
    _pick_nearest_median,
    _representative_record,
    _sample_fingerprint,
    analyze_gaps,
    render_json,
    render_markdown,
    run_live_sample,
    sample_window_ms,
    select_sample_candidates,
)
from schurfer_analytics.token_history_ohlcv_sample_report import (
    TokenHistoryLiveSampleManifest as Manifest,
)
from schurfer_analytics.token_history_ohlcv_sample_report import (
    TokenHistoryLiveSampleReport as LiveSampleReport,
)

T0 = datetime(2026, 8, 1, tzinfo=UTC)
DAY_MS = 24 * 60 * 60 * 1000


def _daily_bar(ts_ms: int) -> list[float]:
    return [ts_ms, 100, 101, 99, 100, 1]


def _instrument(
    *,
    exchange: str = "binance",
    identity_key: str = "key-1",
    unified_symbol: str = "ERA/USDT:USDT",
    base: str = "ERA",
    decisions: int = 1,
    min_days: int = 200,
    max_days: int = 200,
) -> InstrumentSummary:
    return InstrumentSummary(
        exchange=exchange,
        identity_key=identity_key,
        unified_symbol=unified_symbol,
        base=base,
        decisions=decisions,
        min_available_history_days=min_days,
        max_available_history_days=max_days,
    )


def _record(
    *,
    pump_event_id: int,
    exchange: str = "binance",
    identity_key: str = "key-1",
    unified_symbol: str = "ERA/USDT:USDT",
    base: str = "ERA",
    decision_ts: datetime = T0,
    available_history_days: int = 200,
    onboarded_at: datetime | None = None,
    readiness: str = "identity_ready",
) -> IdentityRecord:
    return IdentityRecord(
        pump_event_id=pump_event_id,
        base=base,
        exchange=exchange,
        decision_ts=decision_ts,
        readiness=readiness,
        identity_key=identity_key,
        unified_symbol=unified_symbol,
        available_history_days=available_history_days,
        onboarded_at=(
            onboarded_at
            if onboarded_at is not None
            else decision_ts - timedelta(days=available_history_days)
        ),
    )


def _candidate(
    *,
    exchange: str = "binance",
    bucket: str = "at_least_90d",
    identity_key: str = "key-1",
    unified_symbol: str = "ERA/USDT:USDT",
    base: str = "ERA",
    available_history_days: int = 10,
    pump_event_id: int = 1,
    decision_ts: datetime = T0,
    onboarded_at: datetime | None = None,
    selected: bool = True,
    exclusion_reason: str | None = None,
) -> SampleCandidate:
    return SampleCandidate(
        exchange=exchange,
        bucket=bucket,
        identity_key=identity_key,
        unified_symbol=unified_symbol,
        base=base,
        available_history_days=available_history_days,
        pump_event_id=pump_event_id,
        decision_ts=decision_ts,
        onboarded_at=(
            onboarded_at
            if onboarded_at is not None
            else decision_ts - timedelta(days=available_history_days)
        ),
        selected=selected,
        exclusion_reason=exclusion_reason,
    )


def _preflight_manifest() -> TokenHistoryPreflightManifest:
    return TokenHistoryPreflightManifest(
        protocol_version="v",
        replay_engine_version="v",
        replay_query_version="v",
        report_version="token_history_identity_preflight_report_v1",
        code_revision="abc123",
        working_tree_dirty=False,
        generated_at=T0,
        dataset_since=T0 - timedelta(days=30),
        dataset_until_exclusive=T0,
        decision_fingerprint="deadbeef",
        identity_fingerprint="cafef00d",
        input_fingerprint="combined123",
        strategy_versions=("pump_short_v1_market_quality",),
        resolver_version="v1",
        required_horizons=(60, 480),
        fallback_allowed=False,
        history_window_days=(90, 365),
    )


# --- sample_window_ms -----------------------------------------------------


def test_sample_window_ms_uses_full_history_for_a_young_instrument() -> None:
    onboarded_at = T0 - timedelta(days=10)
    start_ms, end_ms = sample_window_ms(onboarded_at, T0)
    assert start_ms == int(onboarded_at.timestamp() * 1000)
    assert end_ms == int(T0.timestamp() * 1000)


def test_sample_window_ms_clamps_an_old_instrument_to_365_days() -> None:
    onboarded_at = T0 - timedelta(days=2000)
    start_ms, end_ms = sample_window_ms(onboarded_at, T0)
    expected_start = T0 - timedelta(days=MAX_SAMPLE_WINDOW_DAYS)
    assert start_ms == int(expected_start.timestamp() * 1000)
    assert end_ms == int(T0.timestamp() * 1000)
    assert (end_ms - start_ms) == MAX_SAMPLE_WINDOW_DAYS * DAY_MS


# --- _pick_nearest_median --------------------------------------------------


def test_pick_nearest_median_picks_the_closest_to_median() -> None:
    instruments = [
        _instrument(identity_key="short", max_days=10),
        _instrument(identity_key="mid", max_days=100),
        _instrument(identity_key="long", max_days=400),
    ]
    picked = _pick_nearest_median(instruments)
    assert picked.identity_key == "mid"


def test_pick_nearest_median_is_stable_under_input_permutation() -> None:
    instruments = [
        _instrument(identity_key="a", max_days=50),
        _instrument(identity_key="b", max_days=150),
        _instrument(identity_key="c", max_days=90),
    ]
    forward = _pick_nearest_median(instruments)
    backward = _pick_nearest_median(list(reversed(instruments)))
    assert forward.identity_key == backward.identity_key


# --- _representative_record -------------------------------------------------


def test_representative_record_picks_latest_decision_then_highest_event_id() -> None:
    records = (
        _record(pump_event_id=1, decision_ts=T0 - timedelta(days=1)),
        _record(pump_event_id=3, decision_ts=T0),
        _record(pump_event_id=2, decision_ts=T0),
    )
    picked = _representative_record(records, "binance", "key-1")
    assert picked.pump_event_id == 3


def test_representative_record_ignores_non_ready_records() -> None:
    records = (_record(pump_event_id=1, readiness="identity_conflict"),)
    with pytest.raises(ValueError, match="no identity_ready record"):
        _representative_record(records, "binance", "key-1")


# --- select_sample_candidates -----------------------------------------------


def test_select_sample_candidates_picks_one_per_exchange_and_bucket() -> None:
    instruments = (
        _instrument(identity_key="binance-short", max_days=10),
        _instrument(identity_key="binance-long", max_days=400),
        _instrument(identity_key="bybit-short", exchange="bybit", max_days=5),
    )
    records = (
        _record(pump_event_id=1, identity_key="binance-short", available_history_days=10),
        _record(pump_event_id=2, identity_key="binance-long", available_history_days=400),
        _record(
            pump_event_id=3,
            exchange="bybit",
            identity_key="bybit-short",
            available_history_days=5,
        ),
    )
    candidates = select_sample_candidates(instruments, records)
    assert len(candidates) == 3
    assert all(candidate.selected for candidate in candidates)
    keys = {(candidate.exchange, candidate.bucket) for candidate in candidates}
    assert keys == {
        ("binance", "under_90d"),
        ("binance", "at_least_365d"),
        ("bybit", "under_90d"),
    }


def test_select_sample_candidates_truncates_by_budget_with_a_named_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.MAX_SAMPLE_INSTRUMENTS", 1
    )
    instruments = (
        _instrument(identity_key="long", max_days=400),
        _instrument(identity_key="short", max_days=10),
    )
    records = (
        _record(pump_event_id=1, identity_key="long", available_history_days=400),
        _record(pump_event_id=2, identity_key="short", available_history_days=10),
    )
    candidates = select_sample_candidates(instruments, records)
    assert len(candidates) == 2
    selected = [c for c in candidates if c.selected]
    excluded = [c for c in candidates if not c.selected]
    assert len(selected) == 1
    assert len(excluded) == 1
    # Richest history bucket has priority under the truncation order.
    assert selected[0].identity_key == "long"
    assert excluded[0].exclusion_reason == "sample_budget_exhausted"


def test_select_sample_candidates_truncation_prefers_richest_history_across_exchanges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An early-alphabet exchange's short-history bucket must NOT survive a
    cap ahead of a later-alphabet exchange's long-history bucket: bucket
    richness is the primary sort key, exchange name only breaks ties within
    the same bucket rank."""
    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.MAX_SAMPLE_INSTRUMENTS", 1
    )
    instruments = (
        _instrument(exchange="aaa_exchange", identity_key="aaa-short", max_days=10),
        _instrument(exchange="zzz_exchange", identity_key="zzz-long", max_days=400),
    )
    records = (
        _record(
            pump_event_id=1,
            exchange="aaa_exchange",
            identity_key="aaa-short",
            available_history_days=10,
        ),
        _record(
            pump_event_id=2,
            exchange="zzz_exchange",
            identity_key="zzz-long",
            available_history_days=400,
        ),
    )
    candidates = select_sample_candidates(instruments, records)
    selected = [c for c in candidates if c.selected]
    assert len(selected) == 1
    assert selected[0].exchange == "zzz_exchange"


def test_select_sample_candidates_is_stable_under_input_permutation() -> None:
    instruments = (
        _instrument(identity_key="a", max_days=10),
        _instrument(identity_key="b", exchange="bybit", max_days=100),
        _instrument(identity_key="c", exchange="gate", max_days=400),
    )
    records = (
        _record(pump_event_id=1, identity_key="a", available_history_days=10),
        _record(pump_event_id=2, exchange="bybit", identity_key="b", available_history_days=100),
        _record(pump_event_id=3, exchange="gate", identity_key="c", available_history_days=400),
    )
    forward = select_sample_candidates(instruments, records)
    backward = select_sample_candidates(tuple(reversed(instruments)), tuple(reversed(records)))
    assert forward == backward


def test_select_sample_candidates_uses_the_snapshot_onboarded_at() -> None:
    onboarded_at = T0 - timedelta(days=42)
    instruments = (_instrument(max_days=42),)
    records = (
        _record(
            pump_event_id=1,
            available_history_days=42,
            onboarded_at=onboarded_at,
            decision_ts=T0,
        ),
    )
    candidates = select_sample_candidates(instruments, records)
    assert len(candidates) == 1
    assert candidates[0].onboarded_at == onboarded_at
    assert candidates[0].decision_ts == T0


# --- analyze_gaps -----------------------------------------------------------


def test_analyze_gaps_reports_zero_missing_when_fully_covered() -> None:
    start_ms = int(T0.timestamp() * 1000)
    end_ms = start_ms + 5 * DAY_MS
    candles = tuple(start_ms + offset * DAY_MS for offset in range(5))
    gaps = analyze_gaps(candles, start_ms, end_ms)
    assert gaps.expected_full_days == 5
    assert gaps.returned_days == 5
    assert gaps.leading_missing_days == 0
    assert gaps.internal_missing_dates == ()
    assert gaps.trailing_missing_days == 0
    assert gaps.possible_retention_or_listing_gap is False


def test_analyze_gaps_detects_a_leading_gap() -> None:
    start_ms = int(T0.timestamp() * 1000)
    end_ms = start_ms + 5 * DAY_MS
    candles = tuple(start_ms + offset * DAY_MS for offset in range(2, 5))
    gaps = analyze_gaps(candles, start_ms, end_ms)
    assert gaps.leading_missing_days == 2
    assert gaps.internal_missing_dates == ()
    assert gaps.trailing_missing_days == 0
    assert gaps.possible_retention_or_listing_gap is True


def test_analyze_gaps_detects_an_internal_gap() -> None:
    start_ms = int(T0.timestamp() * 1000)
    end_ms = start_ms + 5 * DAY_MS
    candles = tuple(start_ms + offset * DAY_MS for offset in range(5) if offset != 2)
    gaps = analyze_gaps(candles, start_ms, end_ms)
    assert gaps.leading_missing_days == 0
    assert len(gaps.internal_missing_dates) == 1
    assert gaps.trailing_missing_days == 0
    assert gaps.possible_retention_or_listing_gap is False


def test_analyze_gaps_detects_a_trailing_gap() -> None:
    start_ms = int(T0.timestamp() * 1000)
    end_ms = start_ms + 5 * DAY_MS
    candles = tuple(start_ms + offset * DAY_MS for offset in range(3))
    gaps = analyze_gaps(candles, start_ms, end_ms)
    assert gaps.leading_missing_days == 0
    assert gaps.internal_missing_dates == ()
    assert gaps.trailing_missing_days == 2


def test_analyze_gaps_does_not_count_partial_edge_days_as_missing() -> None:
    # start_ms/end_ms land mid-day: the partial first and last days must not
    # appear in the expected grid at all, so a fully-covered set of FULL days
    # must show zero gaps even though start_ms/end_ms themselves are never
    # exactly on a candle boundary.
    from schurfer_analytics.ohlcv import ceil_to_timeframe

    start_ms = int(T0.timestamp() * 1000) + 6 * 60 * 60 * 1000  # partial first day
    first_full = ceil_to_timeframe(start_ms, DAY_MS)
    end_ms = first_full + 5 * DAY_MS + 3 * 60 * 60 * 1000  # 5 full days + partial last day
    candles = tuple(first_full + offset * DAY_MS for offset in range(5))
    gaps = analyze_gaps(candles, start_ms, end_ms)
    assert gaps.expected_full_days == 5
    assert gaps.leading_missing_days == 0
    assert gaps.internal_missing_dates == ()
    assert gaps.trailing_missing_days == 0


# --- _sample_fingerprint -----------------------------------------------------


def test_sample_fingerprint_is_stable_for_identical_input() -> None:
    candidates = (_candidate(),)
    assert _sample_fingerprint(candidates) == _sample_fingerprint(candidates)


def test_sample_fingerprint_changes_when_selection_changes() -> None:
    selected = (_candidate(selected=True, exclusion_reason=None),)
    excluded = (_candidate(selected=False, exclusion_reason="sample_budget_exhausted"),)
    assert _sample_fingerprint(selected) != _sample_fingerprint(excluded)


# --- _fetch_instrument_sample (mocked exchange, no network) -----------------


async def test_fetch_instrument_sample_completed_with_full_gap_analysis() -> None:
    onboarded_at = T0 - timedelta(days=5)
    candidate = _candidate(onboarded_at=onboarded_at, decision_ts=T0, available_history_days=5)
    start_ms, _end_ms = sample_window_ms(onboarded_at, T0)
    bars = [_daily_bar(start_ms + offset * DAY_MS) for offset in range(5)]
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=bars)

    result = await _fetch_instrument_sample(exchange, candidate)

    assert result.outcome == "completed"
    assert result.error_type is None
    assert result.gap_analysis is not None
    assert result.gap_analysis.leading_missing_days == 0
    assert result.raw_page_size_stats is not None
    assert result.normalized_page_size_stats is not None
    assert result.latency_stats is not None
    assert result.empty_calls == 0
    assert result.timeout_calls == 0
    assert result.error_calls == 0
    assert result.filtered_or_deduplicated_rows_total == 0


async def test_fetch_instrument_sample_partial_coverage_is_not_reported_as_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent partial return (pre-existing fetch_symbol_candles behavior:
    an empty page after retries) must be labeled partial_coverage, not
    completed, even though fetch_symbol_candles itself did not raise."""
    monkeypatch.setattr("schurfer_analytics.ohlcv._EMPTY_PAGE_RETRY_DELAY_SECONDS", 0)
    onboarded_at = T0 - timedelta(days=5)
    candidate = _candidate(onboarded_at=onboarded_at, decision_ts=T0, available_history_days=5)
    start_ms, _end_ms = sample_window_ms(onboarded_at, T0)
    # Only 2 of the 5 expected days are ever returned; the exchange then
    # returns empty pages until retries are exhausted, which is a legitimate,
    # pre-existing silent-partial-result path in fetch_symbol_candles.
    bars = [_daily_bar(start_ms + offset * DAY_MS) for offset in range(2)]
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=[bars, [], [], []])

    result = await _fetch_instrument_sample(exchange, candidate)

    assert result.outcome == "partial_coverage"
    assert result.gap_analysis is not None
    assert result.gap_analysis.trailing_missing_days == 3


async def test_fetch_instrument_sample_no_data_when_nothing_is_returned() -> None:
    onboarded_at = T0 - timedelta(days=5)
    candidate = _candidate(onboarded_at=onboarded_at, decision_ts=T0, available_history_days=5)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])

    result = await _fetch_instrument_sample(exchange, candidate)

    assert result.outcome == "no_data"
    assert result.gap_analysis is not None
    assert result.gap_analysis.expected_full_days == 5
    assert result.gap_analysis.returned_days == 0


async def test_fetch_instrument_sample_no_full_days_expected_for_a_sub_day_window() -> None:
    # onboarded_at only an hour before decision_ts: the window is too short
    # to contain even one full 1d bar, regardless of what the exchange
    # returns.
    onboarded_at = T0 - timedelta(hours=1)
    candidate = _candidate(onboarded_at=onboarded_at, decision_ts=T0, available_history_days=0)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])

    result = await _fetch_instrument_sample(exchange, candidate)

    assert result.outcome == "no_full_days_expected"
    assert result.gap_analysis is not None
    assert result.gap_analysis.expected_full_days == 0


async def test_fetch_instrument_sample_separates_raw_from_normalized_page_stats() -> None:
    """A page cap of 200 bars, hit twice, with one empty retry in between
    (200, 0, 200): the empty retry's size-0 call must not drag raw/normalized
    min_bars down to 0, and must show up as an empty_calls count instead."""
    onboarded_at = T0 - timedelta(days=500)  # clamped to MAX_SAMPLE_WINDOW_DAYS
    candidate = _candidate(onboarded_at=onboarded_at, decision_ts=T0, available_history_days=500)
    start_ms, _end_ms = sample_window_ms(onboarded_at, T0)

    async def paged_fetch_ohlcv(
        _symbol: str, _timeframe: str, since: int, limit: int
    ) -> list[list[float]]:
        bars = [
            _daily_bar(start_ms + offset * DAY_MS)
            for offset in range(MAX_SAMPLE_WINDOW_DAYS)
            if start_ms + offset * DAY_MS > since
        ]
        return bars[: min(limit, 200)]

    call_count = {"n": 0}

    async def flaky_fetch_ohlcv(
        symbol: str, timeframe: str, since: int, limit: int
    ) -> list[list[float]]:
        call_count["n"] += 1
        if call_count["n"] == 2:
            return []
        return await paged_fetch_ohlcv(symbol, timeframe, since, limit)

    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=flaky_fetch_ohlcv)

    result = await _fetch_instrument_sample(exchange, candidate)

    assert result.empty_calls == 1
    assert result.raw_page_size_stats is not None
    assert result.raw_page_size_stats.min_bars > 0
    assert result.normalized_page_size_stats is not None
    assert result.normalized_page_size_stats.min_bars > 0


async def test_fetch_instrument_sample_reports_incomplete_fetch_error() -> None:
    onboarded_at = T0 - timedelta(days=10)
    candidate = _candidate(onboarded_at=onboarded_at, decision_ts=T0, available_history_days=10)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(
        side_effect=IncompleteFetchError(
            symbol=candidate.unified_symbol,
            start_ms=0,
            end_ms=1,
            next_cursor_ms=0,
            successful_pages=0,
            api_calls=0,
        )
    )

    result = await _fetch_instrument_sample(exchange, candidate)

    assert result.outcome == "incomplete_fetch_error"
    assert result.error_type == "IncompleteFetchError"
    assert result.gap_analysis is None


async def test_fetch_instrument_sample_isolates_a_generic_exchange_failure() -> None:
    onboarded_at = T0 - timedelta(days=10)
    candidate = _candidate(onboarded_at=onboarded_at, decision_ts=T0, available_history_days=10)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=ValueError("exchange blew up"))

    result = await _fetch_instrument_sample(exchange, candidate)

    assert result.outcome == "fetch_exception"
    assert result.error_type == "ValueError"
    assert result.gap_analysis is None


# --- run_live_sample (mocked EXCHANGE_FACTORIES, no network) ----------------


def _fake_client(bars: list[list[float]] | None = None, *, fail: bool = False) -> AsyncMock:
    client = AsyncMock()
    if fail:
        client.fetch_ohlcv = AsyncMock(side_effect=ValueError("boom"))
    else:
        client.fetch_ohlcv = AsyncMock(return_value=bars or [])
    client.close = AsyncMock()
    return client


async def test_run_live_sample_skips_unselected_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fake_client([])
    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.EXCHANGE_FACTORIES",
        {"binance": lambda: client},
    )
    candidates = (_candidate(selected=False, exclusion_reason="sample_budget_exhausted"),)
    run = await run_live_sample(candidates)
    assert run.results == ()
    assert run.operational_warnings == ()
    client.fetch_ohlcv.assert_not_called()


async def test_run_live_sample_marks_unknown_exchange_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.EXCHANGE_FACTORIES", {}
    )
    candidates = (_candidate(exchange="not_a_real_exchange"),)
    run = await run_live_sample(candidates)
    assert len(run.results) == 1
    assert run.results[0].outcome == "unsupported_exchange"
    assert run.results[0].api_calls == 0


async def test_run_live_sample_continues_after_one_instrument_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_client = _fake_client(fail=True)
    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.EXCHANGE_FACTORIES",
        {"binance": lambda: failing_client},
    )
    candidates = (
        _candidate(identity_key="broken", pump_event_id=1, available_history_days=5),
        _candidate(identity_key="fine", pump_event_id=2, available_history_days=5),
    )
    run = await run_live_sample(candidates)
    assert len(run.results) == 2
    assert run.results[0].outcome == "fetch_exception"
    # The second candidate on the same exchange still ran, using the same
    # (still-failing, in this fixture) client, and did not abort the batch.
    assert run.results[1].outcome == "fetch_exception"


async def test_run_live_sample_closes_every_created_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binance_client = _fake_client([])
    bybit_client = _fake_client(fail=True)
    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.EXCHANGE_FACTORIES",
        {"binance": lambda: binance_client, "bybit": lambda: bybit_client},
    )
    candidates = (
        _candidate(exchange="binance", identity_key="a"),
        _candidate(exchange="bybit", identity_key="b"),
    )
    await run_live_sample(candidates)
    binance_client.close.assert_awaited_once_with(clean_instance_data=True)
    bybit_client.close.assert_awaited_once_with(clean_instance_data=True)


async def test_run_live_sample_records_exchange_client_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_factory() -> AsyncMock:
        raise RuntimeError("could not construct client")

    good_client = _fake_client([])
    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.EXCHANGE_FACTORIES",
        {"binance": broken_factory, "bybit": lambda: good_client},
    )
    candidates = (
        _candidate(exchange="binance", identity_key="a", available_history_days=5),
        _candidate(exchange="bybit", identity_key="b", available_history_days=5),
    )
    run = await run_live_sample(candidates)
    assert len(run.results) == 2
    by_exchange = {result.exchange: result for result in run.results}
    assert by_exchange["binance"].outcome == "exchange_client_init_failed"
    assert by_exchange["binance"].error_type == "RuntimeError"
    assert by_exchange["binance"].api_calls == 0
    # bybit's turn still ran despite binance's factory failing.
    assert by_exchange["bybit"].outcome != "exchange_client_init_failed"


async def test_run_live_sample_records_close_failure_as_warning_without_losing_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _fake_client([])
    client.close = AsyncMock(side_effect=RuntimeError("close blew up"))
    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.EXCHANGE_FACTORIES",
        {"binance": lambda: client, "bybit": lambda: _fake_client([])},
    )
    candidates = (
        _candidate(exchange="binance", identity_key="a", available_history_days=5),
        _candidate(exchange="bybit", identity_key="b", available_history_days=5),
    )
    run = await run_live_sample(candidates)
    # The binance fetch result itself is NOT lost even though closing its
    # client failed afterward.
    assert len(run.results) == 2
    assert any(result.exchange == "binance" for result in run.results)
    assert len(run.operational_warnings) == 1
    assert "binance" in run.operational_warnings[0]
    # bybit's turn still ran despite binance's close() failing.
    assert any(result.exchange == "bybit" for result in run.results)


async def test_run_live_sample_keeps_at_most_one_exchange_client_alive_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"current": 0, "peak": 0}

    def make_factory() -> object:
        def factory() -> AsyncMock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            client = AsyncMock()
            client.fetch_ohlcv = AsyncMock(return_value=[])

            async def _close(*, clean_instance_data: bool = True) -> None:
                state["current"] -= 1

            client.close = _close
            return client

        return factory

    monkeypatch.setattr(
        "schurfer_analytics.token_history_ohlcv_sample_report.EXCHANGE_FACTORIES",
        {"binance": make_factory(), "bybit": make_factory(), "gate": make_factory()},
    )
    candidates = (
        _candidate(exchange="binance", identity_key="a"),
        _candidate(exchange="bybit", identity_key="b"),
        _candidate(exchange="gate", identity_key="c"),
    )
    await run_live_sample(candidates)
    assert state["peak"] == 1
    assert state["current"] == 0


# --- render smoke tests -------------------------------------------------


def _sample_report() -> LiveSampleReport:
    return LiveSampleReport(
        manifest=Manifest(
            report_version="v",
            code_revision="abc123",
            working_tree_dirty=False,
            generated_at=T0,
            timeframe="1d",
            preflight_manifest=_preflight_manifest(),
            sample_fingerprint="cafef00d",
            max_sample_instruments=MAX_SAMPLE_INSTRUMENTS,
            max_sample_window_days=MAX_SAMPLE_WINDOW_DAYS,
        ),
        candidates_considered=2,
        candidates_selected=1,
        candidates_excluded=1,
        exclusion_reasons=(ReadinessRow("sample_budget_exhausted", 1),),
        candidates=(_candidate(),),
        results=(),
        operational_warnings=(),
    )


def test_render_markdown_smoke() -> None:
    text = render_markdown(_sample_report())
    assert "Live OHLCV Sample" in text
    assert "abc123" in text
    assert "sample_budget_exhausted" in text
    assert "non_representative_deterministic_diagnostic_sample" in text
    assert "token_history_identity_preflight_report_v1" in text
    assert "combined123" in text


def test_render_markdown_shows_operational_warnings_when_present() -> None:
    report = LiveSampleReport(
        manifest=Manifest(
            report_version="v",
            code_revision="abc123",
            working_tree_dirty=False,
            generated_at=T0,
            timeframe="1d",
            preflight_manifest=_preflight_manifest(),
            sample_fingerprint="cafef00d",
            max_sample_instruments=MAX_SAMPLE_INSTRUMENTS,
            max_sample_window_days=MAX_SAMPLE_WINDOW_DAYS,
        ),
        candidates_considered=1,
        candidates_selected=1,
        candidates_excluded=0,
        exclusion_reasons=(),
        candidates=(_candidate(),),
        results=(),
        operational_warnings=("failed to close the binance exchange client cleanly: boom",),
    )
    text = render_markdown(report)
    assert "Operational warnings" in text
    assert "failed to close the binance exchange client cleanly" in text


def test_render_json_smoke() -> None:
    import json

    payload = json.loads(render_json(_sample_report()))
    assert payload["manifest"]["report_version"] == "v"
    assert payload["candidates_selected"] == 1
