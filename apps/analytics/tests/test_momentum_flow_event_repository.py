from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_event_repository import (
    EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE,
    MeasurementEvent,
    _select_events,
    measurement_events_statement,
)

T0 = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


def _row(
    event_id: int,
    base: str,
    event_first_seen_at: datetime,
    exchange: str,
    unified_symbol: str | None,
    identity_conflict: bool,
    source_first_seen_at: datetime,
    *,
    identity_key: str | None = "binance:swap:ERAUSDT:v1",
    market_id: str | None = "ERAUSDT",
    market_type: str | None = "swap",
    base_asset: str | None = None,
    quote_asset: str | None = "USDT",
    settle_asset: str | None = "USDT",
    onboarded_at: datetime | None = None,
) -> tuple[
    int,
    str,
    datetime,
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    datetime | None,
    bool,
    datetime,
]:
    return (
        event_id,
        base,
        event_first_seen_at,
        exchange,
        identity_key,
        market_id,
        unified_symbol,
        market_type,
        base_asset or base,
        quote_asset,
        settle_asset,
        onboarded_at or event_first_seen_at - timedelta(days=30),
        identity_conflict,
        source_first_seen_at,
    )


def test_earliest_source_wins_not_alphabetical_exchange_name() -> None:
    # Regression for the colleague-review P1: "binance" < "gate"
    # alphabetically, but gate confirmed the event FIRST here -- the
    # earlier-confirming venue must win, not the alphabetically-earlier
    # exchange name.
    rows = [
        _row(1, "ERA", T0, "gate", "ERA/USDT:USDT", False, T0),
        _row(1, "ERA", T0, "binance", "ERA/USDT:USDT", False, T0 + timedelta(minutes=3)),
    ]
    cohort = _select_events(rows)
    assert cohort.events == (
        MeasurementEvent(
            pump_event_id=1,
            base="ERA",
            exchange="gate",
            identity_key="binance:swap:ERAUSDT:v1",
            market_id="ERAUSDT",
            unified_symbol="ERA/USDT:USDT",
            market_type="swap",
            onboarded_at=T0 - timedelta(days=30),
            trigger_at=T0,
        ),
    )
    assert cohort.exclusion_reasons == ()


def test_exchange_name_is_only_a_tiebreak_for_equal_first_seen_at() -> None:
    rows = [
        _row(1, "ERA", T0, "gate", "ERA/USDT:USDT", False, T0),
        _row(1, "ERA", T0, "binance", "ERA/USDT:USDT", False, T0),
    ]
    cohort = _select_events(rows)
    assert cohort.events[0].exchange == "binance"


def test_identity_conflicted_source_excluded_even_with_unified_symbol() -> None:
    rows = [_row(1, "ERA", T0, "gate", "ERA/USDT:USDT", True, T0)]
    cohort = _select_events(rows)
    assert cohort.events == ()
    assert cohort.exclusion_reasons == ((EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE, 1),)


def test_missing_unified_symbol_excluded_even_when_not_conflicted() -> None:
    # identity_conflict defaults to False even when identity resolution
    # never ran at all -- a resolved unified_symbol is the positive
    # confirmation signal, not just "no conflict flagged" by default.
    rows = [_row(1, "ERA", T0, "gate", None, False, T0)]
    cohort = _select_events(rows)
    assert cohort.events == ()
    assert cohort.exclusion_reasons == ((EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE, 1),)


@pytest.mark.parametrize(
    "overrides",
    [
        {"identity_key": None},
        {"market_id": None},
        {"market_type": "spot"},
        {"base_asset": "OTHER"},
        {"quote_asset": "USDC"},
        {"settle_asset": "USDC"},
        {"onboarded_at": T0 + timedelta(minutes=1)},
    ],
)
def test_exact_identity_contract_fails_closed(overrides: dict[str, object]) -> None:
    row = _row(
        1,
        "ERA",
        T0,
        "gate",
        "ERA/USDT:USDT",
        False,
        T0,
        **overrides,  # type: ignore[arg-type]
    )
    cohort = _select_events([row])
    assert cohort.events == ()
    assert cohort.exclusion_reasons == ((EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE, 1),)


def test_later_qualifying_source_cannot_replace_disqualified_first_source() -> None:
    rows = [
        _row(1, "ERA", T0, "gate", None, False, T0),  # missing symbol
        _row(1, "ERA", T0, "okx", "ERA/USDT:USDT", True, T0),  # conflicted
        _row(1, "ERA", T0, "binance", "ERA/USDT:USDT", False, T0 + timedelta(minutes=1)),
    ]
    cohort = _select_events(rows)
    assert cohort.events == ()
    assert cohort.exclusion_reasons == ((EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE, 1),)


def test_multiple_events_mixed_qualifying_and_excluded() -> None:
    rows = [
        _row(1, "ERA", T0, "gate", "ERA/USDT:USDT", False, T0),
        _row(2, "BEAT", T0, "okx", None, False, T0),  # excluded: no symbol
        _row(3, "ZK", T0, "bybit", "ZK/USDT:USDT", True, T0),  # excluded: conflict
    ]
    cohort = _select_events(rows)
    assert {e.pump_event_id for e in cohort.events} == {1}
    assert cohort.exclusion_reasons == ((EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE, 2),)


def test_no_rows_produces_empty_cohort_and_no_exclusions() -> None:
    cohort = _select_events([])
    assert cohort.events == ()
    assert cohort.exclusion_reasons == ()


def test_source_less_event_is_counted_in_the_identity_funnel() -> None:
    row = (
        1,
        "ERA",
        T0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    cohort = _select_events([row])
    assert cohort.events == ()
    assert cohort.exclusion_reasons == ((EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE, 1),)


def test_measurement_events_statement_compiles_with_and_without_since() -> None:
    until = T0 + timedelta(days=1)
    compiled_with_since = str(measurement_events_statement(since=T0, until=until))
    compiled_without_since = str(measurement_events_statement(since=None, until=until))
    assert "pump_events" in compiled_with_since
    assert "pump_event_sources" in compiled_with_since
    # Both the event's own first_seen_at AND the source's own first_seen_at
    # are bounded by `until` -- a source confirming long after the cutoff
    # must not be pulled in just because its parent event started earlier.
    assert compiled_with_since.count("first_seen_at") >= 2
    assert compiled_without_since.count("first_seen_at") >= 2
