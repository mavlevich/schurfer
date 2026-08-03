"""Renewable, owner-checked Redis leases for exchange order operations."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Self

import structlog

log = structlog.get_logger()

if TYPE_CHECKING:
    from types import TracebackType

ORDER_LOCK_TTL_MS = 30_000
ORDER_LOCK_RENEW_INTERVAL_SECONDS = 10.0

_RENEW_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""

_RELEASE_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class OrderLockLostError(RuntimeError):
    """Raised when exclusivity could not be maintained for an order operation."""


class OrderLockLease:
    """A Redis lock whose TTL is renewed only while the caller still owns it.

    Losing the lease does not cancel an in-flight exchange request: cancellation can
    hide an order that the venue already accepted. Instead, the protected sequence
    finishes its safety/compensation path and context exit raises ``OrderLockLostError``
    so callers cannot treat the outcome as a normal success.
    """

    def __init__(
        self,
        *,
        rdb: Any,
        key: str,
        token: str,
        operation: str,
        ttl_ms: int,
        renew_interval_seconds: float,
    ) -> None:
        self._rdb = rdb
        self.key = key
        self.token = token
        self.operation = operation
        self._ttl_ms = ttl_ms
        self._renew_interval_seconds = renew_interval_seconds
        self._heartbeat: asyncio.Task[None] | None = None
        self._lost_reason: str | None = None

    @classmethod
    async def acquire(
        cls,
        *,
        rdb: Any,
        key: str,
        operation: str,
        ttl_ms: int | None = None,
        renew_interval_seconds: float | None = None,
    ) -> Self | None:
        ttl_ms = ORDER_LOCK_TTL_MS if ttl_ms is None else ttl_ms
        renew_interval_seconds = (
            ORDER_LOCK_RENEW_INTERVAL_SECONDS
            if renew_interval_seconds is None
            else renew_interval_seconds
        )
        if ttl_ms <= 0:
            raise ValueError("order lock TTL must be positive")
        if renew_interval_seconds <= 0:
            raise ValueError("order lock renewal interval must be positive")
        if renew_interval_seconds * 1000 >= ttl_ms:
            raise ValueError("order lock renewal interval must be shorter than its TTL")

        token = str(uuid.uuid4())
        acquired = await rdb.set(key, token, nx=True, px=ttl_ms)
        if not acquired:
            return None
        return cls(
            rdb=rdb,
            key=key,
            token=token,
            operation=operation,
            ttl_ms=ttl_ms,
            renew_interval_seconds=renew_interval_seconds,
        )

    @property
    def lost(self) -> bool:
        return self._lost_reason is not None

    async def __aenter__(self) -> Self:
        if self._heartbeat is not None:
            raise RuntimeError("order lock lease cannot be entered twice")
        self._heartbeat = asyncio.create_task(
            self._renew_loop(),
            name=f"order-lock:{self.operation}:{self.key}",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        await self._stop_heartbeat()
        await self._release()
        if self._lost_reason is not None and exc_type is None:
            raise OrderLockLostError(
                f"order lock lease lost during {self.operation}: {self._lost_reason}"
            )
        return False

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self._renew_interval_seconds)
            try:
                renewed = await self._rdb.eval(
                    _RENEW_LOCK,
                    1,
                    self.key,
                    self.token,
                    str(self._ttl_ms),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._mark_lost(f"renewal failed: {error}")
                return
            if int(renewed or 0) != 1:
                self._mark_lost("ownership changed or lease expired")
                return

    async def _stop_heartbeat(self) -> None:
        heartbeat = self._heartbeat
        self._heartbeat = None
        if heartbeat is None:
            return
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat

    async def _release(self) -> None:
        try:
            released = await self._rdb.eval(_RELEASE_LOCK, 1, self.key, self.token)
        except Exception as error:
            # The TTL remains the final cleanup boundary. A release transport error
            # does not prove ownership was lost while the exchange operation ran.
            log.error(
                "execution.order_lock.release_failed",
                lock_key=self.key,
                operation=self.operation,
                err=str(error),
            )
            return
        if int(released or 0) != 1:
            self._mark_lost("owner-only release found a different or expired lease")

    def _mark_lost(self, reason: str) -> None:
        if self._lost_reason is None:
            self._lost_reason = reason
            log.critical(
                "execution.order_lock.lost",
                lock_key=self.key,
                operation=self.operation,
                reason=reason,
            )
