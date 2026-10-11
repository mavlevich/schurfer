"""HYP-015 executable evaluation: the paper cohort as orders the venue would take.

The confirmation design (docs/research/hyp015-confirmation-stage-v1.md, question 2)
registers an executable view beside the paper one. Each filled paper entry is sized as a
real USD 50 market order from the Bybit rules valid at its entry
(`bybit_order_rules.size_order`):

- an entry whose order the venue would refuse, or that has no proven rules, is not an
  order: it leaves the executable pairs and the executable portfolio, and its reason is
  counted;
- an accepted entry keeps its percent returns, and its dollar results are scaled to the
  rounded notional. An accepted notional that is not finite, not positive or above the
  contract's position is a data or caller error and stops the evaluation; it is never
  quietly counted as a venue refusal;
- slots and selection are replayed with the registered rule (`replay_fixed_bank`) on
  what remains, so a refused entry frees its slot and cannot carry the result.

A WATCH whose paper entry never filled is not an order either way and stays in the funnel
exactly as in the paper evaluation. With every entry accepted at exactly the contract's
position, the result equals `evaluate_cohort`. This module reads nothing: sizings are
passed in, decided from decision-time rules and prices only.
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .momentum_flow_hold12h_verdict_report import (
    CohortEvaluation,
    PortfolioEntry,
    ProbeClass,
    ProbeRecord,
    WatchDecision,
    assemble_verdict_inputs,
    build_eligible_portfolio,
    classify_and_pair,
    replay_fixed_bank,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .bybit_order_rules import OrderSizing
    from .momentum_flow_hold12h_verdict import Hold12hVerdictContract
    from .momentum_flow_hold12h_verdict_report import ActualFundingSource

RULES_MISSING = "rules_missing"


@dataclass(frozen=True)
class ExecutableView:
    watches: tuple[WatchDecision, ...]
    probes: dict[str, ProbeRecord]
    notional_by_watch: dict[str, float]
    refused: dict[str, int]  # reason -> filled paper entries the venue would not take
    accepted: int


def executable_view(
    watches: Sequence[WatchDecision],
    probes: dict[str, ProbeRecord],
    sizings: Mapping[str, OrderSizing],
    *,
    position_usd: float,
) -> ExecutableView:
    """Split the filled entries into accepted orders and refused ones. A filled entry
    with no sizing at all counts as `rules_missing`, never as accepted. An accepted
    notional outside `0 < notional <= position_usd` raises."""
    kept: list[WatchDecision] = []
    notional: dict[str, float] = {}
    refused: Counter[str] = Counter()
    for watch in watches:
        probe = probes.get(watch.watch_id)
        if probe is None or not probe.entry_ok:
            kept.append(watch)
            continue
        sizing = sizings.get(watch.watch_id)
        if sizing is None:
            refused[RULES_MISSING] += 1
            continue
        if not sizing.feasible or sizing.notional_usd is None:
            refused[sizing.reason or RULES_MISSING] += 1
            continue
        amount = float(sizing.notional_usd)
        if not (math.isfinite(amount) and 0 < amount <= position_usd):
            raise ValueError(
                f"accepted order for {watch.watch_id} has notional {sizing.notional_usd}, "
                f"outside (0, {position_usd}]"
            )
        kept.append(watch)
        notional[watch.watch_id] = amount
    kept_ids = {w.watch_id for w in kept}
    return ExecutableView(
        watches=tuple(kept),
        probes={k: v for k, v in probes.items() if k in kept_ids},
        notional_by_watch=notional,
        refused=dict(sorted(refused.items())),
        accepted=len(notional),
    )


def _scaled(
    entries: Sequence[PortfolioEntry], notional: Mapping[str, float], position_usd: float
) -> tuple[PortfolioEntry, ...]:
    def scale(value: float | None, factor: float) -> float | None:
        return None if value is None else value * factor

    out = []
    for entry in entries:
        factor = notional[entry.tie_break] / position_usd
        out.append(
            dataclasses.replace(
                entry,
                pnl_usd=scale(entry.pnl_usd, factor),
                mae_usd=scale(entry.mae_usd, factor),
                pnl_zero_funding_usd=scale(entry.pnl_zero_funding_usd, factor),
            )
        )
    return tuple(out)


def evaluate_executable_cohort(
    contract: Hold12hVerdictContract,
    watches: Sequence[WatchDecision],
    probes: dict[str, ProbeRecord],
    funding: ActualFundingSource,
    sizings: Mapping[str, OrderSizing],
) -> tuple[CohortEvaluation, ExecutableView]:
    """The verdict inputs on the executable view, with `evaluate_cohort`'s invariants."""
    view = executable_view(watches, probes, sizings, position_usd=contract.position_usd)
    counts, pairs = classify_and_pair(view.watches, view.probes, funding)
    if sum(counts.values()) != len(view.watches):
        raise ValueError("funnel does not account for every executable WATCH decision")
    if counts[ProbeClass.ANALYZABLE] != len(pairs):
        raise ValueError("ANALYZABLE count does not equal the number of analyzable pairs")
    entries = {
        policy: _scaled(
            build_eligible_portfolio(
                view.watches,
                view.probes,
                funding,
                position_usd=contract.position_usd,
                policy_720=policy,
            ),
            view.notional_by_watch,
            contract.position_usd,
        )
        for policy in (True, False)
    }
    eligible_ids = {e.tie_break for e in entries[True]}
    if not all(p.watch_id in eligible_ids for p in pairs):
        raise ValueError("an analyzable pair is missing from the executable portfolio stream")
    portfolio_720 = replay_fixed_bank(entries[True], max_slots=contract.max_concurrent_slots)
    portfolio_240 = replay_fixed_bank(entries[False], max_slots=contract.max_concurrent_slots)
    inputs = assemble_verdict_inputs(
        contract,
        total_watches=len(view.watches),
        funnel=counts,
        pairs=pairs,
        portfolio_720=portfolio_720,
        portfolio_240=portfolio_240,
    )
    return CohortEvaluation(inputs, counts, pairs, portfolio_720, portfolio_240), view
