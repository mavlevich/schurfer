from decimal import Decimal

from schurfer_execution.reconciliation import (
    ActionDirective,
    DiscrepancyType,
    ExchangePositionSnapshot,
    FullIdentity,
    OrderAttemptSnapshot,
    OwnedTradeSnapshot,
    RedisPositionSnapshot,
    classify_position_state,
)


def _identity() -> FullIdentity:
    return FullIdentity("bybit", "BTC/USDT:USDT", "BTCUSDT", "swap", "long")


def _attempt(*, status: str = "completed") -> OrderAttemptSnapshot:
    identity = _identity()
    return OrderAttemptSnapshot(
        identity=identity,
        attempt_id=7,
        base="BTC",
        client_order_id="client-7",
        order_id="order-7",
        status=status,
        requested_amount=Decimal("1"),
        filled_amount=Decimal("1"),
        trade_id=9,
    )


def _snapshots() -> (
    tuple[
        ExchangePositionSnapshot,
        OwnedTradeSnapshot,
        RedisPositionSnapshot,
    ]
):
    identity = _identity()
    return (
        ExchangePositionSnapshot(identity, Decimal("1.000000001"), Decimal("100"), {}),
        OwnedTradeSnapshot(identity, 9, 7, "BTC", Decimal("1"), Decimal("100")),
        RedisPositionSnapshot(identity, 9, "bybit", "BTC"),
    )


def test_exact_three_way_state_allows_entry() -> None:
    exchange, trade, redis = _snapshots()

    decision = classify_position_state(_identity(), exchange, trade, [_attempt()], redis)

    assert decision.discrepancy is DiscrepancyType.HEALTHY
    assert decision.directive is ActionDirective.ALLOW_ENTRY
    assert decision.allow_entry
    assert not decision.incident_required


def test_unresolved_attempt_fails_closed_even_when_position_matches() -> None:
    exchange, trade, redis = _snapshots()

    decision = classify_position_state(
        _identity(), exchange, trade, [_attempt(status="submission_unknown")], redis
    )

    assert decision.discrepancy is DiscrepancyType.UNRESOLVED_ORDER_ATTEMPT
    assert not decision.allow_entry
    assert decision.incident_required


def test_missing_redis_requires_manual_intervention_not_automatic_repair() -> None:
    exchange, trade, _redis = _snapshots()

    decision = classify_position_state(_identity(), exchange, trade, [_attempt()], None)

    assert decision.discrepancy is DiscrepancyType.MISSING_REDIS
    assert decision.directive is ActionDirective.MANUAL_INTERVENTION_REQUIRED
    assert decision.manual_required


def test_incomplete_identity_never_matches_by_symbol_guess() -> None:
    identity = FullIdentity("bybit", "BTC/USDT:USDT", "", "swap", "long")

    decision = classify_position_state(identity, None, None, [], None)

    assert decision.discrepancy is DiscrepancyType.INCOMPLETE_IDENTITY
    assert decision.manual_required


def test_exact_no_fill_attempt_is_a_safe_terminal_state() -> None:
    decision = classify_position_state(_identity(), None, None, [_attempt(status="no_fill")], None)

    assert decision.discrepancy is DiscrepancyType.HEALTHY
    assert decision.allow_entry


def _untracked_attempt(
    *, filled: Decimal | None, status: str = "completed", attempt_id: int = 7
) -> OrderAttemptSnapshot:
    return OrderAttemptSnapshot(
        identity=_identity(),
        attempt_id=attempt_id,
        base="BTC",
        client_order_id=f"client-{attempt_id}",
        order_id=f"order-{attempt_id}",
        status=status,
        requested_amount=Decimal("1"),
        filled_amount=filled,
        trade_id=None,
    )


def _exchange(contracts: str) -> ExchangePositionSnapshot:
    return ExchangePositionSnapshot(_identity(), Decimal(contracts), Decimal("100"), {})


def test_exact_attempt_matching_the_whole_position_is_missing_trade() -> None:
    decision = classify_position_state(
        _identity(), _exchange("1"), None, [_untracked_attempt(filled=Decimal("1"))], None
    )

    assert decision.discrepancy is DiscrepancyType.MISSING_TRADE_EXACT_ATTEMPT
    assert decision.manual_required


def test_exact_attempt_smaller_than_the_position_is_a_size_mismatch() -> None:
    decision = classify_position_state(
        _identity(), _exchange("10"), None, [_untracked_attempt(filled=Decimal("1"))], None
    )

    assert decision.discrepancy is DiscrepancyType.POSITION_SIZE_MISMATCH
    assert decision.manual_required
    assert not decision.allow_entry


def test_exact_attempt_with_unknown_filled_amount_is_a_size_mismatch() -> None:
    decision = classify_position_state(
        _identity(), _exchange("1"), None, [_untracked_attempt(filled=None)], None
    )

    assert decision.discrepancy is DiscrepancyType.POSITION_SIZE_MISMATCH


def test_no_position_attempts_do_not_count_toward_the_owned_quantity() -> None:
    attempts = [
        _untracked_attempt(filled=Decimal("1")),
        _untracked_attempt(filled=None, status="failed", attempt_id=8),
        _untracked_attempt(filled=Decimal("0"), status="no_fill", attempt_id=9),
    ]

    decision = classify_position_state(_identity(), _exchange("1"), None, attempts, None)

    assert decision.discrepancy is DiscrepancyType.MISSING_TRADE_EXACT_ATTEMPT
