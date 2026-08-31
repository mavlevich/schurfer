from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import ONE_MINUTE_MS, Candle
from schurfer_analytics.pump_recurrence_integrity_report import Regime
from schurfer_analytics.serial_pump_regimes import (
    decision_boundary_ms,
    recurrence_summary,
    resolve_horizon_outcome,
)

_T0 = datetime(2026, 8, 1, tzinfo=UTC)
_TF_MS = ONE_MINUTE_MS


def _regime(
    base: str = "JIMOTHY",
    *,
    first_seen_at: datetime = _T0,
    last_seen_at: datetime | None = None,
) -> Regime:
    return Regime(
        base=base,
        episode_ids=(1,),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at if last_seen_at is not None else first_seen_at,
        max_peak_pct=30.0,
    )


def _candles(
    start_ms: int,
    closes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> tuple[Candle, ...]:
    """One candle per minute starting at start_ms. opens/highs/lows default
    to the close itself (flat candle, no intra-bar move) unless given."""
    opens = opens or closes
    highs = highs or closes
    lows = lows or closes
    return tuple(
        Candle(
            ts_ms=start_ms + i * _TF_MS,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=1.0,
        )
        for i, (open_, close, high, low) in enumerate(zip(opens, closes, highs, lows, strict=True))
    )


def test_decision_boundary_ceils_first_seen_at_to_next_candle_never_floor() -> None:
    # first_seen_at lands mid-bar (30s past a minute boundary) -- the
    # decision boundary must be the NEXT full minute, never the one
    # already in progress at first_seen_at.
    regime = _regime(
        first_seen_at=_T0 + timedelta(seconds=30), last_seen_at=_T0 + timedelta(hours=3)
    )
    boundary = decision_boundary_ms(regime, timeframe_ms=_TF_MS)
    expected = int((_T0 + timedelta(minutes=1)).timestamp() * 1000)
    assert boundary == expected


def test_decision_boundary_on_exact_boundary_stays_put() -> None:
    regime = _regime(first_seen_at=_T0, last_seen_at=_T0 + timedelta(hours=1))
    boundary = decision_boundary_ms(regime, timeframe_ms=_TF_MS)
    assert boundary == int(_T0.timestamp() * 1000)


def test_decision_boundary_ignores_last_seen_at_entirely() -> None:
    # Regression guard (colleague review, 2026-09-01): decision_boundary_ms
    # must depend ONLY on first_seen_at -- a regime whose last_seen_at
    # keeps growing as more episodes merge in (still possible while the
    # regime is within one cooldown of "now") must never change its own
    # already-reported decision instant. Two regimes sharing first_seen_at
    # but differing wildly in last_seen_at must produce the identical
    # boundary.
    early_close = _regime(first_seen_at=_T0, last_seen_at=_T0 + timedelta(minutes=5))
    late_close = _regime(first_seen_at=_T0, last_seen_at=_T0 + timedelta(hours=20))
    assert decision_boundary_ms(early_close, timeframe_ms=_TF_MS) == decision_boundary_ms(
        late_close, timeframe_ms=_TF_MS
    )


def test_resolve_horizon_outcome_missing_decision_candle() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=(),
        btc_candles=_candles(boundary_ms, [50_000.0] * 16),
    )
    assert outcome.resolved is False
    assert outcome.unresolved_reason == "missing_decision_candle"
    assert outcome.forward_return_pct is None


def test_resolve_horizon_outcome_missing_btc_decision_candle() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, [1.0] * 16),
        btc_candles=(),
    )
    assert outcome.resolved is False
    assert outcome.unresolved_reason == "missing_btc_decision_candle"


def test_resolve_horizon_outcome_insufficient_candle_history() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    # Only 5 minutes of candles for a 15-minute horizon.
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, [1.0] * 5),
        btc_candles=_candles(boundary_ms, [50_000.0] * 16),
    )
    assert outcome.resolved is False
    assert outcome.unresolved_reason == "insufficient_candle_history"


def test_resolve_horizon_outcome_insufficient_btc_candle_history() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, [1.0] * 16),
        btc_candles=_candles(boundary_ms, [50_000.0] * 5),
    )
    assert outcome.resolved is False
    assert outcome.unresolved_reason == "insufficient_btc_candle_history"


def test_resolve_horizon_outcome_internal_candle_gap_is_unresolved() -> None:
    # Regression guard (colleague review, 2026-09-01): the tail reaching the
    # horizon end is not enough -- a candle missing from the middle of the
    # path must not silently resolve. 15 candles are needed for a 15m
    # horizon (indices 0-14); drop index 5 entirely (a genuine internal
    # gap) while keeping the first and last bar present.
    boundary_ms = int(_T0.timestamp() * 1000)
    full = list(_candles(boundary_ms, [1.0] * 15))
    with_gap = tuple(full[:5] + full[6:])
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=with_gap,
        btc_candles=_candles(boundary_ms, [50_000.0] * 15),
    )
    assert outcome.resolved is False
    assert outcome.unresolved_reason == "internal_candle_gap"


def test_resolve_horizon_outcome_leading_candle_gap_is_unresolved() -> None:
    # The series starts 2 minutes after boundary_ms (an exchange that only
    # started returning data late) but still reaches the horizon end --
    # the naive "does some candle exist at/after boundary_ms, does the tail
    # reach far enough" checks alone would both pass and silently use a
    # candle 2 minutes after boundary_ms as if it were the entry price AT
    # boundary_ms. Distinct from an internal gap: the series present is
    # itself contiguous, just starting late.
    boundary_ms = int(_T0.timestamp() * 1000)
    late_start = boundary_ms + 2 * _TF_MS
    candles = _candles(late_start, [1.0] * 13)  # covers [2m, 15m) only
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=candles,
        btc_candles=_candles(boundary_ms, [50_000.0] * 15),
    )
    assert outcome.resolved is False
    assert outcome.unresolved_reason == "leading_candle_gap"


def test_resolve_horizon_outcome_internal_btc_candle_gap_is_unresolved() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    full_btc = list(_candles(boundary_ms, [50_000.0] * 15))
    btc_with_gap = tuple(full_btc[:5] + full_btc[6:])
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, [1.0] * 15),
        btc_candles=btc_with_gap,
    )
    assert outcome.resolved is False
    assert outcome.unresolved_reason == "internal_btc_candle_gap"


def test_resolve_horizon_outcome_leading_btc_candle_gap_is_unresolved() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    late_start = boundary_ms + 2 * _TF_MS
    btc_candles = _candles(late_start, [50_000.0] * 13)
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, [1.0] * 15),
        btc_candles=btc_candles,
    )
    assert outcome.resolved is False
    assert outcome.unresolved_reason == "leading_btc_candle_gap"


def test_resolve_horizon_outcome_resolved_forward_return() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    # A 15-minute horizon is exactly 15 one-minute candles covering
    # [boundary, boundary+15m) -- closed_candles keeps only FULLY closed
    # bars, so the 15th (index 14, covering [14m, 15m)) is the last one
    # inside the horizon, not a 16th trailing candle. Entry is the OPEN of
    # the first (boundary) candle -- 1.0 -- flat through to a close of 1.10
    # on the final candle: a clean +10% forward return.
    closes = [1.0] * 14 + [1.10]
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, closes),
        btc_candles=_candles(boundary_ms, [50_000.0] * 15),  # flat BTC: 0% adjustment
    )
    assert outcome.resolved is True
    assert outcome.unresolved_reason is None
    assert outcome.forward_return_pct == pytest.approx(10.0)
    assert outcome.btc_adjusted_return_pct == pytest.approx(10.0)


def test_resolve_horizon_outcome_entry_uses_open_not_close() -> None:
    # Regression guard (colleague review, 2026-09-01): the boundary candle's
    # own OPEN is the entry price, not its close -- the close of that same
    # candle is only known a full timeframe after the decision boundary, a
    # look-ahead this module used to have. open=1.0, close=1.20 on the
    # boundary candle itself; entry must read 1.0, not 1.20.
    boundary_ms = int(_T0.timestamp() * 1000)
    opens = [1.0] + [1.20] * 14
    closes = [1.20] * 15
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, closes, opens=opens),
        btc_candles=_candles(boundary_ms, [50_000.0] * 15),
    )
    assert outcome.resolved is True
    # entry=1.0 (the boundary candle's own open), horizon close=1.20 ->
    # +20%, not the ~0% a close-based entry (1.20 -> 1.20) would report.
    assert outcome.forward_return_pct == pytest.approx(20.0)


def test_resolve_horizon_outcome_btc_adjustment_subtracts_market_move() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    closes = [1.0] * 14 + [1.10]  # target: +10% (see previous test's own comment)
    btc_closes = [50_000.0] * 14 + [51_000.0]  # BTC: +2%
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, closes),
        btc_candles=_candles(boundary_ms, btc_closes),
    )
    assert outcome.forward_return_pct == pytest.approx(10.0)
    assert outcome.btc_adjusted_return_pct == pytest.approx(8.0, abs=1e-6)


def test_resolve_horizon_outcome_mfe_mae_use_high_low_not_close() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    closes = [1.0] * 15
    # Candle index 5 spikes to a high of 1.5 (+50% MFE) and dips to a low
    # of 0.8 (-20% MAE) intra-bar, even though every close is flat at 1.0.
    highs = list(closes)
    lows = list(closes)
    highs[5] = 1.5
    lows[5] = 0.8
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, closes, highs=highs, lows=lows),
        btc_candles=_candles(boundary_ms, [50_000.0] * 15),
    )
    assert outcome.forward_return_pct == pytest.approx(0.0)
    assert outcome.mfe_pct == pytest.approx(50.0)
    assert outcome.mae_pct == pytest.approx(-20.0)
    assert outcome.time_to_peak_minutes == pytest.approx(5.0)


def test_resolve_horizon_outcome_retrace_magnitude() -> None:
    boundary_ms = int(_T0.timestamp() * 1000)
    closes = [1.0] * 15
    highs = list(closes)
    lows = list(closes)
    highs[5] = 2.0  # peak: +100% at minute 5
    # Horizon closes flat at 1.0 -- fully retraced from the peak.
    outcome = resolve_horizon_outcome(
        horizon_label="15m",
        horizon_minutes=15,
        boundary_ms=boundary_ms,
        timeframe_ms=_TF_MS,
        candles=_candles(boundary_ms, closes, highs=highs, lows=lows),
        btc_candles=_candles(boundary_ms, [50_000.0] * 15),
    )
    assert outcome.mfe_pct == pytest.approx(100.0)
    # retrace_magnitude_pct = mfe_pct - forward_return_pct: peak return was
    # +100%, horizon closed flat at +0% -- gave back all 100 points of it.
    # Deliberately a positive MAGNITUDE, not signed the way
    # app.pump_events.retrace_pct (last_pct - peak_pct, always <= 0) is.
    assert outcome.forward_return_pct == pytest.approx(0.0)
    assert outcome.retrace_magnitude_pct == pytest.approx(100.0)


def test_recurrence_summary_single_regime_has_no_next_gap() -> None:
    regimes = (_regime(first_seen_at=_T0),)
    summaries = recurrence_summary(regimes)
    assert len(summaries) == 1
    assert summaries[0].regime_index == 0
    assert summaries[0].regime_count_so_far == 1
    assert summaries[0].next_regime_gap_minutes is None


def test_recurrence_summary_computes_gap_between_regimes() -> None:
    first = _regime(first_seen_at=_T0, last_seen_at=_T0)
    second = _regime(first_seen_at=_T0 + timedelta(hours=5), last_seen_at=_T0 + timedelta(hours=6))
    summaries = recurrence_summary((first, second))
    assert len(summaries) == 2
    assert summaries[0].next_regime_gap_minutes == pytest.approx(5 * 60.0)
    assert summaries[0].regime_count_so_far == 1
    assert summaries[1].regime_count_so_far == 2
    assert summaries[1].next_regime_gap_minutes is None


def test_recurrence_summary_rejects_mixed_bases() -> None:
    regimes = (_regime(base="A", first_seen_at=_T0), _regime(base="B", first_seen_at=_T0))
    with pytest.raises(ValueError, match="single base"):
        recurrence_summary(regimes)


def test_recurrence_summary_rejects_unsorted_regimes() -> None:
    first = _regime(first_seen_at=_T0 + timedelta(hours=5), last_seen_at=_T0 + timedelta(hours=6))
    second = _regime(first_seen_at=_T0, last_seen_at=_T0 + timedelta(hours=1))
    with pytest.raises(ValueError, match="sorted"):
        recurrence_summary((first, second))


def test_recurrence_summary_empty_input() -> None:
    assert recurrence_summary(()) == ()
