from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from schurfer_analytics.momentum_flow_paper_contract import FROZEN_PAPER_CONTRACT
from schurfer_analytics.momentum_flow_paper_market import ExecutableQuote, QuoteFailure
from schurfer_analytics.momentum_flow_paper_repository import (
    PaperHealth,
    PaperProbe,
    PaperRun,
    WatchCandidate,
)
from schurfer_analytics.momentum_flow_paper_worker import (
    PaperWorkerConfig,
    _process_entry,
    process_tick,
)

T0 = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _candidate(*, decision_at: datetime = T0) -> WatchCandidate:
    return WatchCandidate(
        watch_id=uuid4(),
        episode_id=uuid4(),
        exchange="bybit",
        market_type="linear",
        symbol="ERAUSDT",
        bucket_start=decision_at - timedelta(minutes=1),
        decision_at=decision_at,
    )


def _quote(
    *, observed_at: datetime = T0 + timedelta(seconds=2), side: str = "ask"
) -> ExecutableQuote:
    return ExecutableQuote(
        symbol="ERAUSDT",
        unified_symbol="ERA/USDT:USDT",
        market_id="ERAUSDT",
        side=side,
        requested_at=observed_at - timedelta(seconds=1),
        observed_at=observed_at,
        exchange_event_at=observed_at - timedelta(milliseconds=100),
        latency_ms=1000,
        best_bid=9.9,
        best_ask=10.1,
        mid=10,
        spread_bps=200,
        vwap=10.1 if side == "ask" else 9.9,
        impact_bps=100,
        filled_notional_usd=50,
        contract_size=1,
    )


@dataclass
class FakeMarket:
    results: list[ExecutableQuote | QuoteFailure]

    async def quote(self, symbol: str, side: str) -> ExecutableQuote | QuoteFailure:
        result = self.results.pop(0)
        assert result.symbol == symbol
        assert result.side == side
        return result


class FakeStore:
    def __init__(self, candidates: tuple[WatchCandidate, ...] = ()) -> None:
        self.candidates = candidates
        self.claimed: list[UUID] = []
        self.opened: list[UUID] = []
        self.stale: list[UUID] = []
        self.rejected: list[UUID] = []
        self.probes: tuple[PaperProbe, ...] = ()
        self.applied: list[UUID] = []

    async def acquire_worker_lock(self, paper_version: str) -> bool:
        return True

    async def register_run(self, **_: Any) -> PaperRun:
        return PaperRun(FROZEN_PAPER_CONTRACT.paper_version, "hash", {}, T0, "active")

    async def abandon_interrupted_entries(self, **_: Any) -> int:
        return 0

    async def due_watches(self, **_: Any) -> tuple[WatchCandidate, ...]:
        return self.candidates

    async def claim_watch(self, candidate: WatchCandidate, **_: Any) -> UUID:
        paper_id = uuid4()
        self.claimed.append(paper_id)
        return paper_id

    async def reject_stale_entry(self, paper_id: UUID, **_: Any) -> None:
        self.stale.append(paper_id)

    async def reject_quote(self, paper_id: UUID, failure: QuoteFailure) -> None:
        self.rejected.append(paper_id)

    async def open_entry(self, paper_id: UUID, quote: ExecutableQuote, **_: Any) -> None:
        self.opened.append(paper_id)

    async def expire_deadlines(self, **_: Any) -> tuple[int, int]:
        return 0, 0

    async def monitored_probes(self, **_: Any) -> tuple[PaperProbe, ...]:
        return self.probes

    async def pending_horizons(self, paper_id: UUID, **_: Any) -> tuple[int, ...]:
        return (5,)

    async def apply_quote(self, probe: PaperProbe, quote: ExecutableQuote, **_: Any) -> str | None:
        self.applied.append(probe.paper_id)
        return None

    async def record_quote_failure(self, paper_id: UUID, failure: QuoteFailure) -> None:
        self.rejected.append(paper_id)

    async def health(self, **_: Any) -> PaperHealth:
        return PaperHealth(0, 0, 0, 0, 0, 0, 0, 0, 0, None, None)


async def test_process_entry_opens_fresh_exact_quote() -> None:
    store = FakeStore()
    candidate = _candidate()

    result = await _process_entry(
        candidate,
        store=store,
        market=FakeMarket([_quote()]),
        contract=FROZEN_PAPER_CONTRACT,
        clock=lambda: T0 + timedelta(seconds=1),
    )

    assert result == "opened"
    assert store.opened == store.claimed


async def test_process_entry_rejects_stale_without_fetching_quote() -> None:
    store = FakeStore()
    candidate = _candidate()
    market = FakeMarket([])

    result = await _process_entry(
        candidate,
        store=store,
        market=market,
        contract=FROZEN_PAPER_CONTRACT,
        clock=lambda: T0 + timedelta(seconds=31),
    )

    assert result == "stale"
    assert store.stale == store.claimed
    assert market.results == []


async def test_process_tick_counts_entry_and_probe_results() -> None:
    candidate = _candidate()
    store = FakeStore((candidate,))
    probe_id = uuid4()
    store.probes = (PaperProbe(probe_id, "ERAUSDT", T0, 10, "open", 0, 0),)
    run = PaperRun(
        FROZEN_PAPER_CONTRACT.paper_version,
        "hash",
        {},
        T0 - timedelta(seconds=1),
        "active",
    )
    config = PaperWorkerConfig("postgresql://test", "redis:6379")

    result = await process_tick(
        store=store,
        market=FakeMarket([_quote(), _quote(side="bid")]),
        run=run,
        config=config,
        clock=lambda: T0 + timedelta(seconds=1),
    )

    assert result.entries_opened == 1
    assert result.probes_quoted == 1
    assert store.applied == [probe_id]


def test_runtime_cannot_override_frozen_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setenv("MOMENTUM_PAPER_POLL_INTERVAL", "10")

    with pytest.raises(ValueError, match="frozen paper contract"):
        PaperWorkerConfig.from_env()
