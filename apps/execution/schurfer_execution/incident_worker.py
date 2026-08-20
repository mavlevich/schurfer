"""Bounded retry worker for durable fill-resolution incidents.

Periodically retries resolve_fill_price for every open incident. Completes the
deferred journal write (open_trade or try_commit_close) once a price is
confirmed, and gives up to manual_required after a bounded number of attempts —
never retries forever, never fabricates a price to force a resolution.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

from . import exit as exit_module
from . import incidents, journal, notify
from .fill_price import FILL_UNRESOLVED, resolve_fill_price

if TYPE_CHECKING:
    from .config import Config
    from .incidents import Incident

log = structlog.get_logger()

_POLL_INTERVAL_SECONDS = 30
MAX_RESOLUTION_ATTEMPTS = 20
_TRADE_ID_KEY = "trade:id:{exchange}:{base}"


async def run_incident_worker(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
) -> None:
    while True:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("incident_worker.error", err=str(e))


async def _tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    if not cfg.db_url:
        return
    open_incidents = await incidents.load_open_incidents(cfg.db_url)
    for incident in open_incidents:
        try:
            await _process_one(incident, exchanges, rdb, cfg)
        except Exception as e:
            log.error(
                "incident_worker.process_error",
                incident_id=incident.id,
                base=incident.base,
                exchange=incident.exchange,
                err=str(e),
            )


async def _bump_attempt_or_escalate(
    incident: Incident, db_url: str, cfg: Config, *, error: str
) -> None:
    """Record a failed resolution attempt, escalating to manual_required (with a
    one-time alert) once MAX_RESOLUTION_ATTEMPTS is reached. Shared by every path
    that fails to make progress on an incident — an unresolved fill price, a
    missing exchange client, or anything else — so none of them can retry
    silently forever without ever bumping attempt_count towards the bound.
    """
    next_attempt_count = incident.attempt_count + 1
    if next_attempt_count >= MAX_RESOLUTION_ATTEMPTS:
        await incidents.mark_attempt(
            db_url,
            incident.id,
            status=incidents.STATUS_MANUAL_REQUIRED,
            error=f"gave up after {next_attempt_count} attempts: {error}",
        )
        log.critical(
            "incident_worker.manual_required",
            incident_id=incident.id,
            base=incident.base,
            exchange=incident.exchange,
            attempts=next_attempt_count,
        )
        creds = notify.credentials(cfg)
        if creds:
            await notify.notify_alert(
                *creds,
                text=(
                    f"Fill resolution GAVE UP after {next_attempt_count} attempts for "
                    f"{incident.operation} {incident.base}/{incident.exchange} "
                    f"(order {incident.order_id}, incident {incident.id}): {error}. "
                    "Manual reconciliation required."
                ),
            )
    else:
        await incidents.mark_attempt(
            db_url, incident.id, status=incidents.STATUS_RESOLVING, error=error
        )


async def _process_one(
    incident: Incident,
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
) -> None:
    db_url = cfg.db_url
    assert db_url  # _tick already checked

    if incident.operation == "close" and await incidents.has_pending_open(
        db_url, exchange=incident.exchange, base=incident.base
    ):
        # The matching open hasn't resolved yet, so journal.open_trade for it
        # hasn't run and this close's trade_id (captured once at creation,
        # never backfilled) may still be None. Resolving the close now risks
        # permanently losing the journal link — wait for the open side first;
        # this incident is retried again next tick untouched.
        log.info(
            "incident_worker.close_waiting_on_open",
            incident_id=incident.id,
            base=incident.base,
            exchange=incident.exchange,
        )
        return

    exchange = exchanges.get(incident.exchange)
    if exchange is None:
        await _bump_attempt_or_escalate(
            incident, db_url, cfg, error=f"exchange {incident.exchange!r} not configured"
        )
        return

    from . import symbols

    try:
        instrument = symbols.resolve_execution_instrument(exchange, incident.base)
        symbol = instrument.symbol
    except (RuntimeError, ValueError) as e:
        await _bump_attempt_or_escalate(incident, db_url, cfg, error=f"unresolved symbol: {e}")
        return
    resolution = await resolve_fill_price(exchange, symbol=symbol, order={"id": incident.order_id})

    if resolution.status == FILL_UNRESOLVED:
        await _bump_attempt_or_escalate(incident, db_url, cfg, error="fill price still unresolved")
        return

    assert resolution.price is not None
    committed = await incidents.mark_resolved(
        db_url, incident.id, price=resolution.price, source=resolution.source
    )
    if not committed:
        # Someone else already resolved/claimed this incident (or it no longer
        # exists) — nothing left to complete.
        return

    log.info(
        "incident_worker.resolved",
        incident_id=incident.id,
        base=incident.base,
        exchange=incident.exchange,
        operation=incident.operation,
        price=resolution.price,
        source=resolution.source,
    )

    if incident.operation == "close":
        await _complete_close(incident, symbol, resolution.price, rdb, cfg)
    else:
        await _complete_open(incident, symbol, resolution.price, rdb, cfg)

    if await incidents.claim_recovery_notification(db_url, incident.id):
        creds = notify.credentials(cfg)
        if creds:
            await notify.notify_alert(
                *creds,
                text=(
                    f"Fill price confirmed for {incident.operation} "
                    f"{incident.base}/{incident.exchange}: {resolution.price} "
                    f"({resolution.source}). Incident {incident.id} resolved."
                ),
            )


async def _complete_close(
    incident: Incident, symbol: str, price: float, rdb: Any, cfg: Config
) -> None:
    if not cfg.db_url:
        return
    trade_id = incident.trade_id
    if trade_id is None:
        # Wasn't captured at creation time (see incidents.has_pending_open) —
        # this is now the rarer case, e.g. the matching open ended up
        # manual_required instead of completing. Fall back to looking the
        # trade up directly rather than silently dropping this close forever.
        trade_id = await journal.find_open_trade_id(
            cfg.db_url, exchange=incident.exchange, symbol=symbol
        )
    if trade_id is None:
        log.critical(
            "incident_worker.close_missing_trade_id",
            incident_id=incident.id,
            base=incident.base,
            exchange=incident.exchange,
        )
        creds = notify.credentials(cfg)
        if creds:
            await notify.notify_alert(
                *creds,
                text=(
                    f"Fill price confirmed for close {incident.base}/{incident.exchange} "
                    f"(incident {incident.id}), but no open trade could be found in the "
                    "journal to close against. Manual reconciliation required."
                ),
            )
        return
    trade_id_key = _TRADE_ID_KEY.format(exchange=incident.exchange, base=incident.base.upper())
    reason = str(incident.context.get("reason", "reconciled"))
    committed = await journal.try_commit_close(
        cfg.db_url,
        rdb,
        exchange=incident.exchange,
        base=incident.base.upper(),
        trade_id=trade_id,
        exit_order_id=incident.order_id,
        exit_price=price,
        reason=reason,
    )
    if committed:
        await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
    else:
        log.error(
            "incident_worker.close_journal_failed_pending_retry",
            incident_id=incident.id,
            trade_id=trade_id,
        )


async def _complete_open(
    incident: Incident, symbol: str, price: float, rdb: Any, cfg: Config
) -> None:
    if not cfg.db_url:
        return
    setup_context = incident.context.get("setup_context")
    setup_context = setup_context if isinstance(setup_context, dict) else {}
    size_usd = float(incident.context.get("size_usd") or 0)
    leverage = int(incident.context.get("leverage") or 1)
    side = str(incident.context.get("side") or "short")

    trade_id = await journal.open_trade(
        cfg.db_url,
        symbol=symbol,
        exchange=incident.exchange,
        side=side,
        order_id=incident.order_id,
        size_usd=size_usd,
        leverage=leverage,
        entry_price=price,
        setup_context=setup_context,
    )
    if trade_id:
        await rdb.set(
            _TRADE_ID_KEY.format(exchange=incident.exchange, base=incident.base.upper()),
            str(trade_id),
            ex=86400,
        )

    exit_params = exit_module.exit_params(setup_context.get("pump_pct"))
    await rdb.set(
        exit_module.params_key(incident.exchange, incident.base),
        json.dumps(exit_params),
        ex=86400,
    )
    await rdb.set(
        exit_module.entry_key(incident.exchange, incident.base),
        str(price),
        ex=86400,
    )
    await rdb.set(
        exit_module.side_key(incident.exchange, incident.base),
        side,
        ex=86400,
    )
    await rdb.set(
        exit_module.size_usd_key(incident.exchange, incident.base),
        str(size_usd),
        ex=86400,
    )
