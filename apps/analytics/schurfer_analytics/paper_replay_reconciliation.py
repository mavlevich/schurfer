"""Compare the offline replay against what the paper broker actually did.

Registered in docs/research/replay-paper-reconciliation-v1.md before any
agreement rate was computed. This is a correctness check on the simulator, not
a claim about edge: every conclusion this repository holds about exit policy
comes from the replay, and the replay has never been compared against anything
that ran.

The comparison is possible because the exit decision is one shared function
since #361, and because the broker records its own reason string in
`app.trades.notes`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .ohlcv import TIMEFRAME_MS
from .reporting import normalize_code_revision, parse_utc_datetime
from .virtual_strategy import (
    PRODUCTION_EXIT_POLICY,
    MarketPath,
    simulate_recorded_entry,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from .replay import ReplayDecision, ReplayEpisode

RECONCILIATION_VERSION = "replay_paper_reconciliation_v1"

# The date production gained the no-progress exit (422f784). Before it, the
# broker was running a policy the replay's `production` policy does not model.
RECONCILIATION_START = datetime(2026, 8, 18, tzinfo=UTC)

# Trades on these exchanges cannot be replayed at all: ccxt's fetchOHLCV targets
# the spot kline endpoint, so a perpetual-only market has no candles. Reported
# as coverage rather than counted as disagreement (CCXT-003, ENG-032).
UNREPLAYABLE_EXCHANGES = frozenset({"lbank"})

# The two sides name the same rules differently, and the comparison has to be
# told so explicitly rather than string-matching and reporting a disagreement
# that is only a vocabulary difference.
#
# The important pair is the first one. The broker's `no_progress` is the
# pre-activation cut in evaluate_exit: close at 60 minutes when trailing never
# activated. The replay calls that `not_activated`, because `no_progress` was
# already taken in the exit-policy family by a different rule -- a rolling stall
# detector that keeps measuring after activation. Renaming either one would make
# a registered report mean something new, so the names stay and the mapping is
# written down.
#
# `absolute_max_hold` is the replay's name for hitting the end of an extended
# window; with no extension configured it is the same event as `max_hold`.
_RULE_ALIASES = {
    "not_activated": "no_progress",
    "absolute_max_hold": "max_hold",
    "protected_stop": "trailing_stop",
}


def canonical_rule(reason: str | None) -> str:
    """The rule a reason string names, in one vocabulary."""
    if not reason:
        return "unknown"
    keyword = reason.strip().split(" ", 1)[0]
    if not keyword:
        return "unknown"
    return _RULE_ALIASES.get(keyword, keyword)


@dataclass(frozen=True)
class PaperTrade:
    """One closed paper trade, as the broker recorded it."""

    trade_id: int
    pump_event_id: int
    decision_id: str | None
    exchange: str
    base: str
    entry_at: datetime
    entry_price: float
    exit_at: datetime | None
    exit_price: float | None
    reason: str | None

    @property
    def recorded_reason(self) -> str:
        """The reason keyword, without the numbers the broker appends.

        `notes` looks like `no_progress age=60min` or `initial_sl move=-8.5%`.
        The keyword is what identifies the rule that fired; the numbers are
        specific to the tick the broker saw and cannot be reproduced from bars.
        """
        return canonical_rule(self.reason)


@dataclass(frozen=True)
class Comparison:
    """One trade, replayed and compared."""

    trade: PaperTrade
    status: str
    replayed_reason: str | None = None
    replayed_exit_at: datetime | None = None
    replayed_exit_price: float | None = None
    detail: str | None = None

    @property
    def replayed_rule(self) -> str:
        return canonical_rule(self.replayed_reason)

    @property
    def reasons_match(self) -> bool:
        return self.status == "compared" and self.replayed_rule == self.trade.recorded_reason

    @property
    def exit_time_delta_minutes(self) -> float | None:
        if not self.reasons_match or self.replayed_exit_at is None or self.trade.exit_at is None:
            return None
        return (self.replayed_exit_at - self.trade.exit_at).total_seconds() / 60

    @property
    def exit_price_delta_pct(self) -> float | None:
        if (
            not self.reasons_match
            or self.replayed_exit_price is None
            or self.trade.exit_price is None
            or self.trade.exit_price <= 0
        ):
            return None
        return (self.replayed_exit_price - self.trade.exit_price) / self.trade.exit_price * 100


@dataclass(frozen=True)
class ReconciliationReport:
    comparisons: tuple[Comparison, ...]
    generated_at: datetime
    code_revision: str

    @property
    def reconcilable(self) -> tuple[Comparison, ...]:
        return tuple(item for item in self.comparisons if item.status == "compared")

    @property
    def matched(self) -> tuple[Comparison, ...]:
        return tuple(item for item in self.reconcilable if item.reasons_match)

    @property
    def match_rate_pct(self) -> float | None:
        total = len(self.reconcilable)
        return None if total == 0 else len(self.matched) / total * 100

    @property
    def verdict(self) -> str:
        """The pre-registered decision rule, applied without reinterpretation."""
        rate = self.match_rate_pct
        if rate is None or len(self.reconcilable) < 80:
            return "inconclusive"
        if rate >= 85.0:
            return "agrees"
        if rate < 70.0:
            return "disagrees"
        return "inconclusive"


def _episode_decision(
    episode: ReplayEpisode, decision_id: str | None
) -> tuple[ReplayDecision | None, str]:
    """The decision the trade was opened from, or the episode's first.

    A trade carries the decision_id it acted on. Falling back to the first
    decision keeps an episode comparable when that link is missing, and the
    fallback is recorded in the comparison rather than hidden.
    """
    if decision_id:
        for decision in episode.decisions:
            if decision.decision_id == decision_id:
                return decision, "recorded_decision"
    return (episode.decisions[0], "first_decision_fallback") if episode.decisions else (None, "")


def reconcile(
    trades: Iterable[PaperTrade],
    episodes: Mapping[int, ReplayEpisode],
    paths: Mapping[int, MarketPath],
    *,
    generated_at: datetime,
    code_revision: str,
) -> ReconciliationReport:
    """Replay each trade at its own recorded entry and compare the exit reason."""
    comparisons: list[Comparison] = []
    for trade in trades:
        if trade.exchange.casefold() in UNREPLAYABLE_EXCHANGES:
            comparisons.append(
                Comparison(trade, "unreplayable_exchange", detail=f"{trade.exchange}: no OHLCV")
            )
            continue
        if trade.reason is None:
            comparisons.append(Comparison(trade, "no_recorded_reason"))
            continue
        episode = episodes.get(trade.pump_event_id)
        if episode is None:
            comparisons.append(Comparison(trade, "episode_unavailable"))
            continue
        path = paths.get(trade.pump_event_id)
        if path is None or path.status != "complete" or not path.candles:
            comparisons.append(
                Comparison(trade, "market_path_unavailable", detail=path.error if path else None)
            )
            continue
        decision, selection_reason = _episode_decision(episode, trade.decision_id)
        if decision is None:
            comparisons.append(Comparison(trade, "episode_has_no_decisions"))
            continue

        entry_at_ms = int(trade.entry_at.timestamp() * 1000)
        if not path.candles or entry_at_ms + TIMEFRAME_MS > path.candles[-1].ts_ms:
            comparisons.append(Comparison(trade, "entry_outside_market_path"))
            continue
        try:
            replayed = simulate_recorded_entry(
                episode,
                path,
                decision,
                entry_at_ms=entry_at_ms,
                entry_price=trade.entry_price,
                selection_reason=selection_reason,
                exit_policy=PRODUCTION_EXIT_POLICY,
            )
        except (ValueError, RuntimeError) as exc:
            comparisons.append(Comparison(trade, "simulation_failed", detail=str(exc)))
            continue
        if replayed.status != "complete" or replayed.exit_reason is None:
            comparisons.append(Comparison(trade, "simulation_unresolved", detail=replayed.status))
            continue
        comparisons.append(
            Comparison(
                trade,
                "compared",
                replayed_reason=replayed.exit_reason,
                replayed_exit_at=replayed.exit_at,
                replayed_exit_price=replayed.exit_price,
            )
        )
    return ReconciliationReport(
        comparisons=tuple(comparisons),
        generated_at=generated_at,
        code_revision=code_revision,
    )


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def render_markdown(report: ReconciliationReport) -> str:
    """The report, with the verdict applied by rule rather than by judgement."""
    lines = [
        "# Replay against paper: reconciliation",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Code revision: `{report.code_revision}`",
        f"Version: `{RECONCILIATION_VERSION}`",
        f"Policy: `{PRODUCTION_EXIT_POLICY.version}`",
        "",
        f"> Verdict: `{report.verdict}`. Pre-registered rule: agrees at 85% or above on "
        "at least 80 reconcilable trades, disagrees below 70%, inconclusive otherwise. "
        "This report never changes production exits.",
        "",
        "## Coverage",
        "",
        "| Status | Trades |",
        "| --- | ---: |",
    ]
    for status, count in Counter(item.status for item in report.comparisons).most_common():
        lines.append(f"| {status} | {count} |")
    lines.extend(
        [
            "",
            f"Reconcilable: **{len(report.reconcilable)}** of {len(report.comparisons)}.",
            "",
            "## Exit reason agreement",
            "",
        ]
    )
    rate = report.match_rate_pct
    if rate is None:
        lines.append("No reconcilable trades.")
    else:
        lines.append(f"Reasons match on **{len(report.matched)} of {len(report.reconcilable)}**")
        lines.append(f"= **{rate:.1f}%**.")
        lines.extend(["", "| Recorded | Replayed | Trades |", "| --- | --- | ---: |"])
        pairs = Counter(
            (item.trade.recorded_reason, item.replayed_rule) for item in report.reconcilable
        )
        for (recorded, replayed), count in pairs.most_common():
            marker = "" if recorded == replayed else " **<-**"
            lines.append(f"| {recorded} | {replayed}{marker} | {count} |")

    time_deltas = [
        value for item in report.matched if (value := item.exit_time_delta_minutes) is not None
    ]
    price_deltas = [
        value for item in report.matched if (value := item.exit_price_delta_pct) is not None
    ]
    lines.extend(
        [
            "",
            "## Where they differ even when they agree",
            "",
            "Reported, not graded: the broker decides on ticker updates at prices no "
            "five-minute bar contains, so these cannot be zero and a zero would be "
            "the surprising result.",
            "",
            "| Measure | Median | n |",
            "| --- | ---: | ---: |",
        ]
    )
    median_time = _median(time_deltas)
    median_price = _median(price_deltas)
    lines.append(
        f"| Exit time delta (min) | {'n/a' if median_time is None else f'{median_time:+.1f}'} "
        f"| {len(time_deltas)} |"
    )
    lines.append(
        f"| Exit price delta (%) | {'n/a' if median_price is None else f'{median_price:+.2f}'} "
        f"| {len(price_deltas)} |"
    )
    return "\n".join(lines) + "\n"


async def load_paper_trades(db_url: str, since: datetime) -> tuple[PaperTrade, ...]:
    """Closed paper pump_short trades, with the reason the broker recorded.

    The reason lives in `notes`, not in `setup_context`: the broker writes the
    string `evaluate_exit` returned. Nothing else in the schema records which
    rule fired, which is why this reconciliation was not possible before anyone
    noticed that column.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("""
                    SELECT t.id, t.symbol, t.exchange, t.notes,
                           t.entry_at, t.entry_price, t.exit_at, t.exit_price,
                           t.setup_context->>'pump_event_id' AS pump_event_id,
                           t.setup_context->>'decision_id' AS decision_id
                    FROM app.trades t
                    JOIN app.strategies s ON s.id = t.strategy_id
                    WHERE s.name = 'pump_short'
                      AND t.status = 'closed'
                      AND t.entry_at >= :since
                      AND (t.setup_context->>'paper')::boolean IS TRUE
                    ORDER BY t.entry_at
                """),
                {"since": since},
            )
            trades = []
            for row in rows.mappings():
                pump_event_id = row["pump_event_id"]
                if pump_event_id is None:
                    continue
                trades.append(
                    PaperTrade(
                        trade_id=int(row["id"]),
                        pump_event_id=int(pump_event_id),
                        decision_id=row["decision_id"],
                        exchange=str(row["exchange"]),
                        base=str(row["symbol"]).split("/", 1)[0],
                        entry_at=row["entry_at"],
                        entry_price=float(row["entry_price"]),
                        exit_at=row["exit_at"],
                        exit_price=None if row["exit_price"] is None else float(row["exit_price"]),
                        reason=row["notes"],
                    )
                )
    finally:
        await engine.dispose()
    return tuple(trades)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile the offline replay against the paper broker"
    )
    parser.add_argument("--since", type=parse_utc_datetime, default=RECONCILIATION_START)
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty", action=argparse.BooleanOptionalAction, required=True
    )
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay import ReplayFilters, build_replay_dataset
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_exit_policy_paths

    generated_at = datetime.now(UTC)
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for paper-replay-reconciliation")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")

    trades = await load_paper_trades(db_url, args.since)
    # The episode set comes from the same builder the exit-policy family uses,
    # so the reconciliation is against the machinery under test rather than a
    # convenience copy of it.
    filters = ReplayFilters(since=args.since, until=generated_at)
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)
    wanted = {trade.pump_event_id for trade in trades}
    episodes = {
        episode.pump_event_id: episode
        for episode in dataset.eligible_episodes
        if episode.pump_event_id in wanted
    }
    paths = await fetch_exit_policy_paths(tuple(episodes.values()), EXCHANGE_FACTORIES)
    report = reconcile(
        trades,
        episodes,
        {path.pump_event_id: path for path in paths},
        generated_at=generated_at,
        code_revision=normalize_code_revision(args.code_revision),
    )
    return render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
