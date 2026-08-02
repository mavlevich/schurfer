from dataclasses import replace
from datetime import UTC, datetime

import pytest
from schurfer_analytics.source_lead_capture import TargetObservation
from schurfer_analytics.source_lead_qualification import (
    EXPECTED_REGISTRY_FINGERPRINT,
    EXPECTED_REGISTRY_VERSION,
    IdentityRegistry,
    load_identity_registry,
    parse_identity_registry,
    qualify_source_lead,
)


def _registry() -> IdentityRegistry:
    digest = "a" * 64
    return parse_identity_registry(
        {
            "schema_version": 1,
            "registry_version": "reviewed_v1",
            "links": [
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "gate",
                    "instrument_identity_key": "gate:swap:ABC_USDT:1",
                    "evidence_url": "https://example.com/gate-abc",
                    "evidence_sha256": digest,
                },
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "binance",
                    "instrument_identity_key": "binance:swap:ABCUSDT:1",
                    "evidence_url": "https://example.com/binance-abc",
                    "evidence_sha256": digest,
                },
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "bybit",
                    "instrument_identity_key": "bybit:swap:ABCUSDT:1",
                    "evidence_url": "https://example.com/bybit-abc",
                    "evidence_sha256": digest,
                },
            ],
        }
    )


def _target(exchange: str, impact: float) -> TargetObservation:
    return TargetObservation(
        target_exchange=exchange,
        status="sampled",
        eligibility_reason="identity_unverified",
        identity_verified=False,
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        occurred_at=None,
        latency_ms=10,
        requested_notional_usd=50.0,
        instrument={"identity_key": f"{exchange}:swap:ABCUSDT:1"},
        ticker={"last": 1.0},
        liquidity={
            "bid_impact_bps": impact,
            "ask_impact_bps": impact,
            "bid_filled_notional_usd": 50.0,
            "ask_filled_notional_usd": 50.0,
        },
        error=None,
    )


def test_registry_rejects_duplicate_asset_exchange_links() -> None:
    payload = {
        "schema_version": 1,
        "registry_version": "reviewed_v1",
        "links": [
            {
                "canonical_asset_id": "asset:abc",
                "exchange": "gate",
                "instrument_identity_key": key,
                "evidence_url": "https://example.com/evidence",
                "evidence_sha256": "a" * 64,
            }
            for key in ("first", "second")
        ],
    }

    with pytest.raises(ValueError, match="one instrument version"):
        parse_identity_registry(payload)


def test_packaged_registry_matches_frozen_contract() -> None:
    registry = load_identity_registry()

    assert registry.version == EXPECTED_REGISTRY_VERSION
    assert registry.fingerprint == EXPECTED_REGISTRY_FINGERPRINT


def test_registry_rejects_content_change_under_frozen_fingerprint() -> None:
    payload = {
        "schema_version": 1,
        "registry_version": EXPECTED_REGISTRY_VERSION,
        "links": [],
        "unexpected_change": True,
    }

    with pytest.raises(ValueError, match="content changed"):
        parse_identity_registry(
            payload,
            expected_version=EXPECTED_REGISTRY_VERSION,
            expected_fingerprint=EXPECTED_REGISTRY_FINGERPRINT,
        )


def test_unapproved_source_fails_closed_before_ticker_matching() -> None:
    result = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:UNKNOWN_USDT:1",
        target_observations=(_target("binance", 1.0),),
        registry=_registry(),
    )

    assert result.status == "excluded"
    assert result.reason == "source_identity_unapproved"
    assert result.canonical_asset_id is None


def test_selector_uses_lowest_round_trip_impact_with_stable_tie_break() -> None:
    result = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:ABC_USDT:1",
        target_observations=(_target("bybit", 2.0), _target("binance", 1.0)),
        registry=_registry(),
    )

    assert result.status == "qualified"
    assert result.canonical_asset_id == "asset:abc"
    assert result.selected_target_exchange == "binance"
    assert result.selected_round_trip_impact_bps == 2.0

    tied = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:ABC_USDT:1",
        target_observations=(_target("bybit", 1.0), _target("binance", 1.0)),
        registry=_registry(),
    )
    assert tied.selected_target_exchange == "binance"


def test_selector_requires_full_two_sided_notional() -> None:
    incomplete = _target("binance", 1.0)
    incomplete = replace(
        incomplete,
        liquidity={**incomplete.liquidity, "bid_filled_notional_usd": 49.0},
    )

    result = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:ABC_USDT:1",
        target_observations=(incomplete,),
        registry=_registry(),
    )

    assert result.status == "excluded"
    assert result.reason == "no_approved_executable_target"
    assert result.details["targets"][0]["reason"] == "target_liquidity_incomplete"


def test_registry_rejects_non_https_or_invalid_digest() -> None:
    with pytest.raises(ValueError, match="https"):
        parse_identity_registry(
            {
                "schema_version": 1,
                "registry_version": "reviewed_v1",
                "links": [
                    {
                        "canonical_asset_id": "asset:abc",
                        "exchange": "gate",
                        "instrument_identity_key": "gate:swap:ABC_USDT:1",
                        "evidence_url": "http://example.com",
                        "evidence_sha256": "a" * 64,
                    }
                ],
            }
        )
