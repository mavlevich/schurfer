"""Synthetic-fixture and rendering tests for liquidation_maker_upper_bound_
report.py (research/liquidation-maker-upper-bound-v1).

`resolve_unified_symbol` is the one piece of this report that could, if
wrong, silently corrupt every episode's own economics by fetching the
wrong instrument's candles -- verified here against synthetic ccxt-shaped
`markets_by_id` fixtures (never a bare ticker reconstruction; see
AI_RULES.md and this function's own docstring), and separately verified
live against real Binance/Bybit markets during development (not re-run in
CI, which must not depend on live exchange reachability).

Colleague review, 2026-09-03: `DirectionReport`/`DirectionFunnel`/
`DirectionResult` became `ScopeReport`/`ScopeFunnel`/`ScopeResult` (one
independent verdict per (direction, exchange, coverage_kind), not per
direction alone -- see the report module's own docstring for why); new
`episode_is_matured` and `_candle_path_sha256` tests below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.liquidation_maker_upper_bound import (
    EXIT_BAR_TIMEFRAME_MS,
    MAX_POSITION_HOLD_MINUTES,
    CascadeEpisode,
)
from schurfer_analytics.liquidation_maker_upper_bound_report import (
    InstrumentBreakdownRow,
    LiquidationMakerUpperBoundReport,
    ReportManifest,
    ScopeFunnel,
    ScopeReport,
    ScopeResult,
    SensitivityCount,
    SlippageSensitivityPoint,
    WeekBreakdownRow,
    _candle_path_sha256,
    _median,
    build_parser,
    check_native_market_id_ambiguous,
    episode_is_matured,
    generate_report,
    render_json,
    render_markdown,
    resolve_unified_symbol,
)
from schurfer_analytics.ohlcv import Candle


class _FakeClient:
    def __init__(self, markets_by_id: dict[str, object]) -> None:
        self.markets_by_id = markets_by_id


def _market(
    *,
    symbol: str,
    swap: bool = True,
    linear: bool = True,
    quote: str = "USDT",
    settle: str = "USDT",
) -> dict[str, object]:
    return {"symbol": symbol, "swap": swap, "linear": linear, "quote": quote, "settle": settle}


def test_resolve_unified_symbol_matches_a_single_usdt_linear_swap() -> None:
    client = _FakeClient({"BTCUSDT": _market(symbol="BTC/USDT:USDT")})
    assert resolve_unified_symbol(client, "BTCUSDT") == "BTC/USDT:USDT"


def test_resolve_unified_symbol_handles_a_list_of_candidates() -> None:
    # ccxt's own markets_by_id returns a list when more than one market
    # type shares the same native id (e.g. spot + swap) -- only the swap
    # one qualifies here.
    client = _FakeClient(
        {
            "BTCUSDT": [
                _market(symbol="BTC/USDT", swap=False, linear=False),
                _market(symbol="BTC/USDT:USDT"),
            ]
        }
    )
    assert resolve_unified_symbol(client, "BTCUSDT") == "BTC/USDT:USDT"


def test_resolve_unified_symbol_rejects_non_usdt_quote_or_settle() -> None:
    client = _FakeClient({"BTCUSD": _market(symbol="BTC/USD:BTC", quote="USD", settle="BTC")})
    assert resolve_unified_symbol(client, "BTCUSD") is None


def test_resolve_unified_symbol_rejects_non_swap_and_non_linear() -> None:
    client = _FakeClient({"BTCUSDT": _market(symbol="BTC/USDT", swap=False)})
    assert resolve_unified_symbol(client, "BTCUSDT") is None
    client2 = _FakeClient({"BTCUSDT": _market(symbol="BTC/USDT:USDT", linear=False)})
    assert resolve_unified_symbol(client2, "BTCUSDT") is None


def test_resolve_unified_symbol_returns_none_for_an_unknown_id() -> None:
    client = _FakeClient({})
    assert resolve_unified_symbol(client, "NOTAREALSYMBOL") is None


def test_resolve_unified_symbol_fails_closed_on_ambiguous_matches() -> None:
    client = _FakeClient(
        {
            "BTCUSDT": [
                _market(symbol="BTC/USDT:USDT"),
                _market(symbol="BTC/USDT:USDT-25DEC26"),  # a second, distinct qualifying market
            ]
        }
    )
    with pytest.raises(ValueError, match="refusing to guess"):
        resolve_unified_symbol(client, "BTCUSDT")


def test_check_native_market_id_ambiguous_accepts_zero_or_one() -> None:
    check_native_market_id_ambiguous(0, "BTCUSDT")
    check_native_market_id_ambiguous(1, "BTCUSDT")


def test_check_native_market_id_ambiguous_rejects_more_than_one() -> None:
    with pytest.raises(ValueError, match="refusing to guess"):
        check_native_market_id_ambiguous(2, "BTCUSDT")


# --- episode_is_matured (colleague review, 2026-09-03) ----------------------

_EXCHANGE = "bybit"
_MARKET = "BTCUSDT"
_T0 = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


def _episode(*, first_trigger_at: datetime, last_trigger_at: datetime) -> CascadeEpisode:
    return CascadeEpisode(1, _EXCHANGE, _MARKET, "long", first_trigger_at, last_trigger_at)


def test_episode_is_matured_false_well_before_the_exit_boundary() -> None:
    episode = _episode(first_trigger_at=_T0, last_trigger_at=_T0)
    database_now = _T0 + timedelta(minutes=1)
    assert episode_is_matured(episode, database_now) is False


def test_episode_is_matured_false_exactly_when_the_bar_opens() -> None:
    episode = _episode(first_trigger_at=_T0, last_trigger_at=_T0)
    boundary = _T0 + timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
    assert episode_is_matured(episode, boundary) is False


def test_episode_is_matured_true_exactly_when_the_bar_closes() -> None:
    episode = _episode(first_trigger_at=_T0, last_trigger_at=_T0)
    boundary = _T0 + timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
    bar_close = boundary + timedelta(milliseconds=EXIT_BAR_TIMEFRAME_MS)
    assert episode_is_matured(episode, bar_close) is True


def test_episode_is_matured_uses_last_trigger_at_not_first() -> None:
    """A multi-minute trigger window: maturity must be computed from the
    LATEST possible entry (last_trigger_at), not the first -- otherwise an
    episode could be read as matured while its own extremum-selection
    window (which scans up to last_trigger_at) hasn't even finished yet."""
    episode = _episode(first_trigger_at=_T0, last_trigger_at=_T0 + timedelta(minutes=4))
    # Matured relative to first_trigger_at but not last_trigger_at.
    almost_matured = (
        _T0
        + timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
        + timedelta(milliseconds=EXIT_BAR_TIMEFRAME_MS)
    )
    assert episode_is_matured(episode, almost_matured) is False
    truly_matured = (
        _T0
        + timedelta(minutes=4 + MAX_POSITION_HOLD_MINUTES)
        + timedelta(milliseconds=EXIT_BAR_TIMEFRAME_MS)
    )
    assert episode_is_matured(episode, truly_matured) is True


# --- _candle_path_sha256 ----------------------------------------------------


def _bar(ts: datetime, close: float) -> Candle:
    return Candle(
        ts_ms=int(ts.timestamp() * 1000),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=None,
    )


def test_candle_path_sha256_is_deterministic_regardless_of_input_order() -> None:
    candles = [_bar(_T0, 100.0), _bar(_T0 + timedelta(minutes=1), 101.0)]
    reversed_candles = list(reversed(candles))
    assert _candle_path_sha256(candles) == _candle_path_sha256(reversed_candles)


def test_candle_path_sha256_changes_when_a_close_price_changes() -> None:
    candles = [_bar(_T0, 100.0)]
    mutated = [_bar(_T0, 100.01)]
    assert _candle_path_sha256(candles) != _candle_path_sha256(mutated)


def test_candle_path_sha256_is_a_real_hex_sha256() -> None:
    digest = _candle_path_sha256([_bar(_T0, 100.0)])
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex


# --- small pure helpers -----------------------------------------------------


def test_median_of_empty_is_none() -> None:
    assert _median([]) is None


def test_median_odd_and_even_counts() -> None:
    assert _median([1.0, 2.0, 3.0]) == 2.0
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5


# --- CLI / argparse ----------------------------------------------------


def test_build_parser_requires_since_and_until() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--no-working-tree-dirty"])


def test_build_parser_requires_working_tree_dirty_to_be_stated() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--since", "2026-08-01T00:00:00+00:00", "--until", "2026-09-01T00:00:00+00:00"]
        )


def test_build_parser_accepts_a_complete_invocation() -> None:
    args = build_parser().parse_args(
        [
            "--since",
            "2026-08-01T00:00:00+00:00",
            "--until",
            "2026-09-01T00:00:00+00:00",
            "--code-revision",
            "deadbeef",
            "--no-working-tree-dirty",
        ]
    )
    assert args.working_tree_dirty is False


async def test_generate_report_rejects_since_after_until() -> None:
    args = build_parser().parse_args(
        [
            "--since",
            "2026-09-01T00:00:00+00:00",
            "--until",
            "2026-08-01T00:00:00+00:00",
            "--code-revision",
            "deadbeef",
            "--no-working-tree-dirty",
        ]
    )
    with pytest.raises(ValueError, match="must be earlier"):
        await generate_report(args)


# --- rendering ------------------------------------------------------------


def _fake_scope(*, position_side: str, exchange: str, coverage_kind: str) -> ScopeReport:
    funnel = ScopeFunnel(
        trigger_minutes=10,
        episodes_primary_threshold=2,
        matured_episodes=2,
        resolved_episodes=1,
        unresolved_by_reason={"missing_exit_bar": 1},
        sensitivity_family=(
            SensitivityCount(100_000.0, 5),
            SensitivityCount(250_000.0, 2),
            SensitivityCount(500_000.0, 1),
        ),
    )
    result = ScopeResult(
        resolved_episodes=1,
        distinct_instrument_clusters=1,
        distinct_utc_weeks=1,
        max_single_instrument_share=1.0,
        max_single_week_share=1.0,
        median_net_return_pct=0.5,
        mean_net_return_pct=0.5,
        profit_factor=None,
        win_rate=1.0,
        median_mfe_pct=1.0,
        median_mae_pct=-0.2,
        max_sequential_drawdown_pct=0.3,
        ci_lower_bound_pct=None,
        ci_upper_bound_pct=None,
        verdict="insufficient_data",
        slippage_sensitivity=(
            SlippageSensitivityPoint(0.0, False, 1, 0.6, None, None),
            SlippageSensitivityPoint(15.0, True, 1, 0.5, None, None),
            SlippageSensitivityPoint(30.0, False, 1, 0.4, None, None),
        ),
        instrument_breakdown=(InstrumentBreakdownRow("BTCUSDT", 1, 0.5),),
        week_breakdown=(WeekBreakdownRow("2026-W31", 1, 0.5),),
    )
    return ScopeReport(
        position_side=position_side,
        exchange=exchange,
        coverage_kind=coverage_kind,
        funnel=funnel,
        result=result,
    )


def _fake_report() -> LiquidationMakerUpperBoundReport:
    since = datetime(2026, 8, 1, tzinfo=UTC)
    until = since + timedelta(days=30)
    generated_at = until
    scopes = (
        _fake_scope(position_side="long", exchange="bybit", coverage_kind="complete_stream"),
        _fake_scope(position_side="short", exchange="bybit", coverage_kind="complete_stream"),
        _fake_scope(
            position_side="long", exchange="binance", coverage_kind="latest_per_symbol_1000ms"
        ),
    )
    return LiquidationMakerUpperBoundReport(
        manifest=ReportManifest(
            report_version="liquidation_maker_upper_bound_report_v2",
            contract_version="liquidation_maker_upper_bound_v1",
            interpretation="post_hoc_oracle_upper_bound_discovery_only_no_trading_authorization",
            trigger_minute_query_version="liquidation_maker_upper_bound_trigger_minutes_v1",
            code_revision="deadbeef",
            working_tree_dirty=False,
            generated_at=generated_at,
            since=since,
            until=until,
            primary_cascade_notional_usd=250_000.0,
            input_fingerprint="a" * 64,
        ),
        scopes=scopes,
        caveats=("test caveat",),
    )


def test_render_markdown_includes_every_scope_and_verdict() -> None:
    output = render_markdown(_fake_report())
    assert output.count("## long-liquidation") == 2  # bybit and binance scopes
    assert output.count("## short-liquidation") == 1
    assert "bybit / complete_stream" in output
    assert "binance / latest_per_symbol_1000ms" in output
    assert "insufficient_data" in output
    assert "$250,000" in output
    assert "test caveat" in output
    # Slippage sensitivity table renders all three points.
    assert output.count("| 0 |") >= 1
    assert output.count("| 15 |") >= 1
    assert output.count("| 30 |") >= 1


def test_render_json_round_trips_the_manifest_and_every_scope() -> None:
    import json

    output = json.loads(render_json(_fake_report()))
    assert output["manifest"]["contract_version"] == "liquidation_maker_upper_bound_v1"
    assert len(output["scopes"]) == 3
    exchanges = {scope["exchange"] for scope in output["scopes"]}
    assert exchanges == {"bybit", "binance"}
    coverage_kinds = {scope["coverage_kind"] for scope in output["scopes"]}
    assert coverage_kinds == {"complete_stream", "latest_per_symbol_1000ms"}
    first_scope_result = output["scopes"][0]["result"]
    assert len(first_scope_result["slippage_sensitivity"]) == 3
    assert first_scope_result["max_sequential_drawdown_pct"] == pytest.approx(0.3)
