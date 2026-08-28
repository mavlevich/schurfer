"""Shared runtime input lifecycle for liquid-taker prospective reports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .exchange_registry import EXCHANGE_FACTORIES
from .replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayEpisode,
    ReplayFilters,
    build_replay_dataset,
)
from .replay_repository import ReplayRepository
from .runtime_observability import log_report_phase
from .virtual_market import DecisionMarketPath, fetch_decision_market_paths

DecisionSelector = Callable[[tuple[ReplayEpisode, ...]], tuple[ReplayDecision, ...]]


@dataclass(frozen=True)
class LiquidTakerRuntimeInputs:
    dataset: ReplayDataset
    market_paths: tuple[DecisionMarketPath, ...]


async def load_liquid_taker_runtime_inputs(
    db_url: str,
    filters: ReplayFilters,
    *,
    report_name: str,
    select_decisions: DecisionSelector,
) -> LiquidTakerRuntimeInputs:
    """Load the shared point-in-time replay graph and exact native market paths."""
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    log_report_phase(report_name, "decisions_loaded", decisions=len(decisions))

    dataset = build_replay_dataset(decisions, filters)
    log_report_phase(
        report_name,
        "dataset_built",
        eligible_episodes=len(dataset.eligible_episodes),
        episodes=len(dataset.episodes),
    )

    selected = select_decisions(dataset.eligible_episodes)
    log_report_phase(report_name, "decisions_selected", selected=len(selected))
    market_paths = await fetch_decision_market_paths(selected, EXCHANGE_FACTORIES)
    log_report_phase(report_name, "market_paths_loaded", paths=len(market_paths))
    return LiquidTakerRuntimeInputs(dataset=dataset, market_paths=market_paths)
