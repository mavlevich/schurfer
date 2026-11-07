"""HYP-032 power steps 4 and 5: return dispersion at random hours, no mean.

Implements steps 4 and 5 of docs/research/bybit-compressed-spring-v1.md. For each H it
moves that H's events, week by week, by a random whole-day shift inside the week onto
random other instruments (so same-hour groups and symbol-days keep their shape; no
event's price is read and no drawn path comes near an event's path), and reports for

- (a) the H-hour net long return (30 bps, funding in the hold), and
- (b) a random compressed hour's price return minus its price-state matched controls' mean

the standard deviation, the ISO-week block standard error of the cluster mean, the design
effect and the minimum detectable effect at 80% power, one-sided 5%. Step 5 then names the
shortest H whose two effects are at most 50 bps and whose counts meet the floor. No mean
leaves the computation.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HORIZONS_H,
    HOUR_MS,
    VERSION,
    Features,
    Hour,
    Series,
    _week,
    grid_hours,
    last_decision_ms,
    load_features,
    sample_shape,
)
from .bybit_compressed_spring_cond4 import (
    Decision,
    Tape,
    close_at,
    controls_for,
    covered_days,
    decide,
    load_tape,
    non_overlapping_events,
    require_full_tape,
    six_hour_return,
)
from .bybit_post_funding_screen import verified
from .reporting import markdown_table, normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

COST_ROUND_TRIP: Final = 0.0030
Z_ONE_SIDED_5PCT: Final = 1.6449
Z_POWER_80PCT: Final = 0.8416
MDE_MAX: Final = 0.0050
REPLICATES: Final = 200
USABLE_CLUSTER_SHARE: Final = 0.90  # of the events' clusters, per replicate
MIN_USABLE_SHARE: Final = 0.95  # of the replicates, per leg
NEAR_LOW: Final = 0.0045  # an H whose larger effect lies in [45, 55] bps gets the
NEAR_HIGH: Final = 0.0055  # minute-bar check before it can be chosen
FLOOR: Final = {"clusters": 150, "symbols": 40, "iso_weeks": 26}
TOP5_MAX: Final = 0.30


def seeded(label: str) -> random.Random:
    """A reproducible generator for sampling (not for secrets)."""
    return random.Random(f"{VERSION}|power|{label}")  # noqa: S311


# ---------------------------------------------------------------- returns in the hold


def hold_return(series: Series, t: int, horizon_h: int) -> float | None:
    """Price return from the close at t to the close at t + H (hourly bars; the entry and
    exit bars of the reading start one minute later, which a dispersion ignores)."""
    entry, exit_ = close_at(series, t), close_at(series, t + horizon_h * HOUR_MS)
    return exit_ / entry - 1 if entry and exit_ else None


def hold_funding(times: Sequence[int], series: Series, t: int, horizon_h: int) -> float | None:
    """Funding to the long for settlements s with t < s <= t + H: -r_s x mark(s) / entry,
    with the hourly close at s standing in for the mark."""
    entry = close_at(series, t)
    if not entry:
        return None
    total = 0.0
    lo = bisect.bisect_right(times, t)
    hi = bisect.bisect_right(times, t + horizon_h * HOUR_MS)
    for s in times[lo:hi]:
        mark = close_at(series, s)
        if not mark:
            return None
        total -= series.funding[s] * mark / entry
    return total


def net_return(times: Sequence[int], series: Series, t: int, horizon_h: int) -> float | None:
    gross = hold_return(series, t, horizon_h)
    funding = hold_funding(times, series, t, horizon_h)
    if gross is None or funding is None:
        return None
    return gross - COST_ROUND_TRIP + funding


# ---------------------------------------------------------------- dispersion statistics


@dataclass(frozen=True)
class Dispersion:
    clusters: int
    weeks: int
    sd: float
    se: float

    @property
    def design_effect(self) -> float:
        return self.se**2 * self.clusters / self.sd**2 if self.sd > 0 else math.nan

    @property
    def mde(self) -> float:
        return (Z_ONE_SIDED_5PCT + Z_POWER_80PCT) * self.se


def dispersion(values: Sequence[float], weeks: Sequence[str]) -> Dispersion | None:
    """Standard deviation of cluster values and the ISO-week block standard error of
    their equal-weight mean (the variance the reading's week-block bootstrap estimates).
    The mean is used inside and never returned."""
    n, w = len(values), len(set(weeks))
    if n < 2 or w < 2:
        return None
    centre = math.fsum(values) / n
    per_week: dict[str, float] = defaultdict(float)
    for x, week in zip(values, weeks, strict=True):
        per_week[week] += x - centre
    variance = math.fsum(v * v for v in per_week.values()) / n**2 * w / (w - 1)
    return Dispersion(n, w, statistics.stdev(values), math.sqrt(variance))


# ---------------------------------------------------------------- pseudo-samples


@dataclass(frozen=True)
class WeekPlan:
    """One ISO week of a horizon's events: the week's days inside that horizon's decision
    range, and each event as (its symbol, its day's index there, its hour of day)."""

    week: str
    days: tuple[int, ...]
    events: tuple[tuple[str, int, int], ...]


def plan(events: Sequence[Decision], horizon_h: int) -> list[WeekPlan]:
    """Only the events' weeks, days, hours of day and symbol identities are kept: never a
    price. Every week's days are those inside the decision range of H."""
    days_by_week: dict[str, list[int]] = defaultdict(list)
    for day in range(FIRST_DECISION_MS, last_decision_ms(horizon_h) + 1, DAY_MS):
        days_by_week[_week(day)].append(day)
    by_week: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for e in sorted(events, key=lambda x: (x.hour.t, x.hour.symbol)):
        day = e.hour.t - e.hour.t % DAY_MS
        week = _week(day)
        by_week[week].append((e.hour.symbol, days_by_week[week].index(day), e.hour.t - day))
    return [WeekPlan(w, tuple(days_by_week[w]), tuple(by_week[w])) for w in sorted(by_week)]


Cluster = tuple[str, int]  # an event cluster: (event symbol, its day start)


@dataclass
class Draws:
    """One replicate's values per source cluster (an event symbol-day); a cluster keeps a
    value when at least one of its events has one, averaged over them."""

    cells: dict[Cluster, list[float]] = field(default_factory=dict)
    weeks: dict[Cluster, str] = field(default_factory=dict)
    drawn: int = 0  # events
    lost: int = 0  # events of clusters with no instrument available at all their hours
    incomplete: int = 0
    no_control: int = 0

    def add(self, cluster: Cluster, week: str, value: float) -> None:
        self.cells.setdefault(cluster, []).append(value)
        self.weeks[cluster] = week

    def clusters(self) -> tuple[list[float], list[str]]:
        keys = sorted(self.cells)
        values = [math.fsum(self.cells[k]) / len(self.cells[k]) for k in keys]
        return values, [self.weeks[k] for k in keys]


def transplant(
    rng: random.Random,
    plans: Sequence[WeekPlan],
    pool: dict[int, list[str]],
    blocked: Callable[[str, int], bool],
    last_t: int,
) -> Iterator[tuple[Cluster, str, int, list[tuple[str, int]] | None]]:
    """Each week's events moved by one random whole-day shift inside the week (never 0
    when the week has another day), so same-hour groups stay together. Each event
    symbol-day gets one instrument for all its events, available (in the pool, not
    blocked, not used) at every one of its new hours. An instrument belongs to at most
    one event symbol per week, fallbacks included, so two source clusters never become
    one symbol-day: an instrument this symbol already holds if one is available, else a
    random one no symbol holds yet. Yields (cluster, week, its event count, its picks
    or None when no such instrument is available, the cluster then lost whole)."""
    for p in plans:
        n = len(p.days)
        shift = rng.randrange(1, n) if n > 1 else 0
        groups: dict[tuple[str, int], list[int]] = {}
        for symbol, day_index, offset in p.events:
            groups.setdefault((symbol, day_index), []).append(offset)
        owner: dict[str, str] = {}  # instrument -> the event symbol holding it this week
        used: set[tuple[str, int]] = set()
        for (symbol, day_index), offsets in groups.items():
            cluster = (symbol, p.days[day_index])
            day = p.days[(day_index + shift) % n]
            hours = [day + offset for offset in offsets]
            free: set[str] = set()
            if all(t <= last_t for t in hours):
                free = set.intersection(*(
                    {s for s in pool.get(t, []) if (s, t) not in used and not blocked(s, t)}
                    for t in hours
                ))  # fmt: skip
            held = sorted(s for s in free if owner.get(s) == symbol)
            unheld = sorted(s for s in free if s not in owner)
            if not held and not unheld:
                yield cluster, p.week, len(hours), None
                continue
            choice = rng.choice(held or unheld)
            owner[choice] = symbol
            used.update((choice, t) for t in hours)
            yield cluster, p.week, len(hours), [(choice, t) for t in hours]


# ---------------------------------------------------------------- the study's decisions


@dataclass
class Universe:
    features: Features
    passing: list[Decision] = field(default_factory=list)
    events: list[Decision] = field(default_factory=list)
    liquid: dict[int, list[str]] = field(default_factory=dict)  # t -> symbols, no events
    compressed: dict[int, dict[str, Hour]] = field(default_factory=dict)  # c1, no events
    rising: dict[int, list[str]] = field(default_factory=dict)  # compressed and R >= 0
    times: dict[str, list[int]] = field(default_factory=dict)
    passing_by_t: dict[int, list[Decision]] = field(default_factory=dict)
    event_times: dict[str, list[int]] = field(default_factory=dict)  # every condition 4 event
    _decisions: dict[tuple[str, int], Decision] = field(default_factory=dict)

    def decision(self, symbol: str, hour: Hour) -> Decision:
        key = (symbol, hour.t)
        if key not in self._decisions:
            self._decisions[key] = decide(self.features.instruments[symbol], None, set(), hour)
        return self._decisions[key]

    def candidates(self, t: int) -> list[Decision]:
        """The control candidates at t, as in the condition 4 report: decisions passing 1
        to 3 (with their S part) and the other compressed hours (without tape)."""
        passing = self.passing_by_t.get(t, [])
        seen = {d.hour.symbol for d in passing}
        others = [
            self.decision(s, h) for s, h in sorted(self.compressed.get(t, {}).items())
            if s not in seen
        ]  # fmt: skip
        return passing + others

    def blocked(self, symbol: str, t: int, horizon_h: int) -> bool:
        """True when the path [t, t + H] of this instrument comes within an hour of any
        condition 4 event's longest path [t_e, t_e + 24 h], at any horizon."""
        times = self.event_times.get(symbol)
        if not times:
            return False
        lo = t - (max(HORIZONS_H) + 1) * HOUR_MS
        hi = t + (horizon_h + 1) * HOUR_MS
        k = bisect.bisect_right(times, lo)
        return k < len(times) and times[k] < hi


def build_universe(features: Features, tapes: dict[str, Tape]) -> Universe:
    u = Universe(features)
    liquid: dict[int, list[str]] = defaultdict(list)
    compressed: dict[int, dict[str, Hour]] = defaultdict(dict)
    rising: dict[int, list[str]] = defaultdict(list)
    by_t: dict[int, list[Decision]] = defaultdict(list)
    for symbol in sorted(features.instruments):
        series = features.instruments[symbol]
        u.times[symbol] = sorted(series.funding)
        tape = tapes.get(symbol)
        days = covered_days(tape) if tape else set()
        for hour in grid_hours(symbol, series):
            if hour.liquidity != "liquid":
                continue
            if hour.passes:
                d = decide(series, tape, days, hour)
                u.passing.append(d)
                by_t[hour.t].append(d)
                if d.condition4 == "true":
                    u.events.append(d)
                    continue
            liquid[hour.t].append(symbol)
            if hour.c1 == "true":
                compressed[hour.t][symbol] = hour
                r = six_hour_return(series, hour.t)
                if r is not None and r >= 0:
                    rising[hour.t].append(symbol)
    u.liquid, u.compressed, u.rising = dict(liquid), dict(compressed), dict(rising)
    u.passing_by_t = dict(by_t)
    times: dict[str, list[int]] = defaultdict(list)
    for e in u.events:
        times[e.hour.symbol].append(e.hour.t)
    u.event_times = {s: sorted(ts) for s, ts in times.items()}
    return u


# ---------------------------------------------------------------- steps 4 and 5


def net_draws(u: Universe, rng: random.Random, horizon_h: int, plans: Any) -> Draws:
    out = Draws()
    last_t = last_decision_ms(horizon_h)
    picks = transplant(rng, plans, u.liquid, lambda s, t: u.blocked(s, t, horizon_h), last_t)
    for cluster, week, count, chosen in picks:
        out.drawn += count
        if chosen is None:
            out.lost += count
            continue
        for symbol, t in chosen:
            value = net_return(u.times[symbol], u.features.instruments[symbol], t, horizon_h)
            if value is None:
                out.incomplete += 1
            else:
                out.add(cluster, week, value)
    return out


def excess_draws(u: Universe, rng: random.Random, horizon_h: int, plans: Any) -> Draws:
    out = Draws()
    last_t = last_decision_ms(horizon_h)
    picks = transplant(rng, plans, u.rising, lambda s, t: u.blocked(s, t, horizon_h), last_t)
    for cluster, week, count, chosen in picks:
        out.drawn += count
        if chosen is None:
            out.lost += count
            continue
        for symbol, t in chosen:
            own = hold_return(u.features.instruments[symbol], t, horizon_h)
            if own is None:
                out.incomplete += 1
                continue
            pseudo = u.decision(symbol, u.compressed[t][symbol])
            allowed = [c for c in u.candidates(t) if not u.blocked(c.hour.symbol, t, horizon_h)]
            legs = [
                hold_return(u.features.instruments[c.hour.symbol], t, horizon_h)
                for c in controls_for(pseudo, allowed)
            ]
            defined = [x for x in legs if x is not None]
            if not defined:
                out.no_control += 1
                continue
            out.add(cluster, week, own - math.fsum(defined) / len(defined))
    return out


def _quantile(xs: Sequence[float], q: float) -> float:
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def summarize(runs: Sequence[Draws], event_clusters: int) -> dict[str, Any]:
    """A replicate is usable when its dispersion is defined and it keeps a value for at
    least 90% of the events' own clusters; the leg is estimable when at least 95% of
    the replicates are. Medians over the usable replicates; the effect also at its 90th
    percentile; `minute_check_replicate` is the lowest usable replicate. `mde` is
    unrounded (step 5 compares it); the rest is in bps. No mean."""
    need = USABLE_CLUSTER_SHARE * event_clusters
    usable: list[tuple[int, Dispersion]] = []
    for index, run in enumerate(runs):
        d = dispersion(*run.clusters())
        if d is not None and d.clusters >= need:
            usable.append((index, d))
    drawn = sum(r.drawn for r in runs)

    def share(n: int) -> float | None:
        return round(n / drawn, 4) if drawn else None

    out: dict[str, Any] = {
        "replicates": len(runs),
        "usable_replicates": len(usable),
        "estimable": bool(usable) and len(usable) >= MIN_USABLE_SHARE * len(runs),
        "lost_share": share(sum(r.lost for r in runs)),
        "incomplete_share": share(sum(r.incomplete for r in runs)),
        "no_control_share": share(sum(r.no_control for r in runs)),
    }
    if not usable:
        return out
    found = [d for _, d in usable]
    mde = statistics.median(d.mde for d in found)
    return out | {
        "clusters_median": statistics.median(d.clusters for d in found),
        "weeks_median": statistics.median(d.weeks for d in found),
        "sd_bps": round(statistics.median(d.sd for d in found) * 1e4, 1),
        "se_bps": round(statistics.median(d.se for d in found) * 1e4, 2),
        "design_effect": round(statistics.median(d.design_effect for d in found), 3),
        "mde": mde,
        "mde_bps": round(mde * 1e4, 1),
        "mde_bps_p90": round(_quantile([d.mde for d in found], 0.9) * 1e4, 1),
        "minute_check_replicate": usable[0][0],
    }


def floor_met(shape: dict[str, Any]) -> bool:
    return all(shape[k] >= v for k, v in FLOOR.items()) and (
        shape["top5_cluster_share"] is not None and shape["top5_cluster_share"] <= TOP5_MAX
    )


def _legs(entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return entry["a_net"], entry["b_excess"]


def verdict(entry: dict[str, Any]) -> str:
    """Why an H is or is not testable: floor_not_met, not_estimable, mde_above_50 or
    testable (both legs estimable, both unrounded effects at most 50 bps)."""
    if not entry["floor_met"]:
        return "floor_not_met"
    if not all(leg["estimable"] for leg in _legs(entry)):
        return "not_estimable"
    return "testable" if max(leg["mde"] for leg in _legs(entry)) <= MDE_MAX else "mde_above_50"


def near_threshold(entry: dict[str, Any]) -> bool:
    """The minute-bar check could change this H's verdict: the floor is met, both legs
    are estimable, neither effect is above 55 bps and at least one is at 45 bps or more."""
    if verdict(entry) not in ("testable", "mde_above_50"):
        return False
    largest = float(max(leg["mde"] for leg in _legs(entry)))
    return NEAR_LOW <= largest <= NEAR_HIGH


def choose_h(per_horizon: dict[str, Any]) -> str:
    """Step 5, shortest H first: an H the minute-bar check could change stops the choice
    (pending_minute_check); otherwise the first testable H. With none,
    insufficient_data when no H meets the floor, else insufficient_power."""
    for h, entry in per_horizon.items():
        if near_threshold(entry):
            return "pending_minute_check"
        if verdict(entry) == "testable":
            return h
    if all(verdict(e) == "floor_not_met" for e in per_horizon.values()):
        return "insufficient_data"
    return "insufficient_power"


def build_report(u: Universe, replicates: int = REPLICATES) -> dict[str, Any]:
    per_horizon: dict[str, Any] = {}
    for horizon in HORIZONS_H:
        kept = [e for e in u.events if e.hour.t <= last_decision_ms(horizon)]
        chosen = non_overlapping_events(kept, horizon)
        plans = plan(chosen, horizon)
        counts = sample_shape([e.hour for e in chosen])
        a_runs = [
            net_draws(u, seeded(f"a|{horizon}|{r}"), horizon, plans) for r in range(replicates)
        ]
        b_runs = [
            excess_draws(u, seeded(f"b|{horizon}|{r}"), horizon, plans) for r in range(replicates)
        ]
        entry = {
            "events": counts,
            "floor_met": floor_met(counts),
            "a_net": summarize(a_runs, counts["clusters"]),
            "b_excess": summarize(b_runs, counts["clusters"]),
        }
        entry["verdict"] = verdict(entry)
        entry["near_threshold"] = near_threshold(entry)
        per_horizon[f"{horizon}h"] = entry
    return {
        "version": VERSION,
        "step": "power steps 4 and 5: dispersion only, no mean",
        "cost_round_trip_bps": COST_ROUND_TRIP * 1e4,
        "mde_max_bps": MDE_MAX * 1e4,
        "horizons": per_horizon,
        "chosen_h": choose_h(per_horizon),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# {VERSION}: power steps 4 and 5", ""]
    horizons = report["horizons"]
    keys = (
        "clusters_median",
        "weeks_median",
        "sd_bps",
        "se_bps",
        "design_effect",
        "mde_bps",
        "mde_bps_p90",
        "usable_replicates",
        "estimable",
        "lost_share",
        "incomplete_share",
        "no_control_share",
    )
    for part in ("a_net", "b_excess"):
        rows = [(k, *(str(horizons[h][part].get(k)) for h in horizons)) for k in keys]
        lines += [*markdown_table((part, *horizons), rows), ""]
    rows = [
        (h, str(v["events"]["clusters"]), v["verdict"], str(v["near_threshold"]))
        for h, v in horizons.items()
    ]
    lines += [*markdown_table(("H", "clusters", "verdict", "near 50 bps"), rows), ""]
    lines += [f"Chosen H: {report['chosen_h']}"]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for name in ("features", "scope", "manifest"):
        parser.add_argument(f"--{name}", type=Path, required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    return parser


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
        build_report(build_universe(features, tapes))
        | provenance
        | {
            "features_sha256": args.features_sha256,
            "manifest_sha256": args.manifest_sha256,
            "code_revision": normalize_code_revision(args.code_revision),
            "working_tree_dirty": args.working_tree_dirty,
        }
    )
    digest = publish_once(args.output_dir / "power.json", report)
    (args.output_dir / "power.md").write_text(render_markdown(report), encoding="utf-8")
    sys.stdout.write(render_markdown(report) + f"\npower sha256 {digest}\n")
