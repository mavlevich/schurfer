"""ENG-031: instrument catalogs were loaded once at startup and never again.

Production evidence, 2026-09-07: AMEMECOIN listed on bingx on 2026-09-04 while
the execution container had been running since roughly 2026-08-28. Its episode
ran to +480% and the trader wrote 29 consecutive skips with reason
`execution_instrument_unresolved` across thirteen hours; the first evaluation
after a deploy restart opened a paper trade on the same instrument. Thirty days
of the same reason: 816 skipped evaluations over seven venues.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

from schurfer_execution.market_refresh import MarketRefresher, run_market_refresher


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _client(*, markets: dict[str, object] | None = None) -> MagicMock:
    client = MagicMock()
    client.markets = markets if markets is not None else {}
    client.load_markets = AsyncMock()
    return client


class TestPeriodicRefresh:
    async def test_reloads_every_client_forcing_a_true_reload(self) -> None:
        first, second = _client(), _client()
        refresher = MarketRefresher({"bingx": first, "lbank": second})

        reloaded = await refresher.refresh_all()

        assert reloaded == 2
        # reload=True, not a cached no-op: ccxt returns the cached catalog
        # unless it is told to refetch, which is the entire point here.
        first.load_markets.assert_awaited_once_with(True)
        second.load_markets.assert_awaited_once_with(True)

    async def test_one_failing_venue_does_not_stop_the_others(self) -> None:
        broken = _client()
        broken.load_markets = AsyncMock(side_effect=RuntimeError("venue down"))
        healthy = _client()
        refresher = MarketRefresher({"broken": broken, "healthy": healthy})

        reloaded = await refresher.refresh_all()

        assert reloaded == 1
        healthy.load_markets.assert_awaited_once()

    async def test_periodic_sweep_ignores_the_per_venue_cooldown(self) -> None:
        """The sweep's own interval is its rate limit; the cooldown exists to
        bound miss-triggered reloads, and must not silence the sweep."""
        clock = _Clock()
        client = _client()
        refresher = MarketRefresher({"bingx": client}, min_interval_seconds=600.0, now=clock)

        await refresher.refresh_all()
        await refresher.refresh_all()

        assert client.load_markets.await_count == 2


class TestRefreshOnMiss:
    async def test_a_miss_reloads_the_venue(self) -> None:
        client = _client()
        refresher = MarketRefresher({"bingx": client}, now=_Clock())

        assert await refresher.refresh("bingx", reason="resolution_miss") is True
        client.load_markets.assert_awaited_once_with(True)

    async def test_repeated_misses_are_bounded_by_the_cooldown(self) -> None:
        """CZ is a real case: listed on lbank, absent from bingx and mexc. A
        base that genuinely does not exist must not turn every tick into an
        API call."""
        clock = _Clock()
        client = _client()
        refresher = MarketRefresher({"bingx": client}, min_interval_seconds=60.0, now=clock)

        assert await refresher.refresh("bingx", reason="resolution_miss") is True
        for _ in range(10):
            assert await refresher.refresh("bingx", reason="resolution_miss") is False
        assert client.load_markets.await_count == 1

        clock.advance(61.0)
        assert await refresher.refresh("bingx", reason="resolution_miss") is True
        assert client.load_markets.await_count == 2

    async def test_a_failed_reload_still_holds_off_the_next_attempt(self) -> None:
        """Otherwise a permanently broken venue turns every miss into another
        call: the cooldown has to be stamped before the attempt, not after a
        success."""
        clock = _Clock()
        client = _client()
        client.load_markets = AsyncMock(side_effect=RuntimeError("venue down"))
        refresher = MarketRefresher({"bingx": client}, min_interval_seconds=60.0, now=clock)

        assert await refresher.refresh("bingx", reason="resolution_miss") is False
        assert await refresher.refresh("bingx", reason="resolution_miss") is False
        assert client.load_markets.await_count == 1

    async def test_concurrent_misses_on_one_venue_reload_once(self) -> None:
        clock = _Clock()
        client = _client()
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow(_reload: bool) -> None:
            started.set()
            await release.wait()

        client.load_markets = AsyncMock(side_effect=_slow)
        refresher = MarketRefresher({"bingx": client}, min_interval_seconds=60.0, now=clock)

        first = asyncio.create_task(refresher.refresh("bingx", reason="miss_a"))
        await started.wait()
        second = asyncio.create_task(refresher.refresh("bingx", reason="miss_b"))
        await asyncio.sleep(0)
        release.set()

        assert await first is True
        assert await second is False
        assert client.load_markets.await_count == 1

    async def test_an_unknown_exchange_is_a_no_op(self) -> None:
        refresher = MarketRefresher({})
        assert await refresher.refresh("nowhere", reason="resolution_miss") is False


class TestRefresherWorker:
    async def test_sweeps_on_the_interval_and_reports_to_the_tracker(self) -> None:
        client = _client()
        refresher = MarketRefresher({"bingx": client})
        tracker = MagicMock()

        task = asyncio.create_task(
            run_market_refresher(refresher, interval_seconds=0.01, tracker=tracker)
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if client.load_markets.await_count:
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert client.load_markets.await_count >= 1
        tracker.tick_started.assert_called()
        tracker.tick_succeeded.assert_called()

    async def test_a_failing_sweep_keeps_the_worker_alive(self) -> None:
        refresher = MagicMock()
        refresher.refresh_all = AsyncMock(side_effect=RuntimeError("boom"))
        tracker = MagicMock()

        task = asyncio.create_task(
            run_market_refresher(refresher, interval_seconds=0.01, tracker=tracker)
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if refresher.refresh_all.await_count >= 2:
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert refresher.refresh_all.await_count >= 2
        tracker.tick_failed.assert_called()
