"""Replicate HYP-022's tight exit on the window its parameters never saw.

HYP-022 found `scaled_p25` at profit factor 1.07 on the discovery window and
called it inconclusive: +0.64 against a 1.0 margin. This reads the window that
pass declared held out, exactly once, to answer the prior question -- does the
effect exist on data the parameters were not derived from.

Registered in docs/research/scaled-exit-replication-v1.md.

The window and the two policies are constants, not options. A held-out window is
only held out until someone can move it with a flag, and there is not a second
one in this line of work. `--since` and `--until` are deliberately absent.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean
from typing import TYPE_CHECKING

from .reporting import normalize_code_revision
from .virtual_strategy import (
    BASELINE_EXIT_POLICY,
    SCALED_P25_EXIT_POLICY,
    simulate_episode,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .replay import ReplayEpisode
    from .virtual_strategy import ExitPolicy, MarketPath, VirtualTrade

REPLICATION_VERSION = "scaled_exit_replication_v1"

# The window HYP-022 declared held out and did not read. A constant because a
# held-out window stops being held out the moment a flag can move it.
HOLDOUT_SINCE = datetime(2026, 8, 25, tzinfo=UTC)

# Two policies, not nine. Running the whole family here would turn a
# confirmation into a best-of-nine, which is the thing a confirmation exists to
# avoid.
CHALLENGER = SCALED_P25_EXIT_POLICY
BASELINE = BASELINE_EXIT_POLICY

# Declared in the registration, before this window was read.
REPLICATION_MARGIN_PCT = 0.30
MINIMUM_TRADES = 150


@dataclass(frozen=True)
class PairedEpisode:
    """One episode simulated under both policies, or unresolved under either.

    Pairing matters: an unpaired comparison would let the two policies be
    measured on different episodes, and the difference would then include
    whatever separates those populations.
    """

    pump_event_id: int
    baseline: VirtualTrade
    challenger: VirtualTrade

    @property
    def complete(self) -> bool:
        return (
            self.baseline.status == "complete"
            and self.challenger.status == "complete"
            and self.baseline.net_return_pct is not None
            and self.challenger.net_return_pct is not None
        )

    @property
    def delta_pct(self) -> float | None:
        if not self.complete:
            return None
        assert self.baseline.net_return_pct is not None
        assert self.challenger.net_return_pct is not None
        return self.challenger.net_return_pct - self.baseline.net_return_pct


@dataclass(frozen=True)
class ReplicationReport:
    pairs: tuple[PairedEpisode, ...]
    generated_at: datetime
    code_revision: str

    @property
    def complete(self) -> tuple[PairedEpisode, ...]:
        return tuple(pair for pair in self.pairs if pair.complete)

    @property
    def mean_delta_pct(self) -> float | None:
        deltas = [value for pair in self.complete if (value := pair.delta_pct) is not None]
        return fmean(deltas) if deltas else None

    @property
    def verdict(self) -> str:
        """The registered rule, applied without reinterpretation."""
        delta = self.mean_delta_pct
        if delta is None or len(self.complete) < MINIMUM_TRADES:
            return "inconclusive"
        if delta >= REPLICATION_MARGIN_PCT:
            return "replicates"
        if delta <= 0.0:
            return "does_not_replicate"
        return "inconclusive"


def _mean_net(trades: Sequence[VirtualTrade]) -> float | None:
    values = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    return fmean(values) if values else None


def _profit_factor(trades: Sequence[VirtualTrade]) -> float | None:
    gains = sum(
        trade.net_pnl_usd
        for trade in trades
        if trade.net_pnl_usd is not None and trade.net_pnl_usd > 0
    )
    losses = -sum(
        trade.net_pnl_usd
        for trade in trades
        if trade.net_pnl_usd is not None and trade.net_pnl_usd < 0
    )
    return None if losses <= 0 else gains / losses


def _win_rate_pct(trades: Sequence[VirtualTrade]) -> float | None:
    resolved = [trade for trade in trades if trade.net_return_pct is not None]
    if not resolved:
        return None
    wins = sum(1 for trade in resolved if (trade.net_return_pct or 0) > 0)
    return wins / len(resolved) * 100


def replicate(
    episodes: Sequence[ReplayEpisode],
    paths: dict[int, MarketPath],
    *,
    generated_at: datetime,
    code_revision: str,
) -> ReplicationReport:
    pairs: list[PairedEpisode] = []
    for episode in episodes:
        path = paths.get(episode.pump_event_id)
        if path is None:
            continue
        pairs.append(
            PairedEpisode(
                pump_event_id=episode.pump_event_id,
                baseline=simulate_episode(episode, path, exit_policy=BASELINE),
                challenger=simulate_episode(episode, path, exit_policy=CHALLENGER),
            )
        )
    return ReplicationReport(
        pairs=tuple(pairs),
        generated_at=generated_at,
        code_revision=code_revision,
    )


def _policy_row(name: str, policy: ExitPolicy, trades: Sequence[VirtualTrade]) -> str:
    mean_net = _mean_net(trades)
    factor = _profit_factor(trades)
    win_rate = _win_rate_pct(trades)
    durations = [trade.duration_minutes for trade in trades if trade.duration_minutes is not None]
    return (
        f"| {name} | `{policy.version}` | "
        f"{'n/a' if mean_net is None else f'{mean_net:+.2f}%'} | "
        f"{'n/a' if factor is None else f'{factor:.2f}'} | "
        f"{'n/a' if win_rate is None else f'{win_rate:.1f}%'} | "
        f"{'n/a' if not durations else f'{fmean(durations):.1f}m'} |"
    )


def render_markdown(report: ReplicationReport) -> str:
    complete = report.complete
    baseline_trades = [pair.baseline for pair in complete]
    challenger_trades = [pair.challenger for pair in complete]
    delta = report.mean_delta_pct

    lines = [
        "# Scaled exit: replication on the held-out window",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Code revision: `{report.code_revision}`",
        f"Version: `{REPLICATION_VERSION}`",
        f"Window: {HOLDOUT_SINCE.date().isoformat()} onward, read once",
        "",
        f"> Verdict: `{report.verdict}`. Registered rule: replicates at "
        f"+{REPLICATION_MARGIN_PCT:.2f} points or more on at least {MINIMUM_TRADES} "
        "completed trades, does not replicate at or below zero, inconclusive "
        "between. This report never changes production exits.",
        "",
        "## Result",
        "",
        f"Paired episodes: {len(report.pairs)}. Completed under both policies: "
        f"**{len(complete)}**.",
        "",
        f"Mean paired delta: **{'n/a' if delta is None else f'{delta:+.2f} points'}**.",
        "",
        "| Policy | Version | Mean net | Profit factor | Win rate | Duration |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
        _policy_row("baseline", BASELINE, baseline_trades),
        _policy_row("scaled_p25", CHALLENGER, challenger_trades),
        "",
        "## Exit reasons",
        "",
        "| Policy | Reason | Episodes |",
        "| --- | --- | ---: |",
    ]
    for name, trades in (("baseline", baseline_trades), ("scaled_p25", challenger_trades)):
        for reason, count in Counter(
            trade.exit_reason or "unresolved" for trade in trades
        ).most_common():
            lines.append(f"| {name} | {reason} | {count} |")
    return "\n".join(lines) + "\n"


async def _run(code_revision: str) -> str:
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay import ReplayFilters, build_replay_dataset
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_exit_policy_paths

    generated_at = datetime.now(UTC)
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for scaled-exit-replication")

    filters = ReplayFilters(since=HOLDOUT_SINCE, until=generated_at)
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)
    paths = await fetch_exit_policy_paths(dataset.eligible_episodes, EXCHANGE_FACTORIES)
    report = replicate(
        dataset.eligible_episodes,
        {path.pump_event_id: path for path in paths},
        generated_at=generated_at,
        code_revision=normalize_code_revision(code_revision),
    )
    return render_markdown(report)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replicate the scaled exit on HYP-022's held-out window"
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty", action=argparse.BooleanOptionalAction, required=True
    )
    args = parser.parse_args()
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    sys.stdout.write(asyncio.run(_run(args.code_revision)))
