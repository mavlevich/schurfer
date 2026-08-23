"""analysis/early-momentum-net-evidence-v1 -- pure funnel/integrity/economics/
robustness/verdict logic for the early_momentum_v4 formal promotion cohort.

No I/O here (see `early_momentum_net_evidence_repository.py` for the single
REPEATABLE-READ read-only query and `early_momentum_net_evidence_report.py`
for the CLI/rendering) -- every function in this module is a pure
transformation over already-fetched rows, so the funnel/integrity/economics
logic is fully unit-testable without a database.

Cohort identity is anchored on `episode.armed_at`, never `trade.entry_at`: a
trade can open after the formal cutoff while its episode armed before it
(e.g. armed just before a deploy, opened just after) -- that trade is not
formal evidence. `app.early_momentum_episodes` is the authoritative source
for cohort membership; `trade.setup_context->>'strategy'` is only ever a
cross-check (used here to find v4-looking trades with no matching episode at
all, which is itself an integrity violation, never as membership proof).

Frozen contract (do not retune after seeing results -- a strategy change
after this analysis means v5 and a new untouched cohort):
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from statistics import fmean, median
from typing import Any

from schurfer_performance import PAPER_ACCOUNTING_VERSION

from .clustered_inference import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    BootstrapEstimate,
    ClusterObservation,
    cluster_bootstrap_mean,
    leave_one_cluster_out_means,
)
from .reporting import profit_factor

# --- Frozen cohort contract ---------------------------------------------

REPORT_VERSION = "early_momentum_net_evidence_v1"
STRATEGY_NAME = "early_momentum"
STRATEGY_VERSION = "4"
FORMAL_COHORT_START = datetime(2026, 8, 23, 14, 53, 57, 243399, tzinfo=UTC)
EXPECTED_CONTRACT_SHA256_HEX = "bdda6c6423b0cc69d8b6266269cda07c31e20f4d256b1793229ab47beb5cb1ac"
ACCOUNTING_VERSION = PAPER_ACCOUNTING_VERSION
MODE = "paper"
SIDE = "long"
EXPECTED_SETUP_CONTEXT_STRATEGY = f"{STRATEGY_NAME}_v{STRATEGY_VERSION}"

# episode TTL (<=1h) + max_hold (4h) + 1h operational buffer -- see
# apps/execution/schurfer_execution/early_momentum.py's own
# ttl_seconds=3600 and _EXIT_PARAMS["max_hold_min"]=240.0.
COHORT_MATURITY_BUFFER_SECONDS = 6 * 3600

# Timestamps are written by different processes/hosts (scanner vs trigger);
# a `trade.entry_at < episode.armed_at` by more than this is a genuine
# ordering violation, not clock jitter.
ENTRY_ARMED_CLOCK_TOLERANCE_SECONDS = 5

# Pre-registered before any run: a sequence of portfolio entries with no gap
# larger than this belongs to the same "wave". Never retuned after seeing
# results (see docs/research/early-momentum-net-evidence-v1.md).
ENTRY_WAVE_GAP_SECONDS = 60 * 60

MIN_CLOSED_TRADES = 100
MIN_DISTINCT_CLUSTERS = 30
MIN_DISTINCT_UTC_WEEKS = 4
INTERIM_CHECKPOINT_MIN_CLOSED_TRADES = 50

MIN_PROFIT_FACTOR = 1.20
# 90%, not the more conventional 95%: this gate authorizes only a
# hard-capital-limited LIVE_MICRO step, calibrated to the same 0.10
# significance level already used in prior validation protocols (see
# liquidation_cascade_validation_report.py's SHUFFLED_LABEL_SIGNIFICANCE_
# THRESHOLD). Raising position size beyond LIVE_MICRO requires a longer
# evidence window AND the tighter 95% gate -- documented, not accidental.
BOOTSTRAP_CONFIDENCE_LEVEL = 0.90

EXIT_REASON_CATEGORIES: tuple[str, ...] = (
    "take_profit",
    "max_hold",
    "no_progress",
    "initial_sl",
    "trailing_stop",
)
UNKNOWN_EXIT_REASON = "unknown"


def parse_exit_reason(notes: str | None) -> str:
    """`Trade.notes` for an early_momentum close is one of exactly the five
    `return f"..."` strings in exit.py (e.g. "take_profit move=4.2%"),
    written once at close, never appended to. An unrecognized leading token
    stays `unknown` rather than becoming an arbitrary new category --
    exit.py changing its reason strings must surface as a growing
    `unknown` bucket, not silently invent new labels here."""
    if not notes or not notes.strip():
        return UNKNOWN_EXIT_REASON
    token = notes.strip().split(maxsplit=1)[0]
    return token if token in EXIT_REASON_CATEGORIES else UNKNOWN_EXIT_REASON


def contract_sha256_hex(raw: bytes) -> str:
    return raw.hex()


# --- Raw dataset shape (exactly what the repository fetches) ------------


@dataclass(frozen=True)
class EpisodeRow:
    episode_id: str
    strategy_id: int
    contract_sha256: bytes
    exchange: str
    native_market_id: str
    execution_symbol: str | None
    source_exchange: str
    source_native_id: str
    execution_identity_key: str
    source_identity_key: str
    cluster_key: str
    armed_at: datetime
    expires_at: datetime
    status: str
    terminal_reason: str | None
    claimed_at: datetime | None
    claim_expires_at: datetime | None
    claim_attempts: int


@dataclass(frozen=True)
class TradeRow:
    trade_id: int
    episode_id: str | None
    strategy_id: int
    symbol: str
    exchange: str
    side: str
    size_usd: float
    leverage: float
    entry_price: float
    entry_at: datetime
    exit_price: float | None
    exit_at: datetime | None
    fees_usd: float
    funding_usd: float
    slippage_usd: float | None
    gross_pnl_usd: float | None
    gross_pnl_pct: float | None
    net_pnl_usd: float | None
    net_pnl_pct: float | None
    accounting_version: str
    accounting_status: str
    accounting_error: str | None
    status: str
    notes: str | None
    entry_idempotency_key: str | None
    is_paper: bool
    setup_context_strategy: str | None
    entry_ask_impact_bps: float | None
    entry_bid_impact_bps: float | None


@dataclass(frozen=True)
class ExitLiquidityRow:
    trade_id: int
    requested_notional_usd: float
    filled_notional_usd: float | None
    spread_bps: float | None
    ask_impact_bps: float | None
    bid_impact_bps: float | None
    latency_ms: int
    status: str
    error: str | None


@dataclass(frozen=True)
class RawDataset:
    """Everything the funnel needs, exactly as fetched, pre-filtering.
    The reproducibility fingerprint is computed over this whole dataset,
    never over just the final comparable set -- see `dataset_fingerprint`."""

    cohort_start: datetime
    cohort_end: datetime
    db_snapshot_at: datetime
    episodes: tuple[EpisodeRow, ...]
    # Includes v4-labeled orphan trades with no episode_id at all (the
    # cross-check set) -- not just trades reachable via an episode join.
    trades: tuple[TradeRow, ...]
    exit_liquidity: tuple[ExitLiquidityRow, ...]


def _fingerprint_ready(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_fingerprint_tuple(row: Any) -> tuple[Any, ...]:
    """Every field of the dataclass, in declaration order -- generic over
    EpisodeRow/TradeRow/ExitLiquidityRow so a future field addition is
    automatically covered, rather than a hand-picked subset that can (and
    did -- colleague review: prices, size/leverage, PnL percentages,
    identity fields, execution_symbol, claim_expires_at, and every
    exit-liquidity field beyond trade_id/notional/status were all missing)
    silently drift out of sync with what the funnel actually consumes."""
    return tuple(_fingerprint_ready(getattr(row, f.name)) for f in fields(row))


def dataset_fingerprint(dataset: RawDataset) -> str:
    """Hashes the full pre-funnel dataset -- every field of every formal
    episode, every linked/orphan trade row, and every exit-liquidity
    observation -- not just whatever survives to the final profitable
    rows. A fingerprint computed only over the comparable set, or over a
    hand-picked subset of fields, would silently miss a changed exclusion
    (e.g. a trade that used to be excluded as incomplete-accounting later
    reads as complete) between two runs against the same nominal cohort
    window. Sorting is safe despite `None`-containing fields later in each
    tuple: `episode_id`/`trade_id` is always the first, always-unique
    field, so Python's tuple comparison never needs to look past it."""
    payload = {
        "cohort_start": dataset.cohort_start.isoformat(),
        "cohort_end": dataset.cohort_end.isoformat(),
        "episodes": sorted(_row_fingerprint_tuple(e) for e in dataset.episodes),
        "trades": sorted(_row_fingerprint_tuple(t) for t in dataset.trades),
        "exit_liquidity": sorted(_row_fingerprint_tuple(row) for row in dataset.exit_liquidity),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# --- Funnel + integrity -------------------------------------------------


@dataclass(frozen=True)
class FunnelStep:
    step: int
    label: str
    remaining: int
    excluded: int
    exclusion_reason: str | None
    example_ids: tuple[str, ...]


@dataclass(frozen=True)
class IntegrityViolation:
    severity: str  # "cohort" | "row"
    code: str
    detail: str
    episode_id: str | None
    trade_id: int | None


@dataclass(frozen=True)
class ComparableTrade:
    """One trade in the final comparable set, plus everything the
    economics/concurrency/robustness/capacity sections need."""

    trade_id: int
    episode_id: str
    cluster_key: str
    native_market_id: str
    source_exchange: str
    exchange: str
    entry_at: datetime
    exit_at: datetime
    entry_price: float
    exit_price: float
    size_usd: float
    leverage: float
    fees_usd: float
    funding_usd: float
    slippage_usd: float
    gross_pnl_usd: float
    gross_pnl_pct: float
    net_pnl_usd: float
    net_pnl_pct: float
    exit_reason: str
    entry_ask_impact_bps: float | None
    entry_bid_impact_bps: float | None


@dataclass(frozen=True)
class FunnelResult:
    steps: tuple[FunnelStep, ...]
    comparable: tuple[ComparableTrade, ...]
    cohort_violations: tuple[IntegrityViolation, ...]
    row_violations: tuple[IntegrityViolation, ...]


def _example_ids(values: list[str], limit: int = 5) -> tuple[str, ...]:
    return tuple(sorted(values)[:limit])


def build_funnel(dataset: RawDataset) -> FunnelResult:
    steps: list[FunnelStep] = []
    cohort_violations: list[IntegrityViolation] = []
    row_violations: list[IntegrityViolation] = []

    def _step(
        label: str, remaining: list[Any], excluded_ids: list[str], reason: str | None
    ) -> None:
        steps.append(
            FunnelStep(
                step=len(steps) + 1,
                label=label,
                remaining=len(remaining),
                excluded=len(excluded_ids),
                exclusion_reason=reason,
                example_ids=_example_ids(excluded_ids),
            )
        )

    # Step 1: all formal v4 episodes (armed_at in the cohort window --
    # already the repository's own scope, nothing excluded here).
    episodes = list(dataset.episodes)
    _step("all_formal_v4_episodes", episodes, [], None)

    # Step 2: correct strategy id and contract hash. A strategy-id mismatch
    # is impossible by construction (the repository's own WHERE clause
    # already filters on strategy_id) -- kept as a defensive assertion via
    # the hash check, which the query cannot pre-filter on cheaply enough
    # to trust blindly.
    observed_hashes = {e.contract_sha256.hex() for e in episodes}
    unexpected_hashes = observed_hashes - {EXPECTED_CONTRACT_SHA256_HEX}
    if unexpected_hashes:
        cohort_violations.append(
            IntegrityViolation(
                severity="cohort",
                code="unexpected_contract_hash",
                detail=f"observed hash(es) not equal to expected: {sorted(unexpected_hashes)}",
                episode_id=None,
                trade_id=None,
            )
        )
    if len(observed_hashes) > 1:
        cohort_violations.append(
            IntegrityViolation(
                severity="cohort",
                code="multiple_contract_hashes_in_cohort",
                detail=(
                    f"{len(observed_hashes)} distinct contract hashes observed: "
                    f"{sorted(observed_hashes)}"
                ),
                episode_id=None,
                trade_id=None,
            )
        )
    hash_ok = [e for e in episodes if e.contract_sha256.hex() == EXPECTED_CONTRACT_SHA256_HEX]
    _step(
        "correct_strategy_and_contract_hash",
        hash_ok,
        [e.episode_id for e in episodes if e not in hash_ok],
        "contract_hash_mismatch",
    )
    episodes = hash_ok

    # Step 3: valid canonical identity (route resolution actually
    # succeeded at arm time).
    identity_ok = [
        e
        for e in episodes
        if e.cluster_key.strip()
        and e.execution_identity_key.strip()
        and e.source_identity_key.strip()
    ]
    _step(
        "valid_canonical_identity",
        identity_ok,
        [e.episode_id for e in episodes if e not in identity_ok],
        "missing_or_blank_identity_fields",
    )
    episodes = identity_ok

    # Step 4: episode reached claim/open, or has an explained terminal
    # reason. An episode still "armed"/"claimed" that has NOT yet reached
    # its own maturity horizon is a normal right-censored case (excluded,
    # not a violation -- the reaper simply hasn't had a chance yet). But
    # once armed_at is old enough that the episode should certainly have
    # resolved one way or another (colleague review: a mature cohort run
    # is only reachable at all once cohort_end itself is
    # COHORT_MATURITY_BUFFER_SECONDS old -- see generate_report's own
    # CohortNotMatureError gate -- so in practice every episode reaching
    # this check IS already past its own maturity; the per-episode check
    # below is defense-in-depth for build_funnel being called directly,
    # e.g. from a test, without that outer gate), a still-armed/claimed
    # episode means the reaper never got to it -- a genuine lifecycle
    # failure and a possible selection-bias risk (an unexecuted signal
    # quietly disappearing from the funnel would let the report show PASS
    # while hiding exactly the failures that should count against it), so
    # it is a row-level integrity violation, not a silent exclusion.
    def _episode_maturity_seconds(e: EpisodeRow) -> float:
        return (dataset.db_snapshot_at - e.armed_at).total_seconds()

    explained: list[EpisodeRow] = []
    for e in episodes:
        if e.status in ("opened", "closed"):
            explained.append(e)
        elif e.status in ("expired", "rejected", "suppressed"):
            if e.terminal_reason is not None:
                explained.append(e)
            # else: an unexplained terminal state stays excluded below,
            # matching the step's own "stuck_unresolved" framing.
        elif e.status in ("armed", "claimed") and (
            _episode_maturity_seconds(e) >= COHORT_MATURITY_BUFFER_SECONDS
        ):
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="episode_stuck_unresolved_past_maturity",
                    detail=(
                        f"status={e.status!r} at snapshot, "
                        f"{_episode_maturity_seconds(e):.0f}s since armed_at -- "
                        "the reaper never resolved this episode"
                    ),
                    episode_id=e.episode_id,
                    trade_id=None,
                )
            )
        # else: either a still-armed/claimed episode within its own
        # maturity horizon (normal right-censored case), or an unexplained
        # terminal state -- both stay excluded below without a violation.
    _step(
        "reached_claim_open_or_explained_terminal",
        explained,
        [e.episode_id for e in episodes if e not in explained],
        "stuck_unresolved_past_maturity",
    )
    episodes = explained

    # From here on, work per-episode against its trade row(s).
    trades_by_episode: dict[str, list[TradeRow]] = defaultdict(list)
    for t in dataset.trades:
        if t.episode_id is not None:
            trades_by_episode[t.episode_id].append(t)

    # Cross-check: a trade that looks like early_momentum_v4 by its own
    # setup_context but has no episode_id at all. Authoritative membership
    # is the episodes table, so this trade was never a funnel candidate --
    # its existence is itself the violation.
    for t in dataset.trades:
        if t.episode_id is None and t.setup_context_strategy == EXPECTED_SETUP_CONTEXT_STRATEGY:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="v4_trade_without_episode",
                    detail=(
                        "trade declares early_momentum_v4 via setup_context but has no episode_id"
                    ),
                    episode_id=None,
                    trade_id=t.trade_id,
                )
            )

    # Step 5: exactly one trade-leg per episode.
    with_expected_trade_count: list[EpisodeRow] = []
    for e in episodes:
        leg_count = len(trades_by_episode.get(e.episode_id, []))
        opened_or_closed = e.status in ("opened", "closed")
        if opened_or_closed and leg_count == 0:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="episode_opened_without_trade",
                    detail="episode status is opened/closed but no trade row references it",
                    episode_id=e.episode_id,
                    trade_id=None,
                )
            )
        elif leg_count > 1:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="multiple_trades_per_episode",
                    detail=f"{leg_count} trade rows reference this episode, expected at most 1",
                    episode_id=e.episode_id,
                    trade_id=None,
                )
            )
        elif leg_count == 1:
            with_expected_trade_count.append(e)
        # leg_count == 0 and not opened/closed: a normal rejected/expired/
        # suppressed episode that never opened -- not a violation, simply
        # has no trade to carry forward.
    _step(
        "exactly_one_trade_leg",
        with_expected_trade_count,
        [e.episode_id for e in episodes if e not in with_expected_trade_count],
        "zero_or_multiple_trade_legs",
    )
    episodes = with_expected_trade_count

    def _the_trade(e: EpisodeRow) -> TradeRow:
        return trades_by_episode[e.episode_id][0]

    # Step 6: trade is genuinely paper.
    paper_ok = [e for e in episodes if _the_trade(e).is_paper]
    _step(
        "trade_is_paper",
        paper_ok,
        [e.episode_id for e in episodes if e not in paper_ok],
        "not_paper",
    )
    for e in episodes:
        if e not in paper_ok:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="not_paper",
                    detail="trade setup_context.paper is not true",
                    episode_id=e.episode_id,
                    trade_id=_the_trade(e).trade_id,
                )
            )
    episodes = paper_ok

    # Step 7: side is long.
    side_ok = [e for e in episodes if _the_trade(e).side == SIDE]
    _step(
        "side_is_long",
        side_ok,
        [e.episode_id for e in episodes if e not in side_ok],
        "unexpected_side",
    )
    for e in episodes:
        if e not in side_ok:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="unexpected_side",
                    detail=f"side={_the_trade(e).side!r}, expected {SIDE!r}",
                    episode_id=e.episode_id,
                    trade_id=_the_trade(e).trade_id,
                )
            )
    episodes = side_ok

    # Step 8: strategy metadata (the trade's own strategy_id FK) matches
    # the episode's.
    identity_match = [e for e in episodes if _the_trade(e).strategy_id == e.strategy_id]
    _step(
        "strategy_metadata_matches_episode",
        identity_match,
        [e.episode_id for e in episodes if e not in identity_match],
        "strategy_identity_mismatch",
    )
    for e in episodes:
        if e not in identity_match:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="strategy_identity_mismatch",
                    detail=(
                        f"trade.strategy_id={_the_trade(e).strategy_id} != "
                        f"episode.strategy_id={e.strategy_id}"
                    ),
                    episode_id=e.episode_id,
                    trade_id=_the_trade(e).trade_id,
                )
            )
    episodes = identity_match

    # Step 8b: route and strategy-label identity consistency (colleague
    # review -- only trade.strategy_id was previously checked, leaving a
    # misrouted or mislabeled trade able to reach formal economics).
    def _identity_violations(e: EpisodeRow) -> list[str]:
        t = _the_trade(e)
        problems: list[str] = []
        if t.exchange != e.exchange:
            problems.append(f"trade.exchange={t.exchange!r} != episode.exchange={e.exchange!r}")
        if e.execution_symbol is None:
            problems.append("episode.execution_symbol is missing for an opened/closed episode")
        elif t.symbol != e.execution_symbol:
            problems.append(
                f"trade.symbol={t.symbol!r} != episode.execution_symbol={e.execution_symbol!r}"
            )
        if t.setup_context_strategy != EXPECTED_SETUP_CONTEXT_STRATEGY:
            problems.append(
                f"trade.setup_context.strategy={t.setup_context_strategy!r} != "
                f"{EXPECTED_SETUP_CONTEXT_STRATEGY!r}"
            )
        expected_key = f"{e.episode_id}:entry:base"
        if t.entry_idempotency_key != expected_key:
            problems.append(
                f"trade.entry_idempotency_key={t.entry_idempotency_key!r} != {expected_key!r}"
            )
        return problems

    route_identity_ok: list[EpisodeRow] = []
    for e in episodes:
        problems = _identity_violations(e)
        if problems:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="route_or_strategy_identity_mismatch",
                    detail="; ".join(problems),
                    episode_id=e.episode_id,
                    trade_id=_the_trade(e).trade_id,
                )
            )
        else:
            route_identity_ok.append(e)
    _step(
        "route_and_strategy_identity_consistent",
        route_identity_ok,
        [e.episode_id for e in episodes if e not in route_identity_ok],
        "route_or_strategy_identity_mismatch",
    )
    episodes = route_identity_ok

    # Temporal sanity (colleague correction #2), attached at this point so
    # every episode still in play has a resolved single trade to check.
    temporally_sane: list[EpisodeRow] = []
    for e in episodes:
        t = _the_trade(e)
        delta = (t.entry_at - e.armed_at).total_seconds()
        violations_here: list[str] = []
        if delta < -ENTRY_ARMED_CLOCK_TOLERANCE_SECONDS:
            violations_here.append(f"entry_at precedes armed_at by {-delta:.1f}s")
        if e.expires_at <= e.armed_at:
            violations_here.append("expires_at <= armed_at")
        if e.claimed_at is not None and (e.claimed_at - e.armed_at).total_seconds() < (
            -ENTRY_ARMED_CLOCK_TOLERANCE_SECONDS
        ):
            violations_here.append("claimed_at precedes armed_at beyond tolerance")
        if t.exit_at is not None and t.exit_at < t.entry_at:
            violations_here.append("exit_at precedes entry_at")
        if t.status in ("closed",) and e.claim_attempts < 1:
            violations_here.append("opened trade has claim_attempts < 1")
        if violations_here:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="temporal_inconsistency",
                    detail="; ".join(violations_here),
                    episode_id=e.episode_id,
                    trade_id=t.trade_id,
                )
            )
        else:
            temporally_sane.append(e)
    _step(
        "temporal_sanity",
        temporally_sane,
        [e.episode_id for e in episodes if e not in temporally_sane],
        "temporal_inconsistency",
    )
    episodes = temporally_sane

    # Step 9: trade is closed and its outcome is mature.
    def _maturity_deadline(t: TradeRow) -> datetime:
        return t.entry_at + timedelta(seconds=COHORT_MATURITY_BUFFER_SECONDS)

    closed_mature: list[EpisodeRow] = []
    for e in episodes:
        t = _the_trade(e)
        if t.status == "closed" and t.exit_at is not None:
            closed_mature.append(e)
        elif t.status == "cancelled":
            continue  # administratively cancelled -- explained, not a violation
        elif t.status == "open" and dataset.db_snapshot_at < _maturity_deadline(t):
            continue  # still legitimately running within its own horizon
        else:
            # open, but past its own maturity horizon -- exactly the
            # "stranded open position" hard gate.
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="open_past_maturity_horizon",
                    detail=(
                        f"trade still open at snapshot {dataset.db_snapshot_at.isoformat()}, "
                        f"past its maturity deadline {_maturity_deadline(t).isoformat()}"
                    ),
                    episode_id=e.episode_id,
                    trade_id=t.trade_id,
                )
            )
    _step(
        "closed_and_mature",
        closed_mature,
        [e.episode_id for e in episodes if e not in closed_mature],
        "still_open_or_cancelled",
    )
    episodes = closed_mature

    # Step 10: accounting_version matches the frozen contract.
    accounting_version_ok = [
        e for e in episodes if _the_trade(e).accounting_version == ACCOUNTING_VERSION
    ]
    _step(
        "accounting_version_matches_contract",
        accounting_version_ok,
        [e.episode_id for e in episodes if e not in accounting_version_ok],
        "accounting_version_mismatch",
    )
    for e in episodes:
        if e not in accounting_version_ok:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="accounting_version_mismatch",
                    detail=(
                        f"accounting_version={_the_trade(e).accounting_version!r}, "
                        f"expected {ACCOUNTING_VERSION!r}"
                    ),
                    episode_id=e.episode_id,
                    trade_id=_the_trade(e).trade_id,
                )
            )
    episodes = accounting_version_ok

    # Step 11: accounting_status is complete.
    accounting_complete = [e for e in episodes if _the_trade(e).accounting_status == "complete"]
    _step(
        "accounting_status_complete",
        accounting_complete,
        [e.episode_id for e in episodes if e not in accounting_complete],
        "incomplete_accounting",
    )
    for e in episodes:
        if e not in accounting_complete:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="incomplete_accounting_on_closed_trade",
                    detail=(
                        f"accounting_status={_the_trade(e).accounting_status!r}, "
                        f"error={_the_trade(e).accounting_error!r}"
                    ),
                    episode_id=e.episode_id,
                    trade_id=_the_trade(e).trade_id,
                )
            )
    episodes = accounting_complete

    # Step 12: gross/net PnL, fees, funding, slippage are all populated.
    required_populated: list[EpisodeRow] = []
    for e in episodes:
        t = _the_trade(e)
        missing = [
            name
            for name, value in (
                ("gross_pnl_usd", t.gross_pnl_usd),
                ("gross_pnl_pct", t.gross_pnl_pct),
                ("net_pnl_usd", t.net_pnl_usd),
                ("net_pnl_pct", t.net_pnl_pct),
                ("fees_usd", t.fees_usd),
                ("funding_usd", t.funding_usd),
                ("slippage_usd", t.slippage_usd),
            )
            if value is None
        ]
        if missing:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="missing_required_accounting_field",
                    detail=f"accounting_status=complete but missing: {', '.join(missing)}",
                    episode_id=e.episode_id,
                    trade_id=t.trade_id,
                )
            )
        else:
            required_populated.append(e)
    _step(
        "required_pnl_and_cost_fields_populated",
        required_populated,
        [e.episode_id for e in episodes if e not in required_populated],
        "missing_required_accounting_field",
    )
    episodes = required_populated

    # Every trade with a "complete" accounting_status but net_pnl_usd
    # present *despite* an earlier-flagged incomplete-accounting row is
    # already excluded by step 11 -- explicitly re-check the inverse
    # (incomplete accounting somehow still carrying a PnL number) across
    # the whole dataset, independent of the funnel's own narrowing, since
    # that inconsistency could exist on a row the funnel already dropped
    # for an unrelated reason.
    for t in dataset.trades:
        if t.accounting_status != "complete" and t.net_pnl_usd is not None:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="pnl_present_despite_incomplete_accounting",
                    detail=f"accounting_status={t.accounting_status!r} but net_pnl_usd is set",
                    episode_id=t.episode_id,
                    trade_id=t.trade_id,
                )
            )

    # Step 14: accounting arithmetic actually reconciles (colleague review --
    # step 13 only checked that these fields were non-None, never that
    # they're mutually consistent; a corrupted or miscomputed row with
    # implausible numbers would otherwise sail through as "complete").
    # Tolerance accounts for Numeric(18,4)/Numeric(10,4) DB storage rounding
    # plus float division error, not for genuine miscalculation.
    def _accounting_reconciliation_problems(t: TradeRow) -> list[str]:
        exit_price, slippage_usd, gross_pnl_usd, gross_pnl_pct, net_pnl_usd, net_pnl_pct = (
            t.exit_price,
            t.slippage_usd,
            t.gross_pnl_usd,
            t.gross_pnl_pct,
            t.net_pnl_usd,
            t.net_pnl_pct,
        )
        values = (
            t.size_usd,
            t.leverage,
            t.entry_price,
            exit_price,
            t.fees_usd,
            t.funding_usd,
            slippage_usd,
            gross_pnl_usd,
            gross_pnl_pct,
            net_pnl_usd,
            net_pnl_pct,
        )
        if any(v is None or not math.isfinite(v) for v in values):
            return ["a required accounting value is missing, NaN, or infinite"]
        # Narrowed by the check above -- every optional field is now a
        # concrete finite float, not None.
        assert exit_price is not None
        assert slippage_usd is not None
        assert gross_pnl_usd is not None
        assert gross_pnl_pct is not None
        assert net_pnl_usd is not None
        assert net_pnl_pct is not None

        if t.size_usd <= 0 or t.leverage <= 0 or t.entry_price <= 0 or exit_price <= 0:
            # A non-positive size makes every ratio below (division by
            # size_usd) meaningless or a ZeroDivisionError outright -- this
            # is already a hard problem on its own, report it and stop.
            return [
                f"non-positive size_usd={t.size_usd}/leverage={t.leverage}/"
                f"entry_price={t.entry_price}/exit_price={exit_price}"
            ]

        problems: list[str] = []
        expected_gross_usd = t.size_usd * gross_pnl_pct / 100
        if not math.isclose(gross_pnl_usd, expected_gross_usd, rel_tol=1e-3, abs_tol=0.05):
            problems.append(
                f"gross_pnl_usd={gross_pnl_usd:.4f} inconsistent with "
                f"size_usd*gross_pnl_pct/100={expected_gross_usd:.4f}"
            )

        expected_net_usd = gross_pnl_usd - t.fees_usd - t.funding_usd - slippage_usd
        if not math.isclose(net_pnl_usd, expected_net_usd, rel_tol=1e-3, abs_tol=0.05):
            problems.append(
                f"net_pnl_usd={net_pnl_usd:.4f} inconsistent with "
                f"gross-fees-funding-slippage={expected_net_usd:.4f}"
            )

        expected_net_pct = net_pnl_usd / t.size_usd * 100
        if not math.isclose(net_pnl_pct, expected_net_pct, rel_tol=1e-3, abs_tol=0.05):
            problems.append(
                f"net_pnl_pct={net_pnl_pct:.4f} inconsistent with "
                f"net_pnl_usd/size_usd*100={expected_net_pct:.4f}"
            )
        return problems

    accounting_reconciled: list[EpisodeRow] = []
    for e in episodes:
        t = _the_trade(e)
        problems = _accounting_reconciliation_problems(t)
        if problems:
            row_violations.append(
                IntegrityViolation(
                    severity="row",
                    code="accounting_arithmetic_inconsistent",
                    detail="; ".join(problems),
                    episode_id=e.episode_id,
                    trade_id=t.trade_id,
                )
            )
        else:
            accounting_reconciled.append(e)
    _step(
        "accounting_arithmetic_reconciled",
        accounting_reconciled,
        [e.episode_id for e in episodes if e not in accounting_reconciled],
        "accounting_arithmetic_inconsistent",
    )
    episodes = accounting_reconciled

    # Step 15: final comparable set.
    comparable = tuple(
        ComparableTrade(
            trade_id=(t := _the_trade(e)).trade_id,
            episode_id=e.episode_id,
            cluster_key=e.cluster_key,
            native_market_id=e.native_market_id,
            source_exchange=e.source_exchange,
            exchange=e.exchange,
            entry_at=t.entry_at,
            exit_at=t.exit_at,  # type: ignore[arg-type]
            entry_price=t.entry_price,
            exit_price=t.exit_price,  # type: ignore[arg-type]
            size_usd=t.size_usd,
            leverage=t.leverage,
            fees_usd=t.fees_usd,
            funding_usd=t.funding_usd,
            slippage_usd=t.slippage_usd,  # type: ignore[arg-type]
            gross_pnl_usd=t.gross_pnl_usd,  # type: ignore[arg-type]
            gross_pnl_pct=t.gross_pnl_pct,  # type: ignore[arg-type]
            net_pnl_usd=t.net_pnl_usd,  # type: ignore[arg-type]
            net_pnl_pct=t.net_pnl_pct,  # type: ignore[arg-type]
            exit_reason=parse_exit_reason(t.notes),
            entry_ask_impact_bps=t.entry_ask_impact_bps,
            entry_bid_impact_bps=t.entry_bid_impact_bps,
        )
        for e in episodes
    )
    _step("final_comparable_set", list(comparable), [], None)

    return FunnelResult(
        steps=tuple(steps),
        comparable=comparable,
        cohort_violations=tuple(cohort_violations),
        row_violations=tuple(row_violations),
    )


# --- Time-key helpers -----------------------------------------------------


def utc_day_key(dt: datetime) -> str:
    return dt.astimezone(UTC).date().isoformat()


def utc_week_key(dt: datetime) -> str:
    year, week, _ = dt.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


# --- Economics --------------------------------------------------------


@dataclass(frozen=True)
class ReturnStats:
    mean_pct: float | None
    median_pct: float | None
    p05_pct: float | None
    p25_pct: float | None
    p50_pct: float | None
    p75_pct: float | None
    p95_pct: float | None
    worst_pct: float | None


def _return_stats(values: list[float]) -> ReturnStats:
    if not values:
        return ReturnStats(None, None, None, None, None, None, None, None)
    ordered = tuple(sorted(values))
    return ReturnStats(
        mean_pct=fmean(values),
        median_pct=median(values),
        p05_pct=_percentile(ordered, 0.05),
        p25_pct=_percentile(ordered, 0.25),
        p50_pct=_percentile(ordered, 0.50),
        p75_pct=_percentile(ordered, 0.75),
        p95_pct=_percentile(ordered, 0.95),
        worst_pct=ordered[0],
    )


def _percentile(sorted_values: tuple[float, ...], probability: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    return sorted_values[lower_index] * (1 - weight) + sorted_values[upper_index] * weight


@dataclass(frozen=True)
class EquityPoint:
    exit_at: datetime
    trade_id: int
    net_pnl_usd: float
    cumulative_net_pnl_usd: float


@dataclass(frozen=True)
class GroupedPnl:
    key: str
    trades: int
    net_pnl_usd: float
    mean_net_return_pct: float | None


def _max_drawdown_usd(ordered_pnl_usd: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in ordered_pnl_usd:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown


def _worst_losing_streak(ordered_pnl_usd: list[float]) -> int:
    worst = 0
    current = 0
    for pnl in ordered_pnl_usd:
        if pnl < 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def _grouped(trades: tuple[ComparableTrade, ...], key: Any) -> tuple[GroupedPnl, ...]:
    buckets: dict[str, list[ComparableTrade]] = defaultdict(list)
    for t in trades:
        buckets[key(t)].append(t)
    rows = []
    for bucket_key, bucket_trades in buckets.items():
        returns = [t.net_pnl_pct for t in bucket_trades]
        rows.append(
            GroupedPnl(
                key=bucket_key,
                trades=len(bucket_trades),
                net_pnl_usd=sum(t.net_pnl_usd for t in bucket_trades),
                mean_net_return_pct=fmean(returns) if returns else None,
            )
        )
    return tuple(sorted(rows, key=lambda r: r.key))


@dataclass(frozen=True)
class EconomicsSummary:
    closed_trades: int
    wins: int
    losses: int
    win_rate_pct: float | None
    gross_return_on_notional: ReturnStats
    net_return_on_notional: ReturnStats
    gross_return_on_margin: ReturnStats
    net_return_on_margin: ReturnStats
    total_gross_pnl_usd: float
    total_net_pnl_usd: float
    total_fees_usd: float
    total_funding_usd: float
    total_slippage_usd: float
    profit_factor: float | None
    worst_trade_net_pnl_usd: float | None
    worst_losing_streak: int
    max_drawdown_usd: float
    equity_curve: tuple[EquityPoint, ...]
    by_cluster: tuple[GroupedPnl, ...]
    by_utc_day: tuple[GroupedPnl, ...]
    by_utc_week: tuple[GroupedPnl, ...]
    by_source_exchange: tuple[GroupedPnl, ...]
    by_exit_reason: tuple[GroupedPnl, ...]


def compute_economics(comparable: tuple[ComparableTrade, ...]) -> EconomicsSummary:
    ordered = tuple(sorted(comparable, key=lambda t: t.exit_at))
    wins = sum(1 for t in ordered if t.net_pnl_usd > 0)
    losses = sum(1 for t in ordered if t.net_pnl_usd < 0)

    def _margin_pct(t: ComparableTrade, pnl_usd: float) -> float:
        margin = t.size_usd / t.leverage if t.leverage else t.size_usd
        return pnl_usd / margin * 100 if margin else 0.0

    gross_notional = [t.gross_pnl_pct for t in ordered]
    net_notional = [t.net_pnl_pct for t in ordered]
    gross_margin = [_margin_pct(t, t.gross_pnl_usd) for t in ordered]
    net_margin = [_margin_pct(t, t.net_pnl_usd) for t in ordered]

    ordered_pnl_usd = [t.net_pnl_usd for t in ordered]
    equity = 0.0
    equity_curve = []
    for t in ordered:
        equity += t.net_pnl_usd
        equity_curve.append(
            EquityPoint(
                exit_at=t.exit_at,
                trade_id=t.trade_id,
                net_pnl_usd=t.net_pnl_usd,
                cumulative_net_pnl_usd=equity,
            )
        )

    return EconomicsSummary(
        closed_trades=len(ordered),
        wins=wins,
        losses=losses,
        win_rate_pct=(wins / len(ordered) * 100) if ordered else None,
        gross_return_on_notional=_return_stats(gross_notional),
        net_return_on_notional=_return_stats(net_notional),
        gross_return_on_margin=_return_stats(gross_margin),
        net_return_on_margin=_return_stats(net_margin),
        total_gross_pnl_usd=sum(t.gross_pnl_usd for t in ordered),
        total_net_pnl_usd=sum(t.net_pnl_usd for t in ordered),
        total_fees_usd=sum(t.fees_usd for t in ordered),
        total_funding_usd=sum(t.funding_usd for t in ordered),
        total_slippage_usd=sum(t.slippage_usd for t in ordered),
        profit_factor=profit_factor(net_notional) if net_notional else None,
        worst_trade_net_pnl_usd=min(ordered_pnl_usd) if ordered_pnl_usd else None,
        worst_losing_streak=_worst_losing_streak(ordered_pnl_usd),
        max_drawdown_usd=_max_drawdown_usd(ordered_pnl_usd) if ordered_pnl_usd else 0.0,
        equity_curve=tuple(equity_curve),
        by_cluster=_grouped(ordered, lambda t: t.cluster_key),
        by_utc_day=_grouped(ordered, lambda t: utc_day_key(t.exit_at)),
        by_utc_week=_grouped(ordered, lambda t: utc_week_key(t.exit_at)),
        by_source_exchange=_grouped(ordered, lambda t: t.source_exchange),
        by_exit_reason=_grouped(ordered, lambda t: t.exit_reason),
    )


# --- Concurrency and entry waves ---------------------------------------


@dataclass(frozen=True)
class WaveSummary:
    wave_id: int
    start_at: datetime
    end_at: datetime
    trades: int
    net_pnl_usd: float


@dataclass(frozen=True)
class ConcurrencySummary:
    max_concurrent_positions: int
    time_weighted_mean_concurrency: float
    p95_concurrency: float
    max_deployed_notional_usd: float
    mean_deployed_notional_usd: float
    max_required_margin_usd: float
    mean_required_margin_usd: float
    waves: tuple[WaveSummary, ...]
    top1_cluster_pnl_share_pct: float | None
    top5_cluster_pnl_share_pct: float | None
    best_utc_day_pnl_share_pct: float | None
    best_utc_week_pnl_share_pct: float | None


def _position_segments(
    trades: tuple[ComparableTrade, ...],
) -> list[tuple[float, int, float, float]]:
    """Sweep-line over entry/exit instants. Returns one tuple per interval
    between consecutive events: (duration_seconds, concurrent_count,
    deployed_notional_usd, required_margin_usd) as of that interval.
    Closes are processed before opens at an identical timestamp so an
    instantaneous exit+entry never double-counts as 2 concurrent."""
    if not trades:
        return []
    events: list[tuple[datetime, int, float, float]] = []
    for t in trades:
        margin = t.size_usd / t.leverage if t.leverage else t.size_usd
        events.append((t.entry_at, 1, t.size_usd, margin))
        events.append((t.exit_at, -1, -t.size_usd, -margin))
    events.sort(key=lambda e: (e[0], e[1]))

    count = 0
    notional = 0.0
    margin_total = 0.0
    prev_time = events[0][0]
    segments: list[tuple[float, int, float, float]] = []
    for time, delta, notional_delta, margin_delta in events:
        duration = (time - prev_time).total_seconds()
        if duration > 0:
            segments.append((duration, count, notional, margin_total))
        count += delta
        notional += notional_delta
        margin_total += margin_delta
        prev_time = time
    return segments


def _weighted_percentile(
    segments: list[tuple[float, int, float, float]], index: int, probability: float
) -> float:
    total = sum(seg[0] for seg in segments)
    if total <= 0:
        return 0.0
    ordered = sorted(segments, key=lambda seg: seg[index])
    threshold = probability * total
    cumulative = 0.0
    for seg in ordered:
        cumulative += seg[0]
        if cumulative >= threshold:
            return float(seg[index])
    return float(ordered[-1][index])


def _time_weighted_mean(segments: list[tuple[float, int, float, float]], index: int) -> float:
    total = sum(seg[0] for seg in segments)
    if total <= 0:
        return 0.0
    return sum(seg[index] * seg[0] for seg in segments) / total


def _build_waves(trades: tuple[ComparableTrade, ...]) -> tuple[WaveSummary, ...]:
    ordered = sorted(trades, key=lambda t: t.entry_at)
    if not ordered:
        return ()
    waves: list[list[ComparableTrade]] = [[ordered[0]]]
    for t in ordered[1:]:
        gap = (t.entry_at - waves[-1][-1].entry_at).total_seconds()
        if gap <= ENTRY_WAVE_GAP_SECONDS:
            waves[-1].append(t)
        else:
            waves.append([t])
    return tuple(
        WaveSummary(
            wave_id=i,
            start_at=wave[0].entry_at,
            end_at=wave[-1].entry_at,
            trades=len(wave),
            net_pnl_usd=sum(t.net_pnl_usd for t in wave),
        )
        for i, wave in enumerate(waves, start=1)
    )


def _pnl_share_pct(numerator: float, total: float) -> float | None:
    if total == 0:
        return None
    return numerator / total * 100


def compute_concurrency(comparable: tuple[ComparableTrade, ...]) -> ConcurrencySummary:
    segments = _position_segments(comparable)
    max_concurrent = max((seg[1] for seg in segments), default=0)
    time_weighted_mean = _time_weighted_mean(segments, 1)
    p95 = _weighted_percentile(segments, 1, 0.95) if segments else 0.0
    max_notional = max((seg[2] for seg in segments), default=0.0)
    mean_notional = _time_weighted_mean(segments, 2)
    max_margin = max((seg[3] for seg in segments), default=0.0)
    mean_margin = _time_weighted_mean(segments, 3)

    total_pnl = sum(t.net_pnl_usd for t in comparable)
    by_cluster_pnl: dict[str, float] = defaultdict(float)
    for t in comparable:
        by_cluster_pnl[t.cluster_key] += t.net_pnl_usd
    ranked_clusters = sorted(by_cluster_pnl.values(), reverse=True)
    top1 = sum(ranked_clusters[:1])
    top5 = sum(ranked_clusters[:5])

    by_day_pnl: dict[str, float] = defaultdict(float)
    for t in comparable:
        by_day_pnl[utc_day_key(t.exit_at)] += t.net_pnl_usd
    best_day_pnl = max(by_day_pnl.values(), default=0.0)

    by_week_pnl: dict[str, float] = defaultdict(float)
    for t in comparable:
        by_week_pnl[utc_week_key(t.exit_at)] += t.net_pnl_usd
    best_week_pnl = max(by_week_pnl.values(), default=0.0)

    return ConcurrencySummary(
        max_concurrent_positions=max_concurrent,
        time_weighted_mean_concurrency=time_weighted_mean,
        p95_concurrency=p95,
        max_deployed_notional_usd=max_notional,
        mean_deployed_notional_usd=mean_notional,
        max_required_margin_usd=max_margin,
        mean_required_margin_usd=mean_margin,
        waves=_build_waves(comparable),
        top1_cluster_pnl_share_pct=_pnl_share_pct(top1, total_pnl),
        top5_cluster_pnl_share_pct=_pnl_share_pct(top5, total_pnl),
        best_utc_day_pnl_share_pct=_pnl_share_pct(best_day_pnl, total_pnl),
        best_utc_week_pnl_share_pct=_pnl_share_pct(best_week_pnl, total_pnl),
    )


# --- Robustness ---------------------------------------------------------


@dataclass(frozen=True)
class RobustnessSummary:
    leave_best_asset_out_mean_net_return_pct: float | None
    leave_one_week_out: tuple[tuple[str, float], ...]
    mean_net_return_excluding_best_utc_day_pct: float | None
    block_bootstrap: BootstrapEstimate | None
    bootstrap_iterations: int
    bootstrap_seed: int
    confidence_level: float
    caveat: str


_ROBUSTNESS_CAVEAT = (
    "At the minimum evidence floor (100 closed / 30 clusters / 4 UTC weeks), "
    "block-bootstrap-by-day and leave-one-week-out draw from very few blocks "
    "(as few as ~28 days / 4 weeks) -- confidence intervals and leave-one-out "
    "results are correspondingly wide/weak at exactly the floor. Treat a "
    "narrow PASS at the floor as provisional; more weeks of evidence "
    "meaningfully tighten these numbers."
)


def compute_robustness(comparable: tuple[ComparableTrade, ...]) -> RobustnessSummary:
    if not comparable:
        return RobustnessSummary(
            None, (), None, None, 0, 0, BOOTSTRAP_CONFIDENCE_LEVEL, _ROBUSTNESS_CAVEAT
        )

    pnl_by_cluster: dict[str, float] = defaultdict(float)
    for t in comparable:
        pnl_by_cluster[t.cluster_key] += t.net_pnl_usd
    best_cluster = max(pnl_by_cluster, key=lambda k: pnl_by_cluster[k])
    remaining_returns = [t.net_pnl_pct for t in comparable if t.cluster_key != best_cluster]
    leave_best_asset_out = fmean(remaining_returns) if remaining_returns else None

    week_observations = tuple(
        ClusterObservation(utc_week_key(t.exit_at), t.net_pnl_pct) for t in comparable
    )
    week_keys = tuple(sorted({obs.cluster_key for obs in week_observations}))
    leave_one_week_out = (
        leave_one_cluster_out_means(week_observations, week_keys) if len(week_keys) >= 2 else ()
    )

    by_day_pnl: dict[str, float] = defaultdict(float)
    for t in comparable:
        by_day_pnl[utc_day_key(t.exit_at)] += t.net_pnl_usd
    best_day = max(by_day_pnl, key=lambda k: by_day_pnl[k]) if by_day_pnl else None
    excluding_best_day_returns = [
        t.net_pnl_pct for t in comparable if utc_day_key(t.exit_at) != best_day
    ]
    excluding_best_day = fmean(excluding_best_day_returns) if excluding_best_day_returns else None

    day_observations = tuple(
        ClusterObservation(utc_day_key(t.exit_at), t.net_pnl_pct) for t in comparable
    )
    # Only the estimate (point/lower/upper/episodes/clusters) is kept, never
    # the raw .samples array (10,000 floats by default) -- nothing past
    # this function needs the individual draws, and carrying them into the
    # report would bloat the JSON output for no reason.
    bootstrap = (
        cluster_bootstrap_mean(
            day_observations,
            iterations=DEFAULT_BOOTSTRAP_ITERATIONS,
            seed=DEFAULT_BOOTSTRAP_SEED,
            confidence_level=BOOTSTRAP_CONFIDENCE_LEVEL,
        ).estimate
        if day_observations
        else None
    )

    return RobustnessSummary(
        leave_best_asset_out_mean_net_return_pct=leave_best_asset_out,
        leave_one_week_out=leave_one_week_out,
        mean_net_return_excluding_best_utc_day_pct=excluding_best_day,
        block_bootstrap=bootstrap,
        bootstrap_iterations=DEFAULT_BOOTSTRAP_ITERATIONS,
        bootstrap_seed=DEFAULT_BOOTSTRAP_SEED,
        confidence_level=BOOTSTRAP_CONFIDENCE_LEVEL,
        caveat=_ROBUSTNESS_CAVEAT,
    )


# --- Capacity evidence ----------------------------------------------------


@dataclass(frozen=True)
class CapacitySummary:
    comparable_trades: int
    trades_with_entry_impact_data: int
    entry_impact_coverage_pct: float
    trades_with_exit_liquidity_observation: int
    exit_liquidity_coverage_pct: float
    mean_entry_ask_impact_bps: float | None
    p95_entry_ask_impact_bps: float | None
    mean_exit_bid_impact_bps: float | None
    p95_exit_bid_impact_bps: float | None
    mean_exit_spread_bps: float | None
    observed_entry_notional_usd: tuple[float, ...]
    caveat: str


_CAPACITY_CAVEAT = (
    "Derived only from liquidity actually recorded at the traded size "
    "(observed_entry_notional_usd); never extrapolated to a larger notional "
    "the order book was not measured at."
)


def compute_capacity(
    comparable: tuple[ComparableTrade, ...],
    exit_liquidity_by_trade: dict[int, ExitLiquidityRow],
) -> CapacitySummary:
    entry_impacts = [
        t.entry_ask_impact_bps for t in comparable if t.entry_ask_impact_bps is not None
    ]
    exit_rows = [
        exit_liquidity_by_trade[t.trade_id]
        for t in comparable
        if t.trade_id in exit_liquidity_by_trade
    ]
    exit_bid_impacts = [r.bid_impact_bps for r in exit_rows if r.bid_impact_bps is not None]
    exit_spreads = [r.spread_bps for r in exit_rows if r.spread_bps is not None]
    total = len(comparable)

    def _pct(count: int) -> float:
        return count / total * 100 if total else 0.0

    def _p95(values: list[float]) -> float | None:
        return _percentile(tuple(sorted(values)), 0.95) if values else None

    return CapacitySummary(
        comparable_trades=total,
        trades_with_entry_impact_data=len(entry_impacts),
        entry_impact_coverage_pct=_pct(len(entry_impacts)),
        trades_with_exit_liquidity_observation=len(exit_rows),
        exit_liquidity_coverage_pct=_pct(len(exit_rows)),
        mean_entry_ask_impact_bps=fmean(entry_impacts) if entry_impacts else None,
        p95_entry_ask_impact_bps=_p95(entry_impacts),
        mean_exit_bid_impact_bps=fmean(exit_bid_impacts) if exit_bid_impacts else None,
        p95_exit_bid_impact_bps=_p95(exit_bid_impacts),
        mean_exit_spread_bps=fmean(exit_spreads) if exit_spreads else None,
        observed_entry_notional_usd=tuple(sorted({t.size_usd for t in comparable})),
        caveat=_CAPACITY_CAVEAT,
    )


# --- Verdict --------------------------------------------------------------

VERDICT_INVALID_INTEGRITY = "invalid_integrity"
VERDICT_INSUFFICIENT_DATA = "insufficient_data"
VERDICT_FAIL = "fail"
VERDICT_PASS_LIVE_MICRO_CANDIDATE = "pass_live_micro_candidate"  # noqa: S105


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reasons: tuple[str, ...]
    is_interim_checkpoint: bool


def evaluate_verdict(
    *,
    funnel: FunnelResult,
    economics: EconomicsSummary,
    robustness: RobustnessSummary,
) -> Verdict:
    """Row-level integrity violations block formal PASS entirely, even
    though descriptive economics is still computed on the clean subset
    (colleague correction #3): a single unexplained anomaly among hundreds
    of trades can be a symptom of a broader problem, and silently excluding
    it while still authorizing real money is the wrong default. The report
    must be re-run after remediation, never auto-recovered by exclusion."""
    if funnel.cohort_violations:
        return Verdict(
            verdict=VERDICT_INVALID_INTEGRITY,
            reasons=tuple(sorted({v.code for v in funnel.cohort_violations})),
            is_interim_checkpoint=False,
        )
    if funnel.row_violations:
        return Verdict(
            verdict=VERDICT_INVALID_INTEGRITY,
            reasons=tuple(sorted({v.code for v in funnel.row_violations})),
            is_interim_checkpoint=False,
        )
    # Self-check: comparable trades must always have complete accounting by
    # funnel construction. This can never fire today; it exists to catch a
    # future funnel regression rather than to describe new evidence.
    if any(t.net_pnl_usd is None for t in funnel.comparable):
        return Verdict(
            VERDICT_INVALID_INTEGRITY,
            ("internal_error_incomplete_accounting_in_comparable_set",),
            False,
        )

    closed = economics.closed_trades
    distinct_clusters = len({t.cluster_key for t in funnel.comparable})
    distinct_weeks = len({utc_week_key(t.exit_at) for t in funnel.comparable})
    is_interim = INTERIM_CHECKPOINT_MIN_CLOSED_TRADES <= closed < MIN_CLOSED_TRADES

    meets_floor = (
        closed >= MIN_CLOSED_TRADES
        and distinct_clusters >= MIN_DISTINCT_CLUSTERS
        and distinct_weeks >= MIN_DISTINCT_UTC_WEEKS
    )
    if not meets_floor:
        reasons = []
        if closed < MIN_CLOSED_TRADES:
            reasons.append(f"closed_trades_{closed}_below_{MIN_CLOSED_TRADES}")
        if distinct_clusters < MIN_DISTINCT_CLUSTERS:
            reasons.append(f"distinct_clusters_{distinct_clusters}_below_{MIN_DISTINCT_CLUSTERS}")
        if distinct_weeks < MIN_DISTINCT_UTC_WEEKS:
            reasons.append(f"distinct_utc_weeks_{distinct_weeks}_below_{MIN_DISTINCT_UTC_WEEKS}")
        return Verdict(VERDICT_INSUFFICIENT_DATA, tuple(reasons), is_interim)

    fail_reasons: list[str] = []
    mean_net = economics.net_return_on_notional.mean_pct
    if mean_net is None or mean_net <= 0:
        fail_reasons.append("mean_net_return_not_positive")
    if economics.total_net_pnl_usd <= 0:
        fail_reasons.append("total_net_pnl_not_positive")
    if economics.profit_factor is None or economics.profit_factor < MIN_PROFIT_FACTOR:
        fail_reasons.append("profit_factor_below_threshold")
    if robustness.block_bootstrap is None or robustness.block_bootstrap.lower_bound <= 0:
        fail_reasons.append("bootstrap_lower_bound_not_positive")
    if (
        robustness.leave_best_asset_out_mean_net_return_pct is None
        or robustness.leave_best_asset_out_mean_net_return_pct <= 0
    ):
        fail_reasons.append("leave_best_asset_out_not_positive")
    if not robustness.leave_one_week_out or any(v <= 0 for _, v in robustness.leave_one_week_out):
        fail_reasons.append("leave_one_week_out_not_all_positive")

    if fail_reasons:
        return Verdict(VERDICT_FAIL, tuple(fail_reasons), is_interim)
    return Verdict(VERDICT_PASS_LIVE_MICRO_CANDIDATE, (), is_interim)


# --- Legacy version context (descriptive only, never mixed into v4) -----


@dataclass(frozen=True)
class LegacyContextRow:
    """v1/v2/v3 descriptive context, kept in a strictly separate table from
    the v4 formal cohort -- never combined into the same PnL sum. v1 rows
    (pre-episode-lifecycle) are shown as an integrity appendix, not
    economics: see fix/legacy-paper-orphan-reconciliation-v1 for why that
    generation's `open` rows cannot be trusted at face value."""

    setup_context_strategy: str
    total_trades: int
    closed_trades: int
    cancelled_trades: int
    open_trades: int
    complete_accounting_closed_trades: int
    total_net_pnl_usd_complete_only: float | None
