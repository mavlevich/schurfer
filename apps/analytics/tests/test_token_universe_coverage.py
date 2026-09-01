from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.token_universe_coverage import (
    AsOfCoverage,
    SeenInstrument,
    WindowCoverage,
    delisted,
    mark_currently_ready,
)

_T0 = datetime(2026, 8, 1, tzinfo=UTC)


def _seen(
    identity_key: str,
    *,
    exchange: str = "bybit",
    native_market_id: str | None = None,
    base: str | None = None,
    first_seen_ready_at: datetime = _T0,
    last_seen_ready_at: datetime = _T0,
) -> SeenInstrument:
    market_id = native_market_id or identity_key.split(":")[0]
    return SeenInstrument(
        exchange=exchange,
        identity_key=identity_key,
        native_market_id=market_id,
        base=base or market_id.removesuffix("USDT"),
        canonical_market_type="linear_usdt_perpetual",
        first_seen_ready_at=first_seen_ready_at,
        last_seen_ready_at=last_seen_ready_at,
    )


def test_mark_currently_ready_matches_by_identity_key_not_native_market_id() -> None:
    # Colleague review: a market id delisted and later relisted under the
    # SAME native_market_id gets a NEW identity_key (different onboarded_at)
    # -- matching by native_market_id alone would wrongly treat the two
    # lives as one and mark the old, genuinely-gone life "currently ready"
    # just because a new, unrelated listing reused its ticker.
    old_life = _seen("bybit:linear_usdt_perpetual:ABCUSDT:1000", native_market_id="ABCUSDT")
    new_life = _seen("bybit:linear_usdt_perpetual:ABCUSDT:2000", native_market_id="ABCUSDT")
    marked_old = mark_currently_ready(
        (old_life,), frozenset({"bybit:linear_usdt_perpetual:ABCUSDT:2000"})
    )
    marked_new = mark_currently_ready(
        (new_life,), frozenset({"bybit:linear_usdt_perpetual:ABCUSDT:2000"})
    )
    assert marked_old[0].currently_ready is False
    assert marked_new[0].currently_ready is True


def test_mark_currently_ready_sets_true_only_for_matching_keys() -> None:
    seen = (
        _seen("bybit:linear_usdt_perpetual:BTCUSDT:1"),
        _seen("bybit:linear_usdt_perpetual:DEADUSDT:1"),
    )
    marked = mark_currently_ready(seen, frozenset({"bybit:linear_usdt_perpetual:BTCUSDT:1"}))
    by_key = {entry.identity_key: entry for entry in marked}
    assert by_key["bybit:linear_usdt_perpetual:BTCUSDT:1"].currently_ready is True
    assert by_key["bybit:linear_usdt_perpetual:DEADUSDT:1"].currently_ready is False


def test_mark_currently_ready_does_not_mutate_input() -> None:
    original = (_seen("bybit:linear_usdt_perpetual:BTCUSDT:1"),)
    mark_currently_ready(original, frozenset({"bybit:linear_usdt_perpetual:BTCUSDT:1"}))
    assert original[0].currently_ready is None


def test_delisted_raises_on_unclassified_entries() -> None:
    # Colleague review, round 2: currently_ready defaults to None (not
    # classified yet), not False -- calling delisted() before
    # mark_currently_ready must fail loudly, not silently report every
    # entry as delisted the way a bool default did in an earlier version.
    seen = (_seen("bybit:linear_usdt_perpetual:BTCUSDT:1"),)
    with pytest.raises(ValueError, match="unclassified"):
        delisted(seen)


def test_delisted_is_empty_before_marking_is_unreachable_without_marking() -> None:
    # The honest version of the old (buggy) test name: delisted() genuinely
    # cannot be called meaningfully before mark_currently_ready -- it raises
    # instead of silently returning an empty or wrong answer.
    seen = (_seen("bybit:linear_usdt_perpetual:BTCUSDT:1"),)
    marked = mark_currently_ready(seen, frozenset({"bybit:linear_usdt_perpetual:BTCUSDT:1"}))
    assert delisted(marked) == ()


def test_delisted_returns_entries_not_in_currently_ready_set() -> None:
    seen = (
        _seen("bybit:linear_usdt_perpetual:BTCUSDT:1"),
        _seen("bybit:linear_usdt_perpetual:DEADUSDT:1"),
        _seen("bybit:linear_usdt_perpetual:ETHUSDT:1"),
    )
    marked = mark_currently_ready(
        seen,
        frozenset(
            {"bybit:linear_usdt_perpetual:BTCUSDT:1", "bybit:linear_usdt_perpetual:ETHUSDT:1"}
        ),
    )
    gone = delisted(marked)
    assert [entry.identity_key for entry in gone] == ["bybit:linear_usdt_perpetual:DEADUSDT:1"]
    assert gone[0].currently_ready is False


def test_as_of_coverage_no_snapshot_is_never_usable() -> None:
    coverage = AsOfCoverage(
        exchange="bybit",
        as_of=_T0,
        snapshot_captured_at=None,
        native_market_ids=frozenset(),
        identity_keys=frozenset(),
    )
    assert coverage.staleness is None
    assert coverage.is_usable(max_staleness=timedelta(days=365)) is False


def test_as_of_coverage_usable_within_tolerance() -> None:
    coverage = AsOfCoverage(
        exchange="bybit",
        as_of=_T0,
        snapshot_captured_at=_T0 - timedelta(hours=6),
        native_market_ids=frozenset({"BTCUSDT"}),
        identity_keys=frozenset({"bybit:linear_usdt_perpetual:BTCUSDT:1"}),
    )
    assert coverage.staleness == timedelta(hours=6)
    assert coverage.is_usable(max_staleness=timedelta(days=1)) is True


def test_as_of_coverage_stale_beyond_tolerance_is_not_usable() -> None:
    coverage = AsOfCoverage(
        exchange="bybit",
        as_of=_T0,
        snapshot_captured_at=_T0 - timedelta(days=10),
        native_market_ids=frozenset({"BTCUSDT"}),
        identity_keys=frozenset({"bybit:linear_usdt_perpetual:BTCUSDT:1"}),
    )
    assert coverage.is_usable(max_staleness=timedelta(days=1)) is False
    # Boundary is inclusive (<=), not strict.
    assert coverage.is_usable(max_staleness=timedelta(days=10)) is True


def test_window_coverage_has_reliable_coverage_matches_carry_in_flag() -> None:
    reliable = WindowCoverage(
        exchange="bybit",
        window_start=_T0,
        window_end=_T0 + timedelta(days=9),
        carry_in_snapshot_captured_at=_T0 - timedelta(hours=1),
        carry_in_within_tolerance=True,
        seen=(),
    )
    assert reliable.has_reliable_coverage is True

    no_carry_in = WindowCoverage(
        exchange="bybit",
        window_start=_T0,
        window_end=_T0 + timedelta(days=9),
        carry_in_snapshot_captured_at=None,
        carry_in_within_tolerance=False,
        seen=(),
    )
    assert no_carry_in.has_reliable_coverage is False

    stale_carry_in = WindowCoverage(
        exchange="bybit",
        window_start=_T0,
        window_end=_T0 + timedelta(days=9),
        carry_in_snapshot_captured_at=_T0 - timedelta(days=90),
        carry_in_within_tolerance=False,
        seen=(),
    )
    assert stale_carry_in.has_reliable_coverage is False
