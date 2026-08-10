from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import duckdb
import pytest
from schurfer_analytics.ohlcv import IncompleteFetchError
from schurfer_analytics.token_history_identity_preflight_report import (
    IdentityRecord,
    InstrumentSummary,
    TokenHistoryPreflightManifest,
    TokenHistoryPreflightReport,
)
from schurfer_analytics.token_history_ohlcv_sample_report import GapAnalysis
from schurfer_analytics.token_history_parquet_dataset import (
    CANONICAL_ALLOW_FALLBACK,
    CANONICAL_REQUIRED_HORIZONS,
    CANONICAL_RESOLVER_VERSION,
    CANONICAL_SINCE,
    CANONICAL_STRATEGY_VERSIONS,
    CANONICAL_UNTIL,
    FROZEN_VENUE_ALLOWLIST,
    VENUE_EXCLUSION_REASON,
    InstrumentFetchResult,
    ManifestVerificationError,
    NonCanonicalRunError,
    ScopedInstrument,
    _client_init_failed_result,
    _dataset_content_fingerprint,
    _fetch_and_write_instrument,
    _is_explained_partial_coverage,
    _write_and_verify_parquet,
    build_token_history_dataset_report,
    ensure_canonical_filters,
    instrument_hash,
    main,
    render_json,
    render_markdown,
    run_dataset_backfill,
    select_scoped_instruments,
    verify_dataset_on_disk,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from pathlib import Path

    from schurfer_analytics.token_history_parquet_dataset import TokenHistoryDatasetReport

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


def _scoped(
    *,
    exchange: str = "binance",
    identity_key: str = "key-1",
    unified_symbol: str = "ERA/USDT:USDT",
    base: str = "ERA",
    fetch_start_ms: int | None = None,
    fetch_end_ms: int | None = None,
) -> ScopedInstrument:
    start = fetch_start_ms if fetch_start_ms is not None else int(T0.timestamp() * 1000)
    end = fetch_end_ms if fetch_end_ms is not None else start + 5 * DAY_MS
    return ScopedInstrument(
        exchange=exchange,
        identity_key=identity_key,
        unified_symbol=unified_symbol,
        base=base,
        instrument_hash=instrument_hash(exchange, identity_key),
        decisions=(),
        fetch_start_ms=start,
        fetch_end_ms=end,
    )


def _fetch_result(
    *,
    exchange: str = "binance",
    identity_key: str = "key-1",
    canonical_row_hash: str | None = "row-hash-1",
    publishable: bool = True,
) -> InstrumentFetchResult:
    return InstrumentFetchResult(
        exchange=exchange,
        identity_key=identity_key,
        unified_symbol="ERA/USDT:USDT",
        base="ERA",
        instrument_hash=instrument_hash(exchange, identity_key),
        fetch_start_ms=int(T0.timestamp() * 1000),
        fetch_end_ms=int(T0.timestamp() * 1000) + 5 * DAY_MS,
        decisions=(),
        outcome="completed",
        error_type=None,
        error_detail=None,
        api_calls=1,
        successful_pages=1,
        empty_calls=0,
        timeout_calls=0,
        error_calls=0,
        filtered_or_deduplicated_rows_total=0,
        raw_page_size_stats=None,
        normalized_page_size_stats=None,
        latency_stats=None,
        gap_analysis=None,
        bar_count=5,
        parquet_relative_path="binance/x/bars.parquet" if publishable else None,
        parquet_sha256="deadbeef" if publishable else None,
        parquet_bytes=100 if publishable else None,
        canonical_row_hash=canonical_row_hash if publishable else None,
        publishable=publishable,
    )


# --- instrument_hash ---------------------------------------------------


def test_instrument_hash_is_stable_and_hex() -> None:
    first = instrument_hash("binance", "binance:swap:ERAUSDT:v1")
    second = instrument_hash("binance", "binance:swap:ERAUSDT:v1")
    assert first == second
    assert len(first) == 16
    int(first, 16)  # must be valid hex


def test_instrument_hash_differs_for_different_identity_keys() -> None:
    a = instrument_hash("binance", "binance:swap:ERAUSDT:v1")
    b = instrument_hash("binance", "binance:swap:OTHERUSDT:v1")
    assert a != b


def test_instrument_hash_differs_across_exchanges_for_the_same_key() -> None:
    a = instrument_hash("binance", "same-key")
    b = instrument_hash("bybit", "same-key")
    assert a != b


# --- ensure_canonical_filters --------------------------------------------


def _canonical_kwargs() -> dict[str, Any]:
    return {
        "since": CANONICAL_SINCE,
        "until": CANONICAL_UNTIL,
        "strategy_versions": CANONICAL_STRATEGY_VERSIONS,
        "resolver_version": CANONICAL_RESOLVER_VERSION,
        "required_horizons": CANONICAL_REQUIRED_HORIZONS,
        "allow_fallback": CANONICAL_ALLOW_FALLBACK,
    }


def test_canonical_constants_are_frozen_literals_not_shared_mutable_defaults() -> None:
    # Regression for a real bug: these constants must never again be aliases
    # of TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE / TOKEN_HISTORY_PREFLIGHT_
    # STRATEGY_VERSIONS / RESOLVER_VERSION / DEFAULT_REPLAY_HORIZONS. An
    # alias would mean an unrelated future change to those shared constants
    # silently changes both sides of ensure_canonical_filters' comparison at
    # once, defeating the check. Comparing against the literal values those
    # constants held on 2026-08-10 (the day dataset_version v1 was pinned).
    assert datetime(2026, 7, 26, tzinfo=UTC) == CANONICAL_SINCE
    assert CANONICAL_STRATEGY_VERSIONS == ("pump_short_v1_market_quality",)
    assert CANONICAL_RESOLVER_VERSION == "forward_v1"
    assert CANONICAL_REQUIRED_HORIZONS == (480,)


def test_ensure_canonical_filters_accepts_the_canonical_values() -> None:
    ensure_canonical_filters(**_canonical_kwargs())  # must not raise


def test_ensure_canonical_filters_rejects_a_different_since() -> None:
    kwargs = _canonical_kwargs()
    kwargs["since"] = CANONICAL_SINCE + timedelta(days=1)
    with pytest.raises(NonCanonicalRunError):
        ensure_canonical_filters(**kwargs)


def test_ensure_canonical_filters_rejects_a_different_until() -> None:
    kwargs = _canonical_kwargs()
    kwargs["until"] = CANONICAL_UNTIL + timedelta(seconds=1)
    with pytest.raises(NonCanonicalRunError):
        ensure_canonical_filters(**kwargs)


def test_ensure_canonical_filters_rejects_different_strategy_versions() -> None:
    kwargs = _canonical_kwargs()
    kwargs["strategy_versions"] = (*CANONICAL_STRATEGY_VERSIONS, "extra-version")
    with pytest.raises(NonCanonicalRunError):
        ensure_canonical_filters(**kwargs)


def test_ensure_canonical_filters_rejects_a_different_resolver_version() -> None:
    kwargs = _canonical_kwargs()
    kwargs["resolver_version"] = CANONICAL_RESOLVER_VERSION + "-other"
    with pytest.raises(NonCanonicalRunError):
        ensure_canonical_filters(**kwargs)


def test_ensure_canonical_filters_rejects_different_required_horizons() -> None:
    kwargs = _canonical_kwargs()
    kwargs["required_horizons"] = (*CANONICAL_REQUIRED_HORIZONS, 240)
    with pytest.raises(NonCanonicalRunError):
        ensure_canonical_filters(**kwargs)


def test_ensure_canonical_filters_rejects_allow_fallback_true() -> None:
    kwargs = _canonical_kwargs()
    kwargs["allow_fallback"] = not CANONICAL_ALLOW_FALLBACK
    with pytest.raises(NonCanonicalRunError):
        ensure_canonical_filters(**kwargs)


# --- select_scoped_instruments ------------------------------------------


def test_select_scoped_instruments_splits_allowlist_from_excluded() -> None:
    instruments = (
        _instrument(exchange="binance", identity_key="a"),
        _instrument(exchange="bybit", identity_key="b"),
        _instrument(exchange="xt", identity_key="c"),
        _instrument(exchange="mexc", identity_key="d"),
        _instrument(exchange="gate", identity_key="e"),
    )
    records = tuple(
        _record(pump_event_id=i, exchange=instrument.exchange, identity_key=instrument.identity_key)
        for i, instrument in enumerate(instruments, start=1)
    )
    included, excluded = select_scoped_instruments(instruments, records)
    assert {item.exchange for item in included} == set(FROZEN_VENUE_ALLOWLIST)
    assert {item.exchange for item in excluded} == {"mexc", "gate"}
    assert all(item.reason == VENUE_EXCLUSION_REASON for item in excluded)


def test_select_scoped_instruments_merges_windows_across_decisions() -> None:
    """Two decisions on the same instrument: fetch_start must be the
    earliest lookback any decision needs, fetch_end the latest decision_ts,
    not just one representative decision's own window."""
    instrument = _instrument(exchange="binance", identity_key="a", max_days=500)
    early_decision_ts = T0
    late_decision_ts = T0 + timedelta(days=40)
    onboarded_at = T0 - timedelta(days=500)
    records = (
        _record(
            pump_event_id=1,
            identity_key="a",
            decision_ts=early_decision_ts,
            onboarded_at=onboarded_at,
            available_history_days=500,
        ),
        _record(
            pump_event_id=2,
            identity_key="a",
            decision_ts=late_decision_ts,
            onboarded_at=onboarded_at,
            available_history_days=540,
        ),
    )
    included, excluded = select_scoped_instruments((instrument,), records)
    assert excluded == ()
    assert len(included) == 1
    scoped = included[0]
    assert len(scoped.decisions) == 2
    expected_start = max(onboarded_at, early_decision_ts - timedelta(days=365))
    assert scoped.fetch_start_ms == int(expected_start.timestamp() * 1000)
    assert scoped.fetch_end_ms == int(late_decision_ts.timestamp() * 1000)
    # Each decision's own window is preserved separately.
    windows_by_event = {window.pump_event_id: window for window in scoped.decisions}
    assert windows_by_event[2].window_end_ms == int(late_decision_ts.timestamp() * 1000)


def test_select_scoped_instruments_caps_lookback_at_365_days_per_decision() -> None:
    instrument = _instrument(exchange="binance", identity_key="a", max_days=2000)
    onboarded_at = T0 - timedelta(days=2000)
    records = (
        _record(
            pump_event_id=1,
            identity_key="a",
            decision_ts=T0,
            onboarded_at=onboarded_at,
            available_history_days=2000,
        ),
    )
    included, _excluded = select_scoped_instruments((instrument,), records)
    scoped = included[0]
    window = scoped.decisions[0]
    expected_start = T0 - timedelta(days=365)
    assert window.window_start_ms == int(expected_start.timestamp() * 1000)


def test_select_scoped_instruments_is_stable_under_input_permutation() -> None:
    instruments = (
        _instrument(exchange="binance", identity_key="a"),
        _instrument(exchange="bybit", identity_key="b"),
        _instrument(exchange="mexc", identity_key="c"),
    )
    records = (
        _record(pump_event_id=1, exchange="binance", identity_key="a"),
        _record(pump_event_id=2, exchange="bybit", identity_key="b"),
        _record(pump_event_id=3, exchange="mexc", identity_key="c"),
    )
    forward = select_scoped_instruments(instruments, records)
    backward = select_scoped_instruments(tuple(reversed(instruments)), tuple(reversed(records)))
    assert forward == backward


def test_select_scoped_instruments_raises_when_no_ready_record_matches() -> None:
    instrument = _instrument(exchange="binance", identity_key="a")
    with pytest.raises(ValueError, match="no matching identity_ready record"):
        select_scoped_instruments((instrument,), ())


# --- _is_explained_partial_coverage -------------------------------------


def test_leading_only_gap_is_explained() -> None:
    gap = GapAnalysis(
        expected_full_days=10,
        returned_days=3,
        leading_missing_days=7,
        internal_missing_dates=(),
        trailing_missing_days=0,
        possible_retention_or_listing_gap=True,
    )
    assert _is_explained_partial_coverage(gap) is True


def test_internal_gap_is_not_explained() -> None:
    gap = GapAnalysis(
        expected_full_days=10,
        returned_days=9,
        leading_missing_days=0,
        internal_missing_dates=("2026-01-05",),
        trailing_missing_days=0,
        possible_retention_or_listing_gap=False,
    )
    assert _is_explained_partial_coverage(gap) is False


def test_trailing_gap_is_not_explained() -> None:
    gap = GapAnalysis(
        expected_full_days=10,
        returned_days=8,
        leading_missing_days=0,
        internal_missing_dates=(),
        trailing_missing_days=2,
        possible_retention_or_listing_gap=False,
    )
    assert _is_explained_partial_coverage(gap) is False


# --- _write_and_verify_parquet (real DuckDB I/O, no mocking) -------------


def test_write_and_verify_parquet_round_trips_correctly(tmp_path: Path) -> None:
    from schurfer_analytics.ohlcv import Candle

    scoped = _scoped()
    candles = [
        Candle(
            ts_ms=scoped.fetch_start_ms + offset * DAY_MS,
            open=1,
            high=2,
            low=0.5,
            close=1.5,
            volume=10,
        )
        for offset in range(5)
    ]
    relative_path, file_sha256, file_bytes, row_hash = _write_and_verify_parquet(
        candles, scoped, tmp_path, ccxt_version="4.5.68", generated_at_ms=int(T0.timestamp() * 1000)
    )
    written_path = tmp_path / relative_path
    assert written_path.is_file()
    assert file_bytes > 0
    assert len(file_sha256) == 64
    assert len(row_hash) == 64
    # No leftover temp file after the atomic rename.
    assert not (written_path.parent / "bars.parquet.tmp").exists()

    connection = duckdb.connect(":memory:")
    try:
        read_sql = (
            "SELECT ts_ms, exchange, identity_key, schema_version FROM read_parquet("  # noqa: S608
            f"'{written_path.as_posix()}') ORDER BY ts_ms"
        )
        rows = connection.execute(read_sql).fetchall()
    finally:
        connection.close()
    assert len(rows) == 5
    assert rows[0][1] == scoped.exchange
    assert rows[0][2] == scoped.identity_key
    assert rows[0][3] == 1


def test_write_and_verify_parquet_is_deterministic_for_the_same_input(tmp_path: Path) -> None:
    from schurfer_analytics.ohlcv import Candle

    scoped = _scoped()
    candles = [
        Candle(
            ts_ms=scoped.fetch_start_ms + offset * DAY_MS,
            open=1,
            high=2,
            low=0.5,
            close=1.5,
            volume=10,
        )
        for offset in range(3)
    ]
    _, _, _, hash_one = _write_and_verify_parquet(
        candles, scoped, tmp_path / "run1", ccxt_version="4.5.68", generated_at_ms=0
    )
    _, _, _, hash_two = _write_and_verify_parquet(
        candles, scoped, tmp_path / "run2", ccxt_version="4.5.68", generated_at_ms=0
    )
    assert hash_one == hash_two


def test_write_and_verify_parquet_handles_an_apostrophe_in_the_output_path(
    tmp_path: Path,
) -> None:
    """Regression: an output root containing a single quote used to break
    the write/read-back SQL, which embedded the path as a string literal.
    Parameter binding (?) avoids that entirely."""
    from schurfer_analytics.ohlcv import Candle

    quirky_root = tmp_path / "o'brien"
    scoped = _scoped()
    candles = [
        Candle(
            ts_ms=scoped.fetch_start_ms + offset * DAY_MS,
            open=1,
            high=2,
            low=0.5,
            close=1.5,
            volume=10,
        )
        for offset in range(3)
    ]
    relative_path, _sha256, _bytes, _row_hash = _write_and_verify_parquet(
        candles, scoped, quirky_root, ccxt_version="4.5.68", generated_at_ms=0
    )
    assert (quirky_root / relative_path).is_file()


# --- _fetch_and_write_instrument (mocked exchange, real DuckDB write) ----


async def test_fetch_and_write_instrument_completed(tmp_path: Path) -> None:
    scoped = _scoped(fetch_end_ms=None)
    bars = [_daily_bar(scoped.fetch_start_ms + offset * DAY_MS) for offset in range(5)]
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=bars)

    result = await _fetch_and_write_instrument(
        exchange, scoped, tmp_path, ccxt_version="4.5.68", generated_at_ms=0
    )

    assert result.outcome == "completed"
    assert result.publishable is True
    assert result.parquet_relative_path is not None
    assert (tmp_path / result.parquet_relative_path).is_file()


async def test_fetch_and_write_instrument_leading_gap_is_publishable(tmp_path: Path) -> None:
    scoped = _scoped()
    bars = [_daily_bar(scoped.fetch_start_ms + offset * DAY_MS) for offset in range(2, 5)]
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=bars)

    result = await _fetch_and_write_instrument(
        exchange, scoped, tmp_path, ccxt_version="4.5.68", generated_at_ms=0
    )

    assert result.outcome == "partial_coverage"
    assert result.gap_analysis is not None
    assert result.gap_analysis.leading_missing_days > 0
    assert result.publishable is True


async def test_fetch_and_write_instrument_internal_gap_is_not_publishable(tmp_path: Path) -> None:
    scoped = _scoped()
    bars = [
        _daily_bar(scoped.fetch_start_ms + offset * DAY_MS) for offset in range(5) if offset != 2
    ]
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=bars)

    result = await _fetch_and_write_instrument(
        exchange, scoped, tmp_path, ccxt_version="4.5.68", generated_at_ms=0
    )

    assert result.outcome == "partial_coverage"
    assert result.gap_analysis is not None
    assert len(result.gap_analysis.internal_missing_dates) == 1
    assert result.publishable is False
    # The regression this guards against: a non-publishable result must not
    # leave a normal-looking bars.parquet behind in the standard tree, where
    # a later glob read could pick it up without checking the manifest.
    assert result.parquet_relative_path is None
    assert not any(tmp_path.rglob("*.parquet"))


async def test_fetch_and_write_instrument_no_data_does_not_crash_or_write(
    tmp_path: Path,
) -> None:
    """Zero bars returned with no exception (the no_data coverage outcome)
    used to reach _write_and_verify_parquet with an empty candle list,
    which DuckDB's executemany rejects outright. Classifying before writing
    means this path never attempts a write at all."""
    scoped = _scoped()
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])

    result = await _fetch_and_write_instrument(
        exchange, scoped, tmp_path, ccxt_version="4.5.68", generated_at_ms=0
    )

    assert result.outcome == "no_data"
    assert result.publishable is False
    assert result.parquet_relative_path is None
    assert not any(tmp_path.rglob("*.parquet"))


async def test_fetch_and_write_instrument_incomplete_fetch_error(tmp_path: Path) -> None:
    scoped = _scoped()
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(
        side_effect=IncompleteFetchError(
            symbol=scoped.unified_symbol,
            start_ms=0,
            end_ms=1,
            next_cursor_ms=0,
            successful_pages=0,
            api_calls=1,
        )
    )

    result = await _fetch_and_write_instrument(
        exchange, scoped, tmp_path, ccxt_version="4.5.68", generated_at_ms=0
    )

    assert result.outcome == "incomplete_fetch_error"
    assert result.publishable is False
    assert result.parquet_relative_path is None


async def test_fetch_and_write_instrument_generic_exception(tmp_path: Path) -> None:
    scoped = _scoped()
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=ValueError("exchange blew up"))

    result = await _fetch_and_write_instrument(
        exchange, scoped, tmp_path, ccxt_version="4.5.68", generated_at_ms=0
    )

    assert result.outcome == "fetch_exception"
    assert result.publishable is False


async def test_fetch_and_write_instrument_parquet_write_failure_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoped = _scoped()
    bars = [_daily_bar(scoped.fetch_start_ms + offset * DAY_MS) for offset in range(5)]
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=bars)

    def broken_writer(*_args: Any, **_kwargs: Any) -> tuple[str, str, int, str]:
        raise ValueError("disk exploded")

    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset._write_and_verify_parquet",
        broken_writer,
    )

    result = await _fetch_and_write_instrument(
        exchange, scoped, tmp_path, ccxt_version="4.5.68", generated_at_ms=0
    )

    assert result.outcome == "parquet_write_failed"
    assert result.publishable is False
    assert result.bar_count == 5  # the fetch itself succeeded


# --- run_dataset_backfill (mocked EXCHANGE_FACTORIES, real DuckDB write) -


def _fake_client(bars: list[list[float]] | None = None, *, fail: bool = False) -> AsyncMock:
    client = AsyncMock()
    if fail:
        client.fetch_ohlcv = AsyncMock(side_effect=ValueError("boom"))
    else:
        client.fetch_ohlcv = AsyncMock(return_value=bars or [])
    client.close = AsyncMock()
    return client


async def test_run_dataset_backfill_processes_exchanges_in_fixed_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_order: list[str] = []

    def make_factory(exchange: str) -> Callable[[], AsyncMock]:
        def factory() -> AsyncMock:
            call_order.append(exchange)
            return _fake_client([])

        return factory

    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {name: make_factory(name) for name in FROZEN_VENUE_ALLOWLIST},
    )
    scoped = (
        _scoped(exchange="xt", identity_key="c"),
        _scoped(exchange="binance", identity_key="a"),
        _scoped(exchange="bybit", identity_key="b"),
    )
    await run_dataset_backfill(scoped, tmp_path, generated_at_ms=0, ccxt_version="4.5.68")
    assert call_order == list(FROZEN_VENUE_ALLOWLIST)


async def test_run_dataset_backfill_keeps_one_exchange_client_alive_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"current": 0, "peak": 0}

    def make_factory() -> Callable[[], AsyncMock]:
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
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {name: make_factory() for name in FROZEN_VENUE_ALLOWLIST},
    )
    scoped = tuple(
        _scoped(exchange=exchange, identity_key=f"key-{exchange}")
        for exchange in FROZEN_VENUE_ALLOWLIST
    )
    await run_dataset_backfill(scoped, tmp_path, generated_at_ms=0, ccxt_version="4.5.68")
    assert state["peak"] == 1
    assert state["current"] == 0


async def test_run_dataset_backfill_isolates_factory_failure_to_its_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_factory() -> AsyncMock:
        raise RuntimeError("could not construct client")

    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {
            "binance": broken_factory,
            "bybit": lambda: _fake_client([]),
            "xt": lambda: _fake_client([]),
        },
    )
    scoped = (
        _scoped(exchange="binance", identity_key="a"),
        _scoped(exchange="bybit", identity_key="b"),
    )
    results, _warnings = await run_dataset_backfill(
        scoped, tmp_path, generated_at_ms=0, ccxt_version="4.5.68"
    )
    by_exchange = {result.exchange: result for result in results}
    assert by_exchange["binance"].outcome == "exchange_client_init_failed"
    assert by_exchange["bybit"].outcome != "exchange_client_init_failed"


async def test_run_dataset_backfill_records_close_failure_without_losing_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _fake_client([])
    client.close = AsyncMock(side_effect=RuntimeError("close blew up"))
    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {
            "binance": lambda: client,
            "bybit": lambda: _fake_client([]),
            "xt": lambda: _fake_client([]),
        },
    )
    scoped = (_scoped(exchange="binance", identity_key="a"),)
    results, warnings = await run_dataset_backfill(
        scoped, tmp_path, generated_at_ms=0, ccxt_version="4.5.68"
    )
    assert len(results) == 1
    assert len(warnings) == 1
    assert "binance" in warnings[0]


def test_client_init_failed_result_is_never_publishable() -> None:
    scoped = _scoped()
    result = _client_init_failed_result(scoped, RuntimeError("boom"))
    assert result.outcome == "exchange_client_init_failed"
    assert result.publishable is False
    assert result.parquet_relative_path is None


# --- build_token_history_dataset_report (integration, mocked exchanges) -


def _preflight_report(
    instruments: tuple[InstrumentSummary, ...], records: tuple[IdentityRecord, ...]
) -> TokenHistoryPreflightReport:
    manifest = TokenHistoryPreflightManifest(
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
    return TokenHistoryPreflightReport(
        manifest=manifest,
        eligible_episodes=1,
        excluded_episodes=0,
        input_exclusion_reasons=(),
        replay_eligible_baseline_decisions=len(records),
        readiness_distribution=(),
        readiness_by_exchange=(),
        unique_ready_instruments=len(instruments),
        history_window_distribution=(),
        median_available_history_days=200.0,
        instruments=instruments,
        records=records,
    )


async def test_build_token_history_dataset_report_ready_when_all_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instrument = _instrument(exchange="binance", identity_key="a", max_days=5)
    record = _record(
        pump_event_id=1, exchange="binance", identity_key="a", available_history_days=5
    )
    preflight = _preflight_report((instrument,), (record,))

    assert record.onboarded_at is not None
    start_ms = int(record.onboarded_at.timestamp() * 1000)
    bars = [_daily_bar(start_ms + offset * DAY_MS) for offset in range(5)]
    client = _fake_client(bars)
    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {"binance": lambda: client},
    )

    report, output_dir = await build_token_history_dataset_report(
        preflight, tmp_path, generated_at=T0, code_revision="abc123", working_tree_dirty=False
    )

    assert report.manifest.dataset_ready is True
    assert report.manifest.success_count == 1
    assert report.manifest.failure_count == 0
    assert report.manifest.timeframe == "1d"
    assert report.manifest.timeframe_ms == DAY_MS
    assert report.manifest.max_lookback_days == 365
    assert report.manifest.dataset_content_fingerprint != ""
    assert (output_dir / "manifest.json").is_file()
    assert not (output_dir / "manifest.json.tmp").exists()


async def test_build_token_history_dataset_report_not_ready_when_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good_instrument = _instrument(exchange="binance", identity_key="a", max_days=5)
    good_record = _record(
        pump_event_id=1, exchange="binance", identity_key="a", available_history_days=5
    )
    bad_instrument = _instrument(exchange="bybit", identity_key="b", max_days=5)
    bad_record = _record(
        pump_event_id=2, exchange="bybit", identity_key="b", available_history_days=5
    )
    preflight = _preflight_report((good_instrument, bad_instrument), (good_record, bad_record))

    assert good_record.onboarded_at is not None
    start_ms = int(good_record.onboarded_at.timestamp() * 1000)
    bars = [_daily_bar(start_ms + offset * DAY_MS) for offset in range(5)]
    good_client = _fake_client(bars)
    bad_client = _fake_client(fail=True)
    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {"binance": lambda: good_client, "bybit": lambda: bad_client},
    )

    report, _output_dir = await build_token_history_dataset_report(
        preflight, tmp_path, generated_at=T0, code_revision="abc123", working_tree_dirty=False
    )

    assert report.manifest.dataset_ready is False
    assert report.manifest.success_count == 1
    assert report.manifest.failure_count == 1
    assert report.manifest.excluded_by_venue == ()
    # The fingerprint only reflects publishable results, so the one failure
    # must not perturb it relative to a run containing only the success.
    only_good_report, _ = await build_token_history_dataset_report(
        _preflight_report((good_instrument,), (good_record,)),
        tmp_path,
        generated_at=T0,
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert (
        report.manifest.dataset_content_fingerprint
        == only_good_report.manifest.dataset_content_fingerprint
    )


async def test_build_token_history_dataset_report_records_excluded_venues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    included_instrument = _instrument(exchange="binance", identity_key="a", max_days=5)
    included_record = _record(
        pump_event_id=1, exchange="binance", identity_key="a", available_history_days=5
    )
    excluded_instrument = _instrument(exchange="mexc", identity_key="z", max_days=5)
    excluded_record = _record(
        pump_event_id=2, exchange="mexc", identity_key="z", available_history_days=5
    )
    preflight = _preflight_report(
        (included_instrument, excluded_instrument), (included_record, excluded_record)
    )

    assert included_record.onboarded_at is not None
    start_ms = int(included_record.onboarded_at.timestamp() * 1000)
    bars = [_daily_bar(start_ms + offset * DAY_MS) for offset in range(5)]
    client = _fake_client(bars)
    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {"binance": lambda: client},
    )

    report, _output_dir = await build_token_history_dataset_report(
        preflight, tmp_path, generated_at=T0, code_revision="abc123", working_tree_dirty=False
    )

    assert report.manifest.excluded_instrument_count == 1
    assert len(report.excluded_instruments) == 1
    assert report.excluded_instruments[0].exchange == "mexc"
    assert report.excluded_instruments[0].reason == VENUE_EXCLUSION_REASON


async def test_build_token_history_dataset_report_uses_a_fresh_run_id_each_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instrument = _instrument(exchange="binance", identity_key="a", max_days=5)
    record = _record(
        pump_event_id=1, exchange="binance", identity_key="a", available_history_days=5
    )
    preflight = _preflight_report((instrument,), (record,))
    assert record.onboarded_at is not None
    start_ms = int(record.onboarded_at.timestamp() * 1000)
    bars = [_daily_bar(start_ms + offset * DAY_MS) for offset in range(5)]
    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {"binance": lambda: _fake_client(bars)},
    )

    _report_one, dir_one = await build_token_history_dataset_report(
        preflight, tmp_path, generated_at=T0, code_revision="abc123", working_tree_dirty=False
    )
    _report_two, dir_two = await build_token_history_dataset_report(
        preflight, tmp_path, generated_at=T0, code_revision="abc123", working_tree_dirty=False
    )
    assert dir_one != dir_two
    assert dir_one.is_dir()
    assert dir_two.is_dir()


# --- _dataset_content_fingerprint -----------------------------------------


def test_dataset_content_fingerprint_is_order_independent() -> None:
    first = _fetch_result(identity_key="a", canonical_row_hash="hash-a")
    second = _fetch_result(identity_key="b", canonical_row_hash="hash-b")
    assert _dataset_content_fingerprint((first, second)) == _dataset_content_fingerprint(
        (second, first)
    )


def test_dataset_content_fingerprint_ignores_non_publishable_results() -> None:
    published = _fetch_result(identity_key="a", canonical_row_hash="hash-a")
    unpublished = _fetch_result(identity_key="b", publishable=False)
    assert _dataset_content_fingerprint((published,)) == _dataset_content_fingerprint(
        (published, unpublished)
    )


def test_dataset_content_fingerprint_changes_with_row_content() -> None:
    first = _fetch_result(identity_key="a", canonical_row_hash="hash-a")
    changed = _fetch_result(identity_key="a", canonical_row_hash="hash-a-different")
    assert _dataset_content_fingerprint((first,)) != _dataset_content_fingerprint((changed,))


def test_dataset_content_fingerprint_of_no_publishable_results_is_stable() -> None:
    unpublished = _fetch_result(identity_key="a", publishable=False)
    assert _dataset_content_fingerprint(()) == _dataset_content_fingerprint((unpublished,))


# --- verify_dataset_on_disk -------------------------------------------------


async def _built_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TokenHistoryDatasetReport, Path]:
    instrument = _instrument(exchange="binance", identity_key="a", max_days=5)
    record = _record(
        pump_event_id=1, exchange="binance", identity_key="a", available_history_days=5
    )
    preflight = _preflight_report((instrument,), (record,))
    assert record.onboarded_at is not None
    start_ms = int(record.onboarded_at.timestamp() * 1000)
    bars = [_daily_bar(start_ms + offset * DAY_MS) for offset in range(5)]
    monkeypatch.setattr(
        "schurfer_analytics.token_history_parquet_dataset.EXCHANGE_FACTORIES",
        {"binance": lambda: _fake_client(bars)},
    )
    return await build_token_history_dataset_report(
        preflight, tmp_path, generated_at=T0, code_revision="abc123", working_tree_dirty=False
    )


async def test_verify_dataset_on_disk_accepts_a_freshly_built_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, output_dir = await _built_report(tmp_path, monkeypatch)
    verify_dataset_on_disk(report, output_dir)  # must not raise


async def test_verify_dataset_on_disk_rejects_a_missing_parquet_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, output_dir = await _built_report(tmp_path, monkeypatch)
    published = next(r for r in report.results if r.publishable)
    assert published.parquet_relative_path is not None
    (output_dir / published.parquet_relative_path).unlink()
    with pytest.raises(ManifestVerificationError, match="does not exist"):
        verify_dataset_on_disk(report, output_dir)


async def test_verify_dataset_on_disk_rejects_a_corrupted_parquet_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, output_dir = await _built_report(tmp_path, monkeypatch)
    published = next(r for r in report.results if r.publishable)
    assert published.parquet_relative_path is not None
    (output_dir / published.parquet_relative_path).write_bytes(b"not actually parquet")
    with pytest.raises(ManifestVerificationError, match="sha256 mismatch"):
        verify_dataset_on_disk(report, output_dir)


async def test_verify_dataset_on_disk_rejects_an_unexpected_extra_parquet_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, output_dir = await _built_report(tmp_path, monkeypatch)
    stray = output_dir / "exchange=binance" / "instrument=stray" / "bars.parquet"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"stray")
    with pytest.raises(ManifestVerificationError, match="unexpected Parquet"):
        verify_dataset_on_disk(report, output_dir)


async def test_verify_dataset_on_disk_rejects_a_run_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, output_dir = await _built_report(tmp_path, monkeypatch)
    manifest_path = output_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["manifest"]["run_id"] = "not-the-real-run-id"
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(ManifestVerificationError, match="run_id"):
        verify_dataset_on_disk(report, output_dir)


async def test_verify_dataset_on_disk_rejects_a_missing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, output_dir = await _built_report(tmp_path, monkeypatch)
    (output_dir / "manifest.json").unlink()
    with pytest.raises(ManifestVerificationError, match="could not read back"):
        verify_dataset_on_disk(report, output_dir)


# --- main() CLI exit-code semantics -----------------------------------------


def test_main_prints_output_and_exits_0_when_dataset_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run(_args: argparse.Namespace) -> tuple[str, bool]:
        return "rendered-output", True

    monkeypatch.setattr("schurfer_analytics.token_history_parquet_dataset._run", fake_run)
    monkeypatch.setattr("sys.argv", ["token-history-parquet-dataset", "--no-working-tree-dirty"])
    main()
    assert capsys.readouterr().out == "rendered-output"


def test_main_prints_output_but_exits_2_when_dataset_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run(_args: argparse.Namespace) -> tuple[str, bool]:
        return "rendered-output", False

    monkeypatch.setattr("schurfer_analytics.token_history_parquet_dataset._run", fake_run)
    monkeypatch.setattr("sys.argv", ["token-history-parquet-dataset", "--no-working-tree-dirty"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    # Automation must still be able to see what was produced: the result is
    # printed before the process exits nonzero, not swallowed.
    assert capsys.readouterr().out == "rendered-output"


def test_main_exits_3_and_prints_nothing_on_manifest_verification_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run(_args: argparse.Namespace) -> tuple[str, bool]:
        raise ManifestVerificationError("disk does not match manifest")

    monkeypatch.setattr("schurfer_analytics.token_history_parquet_dataset._run", fake_run)
    monkeypatch.setattr("sys.argv", ["token-history-parquet-dataset", "--no-working-tree-dirty"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "disk does not match manifest" in captured.err


# --- render smoke tests ---------------------------------------------------


def test_render_json_and_markdown_smoke(tmp_path: Path) -> None:
    from schurfer_analytics.token_history_parquet_dataset import (
        TokenHistoryDatasetManifest,
        TokenHistoryDatasetReport,
        VenueCount,
    )

    manifest = TokenHistoryDatasetManifest(
        dataset_version="token_history_ohlcv_v1",
        schema_version=1,
        run_id="20260810T000000Z-deadbeef",
        code_revision="abc123",
        working_tree_dirty=False,
        generated_at=T0,
        duckdb_version="1.5.5",
        ccxt_version="4.5.68",
        preflight_manifest=_preflight_report((), ()).manifest,
        frozen_venue_allowlist=FROZEN_VENUE_ALLOWLIST,
        venue_exclusion_reason=VENUE_EXCLUSION_REASON,
        universe_instrument_count=2,
        included_by_venue=(VenueCount("binance", 1),),
        excluded_by_venue=(VenueCount("mexc", 1),),
        included_instrument_count=1,
        excluded_instrument_count=1,
        success_count=1,
        failure_count=0,
        dataset_ready=True,
        timeframe="1d",
        timeframe_ms=DAY_MS,
        max_lookback_days=365,
        dataset_content_fingerprint="deadbeef" * 8,
    )
    report = TokenHistoryDatasetReport(
        manifest=manifest,
        excluded_instruments=(),
        results=(),
        operational_warnings=(),
    )
    markdown = render_markdown(report, tmp_path)
    assert "Parquet Dataset" in markdown
    assert "dataset_ready = True" in markdown

    import json

    payload = json.loads(render_json(report))
    assert payload["manifest"]["run_id"] == "20260810T000000Z-deadbeef"
    assert payload["manifest"]["dataset_ready"] is True
