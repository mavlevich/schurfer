import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution import order_lock as order_lock_module
from schurfer_execution.order_lock import OrderLockLease, OrderLockLostError
from schurfer_execution.orders import place_order
from schurfer_execution.risk import PNL_READY_KEY, TRADING_ENABLED_KEY
from schurfer_execution.supervisor import WorkerReadinessGate


class _LeaseRedis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float]] = {}
        self.renewals = 0

    def _expire(self, key: str) -> None:
        current = self.values.get(key)
        if current is not None and current[1] > 0 and time.monotonic() >= current[1]:
            self.values.pop(key, None)

    def value(self, key: str) -> str | None:
        self._expire(key)
        current = self.values.get(key)
        return current[0] if current is not None else None

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        px: int | None = None,
        ex: int | None = None,
    ) -> bool | None:
        self._expire(key)
        if nx and key in self.values:
            return None
        ttl_seconds = px / 1000 if px is not None else float(ex or 0)
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds > 0 else 0.0
        self.values[key] = (str(value), expires_at)
        return True

    async def get(self, key: str) -> bytes | None:
        if key in {TRADING_ENABLED_KEY, PNL_READY_KEY}:
            return b"1"
        value = self.value(key)
        return value.encode() if value is not None else None

    async def delete(self, key: str) -> int:
        existed = key in self.values
        self.values.pop(key, None)
        return int(existed)

    async def eval(
        self,
        script: str,
        _numkeys: int,
        key: str,
        token: str,
        *args: str,
    ) -> int:
        self._expire(key)
        if "pexpire" in script:
            if self.value(key) != token:
                return 0
            self.values[key] = (token, time.monotonic() + int(args[0]) / 1000)
            self.renewals += 1
            return 1
        if self.value(key) != token:
            return 0
        self.values.pop(key, None)
        return 1


async def test_lease_renews_during_operation_longer_than_initial_ttl() -> None:
    rdb = _LeaseRedis()
    lease = await OrderLockLease.acquire(
        rdb=rdb,
        key="lock:order:bybit:BEAT",
        operation="open",
        ttl_ms=150,
        renew_interval_seconds=0.02,
    )
    assert lease is not None

    async with lease:
        # Simulates a slow sequence of exchange API calls. A fixed 150 ms lock
        # would have expired well before the competing request below.
        await asyncio.sleep(0.25)
        competing = await rdb.set(
            "lock:order:bybit:BEAT",
            "second-owner",
            nx=True,
            px=150,
        )
        assert competing is None
        await asyncio.sleep(0.1)

    assert rdb.renewals >= 5
    assert rdb.value("lock:order:bybit:BEAT") is None


async def test_lease_loss_is_raised_without_deleting_new_owner() -> None:
    rdb = _LeaseRedis()
    lease = await OrderLockLease.acquire(
        rdb=rdb,
        key="lock:order:bybit:BEAT",
        operation="close",
        ttl_ms=150,
        renew_interval_seconds=0.02,
    )
    assert lease is not None

    with pytest.raises(OrderLockLostError, match="ownership changed"):
        async with lease:
            rdb.values["lock:order:bybit:BEAT"] = (
                "replacement-owner",
                time.monotonic() + 1,
            )
            await asyncio.sleep(0.05)

    assert rdb.value("lock:order:bybit:BEAT") == "replacement-owner"


async def test_lease_contention_does_not_start_second_heartbeat() -> None:
    rdb = _LeaseRedis()
    first = await OrderLockLease.acquire(
        rdb=rdb,
        key="lock:order:bybit:BEAT",
        operation="open",
        ttl_ms=150,
        renew_interval_seconds=0.02,
    )
    second = await OrderLockLease.acquire(
        rdb=rdb,
        key="lock:order:bybit:BEAT",
        operation="close",
        ttl_ms=150,
        renew_interval_seconds=0.02,
    )

    assert first is not None
    assert second is None
    async with first:
        pass


async def test_slow_exchange_open_keeps_the_real_place_order_lock_renewed() -> None:
    rdb = _LeaseRedis()
    exchange = MagicMock()
    exchange.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
    exchange.set_leverage = AsyncMock()
    exchange.fetch_ticker = AsyncMock(return_value={"last": 1.0})
    exchange.amount_to_precision = MagicMock(return_value="50.0")
    exchange.price_to_precision = MagicMock(return_value="1.1")

    async def slow_market_order(*_args: object, **_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0.25)
        return {"id": "entry-1", "status": "closed", "average": 1.0}

    exchange.create_market_order = AsyncMock(side_effect=slow_market_order)
    exchange.create_stop_market_order = AsyncMock(return_value={"id": "sl-1"})
    lock_key = "lock:order:bybit:BEAT"

    async def competing_open() -> bool | None:
        await asyncio.sleep(0.2)
        return await rdb.set(lock_key, "competing-owner", nx=True, px=150)

    competitor = asyncio.create_task(competing_open())
    with (
        patch.object(order_lock_module, "ORDER_LOCK_TTL_MS", 150),
        patch.object(order_lock_module, "ORDER_LOCK_RENEW_INTERVAL_SECONDS", 0.02),
        patch("schurfer_execution.orders.fetch_positions", return_value=([], set())),
        patch(
            "schurfer_execution.orders.fetch_margin_balance",
            return_value=[{"exchange": "bybit", "free": 1_000.0}],
        ),
    ):
        result = await place_order(
            base="BEAT",
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="short",
            size_usd=50.0,
            leverage=2,
            exchanges={"bybit": exchange},
            rdb=rdb,
            max_positions=5,
            max_position_usd=500.0,
            daily_loss_limit_usd=200.0,
            worker_gate=WorkerReadinessGate(set()),
        )

    assert result["allowed"] is True
    assert await competitor is None
    assert rdb.renewals >= 5
