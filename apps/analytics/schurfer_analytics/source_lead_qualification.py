"""Fail-closed source-lead identity qualification and venue selection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

QUALIFICATION_VERSION = "source_lead_qualified_capture_v1"
VENUE_SELECTOR_VERSION = "lowest_round_trip_impact_v1"
DEFAULT_REGISTRY_RESOURCE = "registry/source_lead_identity_registry_v1.json"
EXPECTED_REGISTRY_VERSION = "source_lead_identity_registry_v1"
EXPECTED_REGISTRY_FINGERPRINT = "31604214fa148d3f86562a212fdc935029c82a7a4959a7b5001b6bd5637ff7f8"


@dataclass(frozen=True)
class CanonicalInstrumentLink:
    canonical_asset_id: str
    exchange: str
    instrument_identity_key: str
    evidence_url: str
    evidence_sha256: str


@dataclass(frozen=True)
class IdentityRegistry:
    version: str
    fingerprint: str
    links_by_identity: dict[tuple[str, str], CanonicalInstrumentLink]


@dataclass(frozen=True)
class QualificationResult:
    status: str
    reason: str
    canonical_asset_id: str | None
    selected_target_exchange: str | None
    selected_round_trip_impact_bps: float | None
    requested_notional_usd: float
    details: dict[str, Any]


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"identity registry {field} must be a non-empty string")
    return value.strip()


def _registry_fingerprint(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def parse_identity_registry(
    payload: Any,
    *,
    expected_version: str | None = None,
    expected_fingerprint: str | None = None,
) -> IdentityRegistry:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("identity registry schema_version must be 1")
    version = _required_string(payload, "registry_version")
    fingerprint = _registry_fingerprint(payload)
    if expected_version is not None and version != expected_version:
        raise ValueError(f"identity registry version is {version!r}, expected {expected_version!r}")
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise ValueError(
            "identity registry content changed without a frozen contract fingerprint bump"
        )
    raw_links = payload.get("links")
    if not isinstance(raw_links, list):
        raise ValueError("identity registry links must be an array")

    links: dict[tuple[str, str], CanonicalInstrumentLink] = {}
    asset_exchanges: set[tuple[str, str]] = set()
    for raw in raw_links:
        if not isinstance(raw, dict):
            raise ValueError("identity registry link must be an object")
        link = CanonicalInstrumentLink(
            canonical_asset_id=_required_string(raw, "canonical_asset_id"),
            exchange=_required_string(raw, "exchange").lower(),
            instrument_identity_key=_required_string(raw, "instrument_identity_key"),
            evidence_url=_required_string(raw, "evidence_url"),
            evidence_sha256=_required_string(raw, "evidence_sha256").lower(),
        )
        if not link.evidence_url.startswith("https://"):
            raise ValueError("identity registry evidence_url must use https")
        if len(link.evidence_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in link.evidence_sha256
        ):
            raise ValueError("identity registry evidence_sha256 must be lowercase SHA-256")
        key = (link.exchange, link.instrument_identity_key)
        if key in links:
            raise ValueError(f"duplicate identity registry key: {key}")
        asset_exchange = (link.canonical_asset_id, link.exchange)
        if asset_exchange in asset_exchanges:
            raise ValueError(
                "identity registry permits only one instrument version per asset/exchange"
            )
        links[key] = link
        asset_exchanges.add(asset_exchange)
    return IdentityRegistry(
        version=version,
        fingerprint=fingerprint,
        links_by_identity=links,
    )


def load_identity_registry() -> IdentityRegistry:
    resource = files("schurfer_analytics").joinpath(DEFAULT_REGISTRY_RESOURCE)
    return parse_identity_registry(
        json.loads(resource.read_text(encoding="utf-8")),
        expected_version=EXPECTED_REGISTRY_VERSION,
        expected_fingerprint=EXPECTED_REGISTRY_FINGERPRINT,
    )


def _finite_nonnegative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def qualify_source_lead(
    *,
    source_exchange: str,
    source_identity_key: str | None,
    target_observations: tuple[Any, ...],
    registry: IdentityRegistry,
) -> QualificationResult:
    """Link exact reviewed identities, then select the cheapest executable venue."""
    requested_notional = (
        float(target_observations[0].requested_notional_usd) if target_observations else 0.0
    )
    source_link = registry.links_by_identity.get(
        (source_exchange.lower(), source_identity_key or "")
    )
    if source_link is None:
        return QualificationResult(
            status="excluded",
            reason="source_identity_unapproved",
            canonical_asset_id=None,
            selected_target_exchange=None,
            selected_round_trip_impact_bps=None,
            requested_notional_usd=requested_notional,
            details={"targets": []},
        )

    diagnostics: list[dict[str, Any]] = []
    eligible: list[tuple[float, str]] = []
    for observation in target_observations:
        instrument = observation.instrument if isinstance(observation.instrument, dict) else {}
        identity_key = instrument.get("identity_key")
        link = registry.links_by_identity.get(
            (str(observation.target_exchange).lower(), str(identity_key or ""))
        )
        diagnostic: dict[str, Any] = {
            "exchange": observation.target_exchange,
            "observation_status": observation.status,
            "identity_key": identity_key,
            "identity_approved": link is not None,
            "canonical_match": (
                link is not None and link.canonical_asset_id == source_link.canonical_asset_id
            ),
        }
        if observation.status != "sampled" or link is None:
            diagnostics.append(diagnostic)
            continue
        if link.canonical_asset_id != source_link.canonical_asset_id:
            diagnostic["reason"] = "canonical_asset_mismatch"
            diagnostics.append(diagnostic)
            continue

        liquidity = observation.liquidity if isinstance(observation.liquidity, dict) else {}
        bid_impact = _finite_nonnegative(liquidity.get("bid_impact_bps"))
        ask_impact = _finite_nonnegative(liquidity.get("ask_impact_bps"))
        bid_filled = _finite_nonnegative(liquidity.get("bid_filled_notional_usd"))
        ask_filled = _finite_nonnegative(liquidity.get("ask_filled_notional_usd"))
        if (
            bid_impact is None
            or ask_impact is None
            or bid_filled is None
            or ask_filled is None
            or bid_filled + 0.01 < requested_notional
            or ask_filled + 0.01 < requested_notional
        ):
            diagnostic["reason"] = "target_liquidity_incomplete"
            diagnostics.append(diagnostic)
            continue
        round_trip_impact = round(bid_impact + ask_impact, 4)
        diagnostic["round_trip_impact_bps"] = round_trip_impact
        diagnostics.append(diagnostic)
        eligible.append((round_trip_impact, str(observation.target_exchange)))

    if not eligible:
        return QualificationResult(
            status="excluded",
            reason="no_approved_executable_target",
            canonical_asset_id=source_link.canonical_asset_id,
            selected_target_exchange=None,
            selected_round_trip_impact_bps=None,
            requested_notional_usd=requested_notional,
            details={"targets": diagnostics},
        )

    selected_impact, selected_exchange = min(eligible, key=lambda item: (item[0], item[1]))
    return QualificationResult(
        status="qualified",
        reason="lowest_round_trip_impact",
        canonical_asset_id=source_link.canonical_asset_id,
        selected_target_exchange=selected_exchange,
        selected_round_trip_impact_bps=selected_impact,
        requested_notional_usd=requested_notional,
        details={"targets": diagnostics},
    )
