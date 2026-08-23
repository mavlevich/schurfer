from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_market_quality import (
    Capability,
    SeriesIdentity,
    WindowQualityEvidence,
    WindowQualityPolicy,
    WindowQualityReason,
    validate,
)

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(minutes=120)


def _policy(**overrides: object) -> WindowQualityPolicy:
    fields: dict[str, object] = {
        "cadence_seconds": 60,
        "required_bucket_count": 121,
        "max_bucket_lag_seconds": 180,
        "max_oi_age_seconds_by_exchange": (("binance", 180), ("bybit", 600)),
        "required_capabilities": (Capability.PRICE, Capability.TRADES, Capability.OPEN_INTEREST),
        "allowed_market_types": ("linear",),
        "allowed_capture_versions": frozenset({"v1"}),
    }
    fields.update(overrides)
    return WindowQualityPolicy(**fields)  # type: ignore[arg-type]


def _clean_evidence(**overrides: object) -> WindowQualityEvidence:
    """A window that satisfies every predicate the default policy checks --
    every single-defect test below mutates exactly one field off this."""
    fields: dict[str, object] = {
        "identity": SeriesIdentity(exchange="bybit", market_type="linear", symbol="BEATUSDT"),
        "window_start": WINDOW_START,
        "window_end": NOW,
        "raw_row_count": 121,
        "distinct_bucket_count": 121,
        "max_gap_seconds": 60.0,
        "latest_bucket_start": NOW - timedelta(seconds=60),
        "capture_versions": ("v1",),
        "universe_versions": ("uv1",),
        "price_complete_count": 121,
        "trades_complete_count": 121,
        "oi_complete_count": 121,
        "first_oi_event_at": WINDOW_START,
        "latest_oi_event_at": NOW - timedelta(seconds=90),
        "unbackfilled_gap_minutes_sum": 0,
        "has_future_timestamp": False,
        "has_invalid_price": False,
        "has_invalid_open_interest": False,
        "has_duplicate_bucket": False,
    }
    fields.update(overrides)
    return WindowQualityEvidence(**fields)  # type: ignore[arg-type]


def test_clean_window_qualifies_with_no_reasons() -> None:
    result = validate(_clean_evidence(), _policy(), evaluated_at=NOW)
    assert result.qualified is True
    assert result.reasons == ()


def test_evidence_rejects_distinct_bucket_count_exceeding_raw_row_count() -> None:
    with pytest.raises(ValueError, match="distinct_bucket_count"):
        _clean_evidence(raw_row_count=100, distinct_bucket_count=101)


def test_wrong_market_type_is_rejected() -> None:
    evidence = _clean_evidence(
        identity=SeriesIdentity(exchange="bybit", market_type="inverse", symbol="BEATUSDT")
    )
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.WRONG_MARKET_TYPE in result.reasons


def test_insufficient_rows_is_rejected() -> None:
    evidence = _clean_evidence(raw_row_count=100, distinct_bucket_count=100)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.INSUFFICIENT_ROWS in result.reasons


def test_duplicate_bucket_is_rejected() -> None:
    evidence = _clean_evidence(has_duplicate_bucket=True)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.DUPLICATE_BUCKET in result.reasons


def test_gap_from_max_gap_seconds_is_rejected() -> None:
    """121 rows can still span more than 120 real minutes if quality
    filtering dropped rows out of the middle -- max_gap_seconds catches
    that even when raw_row_count alone looks sufficient."""
    evidence = _clean_evidence(max_gap_seconds=130.0)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.GAP in result.reasons


def test_unbackfilled_gap_minutes_alone_is_rejected() -> None:
    evidence = _clean_evidence(unbackfilled_gap_minutes_sum=3)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.GAP in result.reasons


def test_gap_reason_is_not_duplicated_when_both_signals_fire() -> None:
    evidence = _clean_evidence(max_gap_seconds=130.0, unbackfilled_gap_minutes_sum=3)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert result.reasons.count(WindowQualityReason.GAP) == 1


def test_incomplete_price_is_rejected() -> None:
    evidence = _clean_evidence(price_complete_count=120)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.INCOMPLETE_PRICE in result.reasons


def test_incomplete_trades_is_rejected() -> None:
    evidence = _clean_evidence(trades_complete_count=120)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.INCOMPLETE_TRADES in result.reasons


def test_incomplete_oi_is_rejected() -> None:
    evidence = _clean_evidence(oi_complete_count=120)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.INCOMPLETE_OI in result.reasons


def test_stale_bucket_is_rejected() -> None:
    evidence = _clean_evidence(latest_bucket_start=NOW - timedelta(seconds=200))
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.STALE_BUCKET in result.reasons


def test_stale_oi_is_rejected_past_the_per_exchange_threshold() -> None:
    # bybit's threshold is 600s in the default policy.
    evidence = _clean_evidence(latest_oi_event_at=NOW - timedelta(seconds=601))
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.STALE_OI in result.reasons


def test_oi_freshness_uses_the_exchange_specific_threshold() -> None:
    """The same OI age must pass for bybit (600s limit) and fail for
    binance (180s limit) -- the whole point of a per-exchange table."""
    age = NOW - timedelta(seconds=400)
    bybit_evidence = _clean_evidence(latest_oi_event_at=age)
    binance_evidence = _clean_evidence(
        identity=SeriesIdentity(exchange="binance", market_type="linear", symbol="BEATUSDT"),
        latest_oi_event_at=age,
    )
    policy = _policy()
    assert (
        WindowQualityReason.STALE_OI
        not in validate(bybit_evidence, policy, evaluated_at=NOW).reasons
    )
    assert (
        WindowQualityReason.STALE_OI in validate(binance_evidence, policy, evaluated_at=NOW).reasons
    )


def test_stale_oi_is_rejected_fail_closed_when_exchange_has_no_configured_threshold() -> None:
    evidence = _clean_evidence(
        identity=SeriesIdentity(exchange="okx", market_type="linear", symbol="BEATUSDT")
    )
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.STALE_OI in result.reasons


def test_stale_oi_is_rejected_when_no_oi_event_seen_at_all() -> None:
    evidence = _clean_evidence(latest_oi_event_at=None)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.STALE_OI in result.reasons


def test_capture_version_not_in_allowlist_is_rejected_even_when_uniform() -> None:
    """A single, internally-consistent-but-unrecognized capture_version
    must be rejected -- uniqueness alone (count(DISTINCT)==1) is not the
    same guarantee as an explicit allowlist."""
    evidence = _clean_evidence(capture_versions=("v2",))
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.CAPTURE_VERSION_NOT_ALLOWED in result.reasons
    assert WindowQualityReason.MULTIPLE_CAPTURE_VERSIONS not in result.reasons


def test_multiple_capture_versions_is_rejected_even_when_all_individually_allowed() -> None:
    evidence = _clean_evidence(capture_versions=("v1", "v1_5"))
    result = validate(
        evidence, _policy(allowed_capture_versions=frozenset({"v1", "v1_5"})), evaluated_at=NOW
    )
    assert WindowQualityReason.MULTIPLE_CAPTURE_VERSIONS in result.reasons
    assert WindowQualityReason.CAPTURE_VERSION_NOT_ALLOWED not in result.reasons


def test_multiple_universe_versions_is_rejected() -> None:
    evidence = _clean_evidence(universe_versions=("uv1", "uv2"))
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.MULTIPLE_UNIVERSE_VERSIONS in result.reasons


def test_future_timestamp_flag_is_rejected() -> None:
    evidence = _clean_evidence(has_future_timestamp=True)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.FUTURE_TIMESTAMP in result.reasons


def test_invalid_price_flag_is_rejected() -> None:
    evidence = _clean_evidence(has_invalid_price=True)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.INVALID_PRICE in result.reasons


def test_invalid_open_interest_flag_is_rejected() -> None:
    evidence = _clean_evidence(has_invalid_open_interest=True)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.INVALID_OPEN_INTEREST in result.reasons


def test_multiple_simultaneous_reasons_are_all_reported() -> None:
    evidence = _clean_evidence(has_duplicate_bucket=True, has_invalid_price=True)
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert WindowQualityReason.DUPLICATE_BUCKET in result.reasons
    assert WindowQualityReason.INVALID_PRICE in result.reasons
    assert result.qualified is False


def test_result_evidence_round_trips_the_input_evidence() -> None:
    evidence = _clean_evidence()
    result = validate(evidence, _policy(), evaluated_at=NOW)
    assert result.evidence is evidence


def test_unsupported_capability_in_policy_is_never_silently_ignored() -> None:
    """A policy that only requires PRICE must not gate on OI/trades
    completeness at all -- confirms the per-capability loop is genuinely
    driven by required_capabilities, not hardcoded to check all three."""
    evidence = _clean_evidence(trades_complete_count=0, oi_complete_count=0)
    policy = _policy(required_capabilities=(Capability.PRICE,))
    result = validate(evidence, policy, evaluated_at=NOW)
    assert WindowQualityReason.INCOMPLETE_TRADES not in result.reasons
    assert WindowQualityReason.INCOMPLETE_OI not in result.reasons


def test_replace_is_usable_for_building_variants_in_tests() -> None:
    # Sanity check that WindowQualityEvidence stays a plain frozen
    # dataclass (dataclasses.replace works), not something with custom
    # __init__ semantics that would break this idiom for future tests.
    base = _clean_evidence()
    variant = replace(base, raw_row_count=50, distinct_bucket_count=50)
    assert variant.raw_row_count == 50
    assert base.raw_row_count == 121
