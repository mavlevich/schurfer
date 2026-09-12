"""Per-exchange eligibility funnel for the net-buy accumulation scanner.

Answers the coverage question the outcome-blind calibration raised: fires cluster
on a single venue, so is the other venue genuinely quieter or is it dropping out
of eligibility (absent capture, W/B incompleteness, unavailability, or the
baseline-activity floor)? This report is diagnostic only -- it reads no outcomes,
places no order, and never gates a verdict. It runs a STANDALONE funnel query
that shares no SQL with, and does not change, the frozen v1 scanner; its stage
counts are diagnostic and are not a statement of the frozen v1 eligibility rule.
It prints, per exchange, how many instrument-minutes and distinct instruments
survive each stage.

The heavy work is SQL-side in the repository; this module orchestrates and
renders. It reads the frozen cold-bar Parquet read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .net_buy_accumulation_repository import scan_eligibility_funnel

# The funnel is a coverage diagnostic, not a held-out discovery read, so it is not
# bounded by the discovery cutoff; the caller passes the window it wants to probe.
# The gating stages, in cumulative order (each is a superset filter of the next).
# B is gated on PRESENCE (see the repository's deliberate-divergence note); B
# trades_complete is reported separately as a diagnostic, not a gate.
_STAGES = (
    ("minutes_in_window", "minutes in window"),
    ("w_present", "W present (1440 bars)"),
    ("w_complete", "+ W trades_complete"),
    ("b_present", "+ B present (10080 bars)"),
    ("available", "+ W available (recv < t)"),
    ("eligible", "+ baseline floor (eligible)"),
)

# Non-gating completeness diagnostics for the B baseline window.
_DIAGNOSTICS = (
    ("b_fully_complete_diag", "B 100% complete (diag)"),
    ("b_ge_99pct_diag", "B >=99% complete (diag)"),
)


def _rows(
    *, cold_bars_dir: str, cohort_start: datetime, cohort_end: datetime
) -> list[dict[str, object]]:
    glob = str(Path(cold_bars_dir) / "bars-*.parquet")
    return scan_eligibility_funnel(
        parquet_glob=glob, cohort_start=cohort_start, cohort_end=cohort_end
    )


def render_markdown(rows: list[dict[str, object]], *, cohort_start: str, cohort_end: str) -> str:
    lines = [
        "# net-buy accumulation -- eligibility funnel (coverage diagnostic)",
        "",
        f"Window: [{cohort_start}, {cohort_end}). Diagnostic only: no outcomes, no "
        "verdict. Counts are instrument-minutes unless noted.",
        "",
        "| Exchange | Instruments | Eligible instruments | "
        + " | ".join(label for _, label in (*_STAGES, *_DIAGNOSTICS))
        + " |",
        "| --- | --- | --- | " + " | ".join("---" for _ in (*_STAGES, *_DIAGNOSTICS)) + " |",
    ]
    for row in rows:
        cells = [
            str(row.get("exchange", "")),
            str(row.get("instruments", 0)),
            str(row.get("eligible_instruments", 0)),
            *[str(row.get(key, 0)) for key, _ in (*_STAGES, *_DIAGNOSTICS)],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if not rows:
        empty = " | ".join("" for _ in (*_STAGES, *_DIAGNOSTICS))
        lines.append("| (no rows) | | | " + empty + " |")
    lines += [
        "",
        "Read the drop between adjacent stages: a venue absent from `minutes in "
        "window` is a capture gap; a large drop at `W complete` is W incompleteness; "
        "at `B present` it is gappy minute coverage (missing bars, not just "
        "incomplete ones); at `available` it is late-arriving trade data; at "
        "`baseline floor` it is genuinely thin activity, not a bug. The two `B "
        ">=..% complete` columns are diagnostics only: this funnel is standalone "
        "and changes no scanner/verdict semantics; the frozen v1 eligibility rule "
        "lives solely in the fire path.",
        "",
    ]
    return "\n".join(lines)


def _parse(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def fingerprint(rows: list[dict[str, object]]) -> str:
    """Deterministic SHA-256 over the funnel rows, so a committed JSON run is a
    reproducible evidence artifact rather than a hand-copied observation."""
    payload = json.dumps(rows, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def render_json(
    rows: list[dict[str, object]],
    *,
    cohort_start: str,
    cohort_end: str,
    code_revision: str,
) -> str:
    return json.dumps(
        {
            "cohort_start": cohort_start,
            "cohort_end": cohort_end,
            "generated_at": datetime.now(UTC).isoformat(),
            "code_revision": code_revision,
            "fingerprint_sha256": fingerprint(rows),
            "funnel": rows,
        },
        indent=2,
        sort_keys=True,
        default=str,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-bars", required=True, help="dir with bars-*.parquet + manifests")
    parser.add_argument("--cohort-start", required=True)
    parser.add_argument("--cohort-end", required=True)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--code-revision", default="unknown")
    args = parser.parse_args()

    cohort_start = _parse(args.cohort_start)
    cohort_end = _parse(args.cohort_end)
    rows = _rows(cold_bars_dir=args.cold_bars, cohort_start=cohort_start, cohort_end=cohort_end)
    if args.format == "json":
        out = render_json(
            rows,
            cohort_start=cohort_start.isoformat(),
            cohort_end=cohort_end.isoformat(),
            code_revision=args.code_revision,
        )
    else:
        out = render_markdown(
            rows, cohort_start=cohort_start.isoformat(), cohort_end=cohort_end.isoformat()
        )
    sys.stdout.write(out + "\n")


__all__ = ["fingerprint", "main", "render_json", "render_markdown"]
