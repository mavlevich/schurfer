"""The historical cost reader never turns a quote into a return observation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics import preblind_book_cost_baseline as baseline


def _paper_row(**changes: Any) -> dict[str, Any]:
    entry = datetime(2026, 9, 28, 12, tzinfo=UTC)
    row = {
        "paper_version": "momentum_flow_paper_v1",
        "exchange": "bybit",
        "entry_status": "opened",
        "position_status": "closed",
        "entry_quote_observed_at": entry,
        "exit_quote_observed_at": entry + timedelta(minutes=240),
        "entry_exchange_event_at": entry,
        "exit_exchange_event_at": entry + timedelta(minutes=240),
        "requested_notional_usd": 50.0,
        "entry_filled_notional_usd": 50.0,
        "exit_filled_notional_usd": 50.0,
        "entry_spread_bps": 8.0,
        "entry_impact_bps": 12.0,
        "exit_impact_bps": 18.0,
    }
    row.update(changes)
    return row


def _source_row(**changes: Any) -> dict[str, Any]:
    row = {
        "capture_id": 17,
        "capture_version": "source_lead_prospective_capture_v1",
        "source_exchange": "gate",
        "target_id": 23,
        "target_exchange": "bybit",
        "target_status": "sampled",
        "observed_at": datetime(2026, 9, 28, 12, tzinfo=UTC),
        "requested_notional_usd": 50.0,
        "spread_bps": 10.0,
        "ask_impact_bps": 15.0,
        "bid_impact_bps": 20.0,
        "ask_filled_notional_usd": 50.0,
        "bid_filled_notional_usd": 50.0,
        "book_age_ms": 300,
    }
    row.update(changes)
    return row


@pytest.mark.asyncio
async def test_production_read_is_not_enabled() -> None:
    with pytest.raises(RuntimeError, match="not registered"):
        await baseline.load_registered_rows("postgresql://invalid/invalid")


def test_break_even_counts_impact_once_and_fees_on_both_sides() -> None:
    expected = ((1.0012 * 1.001) / (0.9982 * 0.999) - 1) * 10_000
    assert baseline.break_even_mid_move_bps(12, 18, 10) == pytest.approx(expected)
    assert baseline.break_even_mid_move_bps(0, 0, 0) == 0
    with pytest.raises(ValueError):
        baseline.break_even_mid_move_bps(0, 10_000, 0)


def test_populations_and_versions_never_pool_and_missingness_is_counted() -> None:
    paper = [
        _paper_row(),
        _paper_row(paper_version="momentum_flow_paper_v1_binance", exchange="binance"),
        _paper_row(exit_quote_observed_at=datetime(2026, 9, 29, tzinfo=UTC)),
        _paper_row(exit_filled_notional_usd=20.0),
    ]
    source = [
        _source_row(),
        _source_row(capture_id=18, target_id=None, target_status=None),
        _source_row(capture_id=19, target_status="fetch_failed"),
    ]
    report = baseline.summarize_cost_rows(paper, source, {"paper_updated_after_cutoff": 136})
    assert report["excluded_from_preblind_snapshot"] == {"paper_updated_after_cutoff": 136}
    assert report["counts"]["paper:momentum_flow_paper_v1:all"] == 3
    assert report["counts"]["paper:momentum_flow_paper_v1:exit_after_cutoff"] == 1
    assert report["counts"]["paper:momentum_flow_paper_v1:invalid_or_short_depth"] == 1
    assert report["counts"]["source:source_lead_prospective_capture_v1:captures"] == 3
    assert report["counts"]["source:source_lead_prospective_capture_v1:no_pre_cutoff_target"] == 1
    assert report["counts"]["source:source_lead_prospective_capture_v1:status:fetch_failed"] == 1
    assert len(report["groups"]) == 3
    assert all(group["entry_spread_bps"]["n"] == 1 for group in report["groups"])
    assert {group["population"] for group in report["groups"]} == {
        "paper",
        "source_immediate_cross",
    }
    assert "return" not in str(report).lower()
    assert "pnl" not in str(report).lower()


def test_timestamp_quality_and_single_observation_percentile() -> None:
    assert baseline.quote_age_quality(-500) == "fresh"
    assert baseline.quote_age_quality(2001) == "outside_2s"
    assert baseline.quote_age_quality(None) == "unknown"
    report = baseline.summarize_cost_rows([_paper_row()], [])
    group = report["groups"][0]
    assert group["quote_age_quality"] == "fresh"
    assert group["entry_spread_bps"]["p50"] == 8
    assert group["entry_spread_bps"]["p99"] == 8
    assert group["requested_notional"] == "$50"


def test_only_allowed_columns_are_in_the_queries() -> None:
    sql = str(baseline.PAPER_ROWS) + str(baseline.SOURCE_ROWS) + str(baseline.EXCLUDED_COUNTS)
    for forbidden in (
        "gross_return",
        "net_return",
        "pnl",
        "funding",
        "fees_usd",
        "trade_decisions",
        "momentum_flow_paper_outcomes",
        "ohlcv",
        "ticker",
    ):
        assert forbidden not in sql.lower()
    assert "t.observed_at < :end" in str(baseline.SOURCE_ROWS)
    assert "CASE WHEN p.exit_quote_observed_at < :end" in str(baseline.PAPER_ROWS)
    assert "p.updated_at >= :end" in str(baseline.EXCLUDED_COUNTS)
