from __future__ import annotations

import json

import pytest
from schurfer_market_quality import Capability, SeriesIdentity, WindowQualityPolicy


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


def test_series_identity_rejects_blank_fields() -> None:
    with pytest.raises(ValueError, match="exchange"):
        SeriesIdentity(exchange="", market_type="linear", symbol="BTCUSDT")
    with pytest.raises(ValueError, match="symbol"):
        SeriesIdentity(exchange="bybit", market_type="linear", symbol="")


def test_policy_to_canonical_dict_is_deterministic_across_runs() -> None:
    policy = _policy()
    first = policy.to_canonical_dict()
    second = policy.to_canonical_dict()
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_policy_hash_changes_when_oi_age_threshold_changes() -> None:
    """Any future tightening of a threshold must change the policy's
    identity -- a strategy hashing this dict must never silently keep the
    same cohort under a changed contract."""
    a = _policy(max_oi_age_seconds_by_exchange=(("binance", 180), ("bybit", 600)))
    b = _policy(max_oi_age_seconds_by_exchange=(("binance", 180), ("bybit", 300)))
    assert a.to_canonical_dict() != b.to_canonical_dict()


def test_oi_age_limit_seconds_looks_up_by_exchange() -> None:
    policy = _policy()
    assert policy.oi_age_limit_seconds("bybit") == 600
    assert policy.oi_age_limit_seconds("binance") == 180


def test_oi_age_limit_seconds_returns_none_for_unconfigured_exchange() -> None:
    policy = _policy()
    assert policy.oi_age_limit_seconds("okx") is None


def test_policy_rejects_unsorted_oi_age_table() -> None:
    with pytest.raises(ValueError, match="pre-sorted"):
        _policy(max_oi_age_seconds_by_exchange=(("bybit", 600), ("binance", 180)))


def test_policy_rejects_duplicate_exchange_in_oi_age_table() -> None:
    with pytest.raises(ValueError, match="repeat"):
        _policy(max_oi_age_seconds_by_exchange=(("bybit", 600), ("bybit", 300)))


def test_policy_rejects_empty_required_capabilities() -> None:
    with pytest.raises(ValueError, match="required_capabilities"):
        _policy(required_capabilities=())


def test_policy_rejects_empty_allowed_capture_versions() -> None:
    with pytest.raises(ValueError, match="allowed_capture_versions"):
        _policy(allowed_capture_versions=frozenset())


def test_policy_rejects_non_positive_cadence() -> None:
    with pytest.raises(ValueError, match="cadence_seconds"):
        _policy(cadence_seconds=0)
