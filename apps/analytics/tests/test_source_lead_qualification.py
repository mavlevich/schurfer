from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from schurfer_analytics.source_lead_capture import TargetObservation
from schurfer_analytics.source_lead_contract import IDENTITY_REGISTRY_V2_START
from schurfer_analytics.source_lead_identity_evidence import (
    ChainContractEvidence,
    EvidenceBundle,
    IdentityClass,
    RawFetch,
    compute_bundle_sha256,
    save_evidence_bundle,
)
from schurfer_analytics.source_lead_qualification import (
    EXPECTED_REGISTRY_FINGERPRINT,
    EXPECTED_REGISTRY_VERSION,
    IDENTITY_MATCH_METHOD_REGISTRY_LOOKUP_V2,
    CanonicalInstrumentLink,
    IdentityRegistry,
    load_identity_registry,
    parse_identity_registry,
    qualify_source_lead,
    verify_registry_against_evidence,
)

_AFTER_CUTOVER = IDENTITY_REGISTRY_V2_START + timedelta(hours=1)
_BEFORE_CUTOVER = IDENTITY_REGISTRY_V2_START - timedelta(hours=1)


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
        eligibility_reason="identity_verified",
        identity_verified=True,
        identity_match_method="registry_exact_v2",
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
        source_first_observed_at=_AFTER_CUTOVER,
        target_observations=(_target("binance", 1.0),),
        registry=_registry(),
    )

    assert result.status == "excluded"
    assert result.reason == "source_identity_unapproved"
    assert result.canonical_asset_id is None


def test_capture_before_registry_v2_activation_is_excluded_even_with_valid_identity() -> None:
    """A capture whose source_first_observed_at predates
    IDENTITY_REGISTRY_V2_START must never be treated as v2-qualified
    prospective evidence, even when its identity and targets would
    otherwise fully qualify -- identity was confirmed retroactively, not in
    real time (colleague review, 2026-08-28)."""
    result = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:ABC_USDT:1",
        source_first_observed_at=_BEFORE_CUTOVER,
        target_observations=(_target("binance", 1.0),),
        registry=_registry(),
    )

    assert result.status == "excluded"
    assert result.reason == "before_identity_registry_v2_activation"
    assert result.canonical_asset_id is None


def test_target_not_registry_confirmed_is_excluded_despite_matching_identity_key() -> None:
    """A target observation whose status is 'sampled' and whose instrument
    identity_key matches a registered link must still be excluded when the
    observation itself never carries registry-confirmed identity -- qualify
    _source_lead must check identity_verified/identity_match_method
    explicitly, not merely infer them from status=='sampled' (colleague
    review, 2026-08-28)."""
    unconfirmed = replace(
        _target("binance", 1.0),
        identity_verified=False,
        identity_match_method=IDENTITY_MATCH_METHOD_REGISTRY_LOOKUP_V2,
    )

    result = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:ABC_USDT:1",
        source_first_observed_at=_AFTER_CUTOVER,
        target_observations=(unconfirmed,),
        registry=_registry(),
    )

    assert result.status == "excluded"
    assert result.reason == "no_approved_executable_target"
    assert result.details["targets"][0]["registry_confirmed"] is False


def test_selector_uses_lowest_round_trip_impact_with_stable_tie_break() -> None:
    """Venue selection still runs the same lowest-round-trip-impact logic
    with a stable tie-break, but ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED=False
    means the result is recorded as excluded/route_evidence_not_yet_
    independent, with the selection preserved under details['would_select']
    rather than ever actually returning status='qualified' (colleague
    review, 2026-08-28, second round: the registry's evidence bundles
    vouch for asset identity, not the specific derivative markets)."""
    result = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:ABC_USDT:1",
        source_first_observed_at=_AFTER_CUTOVER,
        target_observations=(_target("bybit", 2.0), _target("binance", 1.0)),
        registry=_registry(),
    )

    assert result.status == "excluded"
    assert result.reason == "route_evidence_not_yet_independent"
    assert result.canonical_asset_id == "asset:abc"
    assert result.selected_target_exchange is None
    assert result.selected_round_trip_impact_bps is None
    assert result.details["would_select"] == {
        "target_exchange": "binance",
        "round_trip_impact_bps": 2.0,
    }

    tied = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:ABC_USDT:1",
        source_first_observed_at=_AFTER_CUTOVER,
        target_observations=(_target("bybit", 1.0), _target("binance", 1.0)),
        registry=_registry(),
    )
    assert tied.details["would_select"]["target_exchange"] == "binance"


def test_selector_requires_full_two_sided_notional() -> None:
    incomplete = _target("binance", 1.0)
    incomplete = replace(
        incomplete,
        liquidity={**incomplete.liquidity, "bid_filled_notional_usd": 49.0},
    )

    result = qualify_source_lead(
        source_exchange="gate",
        source_identity_key="gate:swap:ABC_USDT:1",
        source_first_observed_at=_AFTER_CUTOVER,
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


# --- evidence-backed registry verification (colleague review, 2026-08-28) ----
# load_identity_registry previously only checked evidence_sha256's *format*,
# never opened the bundle it names -- these exercise the fix directly,
# without needing the real packaged evidence directory.


def _sample_chain_evidence() -> ChainContractEvidence:
    return ChainContractEvidence(
        chain="bsc",
        chain_id=56,
        contract_address="0xaaaa",
        decimals=18,
        decimals_evidence=RawFetch(
            source="rpc:eth_call",
            endpoint="https://fake/",
            observed_at=datetime(2026, 8, 28, tzinfo=UTC),
            raw_sha256="a" * 64,
            wire_exact=True,
            payload={"result": "0x12"},
        ),
        block_number=100,
        block_hash="0x" + "a" * 64,
    )


def _sample_bundle(
    *, base: str, identity_class: IdentityClass = "exact_contract"
) -> EvidenceBundle:
    # Real payload shapes (not just {"ok": True}) so a "good" sample bundle
    # actually passes _validate_identity_class -- needed once
    # verify_registry_against_evidence started re-running that check
    # (colleague review, 2026-08-28).
    gate_evidence = RawFetch(
        source="gate:fetch_currencies",
        endpoint="https://fake/",
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
        raw_sha256="b" * 64,
        wire_exact=False,
        payload={"networks": {"BEP20": {"info": {"addr": "0xaaaa"}}}},
    )
    coingecko_evidence = RawFetch(
        source="test",
        endpoint="https://fake/",
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
        raw_sha256="b" * 64,
        wire_exact=True,
        payload={"platforms": {"binance-smart-chain": "0xaaaa"}},
    )
    target_catalog_evidence = RawFetch(
        source="binance:alpha_catalog_entry",
        endpoint="https://fake/",
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
        raw_sha256="b" * 64,
        wire_exact=False,
        payload={"contractAddress": "0xaaaa", "decimals": 18},
    )
    bundle = EvidenceBundle(
        evidence_version="source_lead_identity_evidence_v2",
        base=base,
        source_exchange="gate",
        target_exchange="binance",
        identity_class=identity_class,
        source_contract=_sample_chain_evidence(),
        target_contract=_sample_chain_evidence(),
        gate_evidence=gate_evidence,
        target_catalog_evidence=target_catalog_evidence,
        coingecko_evidence=coingecko_evidence,
        code_revision="abc123",
        working_tree_dirty=False,
        captured_at=datetime(2026, 8, 28, tzinfo=UTC),
        bundle_sha256="",
    )
    return replace(bundle, bundle_sha256=compute_bundle_sha256(bundle))


def _links_for(base: str, sha256: str) -> dict[tuple[str, str], CanonicalInstrumentLink]:
    evidence_url = f"https://example.com/{base.lower()}-gate-binance.json"
    gate = CanonicalInstrumentLink(
        canonical_asset_id=f"asset:{base.lower()}",
        exchange="gate",
        instrument_identity_key=f"gate:swap:{base}_USDT:1",
        evidence_url=evidence_url,
        evidence_sha256=sha256,
    )
    binance = CanonicalInstrumentLink(
        canonical_asset_id=f"asset:{base.lower()}",
        exchange="binance",
        instrument_identity_key=f"binance:swap:{base}USDT:1",
        evidence_url=evidence_url,
        evidence_sha256=sha256,
    )
    return {
        (gate.exchange, gate.instrument_identity_key): gate,
        (binance.exchange, binance.instrument_identity_key): binance,
    }


def test_registry_load_verifies_link_content_against_its_own_evidence_bundle(
    tmp_path: Path,
) -> None:
    bundle = _sample_bundle(base="ZED")
    save_evidence_bundle(bundle, tmp_path / "zed-gate-binance.json")

    verify_registry_against_evidence(_links_for("ZED", bundle.bundle_sha256), evidence_dir=tmp_path)


def test_registry_load_rejects_link_whose_sha256_does_not_match_bundle_content(
    tmp_path: Path,
) -> None:
    bundle = _sample_bundle(base="ZED")
    save_evidence_bundle(bundle, tmp_path / "zed-gate-binance.json")

    with pytest.raises(ValueError, match="does not match its evidence bundle"):
        verify_registry_against_evidence(_links_for("ZED", "f" * 64), evidence_dir=tmp_path)


def test_registry_load_rejects_link_backed_by_non_exact_contract_bundle(tmp_path: Path) -> None:
    bundle = _sample_bundle(base="ZED", identity_class="same_asset_multichain_candidate")
    save_evidence_bundle(bundle, tmp_path / "zed-gate-binance.json")

    with pytest.raises(ValueError, match="not exact_contract"):
        verify_registry_against_evidence(
            _links_for("ZED", bundle.bundle_sha256), evidence_dir=tmp_path
        )


def test_registry_load_rejects_link_with_no_matching_evidence_bundle_at_all(
    tmp_path: Path,
) -> None:
    bundle = _sample_bundle(base="ZED")
    save_evidence_bundle(bundle, tmp_path / "zed-gate-binance.json")

    with pytest.raises(ValueError, match="no matching evidence bundle"):
        verify_registry_against_evidence(
            _links_for("OTHER", bundle.bundle_sha256), evidence_dir=tmp_path
        )


def test_registry_load_rejects_link_whose_evidence_url_names_a_different_bundle(
    tmp_path: Path,
) -> None:
    """A copy-pasted link row whose evidence_url still points at a
    different asset's file must fail even when its evidence_sha256 was
    (mistakenly) updated to match the actual bundle being used."""
    bundle = _sample_bundle(base="ZED")
    save_evidence_bundle(bundle, tmp_path / "zed-gate-binance.json")
    links = _links_for("ZED", bundle.bundle_sha256)
    stale_url_link = replace(
        links[("gate", "gate:swap:ZED_USDT:1")],
        evidence_url="https://example.com/some-other-asset-gate-binance.json",
    )
    links[("gate", "gate:swap:ZED_USDT:1")] = stale_url_link

    with pytest.raises(ValueError, match="does not name its own evidence bundle"):
        verify_registry_against_evidence(links, evidence_dir=tmp_path)


def test_registry_load_rejects_bundle_that_never_actually_passed_semantic_validation(
    tmp_path: Path,
) -> None:
    """The sha256 check alone only proves a bundle's content matches its own
    claimed hash, not that the content was ever semantically valid. A
    hand-crafted bundle claiming exact_contract with source and target on
    different chains/addresses -- exactly the EDEN-class mistake
    _validate_identity_class exists to catch -- computes a perfectly
    consistent hash over its own (wrong) content."""
    bundle = _sample_bundle(base="ZED")
    assert bundle.target_contract is not None
    mismatched = replace(
        bundle,
        target_contract=replace(bundle.target_contract, contract_address="0xbbbb"),
    )
    mismatched = replace(mismatched, bundle_sha256=compute_bundle_sha256(mismatched))
    save_evidence_bundle(mismatched, tmp_path / "zed-gate-binance.json")

    with pytest.raises(ValueError, match="failed semantic revalidation"):
        verify_registry_against_evidence(
            _links_for("ZED", mismatched.bundle_sha256), evidence_dir=tmp_path
        )
