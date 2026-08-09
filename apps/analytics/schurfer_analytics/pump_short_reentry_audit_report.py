"""Measurement-only audit of the gap between actual paper re-entries and the
first-open-per-event assumption every virtual/backtest report makes.

Why this exists: `select_episode_decision`/`select_score_policy` in
`virtual_strategy.py` (and everything built on it — entry-challenger,
score-challenger, banded-price-extent, the failure-attribution report) only
ever look at the FIRST `opened`/`opened_dry_run` decision per `pump_event_id`.
The real execution service does not enforce "one entry per event" at all: its
only guard is a flat, per-`base` Redis cooldown (`trader:seen:{base}`, TTL
`COOLDOWN_SECONDS`, set atomically at the moment an `opened`/`opened_dry_run`
decision is durably written — see `apps/execution/schurfer_execution/
decisions.py` and `trader.py`). Because a `pump_events` episode can stay open
for days (it only closes after enough consecutive scanner misses), a token can
legitimately re-enter the SAME still-open event once its 24h cooldown expires.
This report quantifies how often that happens and how much it matters
economically. It does not change `virtual_strategy.py`, does not compute a
p-value, and cannot authorize a production strategy change — its only output
is a measured discrepancy plus a human-readable recommendation for a future,
separately-scoped PR.

Design decisions (fixed after four rounds of review before implementation):

1. Cooldown-invariant classification runs on a separate, broader
   "operational" row set — every distinct `opened`/`opened_dry_run` decision
   with a `pump_event_id` in scope, deduplicated by `decision_row_id`,
   regardless of trade status or accounting completeness (see
   `build_operational_rows`). An open position or an incomplete-accounting
   trade can still participate in a real cooldown violation; if this used the
   same funnel-filtered `comparable` set that economics uses, such a
   violation would disappear before it was ever classified. Economics
   (headline PnL, drawdown, event rollup) stays strictly on `comparable` —
   only fully-realized, accounting-complete trades ever contribute a dollar
   figure.

2. Funnel discipline over decision<->trade linkage, including one-to-many
   protection. `paper.open_paper()` writes the trade row BEFORE
   `write_decision()` records the decision (see `trader.py`), so a crash
   between those two calls can in principle leave an orphan trade with no
   matching decision. There is no FK between `app.trades` and
   `app.trade_decisions`: the only link is
   `trades.setup_context->>'decision_id' = trade_decisions.decision_id`, and
   nothing in the schema enforces that link being one-to-one. A
   `unique_trade_link` funnel step fails closed (excludes, does not
   arbitrarily pick one) whenever more than one trade row matches the same
   `decision_row_id` — such a duplicate would otherwise double-count PnL and
   fabricate a spurious near-zero-gap transition. Every exclusion (no trade,
   duplicate trade, no decision, still open/right-censored, incomplete or
   legacy accounting, identity mismatch) is counted in an explicit funnel
   step rather than silently dropped by a `WHERE` clause on a `LEFT JOIN`.
   The reverse-direction orphan-trade diagnostic is scoped to the same
   `paper=true` / `strategy_version` values read directly out of
   `setup_context` — it must never count an unrelated strategy's or a live
   trade's row.

3. Cooldown timing anchors on `trade_decisions.ts`, not `trades.entry_at`.
   `ts` is computed immediately before the same Lua call that sets the Redis
   seen-key (`decisions.py`'s `write_decision`), making it the closest
   durable timestamp to the actual atomic cooldown-start. `entry_at` (the
   simulated/real fill time) is reported alongside for reference
   (`seconds_since_previous_entry`, `seconds_since_previous_exit`) but is not
   the audit's cooldown clock.

4. Transitions are classified per pair of consecutive operational rows on
   the same `base`, not per whole-base label — a single token can show more
   than one kind of repeat in its lifetime:
   - `same_event_under_24h` / `cross_event_under_24h`: the cooldown invariant
     was violated (should not happen; flagged, not assumed impossible).
   - `same_event_after_24h`: a legitimate same-event repeat (e.g. TUT,
     `pump_event_id=3518`, 24h05m entry-to-entry / ~22h33m exit-to-entry).
   - `cross_event_after_24h`: two genuinely independent episodes.

5. Only two headline economics are computed, both on REAL trade data (never
   `VirtualTrade`/`simulate_decision`, which would compare a model against
   itself): `all_actual_trades` (everything that survives the funnel) and
   `actual_first_open_per_event` (one row per `pump_event_id`, the earliest
   by `ts` — the real-PnL analogue of what every existing virtual report
   already assumes). A third "stateful base+24h" reconstruction was
   considered and rejected as a headline comparison: replaying the same
   cooldown rule production already applied to its own already-filtered
   decision stream is close to a tautology, and it cannot recover the
   cooldown-suppressed attempts that were never durably recorded (`trader.py`
   `continue`s before writing a decision when the seen-key is set). It is
   kept only as a diagnostic invariant check (see `base_24h_invariant`).
   Drawdown on the equity curve orders by `exit_at` (when PnL is actually
   realized), not by decision/entry time — overlapping positions would
   otherwise misorder the running peak-to-trough. `comparable` therefore
   requires a non-null `exit_at` in addition to a non-null net PnL.

6. Reentry-opportunity denominators are explicitly bounded proxies, computed
   as of `filters.until` — never as of "now" (`generated_at`), which would
   let a historical run see event-closure information from after its own
   cutoff. An event's underlying `pump_events.closed_at` is clipped to
   `filters.until` before use: if it closed after the cutoff, or never
   closed, it is treated identically to "still open as of the cutoff."
   Whether the event stayed open >=24h past its first recorded open is an
   upper bound on "another entry was structurally possible" — it says
   nothing about whether the score conditions were ever met again, and
   cooldown-suppressed evaluation attempts are `historically_unobservable`
   by construction (never written). No decision is ever reconstructed or
   imputed to fill this gap.

7. The reproducibility fingerprint hashes the full pre-funnel dataset (every
   joined decision/trade/event row plus every orphan trade id), not just the
   post-funnel `comparable` set. Two runs whose `comparable` sets happen to
   match by coincidence but whose funnel exclusions or orphan counts differ
   (e.g. a code change that alters what gets excluded, without changing the
   final surviving rows) must never collide on the same fingerprint.

This report proposes but does not implement three possible fixes to the
first-open-per-event assumption (one-trade-per-event with a durable
event-level fence; a stateful base+24h-cooldown replay; re-entry modeled as
its own challenger layered on the existing baseline) — see the "Future fix
options" section of the rendered report and the corresponding ROADMAP.md
entry. Choosing between them is a deliberate, separate, human decision.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEvent, Trade, TradeDecision
from schurfer_performance.accounting import PAPER_ACCOUNTING_VERSION
from sqlalchemy import String, cast, literal, or_, select
from sqlalchemy.ext.asyncio import create_async_engine

from .episode_replay import CONFIRMATION_COHORT_START
from .outcome_repository import async_database_url
from .reporting import (
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    profit_factor,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.sql import Select

REENTRY_AUDIT_REPORT_VERSION = "pump_short_reentry_audit_report_v1"
REENTRY_AUDIT_DEFAULT_SINCE = CONFIRMATION_COHORT_START
REENTRY_AUDIT_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)

# Must track apps/execution/schurfer_execution/trader.py's _SEEN_TTL_TRADED.
# Analytics does not depend on the execution package, so this is a deliberate,
# commented duplication rather than a cross-package import.
COOLDOWN_SECONDS = 86400

OPEN_ACTIONS = ("opened", "opened_dry_run")


def normalize_symbol_base(symbol: str) -> str:
    """Extract the base asset from a ccxt unified symbol (e.g. 'TUT/USDT:USDT'
    -> 'TUT'). Splits on the first '/' only, which is safe for unicode tickers
    (e.g. the 龙虾 case) since ccxt always uses '/' as the base/quote
    separator regardless of the ticker's script. A symbol with no '/' is
    returned unchanged so the identity-consistency check downstream can flag
    the mismatch instead of raising."""
    return symbol.split("/", 1)[0]


@dataclass(frozen=True)
class ReentryAuditFilters:
    since: datetime
    until: datetime
    strategy_versions: tuple[str, ...]


@dataclass(frozen=True)
class _JoinedRow:
    decision_row_id: int
    decision_id: str | None
    ts: datetime
    base: str
    exchange: str
    pump_event_id: int | None
    strategy_version: str | None
    trade_id: int | None
    trade_symbol: str | None
    trade_exchange: str | None
    entry_at: datetime | None
    exit_at: datetime | None
    trade_status: str | None
    accounting_version: str | None
    accounting_status: str | None
    net_pnl_usd: float | None
    net_pnl_pct: float | None
    event_closed_at: datetime | None


@dataclass(frozen=True)
class ComparableRow:
    """A decision+trade pair that survived the full funnel — the only rows
    that ever feed the economics tables. `exit_at` is guaranteed non-null
    (the funnel requires it) since drawdown orders by realized-PnL time."""

    decision_row_id: int
    decision_id: str | None
    ts: datetime
    base: str
    exchange: str
    pump_event_id: int
    trade_id: int
    entry_at: datetime
    exit_at: datetime
    net_pnl_usd: float
    net_pnl_pct: float
    event_closed_at: datetime | None


@dataclass(frozen=True)
class OperationalRow:
    """Every distinct `opened`/`opened_dry_run` decision with a
    `pump_event_id`, deduplicated by `decision_row_id` — used only for
    cooldown-invariant transition classification (see module docstring point
    1). Never used for economics: an open position or an incomplete-
    accounting trade has no reliable PnL, but it must still be able to
    participate in a cooldown violation."""

    decision_row_id: int
    base: str
    pump_event_id: int
    ts: datetime
    entry_at: datetime | None
    exit_at: datetime | None


@dataclass(frozen=True)
class FunnelStep:
    name: str
    count: int
    share_of_previous_pct: float | None
    exclusion_reasons: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class OrphanTradesDiagnostic:
    count: int
    first_entry_at: datetime | None
    last_entry_at: datetime | None
    example_trade_ids: tuple[int, ...]


@dataclass(frozen=True)
class TransitionRow:
    base: str
    pump_event_id: int
    previous_pump_event_id: int
    ts: datetime
    seconds_since_previous_decision: float
    seconds_since_previous_entry: float | None
    seconds_since_previous_exit: float | None
    transition_type: str


@dataclass(frozen=True)
class TransitionSummaryRow:
    transition_type: str
    count: int
    share_pct: float


@dataclass(frozen=True)
class EventRollupSummary:
    total_events: int
    single_entry_events: int
    multiple_entry_events: int


@dataclass(frozen=True)
class MultipleEntryEventRow:
    pump_event_id: int
    base: str
    num_opens: int
    event_net_pnl_usd: float
    first_open_only_net_pnl_usd: float
    delta_usd: float


@dataclass(frozen=True)
class EconomicsSummary:
    label: str
    trade_count: int
    total_net_pnl_usd: float
    mean_net_pnl_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    max_drawdown_usd: float


@dataclass(frozen=True)
class ReentryOpportunityStats:
    events_with_comparable_open: int
    remained_observable_24h_after_first_open: int
    did_not_remain_observable_24h: int
    right_censored_still_open: int


@dataclass(frozen=True)
class ReentryAuditManifest:
    contract_version: str = REENTRY_AUDIT_REPORT_VERSION
    code_revision: str = ""
    working_tree_dirty: bool = False
    generated_at: datetime | None = None
    dataset_since: datetime | None = None
    dataset_until_exclusive: datetime | None = None
    input_fingerprint: str = ""
    strategy_versions: tuple[str, ...] = REENTRY_AUDIT_STRATEGY_VERSIONS
    cooldown_seconds: int = COOLDOWN_SECONDS
    report_scope: str = "historical_discovery_only_no_strategy_change"


@dataclass(frozen=True)
class ReentryAuditReport:
    manifest: ReentryAuditManifest
    funnel: tuple[FunnelStep, ...]
    orphan_trades: OrphanTradesDiagnostic
    transition_summary: tuple[TransitionSummaryRow, ...]
    base_24h_invariant_violations: int
    event_rollup: EventRollupSummary
    multiple_entry_events: tuple[MultipleEntryEventRow, ...]
    economics_all_actual_trades: EconomicsSummary
    economics_actual_first_open_per_event: EconomicsSummary
    reentry_opportunity: ReentryOpportunityStats


def reentry_decisions_statement(filters: ReentryAuditFilters) -> Select[Any]:
    """Pure query builder (no I/O). Decisions drive the query (LEFT JOIN to
    trades/events) so every `opened`/`opened_dry_run` decision is visible even
    with no matching trade — that gap becomes a funnel exclusion, not a
    silent drop. Orphan trades (a trade with no matching decision) are a
    structurally different failure mode and are counted separately by
    `orphan_trades_statement`, since a decision-driven query can never surface
    them."""
    decisions = TradeDecision.__table__
    trades = Trade.__table__
    events = PumpEvent.__table__
    decision_id_text = cast(decisions.c.decision_id, String)
    trade_join = decisions.outerjoin(
        trades,
        trades.c.setup_context["decision_id"].as_string() == decision_id_text,
    ).outerjoin(events, events.c.id == decisions.c.pump_event_id)
    return (
        select(
            decisions.c.id.label("decision_row_id"),
            decisions.c.decision_id,
            decisions.c.ts,
            decisions.c.base,
            decisions.c.exchange,
            decisions.c.pump_event_id,
            decisions.c.strategy_version,
            trades.c.id.label("trade_id"),
            trades.c.symbol.label("trade_symbol"),
            trades.c.exchange.label("trade_exchange"),
            trades.c.entry_at,
            trades.c.exit_at,
            trades.c.status.label("trade_status"),
            trades.c.accounting_version,
            trades.c.accounting_status,
            trades.c.net_pnl_usd,
            trades.c.net_pnl_pct,
            events.c.closed_at.label("event_closed_at"),
        )
        .select_from(trade_join)
        .where(
            decisions.c.action.in_(OPEN_ACTIONS),
            decisions.c.ts >= filters.since,
            decisions.c.ts < filters.until,
            decisions.c.strategy_version.in_(filters.strategy_versions),
        )
        .order_by(decisions.c.ts, decisions.c.id)
    )


def orphan_trades_statement(filters: ReentryAuditFilters) -> Select[Any]:
    """Pure query builder for the reverse-direction diagnostic: a real trade
    row with no matching durable decision (possible because `paper.
    open_paper()` writes the trade before `write_decision()` records the
    decision — see module docstring point 2). Scoped to `paper=true` and
    `strategy_version` in the same set the main query uses, both read
    directly out of `setup_context` (see `trader.py`'s decision payload) —
    an unrelated strategy's or a live trade's row must never inflate this
    count. A trade whose `setup_context` lacks these keys entirely fails
    closed (excluded), matching the rest of this report's fail-closed
    convention."""
    trades = Trade.__table__
    decisions = TradeDecision.__table__
    decision_id_text = cast(decisions.c.decision_id, String)
    linked = (
        select(literal(1))
        .select_from(decisions)
        .where(
            decisions.c.decision_id.is_not(None),
            decision_id_text == trades.c.setup_context["decision_id"].as_string(),
        )
        .exists()
    )
    return (
        select(
            trades.c.id,
            trades.c.symbol,
            trades.c.exchange,
            trades.c.entry_at,
            trades.c.status,
        )
        .where(
            trades.c.entry_at >= filters.since,
            trades.c.entry_at < filters.until,
            trades.c.setup_context["paper"].as_boolean().is_(True),
            trades.c.setup_context["strategy_version"].as_string().in_(filters.strategy_versions),
            or_(
                trades.c.setup_context["decision_id"].as_string().is_(None),
                ~linked,
            ),
        )
        .order_by(trades.c.entry_at)
    )


async def _load_joined_rows(
    engine: AsyncEngine,
    filters: ReentryAuditFilters,
) -> list[_JoinedRow]:
    async with engine.connect() as connection:
        result = await connection.execute(reentry_decisions_statement(filters))
        rows = result.all()
    return [
        _JoinedRow(
            decision_row_id=int(row.decision_row_id),
            decision_id=str(row.decision_id) if row.decision_id is not None else None,
            ts=row.ts,
            base=str(row.base),
            exchange=str(row.exchange),
            pump_event_id=int(row.pump_event_id) if row.pump_event_id is not None else None,
            strategy_version=row.strategy_version,
            trade_id=int(row.trade_id) if row.trade_id is not None else None,
            trade_symbol=row.trade_symbol,
            trade_exchange=row.trade_exchange,
            entry_at=row.entry_at,
            exit_at=row.exit_at,
            trade_status=row.trade_status,
            accounting_version=row.accounting_version,
            accounting_status=row.accounting_status,
            net_pnl_usd=(float(row.net_pnl_usd) if row.net_pnl_usd is not None else None),
            net_pnl_pct=(float(row.net_pnl_pct) if row.net_pnl_pct is not None else None),
            event_closed_at=row.event_closed_at,
        )
        for row in rows
    ]


async def _load_orphan_trades(
    engine: AsyncEngine,
    filters: ReentryAuditFilters,
) -> tuple[OrphanTradesDiagnostic, tuple[int, ...]]:
    """Returns the display-oriented diagnostic (examples capped at 10) plus
    the full sorted id list, which the fingerprint needs for full coverage
    (see module docstring point 7) — the capped examples alone could miss a
    change beyond the first 10 orphans."""
    async with engine.connect() as connection:
        result = await connection.execute(orphan_trades_statement(filters))
        rows = result.all()
    if not rows:
        return OrphanTradesDiagnostic(0, None, None, ()), ()
    entry_ats = [row.entry_at for row in rows]
    all_ids = tuple(sorted(int(row.id) for row in rows))
    diagnostic = OrphanTradesDiagnostic(
        count=len(rows),
        first_entry_at=min(entry_ats),
        last_entry_at=max(entry_ats),
        example_trade_ids=all_ids[:10],
    )
    return diagnostic, all_ids


def _funnel_step(
    name: str,
    rows: Sequence[Any],
    *,
    previous_count: int | None,
    exclusion_reasons: Counter[str] | None = None,
) -> FunnelStep:
    reasons = exclusion_reasons or Counter()
    share = None
    if previous_count is not None and previous_count > 0:
        share = len(rows) / previous_count * 100
    return FunnelStep(
        name=name,
        count=len(rows),
        share_of_previous_pct=share,
        exclusion_reasons=tuple(sorted(reasons.items(), key=lambda item: (-item[1], item[0]))),
    )


def _accounting_complete(row: _JoinedRow) -> bool:
    return (
        row.accounting_version == PAPER_ACCOUNTING_VERSION and row.accounting_status == "complete"
    )


def _has_valid_pnl(row: _JoinedRow) -> bool:
    # exit_at is required alongside the PnL fields: drawdown orders by
    # realized-PnL time (see module docstring point 5), so a comparable row
    # without a known exit time cannot be placed on that timeline.
    return row.net_pnl_usd is not None and row.net_pnl_pct is not None and row.exit_at is not None


def _valid_pnl_exclusion_reason(row: _JoinedRow) -> str:
    if row.net_pnl_usd is None or row.net_pnl_pct is None:
        return "missing_net_pnl"
    return "missing_exit_at"


def build_comparable_rows(
    rows: Sequence[_JoinedRow],
) -> tuple[tuple[FunnelStep, ...], tuple[ComparableRow, ...]]:
    """Pure funnel computation (no I/O), so it can be exercised directly in
    tests against hand-built `_JoinedRow` fixtures without a database."""
    step_all = tuple(rows)

    step_linked = tuple(row for row in step_all if row.trade_id is not None)
    linked_exclusions = Counter("no_matching_trade_row" for row in step_all if row.trade_id is None)

    # Fail closed on a one-to-many decision<->trade link (see module
    # docstring point 2): if a JSON-embedded decision_id somehow matches more
    # than one trade row, keep none of them rather than arbitrarily picking
    # one — a duplicate would otherwise double-count PnL and fabricate a
    # spurious near-zero-gap transition.
    decision_trade_counts = Counter(row.decision_row_id for row in step_linked)
    step_unique = tuple(
        row for row in step_linked if decision_trade_counts[row.decision_row_id] == 1
    )
    unique_exclusions = Counter(
        "duplicate_trade_link"
        for row in step_linked
        if decision_trade_counts[row.decision_row_id] > 1
    )

    step_has_event = tuple(row for row in step_unique if row.pump_event_id is not None)
    has_event_exclusions = Counter(
        "decision_missing_pump_event_id" for row in step_unique if row.pump_event_id is None
    )

    step_closed = tuple(row for row in step_has_event if row.trade_status == "closed")
    closed_exclusions = Counter(
        f"trade_status_{row.trade_status or 'unknown'}"
        for row in step_has_event
        if row.trade_status != "closed"
    )

    step_accounting = tuple(row for row in step_closed if _accounting_complete(row))
    accounting_exclusions = Counter(
        f"accounting_{row.accounting_status or 'unknown'}"
        for row in step_closed
        if not _accounting_complete(row)
    )

    step_identity = tuple(
        row
        for row in step_accounting
        if row.trade_symbol is not None
        and normalize_symbol_base(row.trade_symbol) == row.base
        and row.trade_exchange == row.exchange
    )
    identity_exclusions = Counter(
        "identity_mismatch_base_or_exchange" for row in step_accounting if row not in step_identity
    )

    step_valid_pnl = tuple(row for row in step_identity if _has_valid_pnl(row))
    valid_pnl_exclusions = Counter(
        _valid_pnl_exclusion_reason(row) for row in step_identity if not _has_valid_pnl(row)
    )

    funnel = (
        _funnel_step("all_open_decisions", step_all, previous_count=None),
        _funnel_step(
            "linked_to_trade",
            step_linked,
            previous_count=len(step_all),
            exclusion_reasons=linked_exclusions,
        ),
        _funnel_step(
            "unique_trade_link",
            step_unique,
            previous_count=len(step_linked),
            exclusion_reasons=unique_exclusions,
        ),
        _funnel_step(
            "has_pump_event_id",
            step_has_event,
            previous_count=len(step_unique),
            exclusion_reasons=has_event_exclusions,
        ),
        _funnel_step(
            "trade_closed",
            step_closed,
            previous_count=len(step_has_event),
            exclusion_reasons=closed_exclusions,
        ),
        _funnel_step(
            "accounting_complete",
            step_accounting,
            previous_count=len(step_closed),
            exclusion_reasons=accounting_exclusions,
        ),
        _funnel_step(
            "identity_consistent",
            step_identity,
            previous_count=len(step_accounting),
            exclusion_reasons=identity_exclusions,
        ),
        _funnel_step(
            "comparable",
            step_valid_pnl,
            previous_count=len(step_identity),
            exclusion_reasons=valid_pnl_exclusions,
        ),
    )

    comparable = tuple(
        ComparableRow(
            decision_row_id=row.decision_row_id,
            decision_id=row.decision_id,
            ts=row.ts,
            base=row.base,
            exchange=row.exchange,
            pump_event_id=row.pump_event_id,  # type: ignore[arg-type]
            trade_id=row.trade_id,  # type: ignore[arg-type]
            entry_at=row.entry_at,  # type: ignore[arg-type]
            exit_at=row.exit_at,  # type: ignore[arg-type]
            net_pnl_usd=row.net_pnl_usd,  # type: ignore[arg-type]
            net_pnl_pct=row.net_pnl_pct,  # type: ignore[arg-type]
            event_closed_at=row.event_closed_at,
        )
        for row in step_valid_pnl
    )
    return funnel, comparable


def build_operational_rows(rows: Sequence[_JoinedRow]) -> tuple[OperationalRow, ...]:
    """Every distinct `opened`/`opened_dry_run` decision with a
    `pump_event_id`, independent of the trade-linkage funnel above (see
    module docstring point 1) — deduplicated by `decision_row_id` so a
    one-to-many trade-join fan-out can never inflate this set either."""
    seen: dict[int, OperationalRow] = {}
    for row in sorted(rows, key=lambda item: (item.decision_row_id, item.trade_id or 0)):
        if row.pump_event_id is None or row.decision_row_id in seen:
            continue
        seen[row.decision_row_id] = OperationalRow(
            decision_row_id=row.decision_row_id,
            base=row.base,
            pump_event_id=row.pump_event_id,
            ts=row.ts,
            entry_at=row.entry_at,
            exit_at=row.exit_at,
        )
    return tuple(sorted(seen.values(), key=lambda item: item.ts))


def classify_transitions(rows: Sequence[OperationalRow]) -> tuple[TransitionRow, ...]:
    """Pure classification (no I/O) over the broader operational row set (see
    module docstring point 1) — never over `comparable`, so an open or
    incomplete-accounting decision can still register a cooldown violation.
    Consecutive decisions on the same `base` are compared pairwise — a token
    can show more than one transition type across its lifetime, so this
    never collapses to one label per base."""
    by_base: dict[str, list[OperationalRow]] = defaultdict(list)
    for row in rows:
        by_base[row.base].append(row)

    transitions: list[TransitionRow] = []
    for base, base_rows in by_base.items():
        ordered = sorted(base_rows, key=lambda row: row.ts)
        for previous, current in itertools.pairwise(ordered):
            seconds_since_decision = (current.ts - previous.ts).total_seconds()
            seconds_since_entry = (
                (current.entry_at - previous.entry_at).total_seconds()
                if current.entry_at is not None and previous.entry_at is not None
                else None
            )
            seconds_since_exit = (
                (current.entry_at - previous.exit_at).total_seconds()
                if current.entry_at is not None and previous.exit_at is not None
                else None
            )
            same_event = current.pump_event_id == previous.pump_event_id
            under_cooldown = seconds_since_decision < COOLDOWN_SECONDS
            if same_event and under_cooldown:
                transition_type = "same_event_under_24h"
            elif same_event and not under_cooldown:
                transition_type = "same_event_after_24h"
            elif not same_event and under_cooldown:
                transition_type = "cross_event_under_24h"
            else:
                transition_type = "cross_event_after_24h"
            transitions.append(
                TransitionRow(
                    base=base,
                    pump_event_id=current.pump_event_id,
                    previous_pump_event_id=previous.pump_event_id,
                    ts=current.ts,
                    seconds_since_previous_decision=seconds_since_decision,
                    seconds_since_previous_entry=seconds_since_entry,
                    seconds_since_previous_exit=seconds_since_exit,
                    transition_type=transition_type,
                )
            )
    return tuple(sorted(transitions, key=lambda row: row.ts))


def summarize_transitions(
    transitions: Sequence[TransitionRow],
) -> tuple[tuple[TransitionSummaryRow, ...], int]:
    counts = Counter(row.transition_type for row in transitions)
    total = len(transitions)
    summary = tuple(
        TransitionSummaryRow(
            transition_type=transition_type,
            count=count,
            share_pct=(count / total * 100 if total else 0.0),
        )
        for transition_type, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    violations = counts.get("same_event_under_24h", 0) + counts.get("cross_event_under_24h", 0)
    return summary, violations


def build_event_rollup(
    rows: Sequence[ComparableRow],
) -> tuple[EventRollupSummary, tuple[MultipleEntryEventRow, ...]]:
    by_event: dict[int, list[ComparableRow]] = defaultdict(list)
    for row in rows:
        by_event[row.pump_event_id].append(row)

    single = sum(1 for event_rows in by_event.values() if len(event_rows) == 1)
    multiple = sum(1 for event_rows in by_event.values() if len(event_rows) > 1)
    summary = EventRollupSummary(
        total_events=len(by_event), single_entry_events=single, multiple_entry_events=multiple
    )

    detail_rows = []
    for pump_event_id, event_rows in sorted(by_event.items()):
        if len(event_rows) <= 1:
            continue
        ordered = sorted(event_rows, key=lambda row: row.ts)
        event_total = sum(row.net_pnl_usd for row in ordered)
        first_only = ordered[0].net_pnl_usd
        detail_rows.append(
            MultipleEntryEventRow(
                pump_event_id=pump_event_id,
                base=ordered[0].base,
                num_opens=len(ordered),
                event_net_pnl_usd=event_total,
                first_open_only_net_pnl_usd=first_only,
                delta_usd=event_total - first_only,
            )
        )
    return summary, tuple(detail_rows)


def _max_drawdown_usd(rows: Sequence[ComparableRow]) -> float:
    # Ordered by exit_at, not decision/entry time: PnL is realized at exit,
    # and overlapping positions would misorder the running peak-to-trough
    # if sorted by when they were opened instead (see module docstring
    # point 5).
    ordered = sorted(rows, key=lambda row: row.exit_at)
    cumulative = 0.0
    running_max = 0.0
    max_drawdown = 0.0
    for row in ordered:
        cumulative += row.net_pnl_usd
        running_max = max(running_max, cumulative)
        max_drawdown = max(max_drawdown, running_max - cumulative)
    return max_drawdown


def _economics(label: str, rows: Sequence[ComparableRow]) -> EconomicsSummary:
    if not rows:
        return EconomicsSummary(label, 0, 0.0, None, None, None, 0.0)
    total_pnl = sum(row.net_pnl_usd for row in rows)
    mean_pct = sum(row.net_pnl_pct for row in rows) / len(rows)
    wins = sum(1 for row in rows if row.net_pnl_usd > 0)
    win_rate = wins / len(rows) * 100
    pf = profit_factor(row.net_pnl_pct for row in rows)
    return EconomicsSummary(
        label=label,
        trade_count=len(rows),
        total_net_pnl_usd=total_pnl,
        mean_net_pnl_pct=mean_pct,
        win_rate_pct=win_rate,
        profit_factor=pf,
        max_drawdown_usd=_max_drawdown_usd(rows),
    )


def first_open_per_event(rows: Sequence[ComparableRow]) -> tuple[ComparableRow, ...]:
    by_event: dict[int, ComparableRow] = {}
    for row in rows:
        existing = by_event.get(row.pump_event_id)
        if existing is None or row.ts < existing.ts:
            by_event[row.pump_event_id] = row
    return tuple(by_event.values())


def compute_reentry_opportunity(
    rows: Sequence[ComparableRow],
    *,
    as_of: datetime,
) -> ReentryOpportunityStats:
    """Upper-bound proxy only (see module docstring point 6): whether the
    underlying `pump_events` row stayed structurally open for >=24h past the
    first recorded open for that event, evaluated strictly as of `as_of`
    (`filters.until`, never "now"). An actual `closed_at` at or after `as_of`
    is clipped to "unknown as of the cutoff" — otherwise a historical run
    would see event-closure information from after its own window, which is
    exactly the kind of cutoff-peeking this report must not do. Never
    implies a suppressed decision actually existed."""
    first_opens = first_open_per_event(rows)
    remained = 0
    did_not_remain = 0
    right_censored = 0
    for row in first_opens:
        closed_before_cutoff = None
        if row.event_closed_at is not None and row.event_closed_at < as_of:
            closed_before_cutoff = row.event_closed_at
        if closed_before_cutoff is not None:
            if (closed_before_cutoff - row.ts).total_seconds() >= COOLDOWN_SECONDS:
                remained += 1
            else:
                did_not_remain += 1
        else:
            if (as_of - row.ts).total_seconds() >= COOLDOWN_SECONDS:
                remained += 1
            else:
                right_censored += 1
    return ReentryOpportunityStats(
        events_with_comparable_open=len(first_opens),
        remained_observable_24h_after_first_open=remained,
        did_not_remain_observable_24h=did_not_remain,
        right_censored_still_open=right_censored,
    )


def _fingerprint(
    joined_rows: Sequence[_JoinedRow],
    orphan_trade_ids: Sequence[int],
) -> str:
    """Hashes the full pre-funnel dataset, not just the post-funnel
    `comparable` set (see module docstring point 7) — two runs whose
    `comparable` rows coincidentally match but whose funnel exclusions or
    orphan counts differ must never collide on the same fingerprint."""
    decisions_payload = []
    for row in sorted(joined_rows, key=lambda item: (item.decision_row_id, item.trade_id or 0)):
        record = asdict(row)
        record["ts"] = row.ts.isoformat()
        record["entry_at"] = row.entry_at.isoformat() if row.entry_at is not None else None
        record["exit_at"] = row.exit_at.isoformat() if row.exit_at is not None else None
        record["event_closed_at"] = (
            row.event_closed_at.isoformat() if row.event_closed_at is not None else None
        )
        decisions_payload.append(record)
    payload = {
        "decisions": decisions_payload,
        "orphan_trade_ids": sorted(orphan_trade_ids),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def build_reentry_audit_report(
    engine: AsyncEngine,
    filters: ReentryAuditFilters,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> ReentryAuditReport:
    joined_rows = await _load_joined_rows(engine, filters)
    orphan_trades, orphan_trade_ids = await _load_orphan_trades(engine, filters)

    funnel, comparable = build_comparable_rows(joined_rows)
    operational = build_operational_rows(joined_rows)
    transitions = classify_transitions(operational)
    transition_summary, violations = summarize_transitions(transitions)
    event_rollup, multiple_entry_events = build_event_rollup(comparable)
    economics_all = _economics("all_actual_trades", comparable)
    economics_first = _economics("actual_first_open_per_event", first_open_per_event(comparable))
    reentry_opportunity = compute_reentry_opportunity(comparable, as_of=filters.until)

    manifest = ReentryAuditManifest(
        code_revision=normalize_code_revision(code_revision),
        working_tree_dirty=working_tree_dirty,
        generated_at=generated_at,
        dataset_since=filters.since,
        dataset_until_exclusive=filters.until,
        input_fingerprint=_fingerprint(joined_rows, orphan_trade_ids),
        strategy_versions=filters.strategy_versions,
    )
    return ReentryAuditReport(
        manifest=manifest,
        funnel=funnel,
        orphan_trades=orphan_trades,
        transition_summary=transition_summary,
        base_24h_invariant_violations=violations,
        event_rollup=event_rollup,
        multiple_entry_events=multiple_entry_events,
        economics_all_actual_trades=economics_all,
        economics_actual_first_open_per_event=economics_first,
        reentry_opportunity=reentry_opportunity,
    )


def render_json(report: ReentryAuditReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def _iso_or_na(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "n/a"


def render_markdown(report: ReentryAuditReport) -> str:
    manifest = report.manifest
    lines = [
        "# Pump-Short Re-entry Audit",
        "",
        f"Generated: {_iso_or_na(manifest.generated_at)}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        (
            f"Scope: {_iso_or_na(manifest.dataset_since)} <= decision ts < "
            f"{_iso_or_na(manifest.dataset_until_exclusive)}"
        ),
        f"Strategy versions: {', '.join(manifest.strategy_versions)}",
        f"Cooldown reference (must track trader.py's TTL): {manifest.cooldown_seconds}s",
        "",
        (
            "> Measurement-only. This report never modifies `virtual_strategy.py`, "
            "computes no p-value, and cannot authorize a production strategy change. "
            "It answers exactly one question: how much do the actual paper re-entry "
            "outcomes diverge from the first-open-per-event assumption every "
            "existing virtual report makes."
        ),
        "",
        "## Funnel",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Step", "Count", "% of previous"),
            [
                (
                    step.name,
                    step.count,
                    format_percentage(step.share_of_previous_pct, 1, missing="n/a"),
                )
                for step in report.funnel
            ],
        )
    )
    lines.extend(["", "## Funnel exclusion reasons", ""])
    lines.extend(
        markdown_table(
            ("Step", "Reason", "Count"),
            [
                (step.name, reason, count)
                for step in report.funnel
                for reason, count in step.exclusion_reasons
            ],
        )
    )
    orphan = report.orphan_trades
    lines.extend(
        [
            "",
            "## Orphan trades (trade recorded, no matching decision)",
            "",
            f"Count: {orphan.count}",
            (
                f"Entry range: {_iso_or_na(orphan.first_entry_at)} .. "
                f"{_iso_or_na(orphan.last_entry_at)}"
            ),
            f"Example trade ids: {', '.join(str(t) for t in orphan.example_trade_ids) or 'none'}",
            "",
            "## Transition classification (operational set)",
            "",
            (
                "_Every distinct open decision in scope, not just `comparable` "
                "trades — an open or incomplete-accounting decision can still "
                "register a cooldown violation._"
            ),
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Transition type", "Count", "Share"),
            [
                (row.transition_type, row.count, f"{row.share_pct:.1f}%")
                for row in report.transition_summary
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                f"Cooldown-invariant violations (same_event_under_24h + "
                f"cross_event_under_24h): {report.base_24h_invariant_violations}"
            ),
            "",
            "## Event rollup",
            "",
        ]
    )
    rollup = report.event_rollup
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Total comparable events", rollup.total_events),
                ("Single-entry events", rollup.single_entry_events),
                ("Multiple-entry events", rollup.multiple_entry_events),
            ],
        )
    )
    lines.extend(["", "## Multiple-entry events (detail)", ""])
    lines.extend(
        markdown_table(
            (
                "pump_event_id",
                "base",
                "opens",
                "Event net PnL ($)",
                "First-open-only PnL ($)",
                "Delta ($)",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.num_opens,
                    f"{row.event_net_pnl_usd:.2f}",
                    f"{row.first_open_only_net_pnl_usd:.2f}",
                    f"{row.delta_usd:+.2f}",
                )
                for row in report.multiple_entry_events
            ],
        )
    )
    lines.extend(["", "## Headline economics (real trades only, never simulated)", ""])
    headline = (report.economics_all_actual_trades, report.economics_actual_first_open_per_event)
    for economics in headline:
        lines.extend(
            markdown_table(
                (economics.label, "Value"),
                [
                    ("Trade count", economics.trade_count),
                    ("Total net PnL ($)", format_number(economics.total_net_pnl_usd, 2)),
                    ("Mean net PnL (%)", format_percentage(economics.mean_net_pnl_pct, 4)),
                    ("Win rate (%)", format_percentage(economics.win_rate_pct, 2)),
                    ("Profit factor", format_number(economics.profit_factor, 4)),
                    ("Max drawdown ($)", format_number(economics.max_drawdown_usd, 2)),
                ],
            )
        )
        lines.append("")
    opp = report.reentry_opportunity
    lines.extend(
        [
            "## Re-entry opportunity (upper-bound proxy, not a decision count)",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Events with a comparable first open", opp.events_with_comparable_open),
                (
                    "Remained open >=24h after first open",
                    opp.remained_observable_24h_after_first_open,
                ),
                ("Closed within 24h of first open", opp.did_not_remain_observable_24h),
                ("Still open now (right-censored)", opp.right_censored_still_open),
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "_This does not count suppressed evaluation attempts: `trader.py` "
                "`continue`s before writing a decision when the base's seen-key is "
                "set, so cooldown-suppressed opportunities are "
                "`historically_unobservable` by construction and are never "
                "reconstructed here._"
            ),
            "",
            "## Future fix options (not implemented in this report)",
            "",
            (
                "1. **One trade per pump event.** Simplest replay contract; "
                "requires execution to hold a durable event-level fence, not "
                "just a Redis TTL keyed on `base`."
            ),
            (
                "2. **Stateful base+24h cooldown replay.** Preserves the actual "
                "current semantics, but every virtual report would need to "
                "replay chronologically across ALL events for a base, "
                "maintaining `base -> cooldown_until` state — a much larger "
                "blast radius than option 1."
            ),
            (
                "3. **Re-entry as its own challenger.** Keep the existing "
                "one-entry-per-event baseline untouched; test a post-cooldown "
                "re-entry rule as a separate, independently gated challenger "
                "that is never blended into the baseline's own numbers."
            ),
            "",
            (
                "This report does not choose between them — that decision "
                "belongs in ROADMAP.md as a human call, informed by the "
                "measured divergence above."
            ),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=REENTRY_AUDIT_DEFAULT_SINCE,
        help="inclusive UTC cutoff; defaults to the confirmation cohort start",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument(
        "--strategy-version",
        action="append",
        dest="strategy_version",
        help="repeatable; defaults to the registered pump-short strategy version(s)",
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
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for pump-short-reentry-audit-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReentryAuditFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=tuple(args.strategy_version or REENTRY_AUDIT_STRATEGY_VERSIONS),
    )
    engine = create_async_engine(
        async_database_url(db_url),
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    try:
        sys.stderr.write("pump-short-reentry-audit-report: loading decisions and trades\n")
        report = await build_reentry_audit_report(
            engine,
            filters,
            generated_at=generated_at,
            code_revision=args.code_revision,
            working_tree_dirty=args.working_tree_dirty,
        )
    finally:
        await engine.dispose()
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
