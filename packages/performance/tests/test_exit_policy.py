"""Behaviour of the shared exit policy, stated where replay can rely on it."""

from schurfer_performance.exit_policy import ExitEvaluation, evaluate_exit, exit_params


def _params(**over: float) -> dict[str, float]:
    # The sub-50% bracket: 8% initial stop, 8% activation, 12% trail tightening
    # to 8% after 90 minutes, 60-minute no-progress, 180-minute max hold.
    params = exit_params(30.0)
    params.update(over)
    return params


def _evaluate(
    *,
    current_price: float,
    elapsed_min: float = 1.0,
    best_price: float | None = None,
    side: str = "long",
    entry_price: float = 100.0,
    **over: float,
) -> ExitEvaluation:
    return evaluate_exit(
        side=side,
        entry_price=entry_price,
        current_price=current_price,
        elapsed_min=elapsed_min,
        best_price=best_price,
        params=_params(**over),
    )


def test_pump_size_selects_the_bracket() -> None:
    assert exit_params(30.0)["initial_sl_pct"] == 8.0
    assert exit_params(70.0)["initial_sl_pct"] == 10.0
    assert exit_params(200.0)["initial_sl_pct"] == 12.0
    assert exit_params(None) == exit_params(50.0)


def test_initial_stop_before_activation() -> None:
    assert _evaluate(current_price=93.0).reason is None
    reason = _evaluate(current_price=91.0).reason
    assert reason is not None
    assert reason.startswith("initial_sl")


def test_activation_records_the_best_price_without_closing() -> None:
    result = _evaluate(current_price=110.0)
    assert result.reason is None
    assert result.best_price == 110.0


def test_no_progress_closes_a_flat_trade_long_before_max_hold() -> None:
    """The no-progress exit is what actually ends most losing trades: it fires
    at 60 minutes while max_hold is 180, so a replay that models only max_hold
    holds positions three times too long."""
    assert _evaluate(current_price=100.0, elapsed_min=59.0).reason is None
    reason = _evaluate(current_price=100.0, elapsed_min=61.0).reason
    assert reason is not None
    assert reason.startswith("no_progress")


def test_no_progress_stops_applying_once_trailing_is_active() -> None:
    """Regression guard for the property replay depends on: after activation
    the position is governed by the trail and max_hold alone, so a trade still
    in profit stays open at two hours even though no_progress is 60 minutes.

    The initial stop also stops applying, but that is not separately
    observable: once trailing is active the best price is at least
    entry + activation_pct, so the trail always sits above the initial stop
    level and closes the position first.
    """
    result = _evaluate(current_price=105.0, elapsed_min=120.0, best_price=110.0)
    assert result.reason is None
    assert result.best_price is None


def test_trail_tightens_after_the_configured_age() -> None:
    # 12% trail below a 110 peak closes at 96.8; the tightened 8% closes at 101.2.
    assert _evaluate(current_price=99.0, elapsed_min=89.0, best_price=110.0).reason is None
    reason = _evaluate(current_price=99.0, elapsed_min=91.0, best_price=110.0).reason
    assert reason is not None
    assert reason.startswith("trailing_stop")


def test_max_hold_wins_over_everything_else() -> None:
    reason = _evaluate(current_price=130.0, elapsed_min=180.0, best_price=130.0).reason
    assert reason is not None
    assert reason.startswith("max_hold")


def test_short_side_mirrors_the_long_side() -> None:
    assert _evaluate(side="short", current_price=113.0).reason.startswith("initial_sl")
    assert _evaluate(side="short", current_price=90.0).best_price == 90.0
    reason = _evaluate(side="short", current_price=101.0, best_price=90.0).reason
    assert reason is not None
    assert reason.startswith("trailing_stop")


def test_take_profit_only_applies_when_configured() -> None:
    assert _evaluate(current_price=140.0, best_price=140.0).reason is None
    reason = _evaluate(current_price=140.0, best_price=140.0, take_profit_pct=35.0).reason
    assert reason is not None
    assert reason.startswith("take_profit")


def test_a_new_extreme_is_reported_even_on_a_closing_tick() -> None:
    """The caller persists best_price without inspecting reason, so the policy
    must report an advance regardless of what it decided."""
    result = _evaluate(current_price=120.0, elapsed_min=1.0, best_price=110.0)
    assert result.best_price == 120.0
