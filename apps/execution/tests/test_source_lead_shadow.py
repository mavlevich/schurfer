from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from schurfer_execution import source_lead_shadow as sh
from schurfer_execution.config import Config
from schurfer_execution.execution_intent import (
    ExecutionResult,
    ExecutionStatus,
    TradingMode,
    shadow_decision_id,
)

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _cfg(mode: str | None, *, dry_run: bool = True) -> Config:
    cfg = object.__new__(Config)
    cfg.source_lead_mode = mode
    cfg.dry_run = dry_run
    cfg.auto_trade = False
    cfg.db_url = "postgresql://x"
    return cfg


def test_unset_mode_is_disabled_and_only_shadow_or_disabled_are_allowed() -> None:
    assert sh.resolve_source_lead_mode(_cfg(None)) is TradingMode.DISABLED
    assert sh.resolve_source_lead_mode(_cfg("shadow")) is TradingMode.SHADOW
    assert sh.resolve_source_lead_mode(_cfg("disabled")) is TradingMode.DISABLED
    for mode in ("paper", "live_probe", "live_micro"):
        with pytest.raises(ValueError, match="only 'shadow' or 'disabled'"):
            sh.resolve_source_lead_mode(_cfg(mode))
    with pytest.raises(ValueError, match="ceiling"):
        sh.resolve_source_lead_mode(_cfg("shadow", dry_run=False))


def _market(native: str, **overrides: Any) -> dict[str, Any]:
    market = {
        "id": native,
        "symbol": f"{native[:-4]}/USDT:USDT",
        "base": native[:-4],
        "quote": "USDT",
        "settle": "USDT",
        "active": True,
        "swap": True,
        "linear": True,
    }
    market.update(overrides)
    return market


class _Exchange:
    def __init__(self, markets: list[dict[str, Any]]) -> None:
        self.markets = {f"{m['symbol']}#{i}": m for i, m in enumerate(markets)}


def test_the_instrument_is_the_one_active_linear_swap_with_that_native_id() -> None:
    ex = _Exchange([_market("ABCUSDT"), _market("ABCUSDT", active=False)])
    assert sh.resolve_instrument(ex, "ABCUSDT").symbol == "ABC/USDT:USDT"
    with pytest.raises(LookupError):
        sh.resolve_instrument(ex, "XYZUSDT")
    with pytest.raises(LookupError):
        sh.resolve_instrument(_Exchange([_market("ABCUSDT"), _market("ABCUSDT")]), "ABCUSDT")
    with pytest.raises(LookupError):
        sh.resolve_instrument(_Exchange([_market("ABCUSDT", linear=False)]), "ABCUSDT")


def test_quote_math_matches_the_capture_convention() -> None:
    vwap = sh.ask_vwap_for_notional([["2.00", "10"], ["2.10", "100"]], Decimal(50))
    assert vwap == Decimal(50) / (Decimal(10) + Decimal(30) / Decimal("2.10"))
    assert sh.ask_vwap_for_notional([["2.00", "1"]], Decimal(50)) is None
    assert sh.rounded_quantity(Decimal(50), Decimal("2.01"), Decimal(10)) == Decimal(20)


def test_late_is_measured_from_the_capture_observation() -> None:
    ep = sh.Episode(1, T0, T0 + timedelta(seconds=5), Decimal(2), "bybit:swap:ABCUSDT:1")
    on_time = sh.timing(ep, T0 + timedelta(seconds=30))
    assert on_time == {"detect_latency_ms": 30_000, "from_qualified_ms": 25_000, "late": False}
    assert sh.timing(ep, T0 + timedelta(seconds=31))["late"] is True


# --- shadow_episode ---------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.claims = 0
        self.final: dict[str, Any] = {}
        self.write_failures = 0
        self.claimed = True
        self.superseded = False

    async def claim(self, _ep: sh.Episode, _seen: datetime) -> int | None:
        self.claims += 1
        return 1 if self.claimed else None

    async def finalize(self, _row: int, fields: dict[str, Any]) -> bool:
        if self.write_failures:
            self.write_failures -= 1
            raise OSError("db down")
        if self.superseded:
            return False
        self.final = dict(fields)
        return True


class _Quotes:
    def __init__(
        self,
        *,
        age_ms: int | None = 100,
        depth: str = "1000",
        min_order: str = "1",
        book_error: bool = False,
    ) -> None:
        self.age_ms, self.depth, self.min_order, self.book_error = (
            age_ms,
            depth,
            min_order,
            book_error,
        )
        self.book_calls = 0

    async def spec(self, _symbol: str) -> sh.Spec:
        return sh.Spec(Decimal(1), Decimal(self.min_order))

    async def book(self, _symbol: str) -> sh.Book:
        self.book_calls += 1
        if self.book_error:
            raise sh.VenueError("timeout")
        received = T0 + timedelta(seconds=2)
        ts = None if self.age_ms is None else round(received.timestamp() * 1000) - self.age_ms
        return sh.Book(
            asks=[["2.02", self.depth]],
            ts_ms=ts,
            requested_at=T0 + timedelta(seconds=1, milliseconds=900),
            received_at=received,
        )


class _Broker:
    mode = TradingMode.SHADOW

    def __init__(self, status: ExecutionStatus = ExecutionStatus.SHADOW_RECORDED) -> None:
        self.status = status
        self.intents: list[Any] = []

    async def open(self, intent: Any, *, cfg: Any, rdb: Any) -> ExecutionResult:
        self.intents.append(intent)
        return ExecutionResult(mode=self.mode, status=self.status, reason="gate closed")


EPISODE = sh.Episode(
    capture_id=11,
    observed_at=T0 - timedelta(seconds=3),
    qualified_at=T0 - timedelta(seconds=1),
    capture_ask_vwap=Decimal("2.00"),
    identity_key="bybit:swap:ABCUSDT:1700000000000",
)


def _run(
    *,
    store: _Store | None = None,
    quotes: _Quotes | None = None,
    broker: _Broker | None = None,
    episode: sh.Episode = EPISODE,
    exchange: Any = None,
) -> tuple[str, _Store, _Quotes, _Broker]:
    store, quotes, broker = store or _Store(), quotes or _Quotes(), broker or _Broker()

    async def no_sleep(_s: float) -> None:
        return None

    outcome = asyncio.run(
        sh.shadow_episode(
            episode,
            store=store,  # type: ignore[arg-type]
            quotes=quotes,  # type: ignore[arg-type]
            exchange=exchange or _Exchange([_market("ABCUSDT")]),
            broker=broker,  # type: ignore[arg-type]
            cfg=_cfg("shadow"),
            rdb=None,
            now=lambda: T0,
            sleep=no_sleep,
        )
    )
    return outcome, store, quotes, broker


def test_a_valid_episode_is_recorded_through_the_shadow_broker_with_its_timing() -> None:
    outcome, store, quotes, broker = _run()
    assert outcome == "shadow_recorded"
    intent = broker.intents[0]
    assert store.final["decision_id"] == shadow_decision_id(intent)
    assert intent.instrument.native_market_id == "ABCUSDT" and intent.size_usd == 50.0
    assert intent.setup_context["detect_latency_ms"] == 3000
    assert store.final["process_latency_ms"] == 1900
    assert store.final["quote_latency_ms"] == 100
    assert store.final["book_age_ms"] == 100
    assert store.final["quote_change_bps"] == Decimal(100)  # 2.02 vs 2.00
    assert quotes.book_calls == 1


@pytest.mark.parametrize(
    ("kwargs", "outcome"),
    [
        ({"quotes": _Quotes(age_ms=5000)}, "stale_book"),
        ({"quotes": _Quotes(age_ms=None)}, "no_book_timestamp"),
        ({"quotes": _Quotes(depth="1")}, "insufficient_depth"),
        ({"quotes": _Quotes(min_order="100")}, "below_min_order"),
        ({"quotes": _Quotes(book_error=True)}, "fetch_failed"),
        ({"exchange": _Exchange([])}, "instrument_mismatch"),
        (
            {"episode": sh.Episode(11, T0, T0, Decimal(2), "binance:swap:ABCUSDT:1")},
            "instrument_mismatch",
        ),
        ({"broker": _Broker(ExecutionStatus.REJECTED)}, "broker_rejected"),
    ],
)
def test_every_skip_is_recorded_with_its_own_outcome(kwargs: dict[str, Any], outcome: str) -> None:
    result, store, _q, broker = _run(**kwargs)
    assert result == outcome
    assert store.final["outcome"] == outcome
    if outcome != "broker_rejected":
        assert broker.intents == []


def test_an_existing_claim_is_never_quoted_again() -> None:
    store = _Store()
    store.claimed = False
    outcome, _s, quotes, _b = _run(store=store)
    assert outcome == "already_claimed" and quotes.book_calls == 0


def test_a_failed_write_retries_the_same_fields_without_a_new_quote() -> None:
    store = _Store()
    store.write_failures = 2
    outcome, store, quotes, _b = _run(store=store)
    assert outcome == "shadow_recorded"
    assert quotes.book_calls == 1


def test_a_resolved_claim_is_not_overwritten() -> None:
    store = _Store()
    store.superseded = True
    assert _run(store=store)[0] == "superseded"
