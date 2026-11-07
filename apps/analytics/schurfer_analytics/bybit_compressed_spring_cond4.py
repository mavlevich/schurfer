"""HYP-032 power step 2 report: condition 4 on the sealed tape, counts only.

Implements the report of power step 2 in docs/research/bybit-compressed-spring-v1.md:
the funnel after condition 4, the events per horizon after each horizon's independence
rule, the share of events with a price-state matched control, and the mask sensitivity
check. It reads the features snapshot and the sealed tape (manifest plus parts), never a
price after t and never a return.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    DAY_MS,
    HORIZONS_H,
    HOUR_MS,
    MINUTE_MS,
    RANK_HOURS,
    RANK_MIN_DEFINED,
    VERSION,
    Features,
    Hour,
    Series,
    grid_hours,
    last_decision_ms,
    load_features,
    sample_shape,
)
from .bybit_compressed_spring_tape import RULES
from .bybit_post_funding_screen import file_sha256, verified
from .reporting import markdown_table, normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

S_HOURS: Final = 6
S_MIN_PCT: Final = 0.80
CONTROL_R_CALIPER_SD: Final = 0.5
CONTROLS_PER_EVENT: Final = 5
TAPE_RULES: Final = RULES["rules"]  # the rules the fetcher binds a run to


# ---------------------------------------------------------------- the sealed tape


@dataclass
class Tape:
    """Hourly sums of one instrument's kept minutes, and of its unreserved masked
    minutes, with the hours that cannot carry S (mismatch, or a day not ok)."""

    kept: dict[int, tuple[float, float]] = field(default_factory=dict)  # hour -> buy, sell
    masked: dict[int, tuple[float, float]] = field(default_factory=dict)
    unusable: set[int] = field(default_factory=set)
    event_hours: set[int] = field(default_factory=set)
    ok_days: set[int] = field(default_factory=set)  # day starts with an ok part


def _hourly(minutes: dict[str, list[float]]) -> dict[int, tuple[float, float]]:
    out: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for minute, cell in minutes.items():
        m = int(minute)
        h = m - m % HOUR_MS
        out[h][0] += cell[0]
        out[h][1] += cell[1]
    return {h: (v[0], v[1]) for h, v in out.items()}


def load_tape(manifest_path: Path, manifest_sha256: str) -> tuple[dict[str, Tape], dict[str, Any]]:
    """Parts are checked against the manifest's SHA-256 values before they are read."""
    manifest = json.loads(verified(manifest_path, manifest_sha256).read_bytes())
    if manifest.get("version") != VERSION:
        raise SystemExit(f"manifest is not {VERSION}")
    parts_dir = manifest_path.parent / "parts"
    tapes: dict[str, Tape] = defaultdict(Tape)
    statuses: Counter[str] = Counter()
    for entry in manifest["parts"]:
        path = parts_dir / entry["symbol"] / f"{entry['day']}.json.gz"
        if file_sha256(path) != entry["part_sha256"]:
            raise SystemExit(f"{path} does not match the manifest")
        part = json.loads(gzip.decompress(path.read_bytes()))
        tape = tapes[entry["symbol"]]
        statuses[part["status"]] += 1
        if part["status"] != "ok":
            day_start = _day_start(entry["day"])
            tape.unusable.update(range(day_start, day_start + 24 * HOUR_MS, HOUR_MS))
            continue
        tape.ok_days.add(_day_start(entry["day"]))
        tape.kept.update(_hourly(part["s_minutes"]))
        tape.masked.update(_hourly(part["sensitivity_minutes"]))
        tape.event_hours.update(part.get("event_hours", []))
        for hour, status in part["reconciliation"]["hours"].items():
            if status == "mismatch":
                tape.unusable.add(int(hour))
    return dict(tapes), {"part_statuses": dict(statuses), "manifest": manifest["binding"]}


def _day_start(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def covered_days(tape: Tape) -> set[int]:
    """Days whose part is ok; a quiet ok day with no trade still counts as covered."""
    return tape.ok_days


def s_value(tape: Tape, days: set[int], t: int, *, unmasked: bool = False) -> float | str:
    """S over [t - 6 h, t), or the reason it is undefined. ``unmasked`` adds the masked
    minutes back (sensitivity only) and needs every boundary in [t - 6 h, t] unreserved."""
    buy = sell = 0.0
    for k in range(1, S_HOURS + 1):
        h = t - k * HOUR_MS
        if h - h % (24 * HOUR_MS) not in days:
            return "tape_missing"
        if h in tape.unusable:
            return "tape_mismatch"
        b, s = tape.kept.get(h, (0.0, 0.0))
        buy += b
        sell += s
        if unmasked:
            mb, ms = tape.masked.get(h, (0.0, 0.0))
            buy += mb
            sell += ms
    if unmasked and any(t - k * HOUR_MS in tape.event_hours for k in range(S_HOURS + 1)):
        return "reserved"
    total = buy + sell
    return (sell - buy) / total if total > 0 else "no_trades"


def mid_rank(value: float, history: Sequence[float]) -> float:
    ordered = sorted(history)
    below = bisect.bisect_left(ordered, value)
    equal = bisect.bisect_right(ordered, value) - below
    return (below + equal / 2) / len(ordered)


# ---------------------------------------------------------------- price state (before t)


def close_at(series: Series, t: int) -> float | None:
    bar = series.bars.get(t - HOUR_MS)
    return bar[2] if bar and bar[2] > 0 else None


def six_hour_return(series: Series, t: int) -> float | None:
    now, before = close_at(series, t), close_at(series, t - S_HOURS * HOUR_MS)
    return now / before - 1 if now and before else None


def r_in_sd(series: Series, t: int) -> float | None:
    """R over its own 14-day standard deviation of 6-hour returns (hourly, overlapping,
    ending at t - 1 h back to t - 336 h; at least 269 defined)."""
    r = six_hour_return(series, t)
    if r is None:
        return None
    history = [six_hour_return(series, t - k * HOUR_MS) for k in range(1, RANK_HOURS + 1)]
    defined = [x for x in history if x is not None]
    if len(defined) < RANK_MIN_DEFINED:
        return None
    sd = statistics.pstdev(defined)
    return r / sd if sd > 0 else None


# ---------------------------------------------------------------- condition 4


@dataclass(frozen=True)
class Decision:
    hour: Hour
    s: float | str
    s_pct: float | None
    r: float | None
    r_sd: float | None
    turnover: float

    @property
    def s_part(self) -> str:
        """true / false / a reason: S > 0 and its own percentile at least 0.80."""
        if isinstance(self.s, str):
            return self.s
        if self.s_pct is None:
            return "rank_history_short"
        return "true" if self.s > 0 and self.s_pct >= S_MIN_PCT else "false"

    @property
    def condition4(self) -> str:
        part = self.s_part
        if part != "true":
            return part
        if self.r is None:
            return "kline_gap"
        return "true" if self.r >= 0 else "false"


def decide(series: Series, tape: Tape | None, days: set[int], hour: Hour) -> Decision:
    """The S part needs the tape; R and its scale need only hourly closes before t."""
    t = hour.t
    turnover = sum(
        series.bars[t - (k + 1) * HOUR_MS][3]
        for k in range(24)
        if t - (k + 1) * HOUR_MS in series.bars
    )
    if tape is None:
        return Decision(
            hour, "tape_missing", None, six_hour_return(series, t), r_in_sd(series, t), turnover
        )
    s = s_value(tape, days, t)
    pct = None
    if not isinstance(s, str):
        history = [s_value(tape, days, t - k * HOUR_MS) for k in range(1, RANK_HOURS + 1)]
        defined = [x for x in history if not isinstance(x, str)]
        pct = mid_rank(s, defined) if len(defined) >= RANK_MIN_DEFINED else None
    return Decision(hour, s, pct, six_hour_return(series, t), r_in_sd(series, t), turnover)


def non_overlapping_events(decisions: Iterable[Decision], horizon_h: int) -> list[Decision]:
    out: list[Decision] = []
    free_at: dict[str, int] = {}
    for d in sorted(decisions, key=lambda x: (x.hour.symbol, x.hour.t)):
        if d.hour.t >= free_at.get(d.hour.symbol, 0):
            out.append(d)
            free_at[d.hour.symbol] = d.hour.t + MINUTE_MS + horizon_h * HOUR_MS
    return out


def controls_for(event: Decision, candidates: Sequence[Decision]) -> list[Decision]:
    """Price-state matched controls: same t, condition 1, R >= 0, within 0.5 sd of the
    event's R, setup determinably false via condition 2, 3 or the S part; the 5
    nearest in R, ties by 24 h turnover log ratio."""
    if event.r_sd is None:
        return []
    pool = []
    for c in candidates:
        if c.hour.symbol == event.hour.symbol or c.r is None or c.r < 0 or c.r_sd is None:
            continue
        if abs(c.r_sd - event.r_sd) > CONTROL_R_CALIPER_SD:
            continue
        failed = c.hour.c2 == "false" or c.hour.c3 == "false" or c.s_part == "false"
        if not failed or c.turnover <= 0 or event.turnover <= 0:
            continue
        pool.append(
            (abs(c.r_sd - event.r_sd), abs(math.log(c.turnover / event.turnover)), c.hour.symbol, c)
        )
    pool.sort(key=lambda x: x[:3])
    return [x[3] for x in pool[:CONTROLS_PER_EVENT]]


def sensitivity(tapes: dict[str, Tape], decisions: Sequence[Decision]) -> dict[str, Any]:
    """How much the fixed mask changes S, on windows whose boundaries are all unreserved."""
    shares: list[float] = []
    flips = windows = 0
    agree = compared = 0
    for tape in tapes.values():
        days = covered_days(tape)
        if not days:
            continue
        grid = range(min(days) + HOUR_MS, max(days) + DAY_MS + HOUR_MS, HOUR_MS)
        for t in grid:  # every hour, so a quiet last hour does not drop a defined window
            masked_s = s_value(tape, days, t)
            full_s = s_value(tape, days, t, unmasked=True)
            if isinstance(masked_s, str) or isinstance(full_s, str):
                continue
            kept = sum(sum(tape.kept.get(t - k * HOUR_MS, (0.0, 0.0))) for k in range(1, 7))
            extra = sum(sum(tape.masked.get(t - k * HOUR_MS, (0.0, 0.0))) for k in range(1, 7))
            windows += 1
            shares.append(extra / (kept + extra) if kept + extra > 0 else 0.0)
            flips += (masked_s > 0) != (full_s > 0)
    for d in decisions:
        own = tapes.get(d.hour.symbol)
        if own is None or isinstance(d.s, str) or d.s_pct is None:
            continue
        days = covered_days(own)
        steps = range(1, RANK_HOURS + 1)
        hist = [s_value(own, days, d.hour.t - k * HOUR_MS, unmasked=True) for k in steps]
        full = s_value(own, days, d.hour.t, unmasked=True)
        if isinstance(full, str) or any(isinstance(x, str) for x in hist):
            continue
        defined = [x for x in hist if not isinstance(x, str)]
        full_part = full > 0 and mid_rank(full, defined) >= S_MIN_PCT
        compared += 1
        agree += full_part == (d.s_part == "true")
    shares.sort()
    return {
        "windows_all_unreserved": windows,
        "masked_turnover_share_median": round(shares[len(shares) // 2], 4) if shares else None,
        "masked_turnover_share_p90": round(shares[int(len(shares) * 0.9)], 4) if shares else None,
        "s_sign_changes": flips,
        "decisions_compared": compared,
        "s_part_agreement": round(agree / compared, 4) if compared else None,
    }


def build_report(features: Features, tapes: dict[str, Tape]) -> dict[str, Any]:
    funnel: Counter[str] = Counter()
    passing: list[Decision] = []
    compressed: dict[int, list[tuple[str, Hour]]] = defaultdict(list)
    for symbol in sorted(features.instruments):
        series = features.instruments[symbol]
        tape = tapes.get(symbol)
        days = covered_days(tape) if tape else set()
        for hour in grid_hours(symbol, series):
            if hour.liquidity != "liquid" or hour.c1 != "true":
                continue
            if hour.c2 == "true" and hour.c3 == "true":
                d = decide(series, tape, days, hour)
                passing.append(d)
                funnel[f"condition4_{d.condition4}"] += 1
                funnel[f"s_part_{d.s_part}"] += 1
            else:
                compressed[hour.t].append((symbol, hour))
    events = [d for d in passing if d.condition4 == "true"]
    by_t_passing: dict[int, list[Decision]] = defaultdict(list)
    for d in passing:
        by_t_passing[d.hour.t].append(d)
    cache: dict[int, list[Decision]] = {}
    per_horizon: dict[str, Any] = {}
    for horizon in HORIZONS_H:
        kept = [d for d in events if d.hour.t <= last_decision_ms(horizon)]
        chosen = non_overlapping_events(kept, horizon)
        with_control = 0
        for e in chosen:
            if e.hour.t not in cache:
                cache[e.hour.t] = by_t_passing[e.hour.t] + [
                    decide(features.instruments[sym], None, set(), hour)
                    for sym, hour in compressed[e.hour.t]
                ]
            with_control += bool(controls_for(e, cache[e.hour.t]))
        per_horizon[f"{horizon}h"] = sample_shape([e.hour for e in chosen]) | {
            "events_with_control": with_control,
            "pre_execution_control_share": (
                round(with_control / len(chosen), 4) if chosen else None
            ),
        }
    determinable = sum(1 for d in passing if d.condition4 in ("true", "false"))
    return {
        "version": VERSION,
        "step": "power step 2 report: condition 4, counts only, no return",
        "decisions_passing_1_to_3": len(passing),
        "condition4_determinable_share": round(determinable / len(passing), 4) if passing else None,
        "funnel": dict(sorted(funnel.items())),
        "events": per_horizon,
        "mask_sensitivity": sensitivity(tapes, passing),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# {VERSION}: power step 2, condition 4", ""]
    lines += markdown_table(("Item", "Count"), [(k, v) for k, v in report["funnel"].items()])
    lines += ["", f"Condition 4 determinable: {report['condition4_determinable_share']}", ""]
    horizons = report["events"]
    keys = (
        "decisions",
        "clusters",
        "symbols",
        "iso_weeks",
        "top5_cluster_share",
        "first_half_clusters",
        "events_with_control",
        "pre_execution_control_share",
    )
    rows = [(k, *(str(horizons[h][k]) for h in horizons)) for k in keys]
    lines += markdown_table(("Events", *horizons), rows)
    lines += [""]
    lines += markdown_table(("Mask sensitivity", "Value"), list(report["mask_sensitivity"].items()))
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--features-sha256", required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--scope-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    return parser


def require_full_tape(manifest: dict[str, Any], scope: dict[str, Any], scope_sha256: str) -> None:
    """The report reads only a full tape: no pilot, the expected rules, the frozen scope's
    SHA-256 and exactly its primary symbol-days."""
    binding = manifest.get("binding", {})
    if binding.get("pilot") != 0:
        raise SystemExit(f"manifest is a pilot (pilot={binding.get('pilot')}), not the full tape")
    if binding.get("rules") != TAPE_RULES:
        raise SystemExit(f"manifest rules {binding.get('rules')!r}, expected {TAPE_RULES!r}")
    if binding.get("scope_sha256") != scope_sha256:
        raise SystemExit("manifest was fetched for another scope")
    expected = {(sym, day) for sym, days in scope["primary"].items() for day in days}
    got = {(p["symbol"], p["day"]) for p in manifest["parts"]}
    if got != expected:
        raise SystemExit(
            f"manifest parts differ from the primary scope ({len(got)} vs {len(expected)})"
        )


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    scope = json.loads(verified(args.scope, args.scope_sha256).read_bytes())
    manifest = json.loads(verified(args.manifest, args.manifest_sha256).read_bytes())
    require_full_tape(manifest, scope, args.scope_sha256)
    tapes, provenance = load_tape(args.manifest, args.manifest_sha256)
    if provenance["manifest"].get("features_sha256") != args.features_sha256:
        raise SystemExit("the tape was fetched against another features snapshot")
    features = load_features(verified(args.features, args.features_sha256))
    report = (
        build_report(features, tapes)
        | provenance
        | {
            "features_sha256": args.features_sha256,
            "manifest_sha256": args.manifest_sha256,
            "code_revision": normalize_code_revision(args.code_revision),
            "working_tree_dirty": args.working_tree_dirty,
        }
    )
    digest = publish_once(args.output_dir / "condition4.json", report)
    (args.output_dir / "condition4.md").write_text(render_markdown(report), encoding="utf-8")
    sys.stdout.write(render_markdown(report) + f"\ncondition4 sha256 {digest}\n")
