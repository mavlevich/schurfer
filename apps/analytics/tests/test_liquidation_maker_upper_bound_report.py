"""Synthetic-fixture and rendering tests for liquidation_maker_upper_bound_
report.py (research/liquidation-maker-upper-bound-v1).

`resolve_unified_symbol` is the one piece of this report that could, if
wrong, silently corrupt every episode's own economics by fetching the
wrong instrument's candles -- verified here against synthetic ccxt-shaped
`markets_by_id` fixtures (never a bare ticker reconstruction; see
AI_RULES.md and this function's own docstring), and separately verified
live against real Binance/Bybit markets during development (not re-run in
CI, which must not depend on live exchange reachability).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.liquidation_maker_upper_bound_report import (
    DirectionFunnel,
    DirectionReport,
    DirectionResult,
    LiquidationMakerUpperBoundReport,
    ReportManifest,
    SensitivityCount,
    _median,
    build_parser,
    check_native_market_id_ambiguous,
    generate_report,
    render_json,
    render_markdown,
    resolve_unified_symbol,
)


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


def _fake_report() -> LiquidationMakerUpperBoundReport:
    since = datetime(2026, 8, 1, tzinfo=UTC)
    until = since + timedelta(days=30)
    generated_at = until
    direction = DirectionReport(
        position_side="long",
        funnel=DirectionFunnel(
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
        ),
        result=DirectionResult(
            resolved_episodes=1,
            distinct_asset_clusters=1,
            distinct_utc_weeks=1,
            max_single_asset_share=1.0,
            max_single_week_share=1.0,
            median_net_return_pct=0.5,
            mean_net_return_pct=0.5,
            profit_factor=None,
            win_rate=1.0,
            median_mfe_pct=1.0,
            median_mae_pct=-0.2,
            ci_lower_bound_pct=None,
            ci_upper_bound_pct=None,
            verdict="insufficient_data",
        ),
    )
    short_direction = DirectionReport(
        position_side="short",
        funnel=direction.funnel,
        result=direction.result,
    )
    return LiquidationMakerUpperBoundReport(
        manifest=ReportManifest(
            report_version="liquidation_maker_upper_bound_report_v1",
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
        directions=(direction, short_direction),
        caveats=("test caveat",),
    )


def test_render_markdown_includes_both_directions_and_verdict() -> None:
    output = render_markdown(_fake_report())
    assert output.count("## long-liquidation") == 1
    assert output.count("## short-liquidation") == 1
    assert "insufficient_data" in output
    assert "$250,000" in output
    assert "test caveat" in output


def test_render_json_round_trips_the_manifest() -> None:
    import json

    output = json.loads(render_json(_fake_report()))
    assert output["manifest"]["contract_version"] == "liquidation_maker_upper_bound_v1"
    assert len(output["directions"]) == 2
