from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
import schurfer_analytics.liquid_taker_runtime as runtime
from schurfer_analytics.replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayEpisode,
    ReplayFilters,
)

if TYPE_CHECKING:
    from schurfer_analytics.virtual_market import DecisionMarketPath


def _filters() -> ReplayFilters:
    return ReplayFilters(
        since=datetime(2026, 8, 1, tzinfo=UTC),
        until=datetime(2026, 8, 2, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_runtime_pipeline_loads_shared_inputs_and_reports_each_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = cast("list[ReplayDecision]", [object(), object()])
    episode = cast("ReplayEpisode", object())
    dataset = cast(
        "ReplayDataset",
        SimpleNamespace(eligible_episodes=(episode,), episodes=(episode, episode)),
    )
    selected = cast("tuple[ReplayDecision, ...]", (object(),))
    paths = cast("tuple[DecisionMarketPath, ...]", (object(), object(), object()))
    phases: list[tuple[str, str, dict[str, int]]] = []

    class FakeRepository:
        closed = False

        @classmethod
        def from_url(cls, db_url: str) -> FakeRepository:
            assert cls is FakeRepository
            assert db_url == "postgresql://example"
            return repository

        async def load(self, filters: ReplayFilters) -> list[ReplayDecision]:
            assert filters == _filters()
            return decisions

        async def close(self) -> None:
            self.closed = True

    repository = FakeRepository()
    monkeypatch.setattr(runtime, "ReplayRepository", FakeRepository)
    monkeypatch.setattr(runtime, "build_replay_dataset", lambda _rows, _filters: dataset)

    async def fake_fetch(
        received: tuple[ReplayDecision, ...],
        _factories: dict[str, Any],
    ) -> tuple[DecisionMarketPath, ...]:
        assert received is selected
        return paths

    monkeypatch.setattr(runtime, "fetch_decision_market_paths", fake_fetch)
    monkeypatch.setattr(
        runtime,
        "log_report_phase",
        lambda report, phase, **counts: phases.append((report, phase, counts)),
    )

    result = await runtime.load_liquid_taker_runtime_inputs(
        "postgresql://example",
        _filters(),
        report_name="liquid_taker",
        select_decisions=lambda episodes: selected if episodes == (episode,) else (),
    )

    assert repository.closed is True
    assert result.dataset is dataset
    assert result.market_paths is paths
    assert phases == [
        ("liquid_taker", "decisions_loaded", {"decisions": 2}),
        (
            "liquid_taker",
            "dataset_built",
            {"eligible_episodes": 1, "episodes": 2},
        ),
        ("liquid_taker", "decisions_selected", {"selected": 1}),
        ("liquid_taker", "market_paths_loaded", {"paths": 3}),
    ]


@pytest.mark.asyncio
async def test_runtime_pipeline_closes_repository_when_load_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRepository:
        closed = False

        async def load(self, _filters: ReplayFilters) -> list[ReplayDecision]:
            raise RuntimeError("database unavailable")

        async def close(self) -> None:
            self.closed = True

    repository = FailingRepository()

    class FailingRepositoryFactory:
        @classmethod
        def from_url(cls, _url: str) -> FailingRepository:
            assert cls is FailingRepositoryFactory
            return repository

    monkeypatch.setattr(runtime, "ReplayRepository", FailingRepositoryFactory)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await runtime.load_liquid_taker_runtime_inputs(
            "postgresql://example",
            _filters(),
            report_name="liquid_taker",
            select_decisions=lambda _episodes: (),
        )

    assert repository.closed is True
