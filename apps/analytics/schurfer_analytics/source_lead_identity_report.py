"""Build an auditable review queue for prospective source-lead identities."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import median
from typing import TYPE_CHECKING, Any

from .reporting import (
    format_number,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)
from .source_lead_contract import CAPTURE_VERSION, OPERATIONAL_COHORT_START

if TYPE_CHECKING:
    from .source_lead_identity_repository import SourceLeadIdentityObservation

REPORT_VERSION = "source_lead_identity_review_v1"
COHORT_START = OPERATIONAL_COHORT_START


@dataclass(frozen=True)
class IdentityReviewFilters:
    since: datetime
    until: datetime

    def __post_init__(self) -> None:
        if self.since.tzinfo is None or self.until.tzinfo is None:
            raise ValueError("source-lead identity window must be timezone-aware")
        if self.since < COHORT_START:
            raise ValueError("identity review cannot include left-censored captures")
        if self.since >= self.until:
            raise ValueError("since must be earlier than until")


@dataclass(frozen=True)
class TargetIdentityCandidate:
    exchange: str
    identity_key: str | None
    market_id: str | None
    unified_symbol: str | None
    market_type: str | None
    base_asset: str | None
    quote_asset: str | None
    settle_asset: str | None
    onboarded_at_ms: int | None
    observations: int
    sampled: int
    executable_quotes: int
    first_observed_at: datetime
    last_observed_at: datetime
    median_round_trip_impact_bps: float | None
    review_flags: tuple[str, ...]


@dataclass(frozen=True)
class IdentityReviewGroup:
    base: str
    source_exchange: str
    source_identity_key: str | None
    source_market_id: str | None
    captures: int
    first_observed_at: datetime
    last_observed_at: datetime
    executable_target_exchanges: tuple[str, ...]
    review_state: str
    review_flags: tuple[str, ...]
    proposed_canonical_asset_id: str
    targets: tuple[TargetIdentityCandidate, ...]


@dataclass(frozen=True)
class SourceLeadIdentityReviewReport:
    manifest: dict[str, Any]
    readiness: dict[str, Any]
    exclusion_reasons: dict[str, int]
    review_groups: tuple[IdentityReviewGroup, ...]
    registry_skeleton: dict[str, Any]


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _finite_nonnegative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _round_trip_impact(row: SourceLeadIdentityObservation) -> float | None:
    bid = _finite_nonnegative(row.target_liquidity.get("bid_impact_bps"))
    ask = _finite_nonnegative(row.target_liquidity.get("ask_impact_bps"))
    bid_filled = _finite_nonnegative(row.target_liquidity.get("bid_filled_notional_usd"))
    ask_filled = _finite_nonnegative(row.target_liquidity.get("ask_filled_notional_usd"))
    requested = _finite_nonnegative(row.requested_notional_usd)
    if None in {bid, ask, bid_filled, ask_filled, requested}:
        return None
    assert bid is not None and ask is not None
    assert bid_filled is not None and ask_filled is not None and requested is not None
    if bid_filled + 0.01 < requested or ask_filled + 0.01 < requested:
        return None
    return bid + ask


def _fingerprint(rows: tuple[SourceLeadIdentityObservation, ...]) -> str:
    payload = [
        {
            "capture_id": row.capture_id,
            "event_id": row.event_id,
            "base": row.base,
            "source_first_observed_at": row.source_first_observed_at.isoformat(),
            "capture_status": row.capture_status,
            "eligibility_reason": row.eligibility_reason,
            "source_identity_key": row.source_identity_key,
            "source_market_id": row.source_market_id,
            "source_payload": row.source_payload,
            "target_exchange": row.target_exchange,
            "target_status": row.target_status,
            "target_eligibility_reason": row.target_eligibility_reason,
            "target_observed_at": (
                row.target_observed_at.isoformat() if row.target_observed_at else None
            ),
            "requested_notional_usd": row.requested_notional_usd,
            "target_instrument": row.target_instrument,
            "target_liquidity": row.target_liquidity,
        }
        for row in rows
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _target_candidate(
    exchange: str,
    identity_key: str | None,
    rows: tuple[SourceLeadIdentityObservation, ...],
    *,
    base: str,
    identity_key_bases: dict[tuple[str, str], set[str]],
) -> TargetIdentityCandidate:
    instruments = [row.target_instrument for row in rows if row.target_instrument]
    field_values = {
        field: {_text(instrument.get(field)) for instrument in instruments}
        for field in (
            "market_id",
            "unified_symbol",
            "market_type",
            "base_asset",
            "quote_asset",
            "settle_asset",
        )
    }
    flags: set[str] = set()
    if identity_key is None:
        flags.add("missing_target_identity")
    elif len(identity_key_bases[(exchange, identity_key)]) > 1:
        flags.add("target_identity_maps_multiple_bases")
    for field, values in field_values.items():
        values.discard(None)
        if len(values) > 1:
            flags.add(f"inconsistent_target_{field}")
    base_values = field_values["base_asset"] - {None}
    if base_values and base.upper() not in {
        value.upper() for value in base_values if value is not None
    }:
        flags.add("target_base_mismatch")
    if field_values["market_type"] - {None, "swap"}:
        flags.add("target_not_swap")
    if field_values["quote_asset"] - {None, "USDT"}:
        flags.add("target_not_usdt_quote")
    if field_values["settle_asset"] - {None, "USDT"}:
        flags.add("target_not_usdt_settle")

    observed_times = [row.target_observed_at for row in rows if row.target_observed_at]
    impacts = [impact for row in rows if (impact := _round_trip_impact(row)) is not None]
    onboard_values = {
        value
        for instrument in instruments
        if (value := _integer(instrument.get("onboarded_at_ms"))) is not None
    }
    if len(onboard_values) > 1:
        flags.add("inconsistent_target_onboarded_at")
    if not onboard_values:
        flags.add("missing_target_onboarded_at")
    return TargetIdentityCandidate(
        exchange=exchange,
        identity_key=identity_key,
        market_id=next(iter(field_values["market_id"] - {None}), None),
        unified_symbol=next(iter(field_values["unified_symbol"] - {None}), None),
        market_type=next(iter(field_values["market_type"] - {None}), None),
        base_asset=next(iter(base_values), None),
        quote_asset=next(iter(field_values["quote_asset"] - {None}), None),
        settle_asset=next(iter(field_values["settle_asset"] - {None}), None),
        onboarded_at_ms=next(iter(onboard_values), None),
        observations=len(rows),
        sampled=sum(row.target_status == "sampled" for row in rows),
        executable_quotes=len(impacts),
        first_observed_at=min(observed_times),
        last_observed_at=max(observed_times),
        median_round_trip_impact_bps=median(impacts) if impacts else None,
        review_flags=tuple(sorted(flags)),
    )


def build_source_lead_identity_review(
    rows: tuple[SourceLeadIdentityObservation, ...],
    filters: IdentityReviewFilters,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> SourceLeadIdentityReviewReport:
    if any(
        row.source_first_observed_at < filters.since
        or row.source_first_observed_at >= filters.until
        for row in rows
    ):
        raise ValueError("identity observation falls outside the report window")

    capture_identity: dict[int, tuple[str, str | None]] = {}
    for row in rows:
        identity = (row.base, row.source_identity_key)
        previous = capture_identity.setdefault(row.capture_id, identity)
        if previous != identity:
            raise ValueError(f"capture {row.capture_id} has inconsistent source identity")

    eligible = tuple(
        row
        for row in rows
        if row.capture_status == "complete" and row.eligibility_reason == "eligible"
    )
    exclusions = Counter(
        row.eligibility_reason
        for capture_id, row in {row.capture_id: row for row in rows}.items()
        if row.capture_status != "complete" or row.eligibility_reason != "eligible"
    )

    source_key_bases: dict[str, set[str]] = defaultdict(set)
    target_key_bases: dict[tuple[str, str], set[str]] = defaultdict(set)
    identities_by_base_exchange: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in eligible:
        if row.source_identity_key:
            source_key_bases[row.source_identity_key].add(row.base)
            identities_by_base_exchange[(row.base, "gate")].add(row.source_identity_key)
        identity_key = _text(row.target_instrument.get("identity_key"))
        if row.target_exchange and identity_key:
            target_key_bases[(row.target_exchange, identity_key)].add(row.base)
            identities_by_base_exchange[(row.base, row.target_exchange)].add(identity_key)

    grouped: dict[tuple[str, str | None], list[SourceLeadIdentityObservation]] = defaultdict(list)
    for row in eligible:
        grouped[(row.base, row.source_identity_key)].append(row)

    review_groups: list[IdentityReviewGroup] = []
    registry_links: list[dict[str, Any]] = []
    for (base, source_key), group_rows_list in sorted(grouped.items()):
        group_rows = tuple(group_rows_list)
        captures = {row.capture_id for row in group_rows}
        flags: set[str] = set()
        if source_key is None:
            flags.add("missing_source_identity")
        elif len(source_key_bases[source_key]) > 1:
            flags.add("source_identity_maps_multiple_bases")
        if len(identities_by_base_exchange[(base, "gate")]) > 1:
            flags.add("multiple_gate_identity_versions")
        if any(row.source_payload.get("identity_conflict") is True for row in group_rows):
            flags.add("source_capture_identity_conflict")

        target_groups: dict[tuple[str, str | None], list[SourceLeadIdentityObservation]] = (
            defaultdict(list)
        )
        for row in group_rows:
            if row.target_exchange is None or row.target_observed_at is None:
                continue
            target_identity_key = _text(row.target_instrument.get("identity_key"))
            # A normal target_not_listed observation has no instrument identity to
            # review. Keep it in the capture denominator, but do not let an absent
            # venue block an otherwise reviewable Gate -> target route.
            if target_identity_key is None and row.target_status != "sampled":
                continue
            target_groups[(row.target_exchange, target_identity_key)].append(row)
        targets = tuple(
            _target_candidate(
                exchange,
                identity_key,
                tuple(target_rows),
                base=base,
                identity_key_bases=target_key_bases,
            )
            for (exchange, identity_key), target_rows in sorted(
                target_groups.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        )
        for target in targets:
            if len(identities_by_base_exchange[(base, target.exchange)]) > 1:
                flags.add(f"multiple_{target.exchange}_identity_versions")
            if target.review_flags:
                flags.add(f"target_{target.exchange}_requires_review")

        executable_exchanges = tuple(
            sorted({target.exchange for target in targets if target.executable_quotes > 0})
        )
        if not executable_exchanges:
            state = "no_executable_target"
        elif flags:
            state = "blocked_conflict"
        else:
            state = "needs_authoritative_evidence"
        group = IdentityReviewGroup(
            base=base,
            source_exchange="gate",
            source_identity_key=source_key,
            source_market_id=next(
                (row.source_market_id for row in group_rows if row.source_market_id), None
            ),
            captures=len(captures),
            first_observed_at=min(row.source_first_observed_at for row in group_rows),
            last_observed_at=max(row.source_first_observed_at for row in group_rows),
            executable_target_exchanges=executable_exchanges,
            review_state=state,
            review_flags=tuple(sorted(flags)),
            proposed_canonical_asset_id=f"asset:{base.lower()}",
            targets=targets,
        )
        review_groups.append(group)
        if state == "needs_authoritative_evidence" and source_key is not None:
            links = [("gate", source_key)] + [
                (target.exchange, target.identity_key)
                for target in targets
                if target.executable_quotes > 0 and target.identity_key is not None
            ]
            registry_links.extend(
                {
                    "canonical_asset_id": group.proposed_canonical_asset_id,
                    "exchange": exchange,
                    "instrument_identity_key": identity_key,
                    "evidence_url": None,
                    "evidence_sha256": None,
                    "review_status": "unapproved",
                }
                for exchange, identity_key in links
            )

    states = Counter(group.review_state for group in review_groups)
    report_groups = tuple(review_groups)
    return SourceLeadIdentityReviewReport(
        manifest={
            "report_version": REPORT_VERSION,
            "capture_version": CAPTURE_VERSION,
            "generated_at": generated_at,
            "since": filters.since,
            "until": filters.until,
            "code_revision": normalize_code_revision(code_revision),
            "working_tree_dirty": working_tree_dirty,
            "input_fingerprint": _fingerprint(rows),
            "interpretation": "review_queue_only_no_identity_approval_no_strategy_change",
            "registry_skeleton_is_loadable": False,
        },
        readiness={
            "captures": len(capture_identity),
            "eligible_complete_captures": len({row.capture_id for row in eligible}),
            "review_groups": len(report_groups),
            "needs_authoritative_evidence": states["needs_authoritative_evidence"],
            "blocked_conflict": states["blocked_conflict"],
            "no_executable_target": states["no_executable_target"],
            "registry_links_pending_review": len(registry_links),
            "approved_links_created": 0,
        },
        exclusion_reasons=dict(sorted(exclusions.items())),
        review_groups=report_groups,
        registry_skeleton={
            "schema_version": 1,
            "registry_version": "source_lead_identity_registry_v2_pending_review",
            "links": registry_links,
        },
    )


def render_json(report: SourceLeadIdentityReviewReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True) + "\n"


def render_markdown(report: SourceLeadIdentityReviewReport) -> str:
    manifest = report.manifest
    readiness = report.readiness
    lines = [
        "# Source Lead Identity Review Queue",
        "",
        f"Generated: {manifest['generated_at'].isoformat()}",
        f"Dataset: {manifest['since'].isoformat()} <= observed < {manifest['until'].isoformat()}",
        f"Input fingerprint: `{manifest['input_fingerprint']}`",
        "",
        "> This report proposes review candidates only. It never approves canonical identity.",
        "> Equal tickers are not evidence that two exchange instruments are the same asset.",
        "",
        "## Readiness",
        "",
        *markdown_table(
            ("Eligible captures", "Groups", "Need evidence", "Blocked", "No target", "Links"),
            [
                (
                    str(readiness["eligible_complete_captures"]),
                    str(readiness["review_groups"]),
                    str(readiness["needs_authoritative_evidence"]),
                    str(readiness["blocked_conflict"]),
                    str(readiness["no_executable_target"]),
                    str(readiness["registry_links_pending_review"]),
                )
            ],
        ),
        "",
        "## Review groups",
        "",
        *markdown_table(
            ("Base", "Captures", "State", "Targets", "Flags"),
            [
                (
                    group.base,
                    str(group.captures),
                    group.review_state,
                    ", ".join(group.executable_target_exchanges) or "none",
                    ", ".join(group.review_flags) or "none",
                )
                for group in report.review_groups
            ],
        ),
        "",
        "## Target identities",
        "",
        *markdown_table(
            ("Base", "Exchange", "Identity", "Quotes", "Median RT impact", "Flags"),
            [
                (
                    group.base,
                    target.exchange,
                    target.identity_key or "missing",
                    str(target.executable_quotes),
                    (
                        f"{format_number(target.median_round_trip_impact_bps, 2)} bps"
                        if target.median_round_trip_impact_bps is not None
                        else "n/a"
                    ),
                    ", ".join(target.review_flags) or "none",
                )
                for group in report.review_groups
                for target in group.targets
            ],
        ),
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=parse_utc_datetime, default=COHORT_START)
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", action="store_false", dest="working_tree_dirty")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .source_lead_identity_repository import SourceLeadIdentityRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for source-lead-identity-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = IdentityReviewFilters(
        since=args.since,
        until=args.until or generated_at,
    )
    repository = SourceLeadIdentityRepository.from_url(db_url)
    try:
        rows = await repository.load(filters.since, filters.until)
    finally:
        await repository.close()
    report = build_source_lead_identity_review(
        rows,
        filters,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
