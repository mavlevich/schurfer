"""Fail-closed source-lead identity qualification and venue selection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from .source_lead_contract import IDENTITY_REGISTRY_V2_START
from .source_lead_identity_evidence import (
    EVIDENCE_DIR,
    EvidenceIntegrityError,
    evidence_bundle_path,
    load_all_evidence_bundles,
    revalidate_bundle_identity_class,
)

if TYPE_CHECKING:
    from datetime import datetime

# v1 stays defined (never mutated -- its registry file, its DB rows, and
# migration 0022's CHECK CONSTRAINT pinning EXPECTED_REGISTRY_FINGERPRINT_V1
# are all frozen history) so any code that still needs to reference the old
# contract explicitly can, but nothing in this module uses these below
# QUALIFICATION_VERSION's v2 bump.
QUALIFICATION_VERSION_V1 = "source_lead_qualified_capture_v1"
REGISTRY_VERSION_V1 = "source_lead_identity_registry_v1"
REGISTRY_FINGERPRINT_V1 = "31604214fa148d3f86562a212fdc935029c82a7a4959a7b5001b6bd5637ff7f8"

# research/gate-source-lead-registry-activation-v2: 14 gate<->binance routes
# with real, evidenced exact_contract identity (see
# source_lead_identity_evidence.py and evidence/source_lead/v2/) -- the
# first non-empty identity registry this system has ever qualified against.
# Only captures at or after source_lead_contract.IDENTITY_REGISTRY_V2_START
# may be treated as prospective evidence under this version; see that
# constant's own docstring.
QUALIFICATION_VERSION = "source_lead_qualified_capture_v2"
VENUE_SELECTOR_VERSION = "lowest_round_trip_impact_v1"
DEFAULT_REGISTRY_RESOURCE = "registry/source_lead_identity_registry_v2.json"
EXPECTED_REGISTRY_VERSION = "source_lead_identity_registry_v2"
EXPECTED_REGISTRY_FINGERPRINT = "757fd1327593d07ca27efe17a031ae0eab95bf6998aecc1ec26f0df38667dca0"

# Registry v2's evidence bundles (verify_registry_against_evidence) vouch
# for *asset* identity only -- an on-chain contract match across Gate,
# Binance Alpha, and CoinGecko. Nothing evidences the specific derivative
# markets themselves (native market id, market type, quote/settle asset,
# onboard time): no exchange's futures/perpetual catalog is captured today.
# _resolve_registered_target_market's live re-verification at capture time
# only proves a registered instrument_identity_key genuinely *exists* on
# the exchange, not that it names the *right* project's perpetual rather
# than a different, ticker-colliding one sharing a symbol (colleague
# review, 2026-08-28, second round -- corrected from an earlier, weaker
# framing of this same gap). Until independent route evidence exists,
# qualify_source_lead below computes and records everything it would have
# selected, but never actually returns status='qualified' -- see its
# route_evidence_not_yet_independent branch. Flip this once real
# derivative-market evidence backs the registry.
ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED = False

# Shared vocabulary for TargetObservation.identity_match_method -- defined
# here (not in source_lead_capture.py, which imports these) because
# qualify_source_lead needs REGISTRY_EXACT_V2 to enforce its own fail-closed
# check below. base_symbol_v1 (the pre-registry naive f"{base}/USDT:USDT"
# lookup) stays a valid historical value on old rows but is never written by
# any current code path.
IDENTITY_MATCH_METHOD_BASE_SYMBOL_V1 = "base_symbol_v1"
# The registry resolved a link for this (canonical_asset_id, exchange), but
# no live market matched it exactly (or no link/source identity existed at
# all) -- resolution never completed, so identity was never confirmed.
IDENTITY_MATCH_METHOD_REGISTRY_LOOKUP_V2 = "registry_lookup_v2"
# _resolve_registered_target_market found a live market whose recomputed
# identity_key exactly matches the registered link -- identity is confirmed
# from this point on, even if a later eligibility check or network fetch
# then fails.
IDENTITY_MATCH_METHOD_REGISTRY_EXACT_V2 = "registry_exact_v2"


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
    # (canonical_asset_id, exchange) -> the one link registered for that
    # asset on that exchange -- parse_identity_registry's own uniqueness
    # check (at most one instrument version per asset/exchange) is what
    # makes this a safe 1:1 index, not just a convenience view of
    # links_by_identity. Added for source_lead_capture.py's
    # registry-first target resolution (colleague review, 2026-08-28):
    # given a source link's canonical_asset_id, find the one target link
    # for a given exchange directly, instead of guessing a market symbol
    # from the base ticker and only checking the registry afterward.
    links_by_asset_exchange: dict[tuple[str, str], CanonicalInstrumentLink]


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
    links_by_asset_exchange = {
        (link.canonical_asset_id, link.exchange): link for link in links.values()
    }
    return IdentityRegistry(
        version=version,
        fingerprint=fingerprint,
        links_by_identity=links,
        links_by_asset_exchange=links_by_asset_exchange,
    )


def _asset_base(canonical_asset_id: str) -> str:
    prefix = "asset:"
    if not canonical_asset_id.startswith(prefix):
        raise ValueError(
            f"identity registry canonical_asset_id must start with {prefix!r}: "
            f"{canonical_asset_id!r}"
        )
    return canonical_asset_id[len(prefix) :].upper()


def verify_registry_against_evidence(
    links: dict[tuple[str, str], CanonicalInstrumentLink],
    *,
    evidence_dir: Any = None,
) -> None:
    """Fail closed unless every registry link is actually backed by its own
    evidence bundle's *content*, not merely a well-formed sha256 string.

    A colleague review (2026-08-28, on this same registry-activation PR)
    found that load_identity_registry only checked evidence_sha256's format
    -- never opened the bundle it names, never confirmed the bundle's own
    content hashes to that sha256, never confirmed the bundle actually
    vouches for this asset/exchange pairing. That gap is exactly what
    load_evidence_bundle's own docstring already warned this step would need
    ("any future registry-activation change... must call this, not read the
    JSON file directly") -- this closes it. A hand-edited or copy-pasted
    canonical_asset_id/exchange pairing that doesn't match what was actually
    evidenced now fails registry load outright, instead of silently
    inheriting a fingerprint pinned over its own (possibly wrong) content.

    Deliberately narrower than full route verification: this confirms the
    *asset* identity a bundle vouches for (on-chain contract match across
    Gate/Binance/CoinGecko, identity_class == exact_contract), not that the
    specific registered derivative market (native id/type/quote-settle/
    onboard time) is really the same project's perpetual, rather than a
    different, ticker-colliding one that happens to share a symbol.
    _resolve_registered_target_market's live re-verification at capture
    time only proves the registered market genuinely *exists* with that
    exact id/type/onboarded_at combination -- a wrong-but-real
    instrument_identity_key would still resolve and be marked
    identity_verified=true (colleague review, 2026-08-28: this is a
    materially weaker guarantee than "can never misroute", corrected from
    this function's earlier framing). Independent evidence for the
    derivative markets themselves is tracked as separate follow-up work,
    not yet built."""
    try:
        bundles = load_all_evidence_bundles(evidence_dir)
    except EvidenceIntegrityError as exc:
        raise ValueError(f"identity registry evidence verification failed: {exc}") from exc

    bundle_by_base_exchange = {}
    for evidence_bundle in bundles:
        base = evidence_bundle.base.upper()
        bundle_by_base_exchange[(base, evidence_bundle.source_exchange.lower())] = evidence_bundle
        bundle_by_base_exchange[(base, evidence_bundle.target_exchange.lower())] = evidence_bundle

    for link in links.values():
        base = _asset_base(link.canonical_asset_id)
        bundle = bundle_by_base_exchange.get((base, link.exchange))
        if bundle is None:
            raise ValueError(
                f"identity registry link {link.canonical_asset_id}/{link.exchange} has no "
                "matching evidence bundle (base/exchange not vouched for by any captured bundle)"
            )
        if bundle.bundle_sha256 != link.evidence_sha256:
            raise ValueError(
                f"identity registry link {link.canonical_asset_id}/{link.exchange} evidence_sha256 "
                f"{link.evidence_sha256!r} does not match its evidence bundle's own content hash "
                f"{bundle.bundle_sha256!r}"
            )
        if bundle.identity_class != "exact_contract":
            raise ValueError(
                f"identity registry link {link.canonical_asset_id}/{link.exchange} is backed by a "
                f"{bundle.identity_class!r} bundle, not exact_contract -- never activatable"
            )
        # evidence_url must actually name the bundle this link was verified
        # against, not merely resolve as a well-formed https string
        # (colleague review, 2026-08-28) -- catches a copy-pasted link row
        # whose evidence_url still points at a different asset's file.
        expected_name = evidence_bundle_path(
            bundle.base, bundle.source_exchange, bundle.target_exchange
        ).name
        if not link.evidence_url.endswith(f"/{expected_name}"):
            raise ValueError(
                f"identity registry link {link.canonical_asset_id}/{link.exchange} evidence_url "
                f"{link.evidence_url!r} does not name its own evidence bundle "
                f"({expected_name!r})"
            )
        # The sha256 check above only proves this bundle's content matches
        # what it claims to hash to (untampered since written) -- not that
        # it was ever semantically valid in the first place. Re-running the
        # same check capture_bundle applies before ever saving a bundle
        # closes that gap (colleague review, 2026-08-28).
        try:
            revalidate_bundle_identity_class(bundle)
        except ValueError as exc:
            raise ValueError(
                f"identity registry link {link.canonical_asset_id}/{link.exchange} evidence bundle "
                f"failed semantic revalidation: {exc}"
            ) from exc


def load_identity_registry() -> IdentityRegistry:
    resource = files("schurfer_analytics").joinpath(DEFAULT_REGISTRY_RESOURCE)
    registry = parse_identity_registry(
        json.loads(resource.read_text(encoding="utf-8")),
        expected_version=EXPECTED_REGISTRY_VERSION,
        expected_fingerprint=EXPECTED_REGISTRY_FINGERPRINT,
    )
    verify_registry_against_evidence(registry.links_by_identity, evidence_dir=EVIDENCE_DIR)
    return registry


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
    source_first_observed_at: datetime,
    target_observations: tuple[Any, ...],
    registry: IdentityRegistry,
) -> QualificationResult:
    """Link exact reviewed identities, then select the cheapest executable venue."""
    requested_notional = (
        float(target_observations[0].requested_notional_usd) if target_observations else 0.0
    )
    # A capture from before the v2 registry existed must never be treated as
    # v2-qualified prospective evidence, even if its identity happens to
    # satisfy the (later-populated) registry -- identity was not confirmed
    # in real time when the capture occurred, only retroactively. See
    # IDENTITY_REGISTRY_V2_START's own docstring (colleague review,
    # 2026-08-28). Checked first, before any identity lookup, so this can
    # never be bypassed by a coincidental registry match.
    if source_first_observed_at < IDENTITY_REGISTRY_V2_START:
        return QualificationResult(
            status="excluded",
            reason="before_identity_registry_v2_activation",
            canonical_asset_id=None,
            selected_target_exchange=None,
            selected_round_trip_impact_bps=None,
            requested_notional_usd=requested_notional,
            details={"targets": []},
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
        # identity_verified/identity_match_method are checked explicitly,
        # not merely inferred from status == 'sampled': a colleague review
        # (2026-08-28) found that qualify_source_lead trusted 'sampled' rows
        # implicitly, which happened to be safe only because the one
        # current capture code path always pairs status='sampled' with
        # identity_verified=True -- an incidental property of today's
        # implementation, not a contract this function itself enforced.
        registry_confirmed = (
            bool(getattr(observation, "identity_verified", False))
            and getattr(observation, "identity_match_method", None)
            == IDENTITY_MATCH_METHOD_REGISTRY_EXACT_V2
        )
        diagnostic: dict[str, Any] = {
            "exchange": observation.target_exchange,
            "observation_status": observation.status,
            "identity_key": identity_key,
            "identity_approved": link is not None,
            "registry_confirmed": registry_confirmed,
            "canonical_match": (
                link is not None and link.canonical_asset_id == source_link.canonical_asset_id
            ),
        }
        if observation.status != "sampled" or link is None or not registry_confirmed:
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
    if not ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED:
        # Everything needed to select a venue was computed above -- asset
        # identity and executable liquidity both check out -- but the
        # specific derivative markets themselves are not yet independently
        # evidenced (see ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED's own
        # docstring). Recorded in full under details for visibility, never
        # discarded, but this can never become status='qualified' yet.
        return QualificationResult(
            status="excluded",
            reason="route_evidence_not_yet_independent",
            canonical_asset_id=source_link.canonical_asset_id,
            selected_target_exchange=None,
            selected_round_trip_impact_bps=None,
            requested_notional_usd=requested_notional,
            details={
                "targets": diagnostics,
                "would_select": {
                    "target_exchange": selected_exchange,
                    "round_trip_impact_bps": selected_impact,
                },
            },
        )
    return QualificationResult(
        status="qualified",
        reason="lowest_round_trip_impact",
        canonical_asset_id=source_link.canonical_asset_id,
        selected_target_exchange=selected_exchange,
        selected_round_trip_impact_bps=selected_impact,
        requested_notional_usd=requested_notional,
        details={"targets": diagnostics},
    )
