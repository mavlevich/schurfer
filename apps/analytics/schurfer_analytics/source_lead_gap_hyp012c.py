"""HYP-012c: one pooled hypothesis on the unread HYP-012b holdout (ISO weeks 36-39).

Registered before any holdout read: docs/research/source-lead-gap-hyp012c-v1.md.

Rule (fixed before the read, the upper bound chosen on the burnt HYP-012b discovery
exploration and named as such): a HYP-012b candidate from any of the five formal sources,
one episode per event, whose source first price is at least GAP_MIN and below GAP_MAX above
Bybit's last close at or before the signal. Same entry, exit and costs as HYP-012b
(COST_FUNCTION_VERSION). One primary test against zero, clustered by asset; floor
100 resolved / 30 assets / no week above 45%, and two missingness ceilings (unknown gap
among eligible candidates, unresolved among in-band) that block a positive verdict.
Per-source rows are descriptive only: counts, mean and missingness, no test. Outcomes are
computed ONLY for in-band candidates: every other holdout candidate stays unread. The
claim pins the contract digest and the reader's revision.

Phases, as in HYP-012b: `prepare` freezes the holdout inputs (no return), `read` takes the
claim and computes the result from the stored inputs only. `all` does both.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Any

from .source_lead_multi_source import (
    BOOTSTRAP_ITERATIONS,
    BOOTSTRAP_SEED,
    FORMAL_SOURCES,
    HOLDOUT_FLOOR,
    Candidate,
    VenueResult,
    floor_shortfalls,
    group_result,
    meets_holdout_floor,
    reference_gap,
    round_trip_cost_pct,
)
from .source_lead_multi_source_report import (
    INPUTS_NAME,
    RESULT_NAME,
    _candidate_from_json,
    candles,
    complete_digest,
    outcome_for,
    prepare_inputs,
    read_frozen_stage,
    write_once,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .source_lead_multi_source import Outcome

FAMILY_VERSION = "hyp012c_pooled_gap_v1"
STAGE = "holdout"
HYPOTHESIS = "pooled_gap_0.6pct_to_2pct"
GAP_MIN = 0.006
GAP_MAX = 0.02
SOURCES: tuple[str, ...] = FORMAL_SOURCES
ALPHA = 0.05
# The HYP-012b cost function, pinned literally: taker 10 bps per side, 20 bps round-trip
# impact, funding 5 bps per 8h prorated over the 31-minute hold = 0.403% per trade.
COST_FUNCTION_VERSION = "hyp012b_taker10_impact20_funding5per8h_v1"
COST_PCT_31_MIN = round_trip_cost_pct(31)
# Missingness ceilings: above either one, a positive verdict is not allowed
# (insufficient_data); a mature non-positive result is still `fail`.
MAX_UNKNOWN_GAP_FRACTION = 0.05  # unknown gap among eligible candidates of the 5 sources
MAX_UNRESOLVED_IN_BAND_FRACTION = 0.05  # unresolved among in-band candidates
# Identity-check reasons that leave the gap unknown. A price-level mismatch is a known gap
# outside the 2x band, so that candidate is simply out of band.
UNKNOWN_GAP_REASONS = ("no_source_price", "missing_reference_bar")

CONTRACT: dict[str, Any] = {
    "family_version": FAMILY_VERSION,
    "hypothesis": HYPOTHESIS,
    "stage": STAGE,
    "sources": list(SOURCES),
    "band": [GAP_MIN, GAP_MAX],
    "alpha": ALPHA,
    "floor": HOLDOUT_FLOOR,
    "bootstrap": {"iterations": BOOTSTRAP_ITERATIONS, "seed": BOOTSTRAP_SEED},
    "cost_function_version": COST_FUNCTION_VERSION,
    "cost_pct_per_trade": COST_PCT_31_MIN,
    "max_unknown_gap_fraction": MAX_UNKNOWN_GAP_FRACTION,
    "max_unresolved_in_band_fraction": MAX_UNRESOLVED_IN_BAND_FRACTION,
    "unknown_gap_reasons": list(UNKNOWN_GAP_REASONS),
}


def contract_sha256() -> str:
    return hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()


def band_status(candidate: Candidate, raw: list[list[str]] | None) -> str:
    """Outcome-blind: `in_band`, `out_of_band`, `no_gap` (unusable pre-signal price) or
    `bar_fetch_failed`. Uses the pre-signal reference bar and the source price only."""
    if raw is None:
        return "bar_fetch_failed"
    gap = reference_gap(candidate, candles(raw).get(candidate.reference_ms))
    if gap is None:
        return "no_gap"
    return "in_band" if GAP_MIN <= gap < GAP_MAX else "out_of_band"


def verdict(
    result: VenueResult, *, unknown_gap_fraction: float, unresolved_in_band_fraction: float
) -> str:
    """In order: no estimate is `insufficient_data`; a mature result (at least the resolved
    minimum) with a non-positive mean is `fail`, even below the floor or over a ceiling;
    below the floor, or over either missingness ceiling, is `insufficient_data`; then
    `candidate` needs a positive mean and p below ALPHA, else `fail`."""
    if result.p_value is None or result.mean_net_pct is None:
        return "insufficient_data"
    if result.resolved >= HOLDOUT_FLOOR["min_resolved"] and result.mean_net_pct <= 0:
        return "fail"
    if not meets_holdout_floor(result):
        return "insufficient_data"
    if (
        unknown_gap_fraction > MAX_UNKNOWN_GAP_FRACTION
        or unresolved_in_band_fraction > MAX_UNRESOLVED_IN_BAND_FRACTION
    ):
        return "insufficient_data"
    return "candidate" if result.p_value < ALPHA and result.mean_net_pct > 0 else "fail"


def descriptive_row(label: str, outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """Counts, mean and missingness only: no p-value, no interval (one registered test)."""
    resolved = [o for o in outcomes if o.resolved and o.net_return_pct is not None]
    unresolved = Counter(str(o.reason) for o in outcomes if not o.resolved)
    return {
        "source": label,
        "in_band": len(outcomes),
        "resolved": len(resolved),
        "assets": len({o.candidate.cluster_key for o in resolved}),
        "mean_net_pct": fmean(o.net_return_pct or 0.0 for o in resolved) if resolved else None,
        "unresolved": dict(sorted(unresolved.items())),
    }


def compute_result(
    inputs: dict[str, Any], inputs_sha: str, claim: dict[str, Any]
) -> dict[str, Any]:
    """Pure: the result from the stored inputs. Candidates outside the five sources or the
    band are counted, never evaluated."""
    funnel: Counter[str] = Counter()
    in_band: list[Candidate] = []
    # Missingness per (source, week). Eligible: every candidate of the five sources that
    # reached the price check, the identity-excluded ones included.
    cells: dict[str, dict[str, dict[str, int]]] = {}

    def cell(source: str, week: str) -> dict[str, int]:
        return cells.setdefault(source, {}).setdefault(
            week, {"eligible": 0, "unknown_gap": 0, "in_band": 0, "unresolved": 0}
        )

    for row in inputs.get("route_identity_excluded", []):
        if row["source"] in SOURCES:
            c = cell(row["source"], row["week"])
            c["eligible"] += 1
            c["unknown_gap"] += int(row["reason"] in UNKNOWN_GAP_REASONS)
    for candidate in map(_candidate_from_json, inputs["candidates"]):
        if candidate.source_exchange not in SOURCES:
            funnel["other_source"] += 1
            continue
        status = band_status(candidate, inputs["bars"].get(str(candidate.event_id)))
        funnel[status] += 1
        c = cell(candidate.source_exchange, candidate.week)
        c["eligible"] += 1
        c["unknown_gap"] += int(status in ("no_gap", "bar_fetch_failed"))
        if status == "in_band":
            c["in_band"] += 1
            in_band.append(candidate)
    outcomes: list[Outcome] = [
        outcome_for(c, inputs["bars"].get(str(c.event_id)), str(c.event_id) in inputs["bars"])
        for c in in_band
    ]
    for o in outcomes:
        if not o.resolved:
            cell(o.candidate.source_exchange, o.candidate.week)["unresolved"] += 1
    all_cells = [c for weeks in cells.values() for c in weeks.values()]
    eligible = sum(c["eligible"] for c in all_cells)
    unknown = sum(c["unknown_gap"] for c in all_cells)
    unresolved = sum(1 for o in outcomes if not o.resolved)
    unknown_fraction = unknown / eligible if eligible else 1.0
    unresolved_fraction = unresolved / len(outcomes) if outcomes else 1.0
    pooled = group_result(HYPOTHESIS, outcomes, seed_key=f"{FAMILY_VERSION}:{HYPOTHESIS}")
    return {
        "family_version": FAMILY_VERSION,
        "stage": inputs["stage"],
        "window": inputs["window"],
        "hypothesis": HYPOTHESIS,
        "band": [GAP_MIN, GAP_MAX],
        "cost_function_version": COST_FUNCTION_VERSION,
        "cost_pct_per_trade": COST_PCT_31_MIN,
        "contract_sha256": claim["contract_sha256"],
        "inputs_sha256": inputs_sha,
        "claimed_at": claim["claimed_at"],
        "reader_code_revision": claim["reader_code_revision"],
        "reader_working_tree_dirty": claim["reader_working_tree_dirty"],
        "inputs_code_revision": inputs["code_revision"],
        "prepare_funnel": inputs["funnel"],
        "band_funnel": dict(sorted(funnel.items())),
        "pooled": asdict(pooled),
        "floor_shortfalls": floor_shortfalls(pooled),
        "missingness": {
            "eligible": eligible,
            "unknown_gap": unknown,
            "unknown_gap_fraction": unknown_fraction,
            "in_band": len(outcomes),
            "unresolved_in_band": unresolved,
            "unresolved_in_band_fraction": unresolved_fraction,
            "by_source_week": {s: dict(sorted(w.items())) for s, w in sorted(cells.items())},
        },
        "verdict": verdict(
            pooled,
            unknown_gap_fraction=unknown_fraction,
            unresolved_in_band_fraction=unresolved_fraction,
        ),
        "descriptive_by_source": [
            descriptive_row(s, [o for o in outcomes if o.candidate.source_exchange == s])
            for s in SOURCES
        ],
    }


def read(
    stage_dir: Path,
    now: datetime,
    *,
    reader_code_revision: str,
    reader_working_tree_dirty: bool,
) -> dict[str, Any]:
    """The claim pins the contract digest and the revision of THIS reader, so a resumed
    read with other thresholds or other code is refused."""
    return read_frozen_stage(
        STAGE,
        stage_dir,
        now,
        family_version=FAMILY_VERSION,
        tested_family=lambda: ([HYPOTHESIS], None),
        compute=compute_result,
        pins={
            "contract_sha256": contract_sha256(),
            "reader_code_revision": reader_code_revision,
            "reader_working_tree_dirty": reader_working_tree_dirty,
        },
    )


def render_markdown(payload: dict[str, Any]) -> str:
    p = payload["pooled"]
    miss = payload["missingness"]

    def f(value: Any, digits: int = 3) -> str:
        return "n/a" if value is None else f"{value:.{digits}f}"

    shortfalls = payload["floor_shortfalls"]
    lines = [
        f"# HYP-012c holdout ({payload['window'][0]} .. {payload['window'][1]})",
        "",
        f"Verdict: **{payload['verdict']}**"
        + (f" (below floor: {', '.join(shortfalls)})" if shortfalls else ""),
        "",
        f"Pooled {p['source']}: resolved {p['resolved']}, assets {p['assets']}, max week "
        f"{f(p['max_week_share'], 2)}, mean net {f(p['mean_net_pct'])}% "
        f"[{f(p['ci_lower_pct'])}, {f(p['ci_upper_pct'])}], p {f(p['p_value'], 4)}",
        "",
        f"Band funnel: {payload['band_funnel']}",
        f"Missingness: unknown gap {miss['unknown_gap']}/{miss['eligible']} "
        f"({f(miss['unknown_gap_fraction'], 3)}), unresolved in band "
        f"{miss['unresolved_in_band']}/{miss['in_band']} "
        f"({f(miss['unresolved_in_band_fraction'], 3)})",
        "",
        "Descriptive by source (no test):",
    ]
    for r in payload["descriptive_by_source"]:
        lines.append(
            f"- {r['source']}: in band {r['in_band']}, resolved {r['resolved']}, "
            f"mean net {f(r['mean_net_pct'])}%, unresolved {r['unresolved']}"
        )
    return "\n".join(lines) + "\n"


def band_summary(inputs: dict[str, Any]) -> dict[str, int]:
    """Outcome-blind counts per band status, printed after prepare."""
    counts: Counter[str] = Counter()
    for candidate in map(_candidate_from_json, inputs["candidates"]):
        if candidate.source_exchange in SOURCES:
            counts[band_status(candidate, inputs["bars"].get(str(candidate.event_id)))] += 1
    for row in inputs.get("route_identity_excluded", []):
        if row["source"] in SOURCES:
            counts[f"identity_excluded:{row['reason']}"] += 1
    return dict(sorted(counts.items()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=("prepare", "read", "all"), default="all")
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--code-revision", default="unknown")
    parser.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    parser.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    parser.set_defaults(working_tree_dirty=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    now = datetime.now(UTC)
    inputs_path = args.stage_dir / INPUTS_NAME
    complete_digest(inputs_path)
    if args.phase in ("prepare", "all") and not inputs_path.exists():
        inputs = asyncio.run(
            prepare_inputs(
                STAGE,
                now=now,
                code_revision=args.code_revision,
                working_tree_dirty=args.working_tree_dirty,
                family_version=FAMILY_VERSION,
            )
        )
        args.stage_dir.mkdir(parents=True, exist_ok=True)
        digest = write_once(inputs_path, inputs)
        sys.stdout.write(f"inputs sha256 {digest}\n")
        sys.stdout.write(json.dumps(band_summary(inputs), sort_keys=True) + "\n")
    elif args.phase == "prepare":
        raise SystemExit(f"{inputs_path} exists: inputs are prepared once")
    if args.phase in ("read", "all"):
        payload = read(
            args.stage_dir,
            now,
            reader_code_revision=args.code_revision,
            reader_working_tree_dirty=args.working_tree_dirty,
        )
        sys.stdout.write(render_markdown(payload))
        digest = (args.stage_dir / f"{RESULT_NAME}.sha256").read_text(encoding="utf-8").strip()
        sys.stdout.write(f"\nresult sha256 {digest}\n")


if __name__ == "__main__":
    main()
