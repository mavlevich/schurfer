from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from schurfer_analytics import source_lead_exit_capture as ex

ENTRY = datetime(2026, 9, 29, 12, 0, 20, tzinfo=UTC)


def test_target_is_the_end_of_the_v2_exit_bar() -> None:
    # entry 12:00:20 + 30m = 12:30:20 -> exit bar opens 12:31:00 -> closes 12:32:00.
    assert ex.exit_target_at(ENTRY) == datetime(2026, 9, 29, 12, 32, tzinfo=UTC)
    on_minute = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert ex.exit_target_at(on_minute) == datetime(2026, 9, 29, 12, 31, tzinfo=UTC)


def test_native_symbol_comes_from_the_registered_identity_key() -> None:
    assert ex.native_symbol("bybit:swap:ABCUSDT:1700000000000") == "ABCUSDT"
    assert ex.native_symbol("binance:swap:ABCUSDT:1") is None
    assert ex.native_symbol("bybit:spot:ABCUSDT:1") is None
    assert ex.native_symbol(None) is None


def test_hypothetical_quantity_is_rounded_down_to_the_step_and_both_are_kept() -> None:
    raw, qty = ex.hypothetical_quantity(Decimal(50), Decimal("2.01"), Decimal(10))
    assert raw == Decimal(50) / Decimal("2.01")
    assert qty == Decimal(20)
    with pytest.raises(ValueError, match="positive"):
        ex.hypothetical_quantity(Decimal(50), Decimal(0), Decimal(1))


def test_exit_book_sells_the_quantity_into_the_bids() -> None:
    summary = ex.summarize_exit_book([["1.99", "10"], ["1.98", "30"]], [["2.01", "5"]], Decimal(20))
    assert summary.best_bid == Decimal("1.99")
    assert summary.bid_vwap == (Decimal("19.9") + Decimal("19.8")) / 20
    assert summary.bid_filled_qty == Decimal(20)
    assert summary.spread_bps == Decimal("0.02") / Decimal("2.00") * 10_000
    assert summary.impact_bps is not None and summary.impact_bps > 0


def test_a_book_too_thin_for_the_quantity_has_no_vwap() -> None:
    summary = ex.summarize_exit_book([["1.99", "5"]], [["2.01", "5"]], Decimal(20))
    assert summary.bid_vwap is None
    assert summary.bid_filled_qty == Decimal(5)
    with pytest.raises(ValueError, match="crossed"):
        ex.summarize_exit_book([["2.05", "5"]], [["2.01", "5"]], Decimal(1))


@pytest.mark.parametrize(
    ("delay_s", "label"),
    [(0, "on_time"), (30, "on_time"), (31, "late"), (120, "late"), (121, "missed")],
)
def test_timeliness_boundaries(delay_s: int, label: str) -> None:
    target = datetime(2026, 9, 29, 12, 32, tzinfo=UTC)
    assert ex.timeliness(target, target + timedelta(seconds=delay_s)) == (label, delay_s * 1000)


@pytest.mark.parametrize(
    ("age", "fresh"),
    [(0, True), (2000, True), (2001, False), (-1000, True), (-1001, False), (None, False)],
)
def test_book_freshness_uses_the_qualification_limits(age: int | None, fresh: bool) -> None:
    assert ex.book_is_fresh(age) is fresh


# --- capture_episode ---------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now_value = start

    def now(self) -> datetime:
        return self.now_value

    async def sleep(self, seconds: float) -> None:
        self.now_value += timedelta(seconds=seconds)


class FakeStore:
    def __init__(self) -> None:
        self.claims: list[tuple[int, str, str | None]] = []
        self.final: dict[int, dict[str, Any]] = {}
        self.already = False

    async def claim(
        self, episode: ex.DueEpisode, *, outcome: str, timeliness: str | None
    ) -> int | None:
        if self.already:
            return None
        self.claims.append((episode.capture_id, outcome, timeliness))
        return len(self.claims)

    async def finalize(self, row_id: int, fields: dict[str, Any]) -> None:
        self.final[row_id] = fields


class FakeClient:
    def __init__(self, clock: Clock, *, failures: int = 0, book_delay_ms: int = 100) -> None:
        self.clock = clock
        self.failures = failures
        self.calls = 0
        self.book_delay_ms = book_delay_ms

    async def qty_step(self, _symbol: str) -> Decimal:
        return Decimal(1)

    async def book(self, _symbol: str) -> ex.RawBook:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("timeout")
        now = self.clock.now()
        ts = round(now.timestamp() * 1000) - self.book_delay_ms
        payload = {
            "retCode": 0,
            "time": ts,
            "result": {
                "s": "ABCUSDT",
                "b": [["1.99", "100"]],
                "a": [["2.01", "100"]],
                "ts": ts,
                "u": 7,
                "seq": 9,
                "cts": ts - 5,
            },
        }
        return ex.RawBook(payload, now, now)


def _episode(**overrides: Any) -> ex.DueEpisode:
    values: dict[str, Any] = {
        "capture_id": 11,
        "qualification_version": "source_lead_qualified_capture_v4",
        "target_exchange": "bybit",
        "entry_at": ENTRY,
        "notional_usd": Decimal(50),
        "ask_vwap": Decimal("2.01"),
        "identity_key": "bybit:swap:ABCUSDT:1700000000000",
    }
    values.update(overrides)
    return ex.DueEpisode(**values)


def _run(episode: ex.DueEpisode, store: FakeStore, client: FakeClient, clock: Clock) -> str:
    return asyncio.run(
        ex.capture_episode(episode, store, client, now=clock.now, sleep=clock.sleep)  # type: ignore[arg-type]
    )


def test_an_episode_is_claimed_before_the_request_and_sampled_on_time() -> None:
    episode = _episode()
    clock = Clock(episode.target_at - timedelta(seconds=15))
    store, client = FakeStore(), FakeClient(clock)
    assert _run(episode, store, client, clock) == "sampled"
    assert store.claims == [(11, "claimed", None)]
    row = store.final[1]
    assert row["timeliness"] == "on_time"
    assert row["hypothetical_qty"] == Decimal(24)  # $50 / 2.01 = 24.87 -> step 1
    assert row["book_seq"] == 9 and row["book_update_id"] == 7
    assert row["book_snapshot"]["b"] == [["1.99", "100"]]
    assert len(row["book_sha256"]) == 64
    assert row["attempts"] == 1


def test_fetch_failures_are_retried_inside_the_window() -> None:
    episode = _episode()
    clock = Clock(episode.target_at)
    store, client = FakeStore(), FakeClient(clock, failures=7)
    assert _run(episode, store, client, clock) == "sampled"
    row = store.final[1]
    assert row["attempts"] == 8
    assert row["timeliness"] == "late"  # 7 retries x 5 s = 35 s after target


def test_a_fetch_failing_for_the_whole_window_is_fetch_failed_and_missed() -> None:
    episode = _episode()
    clock = Clock(episode.target_at)
    store, client = FakeStore(), FakeClient(clock, failures=10_000)
    assert _run(episode, store, client, clock) == "fetch_failed"
    row = store.final[1]
    assert row["timeliness"] == "missed"
    assert "timeout" in row["error"]


def test_a_stale_book_is_stored_but_marked_stale() -> None:
    episode = _episode()
    clock = Clock(episode.target_at)
    store, client = FakeStore(), FakeClient(clock, book_delay_ms=5_000)
    assert _run(episode, store, client, clock) == "stale_book"
    assert store.final[1]["timeliness"] == "on_time"


def test_an_episode_discovered_after_the_window_is_recorded_as_missed_without_a_fetch() -> None:
    episode = _episode()
    clock = Clock(episode.target_at + timedelta(minutes=10))
    store, client = FakeStore(), FakeClient(clock)
    assert _run(episode, store, client, clock) == "missed"
    assert store.claims == [(11, "missed", "missed")]
    assert client.calls == 0


def test_an_existing_claim_is_never_requested_again() -> None:
    episode = _episode()
    clock = Clock(episode.target_at)
    store, client = FakeStore(), FakeClient(clock)
    store.already = True
    assert _run(episode, store, client, clock) == "already_claimed"
    assert client.calls == 0


@pytest.mark.parametrize(
    ("overrides", "outcome"),
    [
        ({"target_exchange": "binance"}, "unsupported_venue"),
        ({"identity_key": "garbage"}, "instrument_unresolved"),
        ({"ask_vwap": None}, "instrument_unresolved"),
    ],
)
def test_unusable_episodes_are_recorded_not_skipped(
    overrides: dict[str, Any], outcome: str
) -> None:
    episode = _episode(**overrides)
    clock = Clock(episode.target_at)
    store, client = FakeStore(), FakeClient(clock)
    assert _run(episode, store, client, clock) == outcome
    assert store.claims[0][1] == outcome
    assert client.calls == 0
