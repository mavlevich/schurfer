from __future__ import annotations

from schurfer_analytics.liquidation_cascade_repository import (
    _BARS_FOR_SYMBOLS_SQL,
    _IDENTITY_STATUS_SQL,
    _MINUTE_STATES_SQL,
    _OUTCOME_PATH_SQL,
    _QUOTES_FOR_SYMBOLS_SQL,
    _RELEVANT_SNAPSHOT_TIMESTAMPS_SQL,
    _SYMBOLS_IN_WINDOW_SQL,
    LOOKBACK_MINUTES,
    OI_DROP_TRIGGER_PCT,
    PRICE_DROP_TRIGGER_PCT,
)


def test_reference_thresholds_match_the_runtime_scanner() -> None:
    # Must track apps/execution/schurfer_execution/liquidation_cascade.py's
    # own _SQL_SCANNER thresholds exactly.
    assert PRICE_DROP_TRIGGER_PCT == -0.05
    assert OI_DROP_TRIGGER_PCT == -0.15
    assert LOOKBACK_MINUTES == 15


def test_symbols_in_window_query_pins_capture_version() -> None:
    sql = str(_SYMBOLS_IN_WINDOW_SQL)
    assert "DISTINCT symbol" in sql
    assert "capture_version = :capture_version" in sql


def test_minute_states_query_covers_the_whole_window_ordered_for_streaming() -> None:
    sql = str(_MINUTE_STATES_SQL)
    assert "timeseries.bybit_momentum_bars_1m" in sql
    # Whole-window, not scoped to one symbol -- stream_minute_observations
    # groups the streamed rows by symbol in Python instead.
    assert "symbol = :symbol" not in sql
    assert "LAG(close_price, :lookback_minutes) OVER w" in sql
    assert "LAG(open_interest, :lookback_minutes) OVER w" in sql
    assert "PARTITION BY exchange, symbol ORDER BY bucket_start" in sql
    assert "lag_span_minutes" in sql
    assert "capture_version = :capture_version" in sql
    # ORDER BY symbol so the stream groups each symbol's rows contiguously.
    assert "ORDER BY exchange, symbol, bucket_start" in sql
    # Thresholding happens in Python (to_minute_states), not baked into SQL.
    assert "0.05" not in sql
    assert "0.15" not in sql


def test_outcome_path_query_pins_capture_version_and_exact_symbol() -> None:
    sql = str(_OUTCOME_PATH_SQL)
    assert "symbol = :symbol" in sql
    assert "capture_version = :capture_version" in sql
    assert "ORDER BY symbol, bucket_start" in sql


def test_bars_for_symbols_query_pins_capture_version_and_scopes_to_symbol_list() -> None:
    sql = str(_BARS_FOR_SYMBOLS_SQL)
    assert "capture_version = :capture_version" in sql
    assert "symbol = ANY(:symbols)" in sql
    assert "high_price" in sql
    assert "low_price" in sql


def test_quotes_for_symbols_query_pins_capture_version_and_scopes_to_symbol_list() -> None:
    sql = str(_QUOTES_FOR_SYMBOLS_SQL)
    assert "capture_version = :capture_version" in sql
    assert "symbol = ANY(:symbols)" in sql
    assert "last_bid_price" in sql
    assert "last_ask_price" in sql


def test_relevant_snapshot_timestamps_query_unions_baseline_and_in_window_changes() -> None:
    sql = str(_RELEVANT_SNAPSHOT_TIMESTAMPS_SQL)
    assert "MAX(captured_at)" in sql
    assert "captured_at <= :since" in sql
    assert "captured_at >= :since AND captured_at < :until" in sql
    assert "UNION" in sql


def test_identity_query_scopes_to_the_relevant_snapshot_set_not_a_bare_range() -> None:
    sql = str(_IDENTITY_STATUS_SQL)
    assert "app.momentum_universe_instruments" in sql
    assert "app.momentum_universe_snapshots" in sql
    assert "i.native_market_id = ANY(:symbols)" in sql
    assert "s.captured_at = ANY(:relevant_snapshot_timestamps)" in sql
    # The join key is still the shared (exchange, universe_version,
    # catalog_version) linkage between the two tables -- but the SELECTed
    # per-instrument fields used for stability comparison must be
    # identity_key/onboarded_at, not a second use of catalog_version as if
    # it were per-instrument (colleague review, 2026-08-21).
    assert "s.catalog_version = i.catalog_version" in sql
    assert "i.identity_key" in sql
    assert "i.onboarded_at" in sql
    assert "captured_at >= :since" not in sql
    assert "captured_at < :until" not in sql
