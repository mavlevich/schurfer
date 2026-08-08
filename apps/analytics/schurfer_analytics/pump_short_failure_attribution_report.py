"""Read-only historical diagnostic: where does pump-short's baseline lose money.

This is a discovery-level tool, not a formal challenger report: it never
locks a forward cohort, never produces a p-value, and never changes
production `SCORE_THRESHOLD` or any score component. Its only job is to
decompose the ALREADY-registered baseline (`score_6`) strategy's real
historical trades so a human can decide whether tuning the score is even
worth a formal PR, or whether pump-short's losses come from somewhere a
score component cannot see at all.

Two denominators are kept strictly separate throughout, per the human review
that shaped this report:
- component calibration only ever uses baseline-TRIGGERED, fully-COMPLETED
  decisions with a real, recorded, five-component vector — an episode that
  never triggered has no decision to attribute components to, and must never
  be assigned one arbitrarily; an episode whose component vector could not be
  parsed (any one of the five missing or invalid) is excluded from every
  per-component table and counted separately in coverage, never silently
  dropped.
- policy/veto economics use every eligible episode; a never-triggered or
  vetoed episode is cash (zero return), never a search for a different entry.

Component "availability" is a strict three-way state, never collapsed to
two: `True` (confirmed present, safe to use), `False` (confirmed missing —
its own diagnostic bucket, never a veto trigger), and `None` (quality
unknown — a separate diagnostic bucket, also never a veto trigger). Only a
component confirmed `True` with zero points can ever vote to veto an episode
or enter a calibration/interaction cell; fail-closed, not fail-open.

A "veto" here is always the same pre-declared, uniform rule for every
component: force the baseline's selected episode to cash if that ONE
component scored zero points (confirmed, not unknown) at the baseline
decision, holding everything else (including which decision would have been
selected) unchanged. This is one of exactly five fixed candidates, chosen
before looking at any result — never a threshold swept for the best-looking
cell. A candidate must pass on BOTH axes, never just one:
- population robustness: the PAIRED delta (veto return minus baseline
  return), leave-one-asset-out and leave-one-week-out over the FULL
  denominator (zero-delta episodes included — this is what leave-one-out is
  for), all positive;
- effect size: the trades this veto actually changed number at least
  `MIN_CELL_OBSERVATIONS` and span at least two distinct assets and two
  distinct weeks, and the resulting (post-veto) strategy has positive
  after-cost economics and a profit factor above 1.
A veto touching two lucky trades on one asset, diluted across a large
denominator, can satisfy the first axis alone and still be worthless — the
second axis exists specifically to catch that. The single-cell "best row"
this report can produce carries no p-value and authorizes nothing beyond
being written down as at most one new, separately forward-locked contract.

This report's CLI accepts overrides (`--strategy-version`, `--resolver-
version`, `--allow-fallback`, custom costs) for ad hoc sensitivity runs. Any
such run is marked `canonical_run=false` in the manifest and can never
produce `candidate_veto_found` — it is capped at `sensitivity_only_no_
candidate` regardless of what the numbers show, so a favorable cost
assumption or a relaxed resolver can never manufacture a headline finding.

Allowed final verdicts this report's CODE can produce (exactly one):
- `candidate_veto_found`: one veto passes both the population-robustness and
  effect-size gates above. Only possible on a canonical run.
- `no_existing_feature_separation`: none of the five existing components
  separate winners from losers enough to justify further score tuning on
  this sample (this is also the default when more than one veto looks
  positive at once — picking the best of several here would be exactly the
  post-hoc selection this report is not allowed to authorize).
- `sensitivity_only_no_candidate`: the run used a non-canonical strategy
  version, resolver, fallback setting, or cost override — informative for
  comparison, but never a candidate.

A human reading the execution/exit-reason breakdown table may separately
conclude `execution_quality_only` (losses trace to venue/cost structure, not
the score) — this report's code never emits that verdict automatically,
because its fee/funding/slippage figures are the same MODELED cost
assumption applied uniformly to every trade, not empirically observed
per-venue costs, and cannot by construction separate one venue from another.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import fmean, median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from .clustered_inference import ClusterObservation, leave_one_cluster_out_means
from .decision_quality import (
    SCORE_COMPONENTS,
    SCORE_THRESHOLD_BASELINE_POLICY,
    ComponentSnapshot,
    ScoreSelection,
    select_score_policy,
    selected_policy_decisions,
)
from .episode_replay import CONFIRMATION_COHORT_START, PROTOCOL_VERSION
from .outcomes import RESOLVER_VERSION
from .pump_magnitude_report import PUMP_MAGNITUDE_FLOORS_PCT
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    FOUNDATION_VERSION,
    QUERY_VERSION,
    ReplayDataset,
    ReplayEpisode,
    ReplayFilters,
    build_replay_dataset,
)
from .reporting import (
    ReportWindowNotStartedError,
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    profit_factor,
    resolve_report_until,
)
from .virtual_market import (
    DECISION_MARKET_PATH_VERSION,
    DecisionMarketPath,
    decision_market_path_fingerprint,
)
from .virtual_strategy import (
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    ENTRY_MODEL_VERSION,
    EXIT_MODEL_VERSION,
    VIRTUAL_STRATEGY_VERSION,
    CostParameters,
    MarketPath,
    VirtualTrade,
    decision_impact_bps,
    max_sequential_drawdown_usd,
    simulate_decision,
)

FAILURE_ATTRIBUTION_REPORT_VERSION = "pump_short_failure_attribution_report_v2"
FAILURE_ATTRIBUTION_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
# Not a forward-locked cohort — this report reads already-collected history by
# design (it generates hypotheses, it never confirms one). Reuses the same
# general confirmation-cohort boundary decision_quality_report.py already
# uses, rather than inventing a new start date for this one tool.
FAILURE_ATTRIBUTION_DEFAULT_SINCE = CONFIRMATION_COHORT_START
# Below this many observations, a table cell is marked insufficient rather
# than shown as if it were informative. Deliberately small (this is
# descriptive, not formal inference — no cell here ever gets a p-value), but
# not zero: an N of 1-4 is indistinguishable from a single unlucky trade.
MIN_CELL_OBSERVATIONS = 5
# Raw-value buckets for price_extent reuse the ALREADY-registered pump
# magnitude floors instead of inventing new boundaries for this report.
# Descriptive only — see the module docstring; >=300 is not a challenger.
PRICE_EXTENT_BUCKET_FLOORS_PCT = (0.0, *PUMP_MAGNITUDE_FLOORS_PCT, 300.0)
INTERACTION_PAIRS = (("price_extent", "retrace_from_peak"), ("pump_age", "price_extent"))
DiscoveryVerdict = str  # one of the three code-produced literals in the module docstring


@dataclass(frozen=True)
class FailureAttributionManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    virtual_strategy_version: str
    baseline_policy_key: str
    entry_model_version: str
    exit_model_version: str
    cost_model_version: str
    market_path_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    decision_input_fingerprint: str
    market_path_fingerprint: str
    strategy_versions: tuple[str, ...]
    resolver_version: str
    required_horizons: tuple[int, ...]
    fallback_allowed: bool
    veto_components: tuple[str, ...]
    interaction_pairs: tuple[tuple[str, str], ...]
    min_cell_observations: int
    price_extent_bucket_floors_pct: tuple[float, ...]
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    canonical_run: bool
    report_scope: str = "historical_discovery_only_no_strategy_change"
    veto_rule: str = "force_cash_when_component_confirmed_zero_points_v1"
    sensitivity_basis: str = "paired_delta_leave_one_out_v1"
    data_availability_policy: str = "fail_closed_unknown_never_votes_v1"
    no_trigger_policy: str = "zero_return_cash_when_never_triggered"
    within_bar_policy: str = "conservative_stop_first"


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


# Baseline status, in the order episodes actually resolve to one of these:
# - "cash": never triggered (score_6 never crossed with a passing gate).
# - "unresolved_selection": score/market-quality reconstruction itself failed
#   (bad recorded data) — never a decision to simulate at all.
# - "unresolved_path": a decision WAS selected, but its market path/trade
#   simulation did not complete (missing candles, etc.) — a real attempt that
#   could not be priced, not a cash outcome and not a clean trigger.
# - "triggered": a decision was selected and its trade simulation completed.
BaselineStatus = str


@dataclass(frozen=True)
class BaselineRecord:
    """One eligible episode's baseline (score_6) anchor, kept exactly once."""

    pump_event_id: int
    cluster_key: str
    base: str
    week_key: str
    status: BaselineStatus
    decision_id: str | None
    decision_at: datetime | None
    exchange: str | None
    entry_bid_impact_bps: float | None
    components: tuple[ComponentSnapshot, ...]
    components_resolved: bool
    trade: VirtualTrade | None
    error: str | None = None

    @property
    def episode_net_return_pct(self) -> float | None:
        if self.status == "cash":
            return 0.0
        if self.trade is not None and self.trade.status == "complete":
            return self.trade.net_return_pct
        return None

    @property
    def episode_net_pnl_usd(self) -> float | None:
        if self.status == "cash":
            return 0.0
        if self.trade is not None and self.trade.status == "complete":
            return self.trade.net_pnl_usd
        return None


def _week_key(moment: datetime) -> str:
    iso = moment.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _missing_path(episode: ReplayEpisode, selection: ScoreSelection) -> MarketPath:
    decision = selection.decision
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=decision.exchange if decision else "",
        base=decision.base if decision else episode.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def _build_baseline_record(
    episode: ReplayEpisode,
    path_by_decision: dict[str, MarketPath],
    costs: CostParameters,
) -> BaselineRecord:
    selection = select_score_policy(episode, SCORE_THRESHOLD_BASELINE_POLICY)
    if selection.status == "not_triggered":
        return BaselineRecord(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            _week_key(episode.first_decision_at),
            "cash",
            None,
            None,
            None,
            None,
            (),
            False,
            None,
        )
    if selection.status == "unresolved" or selection.decision is None:
        return BaselineRecord(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            _week_key(episode.first_decision_at),
            "unresolved_selection",
            None,
            None,
            None,
            None,
            (),
            False,
            None,
            error=selection.error or "baseline selection failed",
        )
    decision = selection.decision
    path = path_by_decision.get(decision.decision_id or "") or _missing_path(episode, selection)
    trade = simulate_decision(
        episode,
        path,
        decision,
        selection_reason="failure_attribution:baseline",
        costs=costs,
    )
    components_resolved = len(selection.components) == len(SCORE_COMPONENTS)
    status: BaselineStatus = "triggered" if trade.status == "complete" else "unresolved_path"
    return BaselineRecord(
        episode.pump_event_id,
        episode.cluster_key,
        episode.base,
        _week_key(decision.ts),
        status,
        decision.decision_id,
        decision.ts,
        decision.exchange,
        decision_impact_bps(decision, "bid"),
        selection.components,
        components_resolved,
        trade,
        error=trade.error,
    )


def _available(snapshot: ComponentSnapshot | None) -> bool:
    """Fail-closed: only a CONFIRMED-present component snapshot may be used to
    calibrate, interact, or veto. `False` (confirmed missing) and `None`
    (quality unknown) are both treated as unusable here, even though they are
    reported in separate diagnostic buckets elsewhere."""
    return snapshot is not None and snapshot.data_available is True


# --- baseline_economics ---------------------------------------------------


@dataclass(frozen=True)
class BaselineEconomics:
    eligible_episodes: int
    triggered: int
    cash: int
    unresolved_selection: int
    unresolved_path: int
    triggered_missing_component_vector: int
    trade_rate_pct: float | None
    mean_episode_net_return_pct: float | None
    conditional_trade_net_return_pct: float | None
    total_net_pnl_usd: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    initial_stop_rate_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    max_sequential_drawdown_usd: float | None


def _complete_trades(records: tuple[BaselineRecord, ...]) -> tuple[VirtualTrade, ...]:
    return tuple(
        record.trade
        for record in records
        if record.status == "triggered"
        and record.trade is not None
        and record.trade.status == "complete"
    )


def _mean_or_none(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return fmean(present) if present else None


def _baseline_economics(records: tuple[BaselineRecord, ...]) -> BaselineEconomics:
    resolved = [
        record.episode_net_return_pct
        for record in records
        if record.episode_net_return_pct is not None
    ]
    trades = _complete_trades(records)
    trade_returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    pnl = [trade.net_pnl_usd for trade in trades if trade.net_pnl_usd is not None]
    return BaselineEconomics(
        eligible_episodes=len(records),
        triggered=sum(record.status == "triggered" for record in records),
        cash=sum(record.status == "cash" for record in records),
        unresolved_selection=sum(record.status == "unresolved_selection" for record in records),
        unresolved_path=sum(record.status == "unresolved_path" for record in records),
        triggered_missing_component_vector=sum(
            record.status == "triggered" and not record.components_resolved for record in records
        ),
        trade_rate_pct=(len(trades) / len(resolved) * 100) if resolved else None,
        mean_episode_net_return_pct=fmean(resolved) if resolved else None,
        conditional_trade_net_return_pct=fmean(trade_returns) if trade_returns else None,
        total_net_pnl_usd=sum(pnl) if pnl else None,
        profit_factor=profit_factor(trade_returns),
        win_rate_pct=(
            sum(value > 0 for value in trade_returns) / len(trade_returns) * 100
            if trade_returns
            else None
        ),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
        mean_mfe_pct=_mean_or_none([trade.mfe_pct for trade in trades]),
        mean_mae_pct=_mean_or_none([trade.mae_pct for trade in trades]),
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(trades),
    )


# --- execution / exit-reason breakdown (descriptive; modeled costs, not observed fills) ---


@dataclass(frozen=True)
class ExecutionBreakdownRow:
    dimension: str  # "exchange" | "exit_reason"
    bucket: str
    n: int
    share_pct: float
    mean_gross_return_pct: float | None
    mean_net_return_pct: float | None
    mean_fee_bps: float | None
    mean_funding_bps: float | None
    mean_slippage_bps: float | None
    mean_entry_impact_bps: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_duration_minutes: float | None


def _execution_breakdown(
    records: tuple[BaselineRecord, ...],
    *,
    dimension: str,
    key_fn: Callable[[BaselineRecord], str | None],
) -> tuple[ExecutionBreakdownRow, ...]:
    records_by_key: dict[str, list[BaselineRecord]] = defaultdict(list)
    total = 0
    for record in records:
        if (
            record.status != "triggered"
            or record.trade is None
            or record.trade.status != "complete"
        ):
            continue
        key = key_fn(record)
        if key is None:
            continue
        records_by_key[key].append(record)
        total += 1
    rows = []
    for key, group in sorted(records_by_key.items(), key=lambda item: -len(item[1])):
        trades = [record.trade for record in group if record.trade is not None]
        rows.append(
            ExecutionBreakdownRow(
                dimension=dimension,
                bucket=key,
                n=len(trades),
                share_pct=(len(trades) / total * 100) if total else 0.0,
                mean_gross_return_pct=_mean_or_none([t.gross_return_pct for t in trades]),
                mean_net_return_pct=_mean_or_none([t.net_return_pct for t in trades]),
                mean_fee_bps=_mean_or_none([t.fee_cost_bps for t in trades]),
                mean_funding_bps=_mean_or_none([t.funding_cost_bps for t in trades]),
                mean_slippage_bps=_mean_or_none([t.slippage_cost_bps for t in trades]),
                mean_entry_impact_bps=_mean_or_none(
                    [record.entry_bid_impact_bps for record in group]
                ),
                mean_mfe_pct=_mean_or_none([t.mfe_pct for t in trades]),
                mean_mae_pct=_mean_or_none([t.mae_pct for t in trades]),
                mean_duration_minutes=_mean_or_none([t.duration_minutes for t in trades]),
            )
        )
    return tuple(rows)


# --- loss_concentration ----------------------------------------------------


@dataclass(frozen=True)
class LossConcentrationRow:
    cluster_key: str
    losing_episodes: int
    total_loss_usd: float
    share_of_total_loss_pct: float


def _loss_concentration(records: tuple[BaselineRecord, ...]) -> tuple[LossConcentrationRow, ...]:
    losses_by_cluster: dict[str, float] = defaultdict(float)
    losing_count: dict[str, int] = defaultdict(int)
    for record in records:
        pnl = record.episode_net_pnl_usd
        if pnl is None or pnl >= 0:
            continue
        losses_by_cluster[record.cluster_key] += pnl
        losing_count[record.cluster_key] += 1
    total_loss = sum(losses_by_cluster.values())
    if total_loss == 0:
        return ()
    rows = [
        LossConcentrationRow(
            cluster_key=cluster_key,
            losing_episodes=losing_count[cluster_key],
            total_loss_usd=loss,
            share_of_total_loss_pct=loss / total_loss * 100,
        )
        for cluster_key, loss in losses_by_cluster.items()
    ]
    return tuple(sorted(rows, key=lambda row: row.total_loss_usd))


# --- component_calibration ---------------------------------------------------


@dataclass(frozen=True)
class ComponentCalibrationRow:
    component: str
    bucket: str  # "points=0" / "points=1" / "points=2" / "missing_data" / "unknown_data_quality"
    n: int
    mean_net_return_pct: float | None
    median_net_return_pct: float | None
    win_rate_pct: float | None
    insufficient_cell: bool


def _component_calibration(
    records: tuple[BaselineRecord, ...],
) -> tuple[ComponentCalibrationRow, ...]:
    rows: list[ComponentCalibrationRow] = []
    triggered = tuple(
        record
        for record in records
        if record.status == "triggered"
        and record.trade is not None
        and record.trade.status == "complete"
    )
    for component in SCORE_COMPONENTS:
        by_bucket: dict[str, list[float]] = defaultdict(list)
        for record in triggered:
            snapshot = next((c for c in record.components if c.name == component), None)
            if snapshot is None:
                continue
            net = record.trade.net_return_pct if record.trade else None
            if net is None:
                continue
            if snapshot.data_available is False:
                by_bucket["missing_data"].append(net)
            elif snapshot.data_available is None:
                by_bucket["unknown_data_quality"].append(net)
            else:
                by_bucket[f"points={snapshot.points}"].append(net)
        for bucket, values in sorted(by_bucket.items()):
            rows.append(
                ComponentCalibrationRow(
                    component=component,
                    bucket=bucket,
                    n=len(values),
                    mean_net_return_pct=fmean(values) if values else None,
                    median_net_return_pct=median(values) if values else None,
                    win_rate_pct=(
                        (sum(v > 0 for v in values) / len(values) * 100) if values else None
                    ),
                    insufficient_cell=len(values) < MIN_CELL_OBSERVATIONS,
                )
            )
    return tuple(rows)


# --- raw_value_buckets (price_extent only, descriptive) ---------------------


@dataclass(frozen=True)
class RawValueBucketRow:
    component: str
    bucket_label: str
    n: int
    mean_net_return_pct: float | None
    median_net_return_pct: float | None
    insufficient_cell: bool


def _price_extent_bucket_label(value: float, floors: tuple[float, ...]) -> str:
    for low, high in itertools.pairwise(floors):
        if low <= value < high:
            return f"[{low:.0f},{high:.0f})"
    return f">={floors[-1]:.0f}"


def _raw_value_buckets(records: tuple[BaselineRecord, ...]) -> tuple[RawValueBucketRow, ...]:
    by_bucket: dict[str, list[float]] = defaultdict(list)
    for record in records:
        if (
            record.status != "triggered"
            or record.trade is None
            or record.trade.status != "complete"
        ):
            continue
        snapshot = next((c for c in record.components if c.name == "price_extent"), None)
        if snapshot is None or record.trade.net_return_pct is None:
            continue
        label = _price_extent_bucket_label(snapshot.value, PRICE_EXTENT_BUCKET_FLOORS_PCT)
        by_bucket[label].append(record.trade.net_return_pct)
    floors = PRICE_EXTENT_BUCKET_FLOORS_PCT
    ordered_labels = [f"[{low:.0f},{high:.0f})" for low, high in itertools.pairwise(floors)]
    ordered_labels.append(f">={floors[-1]:.0f}")
    rows = []
    for label in ordered_labels:
        values = by_bucket.get(label, [])
        rows.append(
            RawValueBucketRow(
                component="price_extent",
                bucket_label=label,
                n=len(values),
                mean_net_return_pct=fmean(values) if values else None,
                median_net_return_pct=median(values) if values else None,
                insufficient_cell=len(values) < MIN_CELL_OBSERVATIONS,
            )
        )
    return tuple(rows)


# --- interaction_tables ------------------------------------------------------


@dataclass(frozen=True)
class InteractionCell:
    row_component: str
    row_bucket: str
    col_component: str
    col_bucket: str
    n: int
    mean_net_return_pct: float | None
    insufficient_cell: bool


def _interaction_table(
    records: tuple[BaselineRecord, ...],
    row_component: str,
    col_component: str,
) -> tuple[InteractionCell, ...]:
    by_cell: dict[tuple[int, int], list[float]] = defaultdict(list)
    for record in records:
        if (
            record.status != "triggered"
            or record.trade is None
            or record.trade.status != "complete"
        ):
            continue
        row_snapshot = next((c for c in record.components if c.name == row_component), None)
        col_snapshot = next((c for c in record.components if c.name == col_component), None)
        net = record.trade.net_return_pct
        if net is None or not _available(row_snapshot) or not _available(col_snapshot):
            continue
        assert row_snapshot is not None and col_snapshot is not None
        by_cell[(row_snapshot.points, col_snapshot.points)].append(net)
    cells = []
    for (row_points, col_points), values in sorted(by_cell.items()):
        cells.append(
            InteractionCell(
                row_component=row_component,
                row_bucket=f"points={row_points}",
                col_component=col_component,
                col_bucket=f"points={col_points}",
                n=len(values),
                mean_net_return_pct=fmean(values) if values else None,
                insufficient_cell=len(values) < MIN_CELL_OBSERVATIONS,
            )
        )
    return tuple(cells)


# --- fixed_veto_candidates ---------------------------------------------------


@dataclass(frozen=True)
class VetoCandidateRow:
    component: str
    prevented_losing_trades: int
    prevented_loss_usd: float
    missed_winners: int
    missed_profit_usd: float
    retained_trades: int
    retained_trade_rate_pct: float | None
    trades_per_calendar_week: float | None
    conditional_win_rate_pct: float | None
    conditional_net_return_pct: float | None
    episode_cash_inclusive_net_pct: float | None
    profit_factor: float | None
    max_sequential_drawdown_usd: float | None
    initial_stop_rate_pct: float | None
    paired_delta_vs_baseline_pct: float | None
    # The subset actually changed by this veto (baseline completed trade -> cash).
    # A veto can look robust on the full denominator (large N, many assets/weeks)
    # while only ever having fired on a handful of trades from one or two
    # assets -- these three exist so the promotion gate can require the EFFECT
    # itself, not just the surrounding population, to be broad enough to trust.
    affected_completed_trades: int
    affected_assets: int
    affected_weeks: int


def _vetoed(record: BaselineRecord, component: str) -> bool:
    """Fail-closed: only a CONFIRMED (data_available is True) zero-point
    reading ever vetoes. An unknown (None) or confirmed-missing (False)
    quality state never does, even if its recorded points happen to be 0."""
    if record.status != "triggered":
        return False
    snapshot = next((c for c in record.components if c.name == component), None)
    return _available(snapshot) and snapshot is not None and snapshot.points == 0


def _veto_candidate(
    records: tuple[BaselineRecord, ...],
    component: str,
    n_weeks: int,
) -> VetoCandidateRow:
    prevented_losses_usd = 0.0
    prevented_count = 0
    missed_profit_usd = 0.0
    missed_count = 0
    retained_returns_episode: list[float] = []
    retained_trade_returns: list[float] = []
    retained_trades: list[VirtualTrade] = []
    paired_deltas: list[float] = []
    affected_assets: set[str] = set()
    affected_weeks: set[str] = set()
    affected_completed_trades = 0

    for record in records:
        baseline_return = record.episode_net_return_pct
        baseline_pnl = record.episode_net_pnl_usd
        vetoed = _vetoed(record, component)
        if vetoed:
            # _vetoed only ever fires on a "triggered" record, which by
            # construction already had a completed baseline trade -- this is
            # exactly the set of trades this veto actually changes.
            affected_completed_trades += 1
            affected_assets.add(record.cluster_key)
            affected_weeks.add(record.week_key)
        if vetoed and baseline_pnl is not None:
            if baseline_pnl < 0:
                prevented_count += 1
                prevented_losses_usd += -baseline_pnl
            elif baseline_pnl > 0:
                missed_count += 1
                missed_profit_usd += baseline_pnl
        veto_return = 0.0 if vetoed else baseline_return
        if baseline_return is not None and veto_return is not None:
            paired_deltas.append(veto_return - baseline_return)
        if veto_return is not None:
            retained_returns_episode.append(veto_return)
        if (
            not vetoed
            and record.status == "triggered"
            and record.trade is not None
            and record.trade.status == "complete"
        ):
            retained_trades.append(record.trade)
            if record.trade.net_return_pct is not None:
                retained_trade_returns.append(record.trade.net_return_pct)

    resolved_n = len(retained_returns_episode)
    return VetoCandidateRow(
        component=component,
        prevented_losing_trades=prevented_count,
        prevented_loss_usd=prevented_losses_usd,
        missed_winners=missed_count,
        missed_profit_usd=missed_profit_usd,
        retained_trades=len(retained_trades),
        retained_trade_rate_pct=(len(retained_trades) / resolved_n * 100) if resolved_n else None,
        trades_per_calendar_week=(len(retained_trades) / n_weeks) if n_weeks else None,
        conditional_win_rate_pct=(
            sum(v > 0 for v in retained_trade_returns) / len(retained_trade_returns) * 100
            if retained_trade_returns
            else None
        ),
        conditional_net_return_pct=fmean(retained_trade_returns)
        if retained_trade_returns
        else None,
        episode_cash_inclusive_net_pct=(
            fmean(retained_returns_episode) if retained_returns_episode else None
        ),
        profit_factor=profit_factor(retained_trade_returns),
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(retained_trades),
        initial_stop_rate_pct=(
            sum(t.exit_reason == "initial_sl" for t in retained_trades) / len(retained_trades) * 100
            if retained_trades
            else None
        ),
        paired_delta_vs_baseline_pct=fmean(paired_deltas) if paired_deltas else None,
        affected_completed_trades=affected_completed_trades,
        affected_assets=len(affected_assets),
        affected_weeks=len(affected_weeks),
    )


# --- cluster_and_week_sensitivity --------------------------------------------


@dataclass(frozen=True)
class SensitivityRow:
    label: str  # "baseline" (absolute return) or "veto:<component>" (paired delta)
    n_episodes: int
    n_assets: int
    n_weeks: int
    largest_asset_share_pct: float | None
    largest_week_share_pct: float | None
    min_leave_one_asset_out_pct: float | None
    min_leave_one_week_out_pct: float | None


def _sensitivity_row(
    label: str,
    values_with_keys: tuple[tuple[float, str, str], ...],  # (value, cluster_key, week_key)
) -> SensitivityRow:
    if not values_with_keys:
        return SensitivityRow(label, 0, 0, 0, None, None, None, None)
    assets = Counter(cluster_key for _, cluster_key, _ in values_with_keys)
    weeks = Counter(week_key for _, _, week_key in values_with_keys)
    n = len(values_with_keys)
    largest_asset_share = max(assets.values()) / n * 100 if assets else None
    largest_week_share = max(weeks.values()) / n * 100 if weeks else None

    by_asset = tuple(
        ClusterObservation(cluster_key=cluster_key, value=value)
        for value, cluster_key, _ in values_with_keys
    )
    asset_keys = tuple(assets.keys())
    min_loo_asset = (
        min(value for _, value in leave_one_cluster_out_means(by_asset, asset_keys))
        if len(asset_keys) > 1
        else None
    )
    by_week = tuple(
        ClusterObservation(cluster_key=week_key, value=value)
        for value, _, week_key in values_with_keys
    )
    week_keys = tuple(weeks.keys())
    min_loo_week = (
        min(value for _, value in leave_one_cluster_out_means(by_week, week_keys))
        if len(week_keys) > 1
        else None
    )
    return SensitivityRow(
        label=label,
        n_episodes=n,
        n_assets=len(assets),
        n_weeks=len(weeks),
        largest_asset_share_pct=largest_asset_share,
        largest_week_share_pct=largest_week_share,
        min_leave_one_asset_out_pct=min_loo_asset,
        min_leave_one_week_out_pct=min_loo_week,
    )


def _cluster_and_week_sensitivity(
    records: tuple[BaselineRecord, ...],
) -> tuple[SensitivityRow, ...]:
    baseline_keys = tuple(
        (record.episode_net_return_pct, record.cluster_key, record.week_key)
        for record in records
        if record.episode_net_return_pct is not None
    )
    rows = [_sensitivity_row("baseline", baseline_keys)]
    for component in SCORE_COMPONENTS:
        # Sensitivity of the PAIRED DELTA (veto minus baseline), never the veto
        # policy's own absolute mean — a policy can look robust in isolation
        # while the delta it is actually credited for is not.
        delta_keys = []
        for record in records:
            baseline_return = record.episode_net_return_pct
            if baseline_return is None:
                continue
            veto_return = 0.0 if _vetoed(record, component) else baseline_return
            delta_keys.append((veto_return - baseline_return, record.cluster_key, record.week_key))
        rows.append(_sensitivity_row(f"veto:{component}", tuple(delta_keys)))
    return tuple(rows)


# --- discovery_interpretation -------------------------------------------------


def _is_robust_candidate(veto: VetoCandidateRow, row: SensitivityRow | None) -> bool:
    """A veto only counts as a candidate if BOTH the surrounding population's
    paired delta is robust (leave-one-out over the full denominator, per the
    human review that shaped this: zero-delta episodes stay in that
    computation) AND the effect itself — the trades this veto actually
    touched — is large and diverse enough to trust, not two lucky trades on
    one asset diluted across a big denominator into looking "robust"."""
    if row is None or row.n_assets < 2 or row.n_weeks < 2:
        return False
    if not (
        veto.paired_delta_vs_baseline_pct is not None
        and veto.paired_delta_vs_baseline_pct > 0
        and row.min_leave_one_asset_out_pct is not None
        and row.min_leave_one_asset_out_pct > 0
        and row.min_leave_one_week_out_pct is not None
        and row.min_leave_one_week_out_pct > 0
    ):
        return False
    # The effect itself: enough affected trades, spread over enough distinct
    # assets and weeks that it isn't one or two coincidences.
    if (
        veto.affected_completed_trades < MIN_CELL_OBSERVATIONS
        or veto.affected_assets < 2
        or veto.affected_weeks < 2
    ):
        return False
    # After-cost economics of the resulting (post-veto) strategy must
    # actually be positive, not merely "less negative than baseline" — and
    # there must be trades left to have a profit factor at all.
    return (
        veto.retained_trades > 0
        and veto.episode_cash_inclusive_net_pct is not None
        and veto.episode_cash_inclusive_net_pct > 0
        and veto.conditional_net_return_pct is not None
        and veto.conditional_net_return_pct > 0
        and veto.profit_factor is not None
        and veto.profit_factor > 1
    )


def _discovery_interpretation(
    veto_candidates: tuple[VetoCandidateRow, ...],
    sensitivity: tuple[SensitivityRow, ...],
) -> tuple[DiscoveryVerdict, str]:
    sensitivity_by_label = {row.label: row for row in sensitivity}
    candidates = [
        veto
        for veto in veto_candidates
        if _is_robust_candidate(veto, sensitivity_by_label.get(f"veto:{veto.component}"))
    ]
    if len(candidates) == 1:
        winner = candidates[0]
        return (
            "candidate_veto_found",
            f"Vetoing zero-point `{winner.component}` shows a positive paired delta whose "
            "leave-one-asset-out and leave-one-week-out minima both stay positive, actually "
            f"touched {winner.affected_completed_trades} completed trades across "
            f"{winner.affected_assets} assets and {winner.affected_weeks} weeks (not two "
            "lucky trades diluted into a big denominator), and leaves the resulting strategy "
            "with positive after-cost economics and profit factor > 1 on this historical "
            "sample. Needs its own forward-locked cohort before it means anything.",
        )
    if len(candidates) > 1:
        return (
            "no_existing_feature_separation",
            f"{len(candidates)} of 5 single-component vetoes look positive and robust at "
            "once — more consistent with broad noise than one specific separating feature. "
            "Picking the best of several here would be exactly the post-hoc selection this "
            "report is not allowed to authorize.",
        )
    return (
        "no_existing_feature_separation",
        "None of the five existing score components, at a confirmed zero-point veto, "
        "produced a robust positive paired delta on this historical sample. See the "
        "execution/exit-reason breakdown table for a manual read on execution quality — "
        "this report's code cannot auto-detect that from a uniformly modeled cost.",
    )


# --- top-level report ---------------------------------------------------------


@dataclass(frozen=True)
class FailureAttributionReport:
    manifest: FailureAttributionManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    baseline_economics: BaselineEconomics
    execution_breakdown: tuple[ExecutionBreakdownRow, ...]
    loss_concentration: tuple[LossConcentrationRow, ...]
    component_calibration: tuple[ComponentCalibrationRow, ...]
    raw_value_buckets: tuple[RawValueBucketRow, ...]
    interaction_tables: tuple[InteractionCell, ...]
    fixed_veto_candidates: tuple[VetoCandidateRow, ...]
    cluster_and_week_sensitivity: tuple[SensitivityRow, ...]
    discovery_verdict: DiscoveryVerdict
    discovery_rationale: str
    market_paths: tuple[DecisionMarketPath, ...]


def _is_canonical_run(filters: ReplayFilters, costs: CostParameters) -> bool:
    """Only the registered, unmodified configuration may ever produce
    `candidate_veto_found` — any override (a different strategy cohort,
    resolver, fallback tolerance, or cost assumption) makes this a
    sensitivity run, however the numbers come out. The CLI still allows
    these overrides for legitimate ad hoc comparison; this just stops one
    from quietly becoming a headline finding."""
    return (
        filters.strategy_versions == FAILURE_ATTRIBUTION_STRATEGY_VERSIONS
        and filters.resolver_version == RESOLVER_VERSION
        and filters.allow_fallback is False
        and costs.taker_fee_bps_per_side == DEFAULT_COSTS.taker_fee_bps_per_side
        and costs.funding_cost_bps_per_8h == DEFAULT_COSTS.funding_cost_bps_per_8h
    )


def build_failure_attribution_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[DecisionMarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> FailureAttributionReport:
    if filters.since is None:
        raise ValueError("failure attribution report requires an explicit since")
    revision = normalize_code_revision(code_revision)
    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(decision_id for decision_id, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    path_by_decision = {item.decision_id: item.path for item in paths}

    records = tuple(
        _build_baseline_record(episode, path_by_decision, costs)
        for episode in dataset.eligible_episodes
    )
    n_weeks = len({record.week_key for record in records})

    exchange_rows = _execution_breakdown(records, dimension="exchange", key_fn=lambda r: r.exchange)
    exit_reason_rows = _execution_breakdown(
        records,
        dimension="exit_reason",
        key_fn=lambda r: r.trade.exit_reason if r.trade else None,
    )
    execution_rows = exchange_rows + exit_reason_rows

    veto_candidates = tuple(
        _veto_candidate(records, component, n_weeks) for component in SCORE_COMPONENTS
    )
    sensitivity = _cluster_and_week_sensitivity(records)
    canonical_run = _is_canonical_run(filters, costs)
    if canonical_run:
        verdict, rationale = _discovery_interpretation(veto_candidates, sensitivity)
    else:
        verdict, rationale = (
            "sensitivity_only_no_candidate",
            "This run used a non-canonical strategy version, resolver, fallback setting, or "
            "cost override (see the manifest for exactly which). Useful for comparison, but "
            "a sensitivity run can never produce candidate_veto_found — re-run with the "
            "registered defaults for a canonical read.",
        )

    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    interaction_cells: list[InteractionCell] = []
    for row_component, col_component in INTERACTION_PAIRS:
        interaction_cells.extend(_interaction_table(records, row_component, col_component))

    return FailureAttributionReport(
        manifest=FailureAttributionManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=FAILURE_ATTRIBUTION_REPORT_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            baseline_policy_key=SCORE_THRESHOLD_BASELINE_POLICY.key,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            decision_input_fingerprint=dataset.input_fingerprint,
            market_path_fingerprint=decision_market_path_fingerprint(paths),
            strategy_versions=filters.strategy_versions,
            resolver_version=filters.resolver_version,
            required_horizons=filters.required_horizons,
            fallback_allowed=filters.allow_fallback,
            veto_components=SCORE_COMPONENTS,
            interaction_pairs=INTERACTION_PAIRS,
            min_cell_observations=MIN_CELL_OBSERVATIONS,
            price_extent_bucket_floors_pct=PRICE_EXTENT_BUCKET_FLOORS_PCT,
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
            canonical_run=canonical_run,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=tuple(
            CountRow(name, count)
            for name, count in sorted(exclusions.items(), key=lambda item: (-item[1], item[0]))
        ),
        baseline_economics=_baseline_economics(records),
        execution_breakdown=execution_rows,
        loss_concentration=_loss_concentration(records),
        component_calibration=_component_calibration(records),
        raw_value_buckets=_raw_value_buckets(records),
        interaction_tables=tuple(interaction_cells),
        fixed_veto_candidates=veto_candidates,
        cluster_and_week_sensitivity=sensitivity,
        discovery_verdict=verdict,
        discovery_rationale=rationale,
        market_paths=paths,
    )


# --- rendering -----------------------------------------------------------------


def render_json(report: FailureAttributionReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: FailureAttributionReport) -> str:
    manifest = report.manifest
    lines = [
        "# Pump Short Failure Attribution",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{manifest.decision_input_fingerprint}`",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= decision "
            f"< {manifest.dataset_until_exclusive.isoformat()}"
        ),
        f"Canonical run: {'yes' if manifest.canonical_run else 'no (sensitivity only)'}",
        "",
        (
            "> Historical discovery only. This report never changes production score "
            "settings, never produces a p-value, and authorizes at most one new, "
            "separately forward-locked contract — never a direct promotion."
        ),
        "",
        f"## Verdict: `{report.discovery_verdict}`",
        "",
        report.discovery_rationale,
        "",
        "## Coverage",
        "",
    ]
    be = report.baseline_economics
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Dataset episodes", report.dataset_episodes),
                ("Eligible episodes", report.eligible_episodes),
                ("Excluded episodes", report.excluded_episodes),
                ("Baseline triggered (complete trade)", be.triggered),
                ("Baseline cash (never triggered)", be.cash),
                ("Unresolved: selection failed", be.unresolved_selection),
                ("Unresolved: market path/trade did not complete", be.unresolved_path),
                (
                    "Triggered but missing >=1 component (excluded from attribution tables)",
                    be.triggered_missing_component_vector,
                ),
            ],
        )
    )
    lines.extend(["", "## Input exclusions", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Episodes"),
            [(row.name, row.count) for row in report.input_exclusion_reasons],
        )
    )
    lines.extend(
        [
            "",
            "## Baseline economics (cash-inclusive; profit factor is conditional-on-trade)",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Trade rate", format_percentage(be.trade_rate_pct, missing="n/a")),
                (
                    "Episode net (cash-inclusive)",
                    format_percentage(be.mean_episode_net_return_pct, missing="n/a"),
                ),
                (
                    "Conditional trade net",
                    format_percentage(be.conditional_trade_net_return_pct, missing="n/a"),
                ),
                (
                    "Total net P&L",
                    format_number(be.total_net_pnl_usd, suffix=" USD", missing="n/a"),
                ),
                ("Profit factor (conditional)", format_number(be.profit_factor, missing="n/a")),
                ("Win rate (conditional)", format_percentage(be.win_rate_pct, missing="n/a")),
                (
                    "Initial-SL rate (conditional)",
                    format_percentage(be.initial_stop_rate_pct, missing="n/a"),
                ),
                ("Mean MFE (conditional)", format_percentage(be.mean_mfe_pct, missing="n/a")),
                ("Mean MAE (conditional)", format_percentage(be.mean_mae_pct, missing="n/a")),
                (
                    "Max sequential drawdown",
                    format_number(be.max_sequential_drawdown_usd, suffix=" USD", missing="n/a"),
                ),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Execution / exit-reason breakdown (modeled costs, not observed fills)",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "Dimension",
                "Bucket",
                "N",
                "Share",
                "Gross",
                "Net",
                "Fee bps",
                "Funding bps",
                "Slippage bps",
                "Entry impact bps",
                "MFE",
                "MAE",
                "Duration (min)",
            ),
            [
                (
                    row.dimension,
                    row.bucket,
                    row.n,
                    format_percentage(row.share_pct),
                    format_percentage(row.mean_gross_return_pct, missing="n/a"),
                    format_percentage(row.mean_net_return_pct, missing="n/a"),
                    format_number(row.mean_fee_bps, missing="n/a"),
                    format_number(row.mean_funding_bps, missing="n/a"),
                    format_number(row.mean_slippage_bps, missing="n/a"),
                    format_number(row.mean_entry_impact_bps, missing="n/a"),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                    format_number(row.mean_duration_minutes, missing="n/a"),
                )
                for row in report.execution_breakdown
            ],
        )
    )
    lines.extend(["", "## Loss concentration (losing clusters only, most negative first)", ""])
    lines.extend(
        markdown_table(
            ("Cluster", "Losing episodes", "Total loss", "Share of total loss"),
            [
                (
                    row.cluster_key,
                    row.losing_episodes,
                    format_number(row.total_loss_usd, suffix=" USD"),
                    format_percentage(row.share_of_total_loss_pct),
                )
                for row in report.loss_concentration[:15]
            ],
        )
    )
    lines.extend(
        ["", "## Component calibration (baseline-triggered, fully-resolved trades only)", ""]
    )
    lines.extend(
        markdown_table(
            ("Component", "Bucket", "N", "Mean net", "Median net", "Win rate", "Flag"),
            [
                (
                    row.component,
                    row.bucket,
                    row.n,
                    format_percentage(row.mean_net_return_pct, missing="n/a"),
                    format_percentage(row.median_net_return_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    "insufficient_cell_size" if row.insufficient_cell else "",
                )
                for row in report.component_calibration
            ],
        )
    )
    lines.extend(["", "## Raw price_extent buckets (descriptive only — never a challenger)", ""])
    lines.extend(
        markdown_table(
            ("Bucket", "N", "Mean net", "Median net", "Flag"),
            [
                (
                    row.bucket_label,
                    row.n,
                    format_percentage(row.mean_net_return_pct, missing="n/a"),
                    format_percentage(row.median_net_return_pct, missing="n/a"),
                    "insufficient_cell_size" if row.insufficient_cell else "",
                )
                for row in report.raw_value_buckets
            ],
        )
    )
    lines.extend(["", "## Interaction tables (two pre-declared pairs only)", ""])
    for row_component, col_component in INTERACTION_PAIRS:
        lines.extend([f"### `{row_component}` x `{col_component}`", ""])
        cells = [
            cell
            for cell in report.interaction_tables
            if cell.row_component == row_component and cell.col_component == col_component
        ]
        lines.extend(
            markdown_table(
                (f"{row_component}", f"{col_component}", "N", "Mean net", "Flag"),
                [
                    (
                        cell.row_bucket,
                        cell.col_bucket,
                        cell.n,
                        format_percentage(cell.mean_net_return_pct, missing="n/a"),
                        "insufficient_cell_size" if cell.insufficient_cell else "",
                    )
                    for cell in cells
                ],
            )
        )
        lines.append("")
    lines.extend(
        [
            "## Fixed veto candidates (five: force cash when one component is CONFIRMED zero)",
            "",
            "_No p-value. At most one may become a new, separately forward-locked contract._",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "Component",
                "Affected trades",
                "Affected assets",
                "Affected weeks",
                "Prevented losses",
                "Prevented loss $",
                "Missed winners",
                "Missed profit $",
                "Retained trades",
                "Retained rate",
                "Trades/week",
                "Conditional win rate",
                "Conditional net",
                "Episode net (cash-incl.)",
                "PF (retained)",
                "Max drawdown",
                "Initial-SL rate",
                "Paired delta vs baseline",
            ),
            [
                (
                    row.component,
                    row.affected_completed_trades,
                    row.affected_assets,
                    row.affected_weeks,
                    row.prevented_losing_trades,
                    format_number(row.prevented_loss_usd, suffix=" USD"),
                    row.missed_winners,
                    format_number(row.missed_profit_usd, suffix=" USD"),
                    row.retained_trades,
                    format_percentage(row.retained_trade_rate_pct, missing="n/a"),
                    format_number(row.trades_per_calendar_week, missing="n/a"),
                    format_percentage(row.conditional_win_rate_pct, missing="n/a"),
                    format_percentage(row.conditional_net_return_pct, missing="n/a"),
                    format_percentage(row.episode_cash_inclusive_net_pct, missing="n/a"),
                    format_number(row.profit_factor, missing="n/a"),
                    format_number(row.max_sequential_drawdown_usd, suffix=" USD", missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                    format_percentage(row.paired_delta_vs_baseline_pct, missing="n/a"),
                )
                for row in report.fixed_veto_candidates
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Cluster/week sensitivity (baseline row: absolute return; veto rows: paired delta)",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "Row",
                "N episodes",
                "N assets",
                "N UTC weeks",
                "Largest-asset share",
                "Largest-week share",
                "Min leave-one-asset-out",
                "Min leave-one-week-out",
            ),
            [
                (
                    row.label,
                    row.n_episodes,
                    row.n_assets,
                    row.n_weeks,
                    format_percentage(row.largest_asset_share_pct, missing="n/a"),
                    format_percentage(row.largest_week_share_pct, missing="n/a"),
                    format_percentage(row.min_leave_one_asset_out_pct, missing="n/a"),
                    format_percentage(row.min_leave_one_week_out_pct, missing="n/a"),
                )
                for row in report.cluster_and_week_sensitivity
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Historical discovery-only failure attribution for pump-short's baseline"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
        help="inclusive UTC cutoff",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument(
        "--strategy-version",
        action="append",
        help="recorded strategy cohort; defaults to the registered pump-short cohort",
    )
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="allow fallback outcomes in a separately identified sensitivity run",
    )
    parser.add_argument(
        "--taker-fee-bps-per-side",
        type=float,
        default=DEFAULT_COSTS.taker_fee_bps_per_side,
    )
    parser.add_argument(
        "--funding-cost-bps-per-8h",
        type=float,
        default=DEFAULT_COSTS.funding_cost_bps_per_8h,
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_decision_market_paths

    generated_at = datetime.now(UTC)
    until = resolve_report_until(
        args.until,
        generated_at,
        cohort_start=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
        report_label="failure-attribution",
    )
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for pump-short-failure-attribution-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or FAILURE_ATTRIBUTION_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    costs = CostParameters(
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
    )
    sys.stderr.write("failure-attribution: loading decisions\n")
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)
    sys.stderr.write(
        f"failure-attribution: {len(dataset.eligible_episodes)} eligible episodes, "
        "fetching market paths\n"
    )
    selected = selected_policy_decisions(
        dataset.eligible_episodes,
        (SCORE_THRESHOLD_BASELINE_POLICY,),
    )
    paths = await fetch_decision_market_paths(selected, EXCHANGE_FACTORIES)
    sys.stderr.write("failure-attribution: building report\n")
    report = build_failure_attribution_report(
        dataset,
        filters,
        paths,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except ReportWindowNotStartedError as exc:
        parser.error(str(exc))
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
