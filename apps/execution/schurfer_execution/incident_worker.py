"""Bounded retry worker for durable fill-resolution incidents.

Periodically retries resolve_fill_price for every open incident. Completes the
deferred journal write (open_trade or try_commit_close) once a price is
confirmed, and gives up to manual_required after a bounded number of attempts —
never retries forever, never fabricates a price to force a resolution.
"""

from __future__ import annotations

import asyncio
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
    tracker: Any = None,
) -> None:
    while True:
        if tracker:
            tracker.tick_started()
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg)
            if tracker:
                tracker.tick_succeeded()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if tracker:
                tracker.tick_failed(e)
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

    # Complete the write FIRST, mark_resolved only once that actually
    # succeeded -- marking resolved first (an earlier draft did exactly
    # that) meant a failure inside _complete_open/_complete_close after the
    # price was confirmed left the incident permanently terminal
    # (load_open_incidents only loads pending/resolving) with the write
    # never having happened and nothing left to retry it (colleague
    # review). A price that resolves but never gets written is not
    # "resolved" from this worker's own point of view.
    completed = (
        await _complete_close(incident, symbol, resolution.price, rdb, cfg)
        if incident.operation == "close"
        else await _complete_open(incident, symbol, resolution.price, rdb, cfg)
    )
    if not completed:
        await _bump_attempt_or_escalate(
            incident, db_url, cfg, error=f"{incident.operation} completion failed after resolution"
        )
        return

    committed = await incidents.mark_resolved(
        db_url, incident.id, price=resolution.price, source=resolution.source
    )
    if not committed:
        # Someone else already resolved/claimed this incident (or it no longer
        # exists) -- the write above already landed either way, nothing lost.
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
) -> bool:
    """Returns True only once this close is durably accounted for -- either
    committed straight to the journal, or handed off to write_pending_close's
    own separate retry loop (monitor.py's _retry_pending_closes), which is
    independent of this incident's own status. False (no durable trace at
    all yet) only for the missing-trade-id case, so _process_one retries
    this incident instead of marking it resolved with nothing to show for
    it (colleague review)."""
    if not cfg.db_url:
        return False
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
        return False
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
        # try_commit_close already wrote a durable journal:pending_close
        # marker on this path -- monitor.py's own _retry_pending_closes
        # owns getting it committed from here, independent of this
        # incident's status, so this still counts as handled.
        log.error(
            "incident_worker.close_journal_failed_pending_retry",
            incident_id=incident.id,
            trade_id=trade_id,
        )
    return True


async def _complete_open(
    incident: Incident, symbol: str, price: float, rdb: Any, cfg: Config
) -> bool:
    """Returns True only once journal.complete_open actually produced a
    trade_id -- see _process_one's own comment on why this must gate
    mark_resolved."""
    if not cfg.db_url:
        return False
    setup_context = incident.context.get("setup_context")
    setup_context = setup_context if isinstance(setup_context, dict) else {}
    size_usd = float(incident.context.get("size_usd") or 0)
    leverage = int(incident.context.get("leverage") or 1)
    side = str(incident.context.get("side") or "short")
    # place_order itself has no equivalent recomputation to worry about
    # diverging from here (unlike this recovery path, it always has its own
    # already-resolved exit_params in hand) -- this is the ONE place
    # exit_params legitimately gets derived from setup_context, since a
    # FILL_UNRESOLVED incident's context is all that survives from the
    # original attempt.
    exit_params = exit_module.exit_params(setup_context.get("pump_pct"))

    # Same helper orders.place_order's own happy path calls -- one shared
    # implementation is what guarantees this recovery path and the normal
    # path can never silently write different Redis keys for the same kind
    # of confirmed open.
    trade_id = await journal.complete_open(
        cfg.db_url,
        rdb,
        symbol=symbol,
        exchange=incident.exchange,
        base=incident.base,
        side=side,
        order_id=incident.order_id,
        size_usd=size_usd,
        leverage=leverage,
        entry_price=price,
        exit_params=exit_params,
        setup_context=setup_context,
    )
    return trade_id is not None
