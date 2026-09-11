"""Pure post-fire logic for the net-buy accumulation discovery (HYP contract
`docs/research/net-buy-accumulation-discovery-v1.md`).

The heavy per-minute work -- rolling `score_m`/`score_s`, the full-presence and
baseline-floor eligibility checks, and the edge-triggered fire detection with
reset -- runs SQL-side in DuckDB over the frozen cold-bar Parquet (the repository
module), which is bounded to one row per FIRED episode. This module is the
data-independent remainder: it turns fired episodes into the frozen `adj_return`,
the quantile diagnostics, the reproducible block bootstrap, the joint Holm
correction, and the frozen verdict. Every constant here matches the frozen
contract and is chosen a priori, never tuned on outcomes.

Nothing here reads a database, places an order, or promotes a strategy; a
positive result is a Discovery candidate only, and `adj_return` is
fees-and-funding adjusted with slippage UNKNOWN, never a proven net return.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from itertools import pairwise
from random import Random

from schurfer_performance import COST_MODEL_VERSION, DEFAULT_COSTS, CostParameters

CONTRACT_VERSION = "net_buy_accumulation_discovery_v1"

# --- frozen thresholds and windows (see the contract's Locked decisions) ----
THETA_M = 1.0
THETA_S = 0.60
W_MINUTES = 1440
B_MINUTES = 7 * 1440
HORIZON_MINUTES = 240
BASELINE_ACTIVITY_FLOOR_USD = 100_000.0

# --- frozen verdict constants ----------------------------------------------
STOP_MIN_RESOLVED_FIRES = 100
CANDIDATE_MIN_PER_QUANTILE = 150
CANDIDATE_MIN_CLUSTERS = 30
WEEKLY_MIN_FIRES = 20
LIQUID_SEGMENT_FLOOR_USD = 5_000_000.0
TRADABLE_SHARE_MIN = 0.30
CANDIDATE_SPREAD_PP = 1.0  # diagnostic only, never a gate
TOO_RARE_FIRES_PER_WEEK = 30  # on fully-covered UTC weeks
QUANTILE_COUNT = 5

# --- frozen, reproducible bootstrap ----------------------------------------
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20260911
HOLM_ALPHA = 0.05

PRIMARY_MAG = "P-MAG"
PRIMARY_SHAPE = "P-SHAPE"
PRIMARIES = (PRIMARY_MAG, PRIMARY_SHAPE)

COST_PARAMETERS: CostParameters = DEFAULT_COSTS


def cost_pct_at_horizon(
    horizon_minutes: int = HORIZON_MINUTES, costs: CostParameters = COST_PARAMETERS
) -> float:
    """Fees + funding for one 240m round trip, in percentage points, from the
    shared conservative cost model. Slippage is deliberately excluded (depth was
    never measured); the result is an adjustment, not a proven net cost."""
    fee_cost_bps = 2 * costs.taker_fee_bps_per_side
    funding_cost_bps = costs.funding_cost_bps_per_8h * horizon_minutes / 480
    return (fee_cost_bps + funding_cost_bps) / 100.0


# --- one fired episode (exactly what the repository yields) ------------------


@dataclass(frozen=True)
class FiredEpisode:
    """One edge-triggered fire of one primary. `entry_close`/`exit_close` are the
    `close_price` of bar `t-1` and bar `t+239`; when the exit bar is missing the
    episode is unresolved (`exit_close is None`). `baseline_daily_activity_usd`
    places it in a liquidity segment; `utc_day`/`utc_week` are the fire day/week
    as `YYYY-MM-DD`/ISO `GGGG-Www` strings; `week_fully_covered` marks weeks that
    are not a partial window boundary."""

    primary: str
    instrument: str  # exchange:native_market_id
    cluster: str  # asset cluster key
    exchange: str
    fire_ts: str  # ISO8601 UTC
    utc_day: str
    utc_week: str
    week_fully_covered: bool
    score: float
    entry_close: float
    exit_close: float | None
    baseline_daily_activity_usd: float

    @property
    def resolved(self) -> bool:
        return self.exit_close is not None

    @property
    def gross_return_pct(self) -> float | None:
        if self.exit_close is None or self.entry_close <= 0:
            return None
        return (self.exit_close - self.entry_close) / self.entry_close * 100.0

    @property
    def adj_return_pct(self) -> float | None:
        """Fees+funding-adjusted long return in percentage points; slippage
        UNKNOWN, so this is never a proven net return."""
        gross = self.gross_return_pct
        if gross is None:
            return None
        return gross - cost_pct_at_horizon()

    @property
    def in_tradable_segment(self) -> bool:
        return self.baseline_daily_activity_usd >= LIQUID_SEGMENT_FLOOR_USD


# --- quantiles + Rule 6 ties ------------------------------------------------


@dataclass(frozen=True)
class TieDiagnostics:
    distinct_values: int
    largest_tied_group: int
    adjacent_boundaries_distinct: bool


@dataclass(frozen=True)
class QuantileStat:
    index: int  # 1..QUANTILE_COUNT, 1 = lowest score
    episodes: int
    clusters: int
    median_adj_return_pct: float | None


def _quantile_boundaries(scores: list[float]) -> list[float]:
    ordered = sorted(scores)
    return [
        ordered[min(len(ordered) - 1, (len(ordered) * q) // QUANTILE_COUNT)]
        for q in range(1, QUANTILE_COUNT)
    ]


def assign_quantiles(episodes: list[FiredEpisode]) -> dict[str, int]:
    """Rank fired episodes of one primary into QUANTILE_COUNT score quantiles,
    lowest score = quantile 1. Ties are broken by a stable sort so equal scores
    never split a boundary silently; Rule 6 diagnostics report whether they do."""
    ordered = sorted(episodes, key=lambda e: (e.score, e.instrument, e.fire_ts))
    n = len(ordered)
    out: dict[str, int] = {}
    for i, ep in enumerate(ordered):
        q = min(QUANTILE_COUNT, 1 + (i * QUANTILE_COUNT) // n) if n else 1
        out[f"{ep.instrument}@{ep.fire_ts}"] = q
    return out


def tie_diagnostics(episodes: list[FiredEpisode]) -> TieDiagnostics:
    scores = [e.score for e in episodes]
    distinct = sorted(set(scores))
    counts = {v: scores.count(v) for v in distinct}
    largest = max(counts.values()) if counts else 0
    boundaries = _quantile_boundaries(scores) if len(scores) >= QUANTILE_COUNT else []
    # A boundary value that also appears strictly inside an adjacent bucket means
    # a tie straddles the boundary.
    straddle = any(counts.get(b, 0) > 1 for b in boundaries)
    return TieDiagnostics(
        distinct_values=len(distinct),
        largest_tied_group=largest,
        adjacent_boundaries_distinct=not straddle,
    )


# --- block bootstrap (by UTC day) + Holm ------------------------------------


@dataclass(frozen=True)
class BootstrapResult:
    mean: float
    lower_bound: float  # one-sided 5th percentile
    p_one_sided: float  # share of resample means <= 0


def block_bootstrap_by_day(
    returns_by_day: dict[str, list[float]],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> BootstrapResult:
    """Block bootstrap with the UTC day as the block. Days are resampled with
    replacement to their original count; the mean is recomputed on the pooled
    resample. Fully reproducible from `seed`."""
    days = list(returns_by_day)
    pooled = [r for rs in returns_by_day.values() for r in rs]
    point = statistics.fmean(pooled) if pooled else 0.0
    if not days or not pooled:
        return BootstrapResult(mean=point, lower_bound=0.0, p_one_sided=1.0)
    rng = Random(seed)  # noqa: S311 -- deterministic seeded PRNG for a reproducible bootstrap, not crypto
    means: list[float] = []
    n_days = len(days)
    for _ in range(iterations):
        sample: list[float] = []
        for _ in range(n_days):
            sample.extend(returns_by_day[days[rng.randrange(n_days)]])
        means.append(statistics.fmean(sample) if sample else 0.0)
    means.sort()
    lb = means[int(0.05 * len(means))]
    p = sum(1 for m in means if m <= 0.0) / len(means)
    return BootstrapResult(mean=point, lower_bound=lb, p_one_sided=p)


def holm(p_values: dict[str, float], alpha: float = HOLM_ALPHA) -> dict[str, bool]:
    """Holm-Bonferroni across a family; returns which hypotheses are rejected
    (significant) at `alpha`."""
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(ordered)
    rejected: dict[str, bool] = {}
    still = True
    for i, (key, p) in enumerate(ordered):
        threshold = alpha / (m - i)
        if still and p <= threshold:
            rejected[key] = True
        else:
            still = False
            rejected[key] = False
    return rejected


# --- verdict ----------------------------------------------------------------

VERDICT_STOP = "stop"
VERDICT_TOO_RARE_OR_ILLIQUID = "too_rare_or_illiquid"
VERDICT_INSUFFICIENT = "insufficient_discovery"
VERDICT_CANDIDATE = "discovery_candidate"


@dataclass(frozen=True)
class PrimaryResult:
    primary: str
    fires: int
    resolved_fires: int
    mean_adj_return_pct: float | None
    bootstrap: BootstrapResult | None
    quantiles: tuple[QuantileStat, ...]
    top_minus_bottom_spread_pp: float | None
    monotone_increasing: bool
    ties: TieDiagnostics
    distinct_clusters: int
    min_fires_per_quantile: int
    weekly_min_fires: int
    fires_per_fully_covered_week: float
    tradable_share: float
    verdict_prebonf: str
    p_one_sided: float | None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def evaluate_primary(episodes: list[FiredEpisode]) -> PrimaryResult:
    """Everything computable for one primary before the joint Holm step. The
    verdict here is pre-correction; `evaluate_joint` finalizes candidacy."""
    resolved = [e for e in episodes if e.resolved]
    resolved_returns = [e.adj_return_pct for e in resolved if e.adj_return_pct is not None]
    mean_adj = statistics.fmean(resolved_returns) if resolved_returns else None

    quant = assign_quantiles(episodes)
    by_q: dict[int, list[FiredEpisode]] = {q: [] for q in range(1, QUANTILE_COUNT + 1)}
    for ep in episodes:
        by_q[quant[f"{ep.instrument}@{ep.fire_ts}"]].append(ep)
    stats: list[QuantileStat] = []
    for q in range(1, QUANTILE_COUNT + 1):
        eps = by_q[q]
        rets = [e.adj_return_pct for e in eps if e.adj_return_pct is not None]
        stats.append(
            QuantileStat(
                index=q,
                episodes=len(eps),
                clusters=len({e.cluster for e in eps}),
                median_adj_return_pct=_median(rets),
            )
        )
    top, bottom = stats[-1], stats[0]
    spread = (
        top.median_adj_return_pct - bottom.median_adj_return_pct
        if top.median_adj_return_pct is not None and bottom.median_adj_return_pct is not None
        else None
    )
    medians = [s.median_adj_return_pct for s in stats]
    monotone = all(a is not None and b is not None and a <= b for a, b in pairwise(medians))

    compared = by_q[1] + by_q[QUANTILE_COUNT]
    distinct_clusters = len({e.cluster for e in compared})
    min_per_quantile = min(top.episodes, bottom.episodes)

    covered_weeks: dict[str, int] = {}
    for e in episodes:
        if e.week_fully_covered:
            covered_weeks[e.utc_week] = covered_weeks.get(e.utc_week, 0) + 1
    weekly_min = min(covered_weeks.values()) if covered_weeks else 0
    n_covered_weeks = len(covered_weeks) or 1
    fires_per_week = len(episodes) / n_covered_weeks
    tradable_share = (
        sum(1 for e in resolved if e.in_tradable_segment) / len(resolved) if resolved else 0.0
    )

    verdict = _prebonf_verdict(
        resolved_fires=len(resolved),
        mean_adj=mean_adj,
        min_per_quantile=min_per_quantile,
        distinct_clusters=distinct_clusters,
        weekly_min=weekly_min,
        fires_per_week=fires_per_week,
        tradable_share=tradable_share,
    )
    return PrimaryResult(
        primary=episodes[0].primary if episodes else "",
        fires=len(episodes),
        resolved_fires=len(resolved),
        mean_adj_return_pct=mean_adj,
        bootstrap=None,
        quantiles=tuple(stats),
        top_minus_bottom_spread_pp=spread,
        monotone_increasing=monotone,
        ties=tie_diagnostics(episodes),
        distinct_clusters=distinct_clusters,
        min_fires_per_quantile=min_per_quantile,
        weekly_min_fires=weekly_min,
        fires_per_fully_covered_week=fires_per_week,
        tradable_share=tradable_share,
        verdict_prebonf=verdict,
        p_one_sided=None,
    )


def _prebonf_verdict(
    *,
    resolved_fires: int,
    mean_adj: float | None,
    min_per_quantile: int,
    distinct_clusters: int,
    weekly_min: int,
    fires_per_week: float,
    tradable_share: float,
) -> str:
    # Mature negative binds first and needs only the trade-count floor.
    if resolved_fires >= STOP_MIN_RESOLVED_FIRES and (mean_adj is not None and mean_adj <= 0):
        return VERDICT_STOP
    if fires_per_week < TOO_RARE_FIRES_PER_WEEK or tradable_share < TRADABLE_SHARE_MIN:
        return VERDICT_TOO_RARE_OR_ILLIQUID
    diversity_met = (
        min_per_quantile >= CANDIDATE_MIN_PER_QUANTILE
        and distinct_clusters >= CANDIDATE_MIN_CLUSTERS
        and weekly_min >= WEEKLY_MIN_FIRES
    )
    if not diversity_met:
        return VERDICT_INSUFFICIENT
    if mean_adj is not None and mean_adj > 0:
        return VERDICT_CANDIDATE  # provisional; confirmed only by the joint bootstrap+Holm
    return VERDICT_INSUFFICIENT


def evaluate_joint(episodes_by_primary: dict[str, list[FiredEpisode]]) -> dict[str, PrimaryResult]:
    """Run each primary, then the joint Holm over the two bootstrap p-values. A
    `discovery_candidate` survives only if its Holm-adjusted test is significant
    AND its bootstrap lower bound is positive; otherwise it falls to
    `insufficient_discovery` (a `stop` or `too_rare_or_illiquid` is unchanged)."""
    prelim: dict[str, PrimaryResult] = {}
    p_values: dict[str, float] = {}
    for primary, eps in episodes_by_primary.items():
        res = evaluate_primary(eps)
        boot: BootstrapResult | None = None
        if res.verdict_prebonf == VERDICT_CANDIDATE:
            by_day: dict[str, list[float]] = {}
            for e in eps:
                if e.resolved and e.adj_return_pct is not None:
                    by_day.setdefault(e.utc_day, []).append(e.adj_return_pct)
            boot = block_bootstrap_by_day(by_day)
            p_values[primary] = boot.p_one_sided
        prelim[primary] = PrimaryResult(**{**res.__dict__, "bootstrap": boot})

    rejected = holm(p_values) if p_values else {}
    final: dict[str, PrimaryResult] = {}
    for primary, res in prelim.items():
        verdict = res.verdict_prebonf
        if verdict == VERDICT_CANDIDATE:
            boot = res.bootstrap
            passes = bool(rejected.get(primary)) and boot is not None and boot.lower_bound > 0
            verdict = VERDICT_CANDIDATE if passes else VERDICT_INSUFFICIENT
        final[primary] = PrimaryResult(
            **{
                **res.__dict__,
                "verdict_prebonf": verdict,
                "p_one_sided": res.bootstrap.p_one_sided if res.bootstrap else None,
            }
        )
    return final


__all__ = [
    "CONTRACT_VERSION",
    "COST_MODEL_VERSION",
    "VERDICT_CANDIDATE",
    "VERDICT_INSUFFICIENT",
    "VERDICT_STOP",
    "VERDICT_TOO_RARE_OR_ILLIQUID",
    "BootstrapResult",
    "FiredEpisode",
    "PrimaryResult",
    "QuantileStat",
    "TieDiagnostics",
    "block_bootstrap_by_day",
    "cost_pct_at_horizon",
    "evaluate_joint",
    "evaluate_primary",
    "holm",
]
