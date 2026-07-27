"""Process lifecycle and CLI for bounded forward market-data resolvers."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import structlog

from .derivatives_context_repository import (
    DerivativesContextRepository,
    DerivativesContextStore,
)
from .derivatives_context_resolver import (
    DERIVATIVES_CONTEXT_RESOLVER_VERSION,
    DerivativesContextResolverConfig,
    resolve_derivatives_context_once,
)
from .exchange_registry import EXCHANGE_FACTORIES
from .ohlcv import TIMEFRAME
from .outcome_repository import OutcomeRepository, OutcomeStore
from .outcomes import HORIZONS_MINUTES, OutcomeConfig, resolve_once

log = structlog.get_logger()


async def run_outcome_resolver(
    cfg: OutcomeConfig,
    *,
    once: bool = False,
    store: OutcomeStore | None = None,
    context_config: DerivativesContextResolverConfig | None = None,
    context_store: DerivativesContextStore | None = None,
) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    exchanges: dict[str, Any] = {}
    owned_repository: OutcomeRepository | None = None
    owned_context_repository: DerivativesContextRepository | None = None
    try:
        for name in cfg.exchanges:
            if name in EXCHANGE_FACTORIES:
                exchanges[name] = EXCHANGE_FACTORIES[name]()
        if not exchanges:
            raise ValueError("no supported OUTCOME/PUMP_EXCHANGES configured")
        if store is None:
            owned_repository = OutcomeRepository.from_url(cfg.db_url)
            active_store: OutcomeStore = owned_repository
        else:
            active_store = store
        active_context_store: DerivativesContextStore | None = context_store
        if context_config is not None and context_config.enabled and active_context_store is None:
            owned_context_repository = DerivativesContextRepository.from_url(cfg.db_url)
            active_context_store = owned_context_repository
        log.info(
            "outcomes.starting",
            horizons=HORIZONS_MINUTES,
            timeframe=TIMEFRAME,
            exchanges=list(exchanges),
        )
        if context_config is not None and context_config.enabled:
            log.info(
                "derivatives_context.starting",
                resolver_version=DERIVATIVES_CONTEXT_RESOLVER_VERSION,
                cohort_start=context_config.cohort_start,
                supported_pairs=len(context_config.supported_pairs(tuple(exchanges))),
                before_minutes=context_config.before_minutes,
                after_minutes=context_config.after_minutes,
            )
        while True:
            resolved_count = 0
            context_count = 0
            tick_error: Exception | None = None
            try:
                resolved_count = await resolve_once(cfg, exchanges, active_store)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tick_error = exc
                log.error("outcomes.tick_failed", error=str(exc))
            if (
                context_config is not None
                and context_config.enabled
                and active_context_store is not None
            ):
                try:
                    context_count = await resolve_derivatives_context_once(
                        context_config,
                        exchanges,
                        active_context_store,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tick_error = tick_error or exc
                    log.error("derivatives_context.tick_failed", error=str(exc))
            if once:
                if tick_error is not None:
                    raise tick_error
                return
            outcome_backlog = resolved_count >= cfg.batch_size
            context_backlog = (
                context_config is not None and context_count >= context_config.batch_size
            )
            if tick_error is None and (outcome_backlog or context_backlog):
                log.info(
                    "outcomes.backlog_draining",
                    resolved_count=resolved_count,
                    batch_size=cfg.batch_size,
                    derivatives_context_count=context_count,
                )
                continue
            await asyncio.sleep(cfg.poll_interval_seconds)
    finally:
        await asyncio.gather(
            *[exchange.close() for exchange in exchanges.values()],
            return_exceptions=True,
        )
        if owned_repository is not None:
            await owned_repository.close()
        if owned_context_repository is not None:
            await owned_context_repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve decision outcomes and pump derivatives context"
    )
    parser.add_argument("--once", action="store_true", help="Resolve one due batch and exit")
    args = parser.parse_args()
    asyncio.run(
        run_outcome_resolver(
            OutcomeConfig.from_env(),
            once=args.once,
            context_config=DerivativesContextResolverConfig.from_env(),
        )
    )
