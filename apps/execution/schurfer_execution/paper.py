"""Paper trading: track simulated positions in Redis, monitor exit conditions."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from . import episodes, journal, liquidity, notify, symbols
from . import exit as exit_module

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()


def _display_strategy(setup_context: dict[str, Any]) -> str:
    """Canonical strategy identity for Telegram display.

    Reuses journal.strategy_identity() -- the single source of truth for
    parsing name/version across every setup_context convention a caller
    might use (explicit strategy_name, a combined "name_vN" in strategy, or
    pump_short's bare strategy_version) -- instead of each call site
    inventing its own single-key lookup. The naive `setup_context.get(
    "strategy", "unknown")` this replaced only ever found a value for
    early_momentum/liquidation_cascade (which happen to set "strategy"
    directly); pump_short never has that key, only "strategy_version", so
    every pump_short Telegram message showed "Strategy: unknown" (verified
    against production, 2026-08-23). A malformed identity must still never
    crash notification -- falls back to "unknown" only on that specific
    failure, not for every strategy that doesn't set one particular key.
    """
    try:
        name, version = journal.strategy_identity(setup_context)
    except ValueError:
        return "unknown"
    return f"{name} v{version}"


_KEY_PREFIX = "position:paper:"
_TRADE_ID_KEY = "trade:id:paper:{exchange}:{base}"
_INTERVAL_SECONDS = 30

# A separate namespace from the real position key, never a partial payload
# written under position:paper:* itself -- _tick scans that exact prefix
# every 30s and parses whatever it finds as a full position; a placeholder
# there would either crash it or make it mis-evaluate exit conditions on
# incomplete data. Acquire is a native atomic SET NX (no Lua needed for
# that half); release/replace is CAS'd by token via Lua, same GET==token
# idiom as journal._CAS_DELETE / order_lock._RELEASE_LOCK -- never a plain
# unconditional DELETE, which could remove a different, newer reservation
# that reused the same instrument key in the meantime.
_RESERVATION_KEY_PREFIX = "position:paper:reservation:"
_RESERVATION_TTL_SECONDS = 30

_RELEASE_RESERVATION = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Checking "does the real position already exist" and "acquire the
# reservation" must be one atomic step, not two separate round trips --
# otherwise a legacy Redis-only position (from an older flow that never
# used the reservation namespace at all, so this instrument's real key
# already exists but nothing is holding a reservation) could still be
# clobbered by a reservation acquired in the gap between the two calls
# (colleague review).
_RESERVE_POSITION = """
if redis.call("exists", KEYS[1]) == 1 then
    return 0
end
if redis.call("set", KEYS[2], ARGV[1], "NX", "EX", ARGV[2]) then
    return 1
else
    return 0
end
"""

# Scoped by episode_id (when the position carries one) rather than an
# unconditional DELETE, so a stale/delayed close retry for an old episode
# can never remove a newer position that has since opened on the same
# exchange:base key. cjson is part of Redis's stock Lua environment.
_DELETE_POSITION_IF_EPISODE_MATCHES = """
local current = redis.call("get", KEYS[1])
if current == false then
    return 0
end
local ok, decoded = pcall(cjson.decode, current)
if not ok or type(decoded) ~= "table" then
    return 0
end
if decoded["episode_id"] == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def paper_key(exchange: str, base: str) -> str:
    return f"{_KEY_PREFIX}{exchange}:{base.upper()}"


def reservation_key(exchange: str, base: str) -> str:
    return f"{_RESERVATION_KEY_PREFIX}{exchange}:{base.upper()}"


async def reserve_position(
    rdb: Any,
    *,
    exchange: str,
    base: str,
    token: str,
    ttl_seconds: int = _RESERVATION_TTL_SECONDS,
) -> bool:
    """Fast, atomic fail-closed check for "is anything else already opening
    a position on this instrument" -- called right after claiming an
    episode, before spending an exchange round-trip on a quote. A conflict
    here is a `position_exists`/`suppressed` terminal outcome for the
    episode, not a wasted network call.

    Also refuses to reserve when the real position:paper:* key already
    exists, even if nothing currently holds a reservation for it -- guards
    against clobbering a legacy Redis-only position from a different/older
    flow that never used this reservation namespace at all."""
    acquired = await rdb.eval(
        _RESERVE_POSITION,
        2,
        paper_key(exchange, base),
        reservation_key(exchange, base),
        token,
        ttl_seconds,
    )
    return int(acquired or 0) == 1


async def release_reservation(rdb: Any, *, exchange: str, base: str, token: str) -> bool:
    """CAS release -- only the holder of `token` can release its own
    reservation. A stale/expired caller's release can never remove a
    different (newer) reservation that has since taken the same key."""
    released = await rdb.eval(_RELEASE_RESERVATION, 1, reservation_key(exchange, base), token)
    return int(released or 0) == 1


def _build_entry_payload(
    *,
    instrument: symbols.ExecutionInstrument,
    price: float,
    size_usd: float,
    leverage: int,
    score: int,
    side: str,
    strategy: str,
    params: dict[str, float],
    accounting_version: str,
    entry_slippage_bps: float | None,
    exit_slippage_bps: float | None,
    episode_id: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "base": instrument.base,
        "symbol": instrument.symbol,
        "exchange": instrument.exchange,
        "side": side,
        "strategy": strategy,
        "entry_price": price,
        "size_usd": size_usd,
        "leverage": leverage,
        "opened_at": time.time(),
        "score": score,
        "exit_params": params,
        "accounting_version": accounting_version,
        "entry_slippage_bps": entry_slippage_bps,
        "exit_slippage_bps": exit_slippage_bps,
    }
    if episode_id is not None:
        # early_momentum_v3+: threaded through so close_paper can scope its
        # CAS-delete of the position key by episode_id, not just trade_id.
        entry["episode_id"] = episode_id
    return entry


async def _finish_open(
    rdb: Any,
    *,
    instrument: symbols.ExecutionInstrument,
    entry: dict[str, Any],
    trade_id: int | None,
    cfg: Config,
    strategy: str,
    price: float,
    score: int,
    side: str,
) -> None:
    # trade_id is embedded directly in the position payload (not only the
    # separate trade:id:paper:* key) so close_paper doesn't depend on that
    # second key surviving independently -- a crash between these two
    # rdb.set calls used to be able to leave a position with no discoverable
    # trade_id at all, since close_paper only ever read the separate key
    # (colleague review). The separate key is still written too, for any
    # other reader still relying on it and for reconcile's own repair.
    if trade_id is not None:
        entry = {**entry, "trade_id": trade_id}
    # Written once, complete -- never a partial payload under this key (see
    # reserve_position's docstring for why that matters for _tick).
    await rdb.set(paper_key(instrument.exchange, instrument.base), json.dumps(entry), ex=86400 * 7)
    if trade_id is not None:
        await rdb.set(
            _TRADE_ID_KEY.format(exchange=instrument.exchange, base=instrument.base.upper()),
            str(trade_id),
            ex=86400 * 7,
        )

    creds = notify.credentials(cfg)
    if creds:
        await notify.notify_open(
            *creds,
            strategy=strategy,
            base=instrument.base,
            exchange=instrument.exchange,
            size_usd=entry["size_usd"],
            leverage=entry["leverage"],
            price=price,
            score=score,
            side=side,
            paper=True,
        )

    log.info(
        "paper.opened",
        symbol=instrument.symbol,
        exchange=instrument.exchange,
        price=price,
        score=score,
    )


async def open_paper(
    rdb: Any,
    *,
    instrument: symbols.ExecutionInstrument,
    price: float,
    size_usd: float,
    leverage: int,
    score: int,
    setup_context: dict[str, Any],
    cfg: Config,
    side: str = "short",
    exit_params: dict[str, float] | None = None,
) -> int | None:
    """Returns the journaled trade_id (None if cfg.db_url is unset or the
    journal write failed) -- the caller already computes this value via
    journal.open_trade below; it used to be discarded (execution_intent.py's
    PaperBroker needs an honest trade_id, not a routing-only bool)."""
    params = (
        exit_params
        if exit_params is not None
        else exit_module.exit_params(setup_context.get("pump_pct"))
    )
    paper_context = {**setup_context, "paper": True}
    (
        accounting_version,
        _accounting_status,
        entry_slippage_bps,
        exit_slippage_bps,
    ) = journal.accounting_contract(paper_context, side=side)
    strategy = _display_strategy(setup_context)
    entry = _build_entry_payload(
        instrument=instrument,
        price=price,
        size_usd=size_usd,
        leverage=leverage,
        score=score,
        side=side,
        strategy=strategy,
        params=params,
        accounting_version=accounting_version,
        entry_slippage_bps=entry_slippage_bps,
        exit_slippage_bps=exit_slippage_bps,
    )

    trade_id: int | None = None
    if cfg.db_url:
        trade_id = await journal.open_trade(
            cfg.db_url,
            symbol=instrument.symbol,
            exchange=instrument.exchange,
            side=side,
            order_id=None,
            size_usd=size_usd,
            leverage=leverage,
            entry_price=price,
            setup_context=paper_context,
        )

    await _finish_open(
        rdb,
        instrument=instrument,
        entry=entry,
        trade_id=trade_id,
        cfg=cfg,
        strategy=strategy,
        price=price,
        score=score,
        side=side,
    )
    return trade_id


async def open_paper_for_episode(
    rdb: Any,
    *,
    instrument: symbols.ExecutionInstrument,
    price: float,
    size_usd: float,
    leverage: int,
    score: int,
    setup_context: dict[str, Any],
    cfg: Config,
    side: str,
    exit_params: dict[str, float],
    episode_id: str,
    claim_token: str,
    entry_idempotency_key: str,
) -> journal.OpenTradeOutcome:
    """early_momentum_v3: the trade row and the episode's claimed->opened
    transition are already committed atomically by
    journal.open_trade_for_episode before this returns -- this only writes
    the Redis position (once, complete, never partial under paper_key) and
    sends the open notification. Returns the OpenTradeOutcome so the caller
    can tell created vs recovered and bail out cleanly when trade_id is None
    (claim invalid, or an idempotency-key collision journal already logged).
    """
    if not cfg.db_url:
        raise ValueError("open_paper_for_episode requires cfg.db_url")
    # exit_params is persisted into the durable setup_context (not just the
    # Redis entry payload below) so reconcile_missing_positions can rebuild
    # a crash-orphaned position using the SAME exit contract this trade was
    # actually opened under -- not whatever the code's exit_params constant
    # happens to be at reconcile time, which may have since changed
    # (colleague review).
    paper_context = {**setup_context, "paper": True, "exit_params": exit_params}
    (
        accounting_version,
        _accounting_status,
        entry_slippage_bps,
        exit_slippage_bps,
    ) = journal.accounting_contract(paper_context, side=side)
    strategy = _display_strategy(setup_context)

    outcome = await journal.open_trade_for_episode(
        cfg.db_url,
        episode_id=episode_id,
        claim_token=claim_token,
        symbol=instrument.symbol,
        exchange=instrument.exchange,
        side=side,
        size_usd=size_usd,
        leverage=leverage,
        entry_price=price,
        entry_idempotency_key=entry_idempotency_key,
        setup_context=paper_context,
    )
    if outcome.trade_id is None:
        return outcome

    entry = _build_entry_payload(
        instrument=instrument,
        price=price,
        size_usd=size_usd,
        leverage=leverage,
        score=score,
        side=side,
        strategy=strategy,
        params=exit_params,
        accounting_version=accounting_version,
        entry_slippage_bps=entry_slippage_bps,
        exit_slippage_bps=exit_slippage_bps,
        episode_id=episode_id,
    )
    await _finish_open(
        rdb,
        instrument=instrument,
        entry=entry,
        trade_id=outcome.trade_id,
        cfg=cfg,
        strategy=strategy,
        price=price,
        score=score,
        side=side,
    )
    return outcome


def _rebuild_entry_payload_from_trade(trade: journal.OpenEpisodeTrade) -> dict[str, Any] | None:
    # exit_params must come from THIS trade's own historical setup_context,
    # never the code's current exit_params constant -- if that constant has
    # since changed, silently using it here would rewrite a live position's
    # actual exit contract out from under it (colleague review). A missing
    # value means open_paper_for_episode never persisted it (a genuinely
    # older/malformed row) -- a hard skip, not a silent fallback.
    exit_params = trade.setup_context.get("exit_params")
    if not exit_params:
        log.error(
            "paper.reconcile_missing_exit_params",
            trade_id=trade.trade_id,
            episode_id=trade.episode_id,
            symbol=trade.symbol,
        )
        return None
    # CCXT unified symbols are always "BASE/QUOTE:SETTLE" -- the same split
    # every other caller here relies on to get from a stored symbol back to
    # the base used in paper_key.
    base = trade.symbol.split("/")[0]
    return {
        "base": base,
        "symbol": trade.symbol,
        "exchange": trade.exchange,
        "side": trade.side,
        "strategy": trade.setup_context.get("strategy", "unknown"),
        "entry_price": trade.entry_price,
        "size_usd": trade.size_usd,
        "leverage": trade.leverage,
        "opened_at": trade.entry_at.timestamp(),
        "score": trade.setup_context.get("score", 100),
        "exit_params": exit_params,
        "accounting_version": trade.accounting_version,
        "entry_slippage_bps": trade.entry_slippage_bps,
        "exit_slippage_bps": trade.exit_slippage_bps,
        "episode_id": trade.episode_id,
        "trade_id": trade.trade_id,
    }


async def reconcile_missing_positions(rdb: Any, cfg: Config) -> int:
    """Repair a Redis position key (and its trade-id key) that a crash
    between open_trade_for_episode's DB commit and this module's own
    rdb.set(...) left missing.

    Without this, such a trade is a genuine orphan: durably 'open' in
    Postgres and its episode already 'opened' (so list_actionable's own
    armed/claimed reconciliation never surfaces it either), but with no
    position:paper:* key for `_tick` to ever monitor for TP/SL/max-hold
    (colleague review). Cheap and idempotent -- safe to call every trigger
    tick alongside the episode-lifecycle reconciliation.
    """
    if not cfg.db_url:
        return 0
    trades = await journal.find_open_episode_trades(cfg.db_url)
    repaired = 0
    for trade in trades:
        base = trade.symbol.split("/")[0]
        key = paper_key(trade.exchange, base)
        trade_id_key = _TRADE_ID_KEY.format(exchange=trade.exchange, base=base.upper())
        position_missing = not await rdb.exists(key)
        # The two keys are independent failure points -- a crash can take
        # either one without the other, e.g. the position rdb.set succeeds
        # but the process dies before the trade-id rdb.set runs. Repairing
        # only when the position key is missing used to leave that second
        # case unrepaired forever, even though close_paper's fallback path
        # for legacy positions (without an embedded trade_id) still depends
        # on this key existing (colleague review).
        trade_id_missing = not await rdb.exists(trade_id_key)
        if not position_missing and not trade_id_missing:
            continue
        if position_missing:
            entry = _rebuild_entry_payload_from_trade(trade)
            if entry is None:
                continue
            await rdb.set(key, json.dumps(entry), ex=86400 * 7)
        if position_missing or trade_id_missing:
            await rdb.set(trade_id_key, str(trade.trade_id), ex=86400 * 7)
        log.warning(
            "paper.reconciled_missing_position",
            trade_id=trade.trade_id,
            episode_id=trade.episode_id,
            symbol=trade.symbol,
            position_repaired=position_missing,
            trade_id_key_repaired=True,
        )
        repaired += 1
    return repaired


async def close_paper(
    rdb: Any,
    *,
    pos: dict[str, Any],
    current_price: float,
    reason: str,
    cfg: Config,
    exchange_client: Any | None = None,
) -> None:
    base = pos["base"]
    exchange = pos["exchange"]
    symbol = pos.get("symbol")
    if not symbol and exchange_client is not None:
        try:
            symbol = symbols.resolve_execution_instrument(exchange_client, base).symbol
        except (RuntimeError, ValueError) as exc:
            log.warning(
                "paper.close.unresolved_legacy_symbol",
                base=base,
                exchange=exchange,
                err=str(exc),
            )
    entry_price = float(pos["entry_price"])
    side = pos.get("side", "short")
    strategy = pos.get("strategy", "unknown")
    leverage_raw = pos.get("leverage")

    # Fallback figures for the case journal.close_trade never runs at all (no
    # DB, no trade_id, or exchange_client unavailable). Once close_trade does
    # run, its CloseOutcome below is the single source of truth for both the
    # DB row and this notification — never recomputed independently, so the
    # two can never diverge.
    gross_pnl_pct = (
        (entry_price - current_price) / entry_price * 100
        if side == "short"
        else (current_price - entry_price) / entry_price * 100
    )
    accounting_status = "legacy"
    displayed_pnl_pct = gross_pnl_pct
    # A position stored before size tracking existed has no size_usd at all —
    # None stays None rather than fabricating a dollar figure.
    size_usd_raw = pos.get("size_usd")
    displayed_pnl_usd = (
        float(size_usd_raw) * gross_pnl_pct / 100 if size_usd_raw is not None else None
    )
    fees_usd: float | None = None
    funding_usd: float | None = None
    slippage_usd: float | None = None

    trade_id_key = _TRADE_ID_KEY.format(exchange=exchange, base=base.upper())
    # Prefer the trade_id embedded directly in the position payload -- it
    # can't be separated from the position by a crash between two rdb.set
    # calls the way the standalone key can. Fall back to the standalone key
    # only for legacy positions opened before this field existed (colleague
    # review).
    trade_id_from_pos = pos.get("trade_id")
    trade_id_raw: str | int | None = (
        trade_id_from_pos if trade_id_from_pos is not None else await rdb.get(trade_id_key)
    )
    exit_observation: dict[str, Any] | None = None
    exit_vwap: float | None = None
    fresh_exit_slippage_bps: float | None = None
    if trade_id_raw and cfg.db_url and exchange_client is not None and symbol is not None:
        try:
            exit_observation, exit_vwap, fresh_exit_slippage_bps = await _capture_exit_liquidity(
                exchange_client,
                symbol=symbol,
                exchange=exchange,
                side=side,
                requested_notional_usd=float(pos["size_usd"]),
            )
        except Exception as exc:
            # Capturing evidence is best effort. No malformed position payload,
            # exchange response, or observation bug may keep the position open.
            log.error(
                "paper.exit_liquidity_capture_failed",
                symbol=symbol,
                exchange=exchange,
                err=str(exc),
            )

    # exit_vwap (when available) already reflects the real cost of filling
    # this size, the same way entry_vwap does at open time -- use it as the
    # accounting/display exit price so entry and exit are priced the same
    # way (never entry=VWAP paired with exit=mark, which would make the
    # Entry->Exit line in Telegram misleading about what actually happened).
    # fresh_exit_slippage_bps is already 0.0 in that case, never a second
    # charge on top of a price that already paid it.
    exit_price_for_accounting = exit_vwap if exit_vwap is not None else current_price

    if trade_id_raw and cfg.db_url:
        trade_id = int(trade_id_raw)
        outcome = await journal.close_trade(
            cfg.db_url,
            trade_id=trade_id,
            exit_order_id=None,
            exit_price=exit_price_for_accounting,
            reason=reason,
            fresh_exit_slippage_bps=fresh_exit_slippage_bps,
            exit_observation=exit_observation,
        )
        if not outcome.committed:
            # Regression (colleague review): the Redis position key used to
            # be deleted unconditionally before this call. A DB outage then
            # meant the trade's row stayed "open" forever (never retried --
            # paper trades deliberately skip journal.try_commit_close's
            # pending-close/retry machinery, see below) while the position
            # simultaneously vanished from what _tick monitors, silently
            # orphaning it and permanently blocking re-entry on this symbol
            # (find_open_trade_id would see it "open" forever). Leaving the
            # position untouched here means the next monitor tick naturally
            # re-evaluates and retries the close instead.
            log.error(
                "paper.journal_close_failed",
                symbol=symbol,
                exchange=exchange,
                trade_id=trade_id,
            )
            return
        await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
        if outcome.accounting_status is not None:
            accounting_status = outcome.accounting_status
        if outcome.gross_pnl_pct is not None:
            gross_pnl_pct = outcome.gross_pnl_pct
        if outcome.gross_pnl_usd is not None:
            displayed_pnl_usd = outcome.gross_pnl_usd
        # net when fully resolved, else the same gross figure already
        # labeled "Gross PnL" below -- never a mix of a net percent with
        # a gross dollar amount or vice versa. Falling back to the
        # pre-computed pos-based estimate (not a bare None) covers the
        # idempotent already-closed retry, where CloseOutcome carries no
        # fresh accounting at all.
        displayed_pnl_pct = (
            outcome.net_pnl_pct if outcome.net_pnl_pct is not None else gross_pnl_pct
        )
        if outcome.net_pnl_usd is not None:
            displayed_pnl_usd = outcome.net_pnl_usd
        fees_usd = outcome.fees_usd
        funding_usd = outcome.funding_usd
        slippage_usd = outcome.slippage_usd

    # Paper trades are deliberately NOT routed through journal.try_commit_close:
    # that mechanism writes a journal:pending_close marker that tracker.py
    # treats as "a real close is outstanding" and withholds the trading-ready
    # lease for. A stuck paper-trade journal write must never block real
    # order placement. Reaching here means either there was nothing to
    # commit (no DB/trade_id) or the commit above already succeeded -- safe
    # to stop tracking this position and report the close.
    episode_id = pos.get("episode_id")
    if episode_id is not None:
        # early_momentum_v3+: CAS by episode_id -- a stale retry for an old
        # episode must never delete a newer position that has since opened
        # on the same exchange:base key. Reaching here at all means either
        # there was nothing to commit or journal.close_trade already
        # succeeded (a failed commit already returned above) -- safe to
        # denormalize the episode's own status too (best effort; app.trades
        # is what's actually authoritative).
        await rdb.eval(
            _DELETE_POSITION_IF_EPISODE_MATCHES, 1, paper_key(exchange, base), episode_id
        )
        if cfg.db_url:
            await episodes.mark_closed(cfg.db_url, episode_id=episode_id)
    else:
        await rdb.delete(paper_key(exchange, base))

    creds = notify.credentials(cfg)
    if creds:
        await notify.notify_close(
            *creds,
            strategy=strategy,
            base=base,
            exchange=exchange,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price_for_accounting,
            size_usd=size_usd_raw,
            margin_usd=(
                float(size_usd_raw) / float(leverage_raw)
                if size_usd_raw is not None and leverage_raw
                else None
            ),
            gross_pnl_pct=gross_pnl_pct,
            pnl_pct=displayed_pnl_pct,
            pnl_usd=displayed_pnl_usd,
            pnl_kind="modeled_net" if accounting_status == "complete" else "gross",
            accounting_status=accounting_status,
            fees_usd=fees_usd,
            funding_usd=funding_usd,
            slippage_usd=slippage_usd,
            reason=reason,
            paper=True,
        )

    log.info(
        "paper.closed",
        symbol=symbol,
        exchange=exchange,
        gross_pnl_pct=round(gross_pnl_pct, 2),
        displayed_pnl_pct=round(displayed_pnl_pct, 2),
        accounting_status=accounting_status,
        exit_liquidity_status=(
            exit_observation.get("status") if exit_observation is not None else "not_observed"
        ),
        reason=reason,
    )


async def _capture_exit_liquidity(
    exchange_client: Any,
    *,
    symbol: str,
    exchange: str,
    side: str,
    requested_notional_usd: float,
) -> tuple[dict[str, Any], float | None, float | None]:
    """Capture a fresh close-time book and pick the side that actually prices
    this position's exit (bid for LONG, ask for SHORT — see
    liquidity.book_side_for). Both sides are recorded on the returned
    observation for evidence.

    Returns (observation, exit_vwap, fresh_exit_slippage_bps). exit_vwap is
    the price the caller should actually book the exit at — it already
    reflects the real cost of filling requested_notional_usd on that side,
    the same way early_momentum.py's entry_vwap does. fresh_exit_slippage_bps
    is therefore 0.0 exactly when exit_vwap is available (that cost is
    already inside the price — charging it again in calculate_performance
    would double count it), or None when the book couldn't be read or didn't
    have enough visible depth, so net accounting correctly falls back to
    incomplete rather than guessing.
    """
    capture = await liquidity.capture_snapshot(
        exchange_client,
        symbol,
        required_depth_usd=requested_notional_usd,
    )
    snapshot = capture.snapshot or {}
    bid_vwap, bid_impact, bid_filled = liquidity.quote_for_book_side(
        snapshot, book_side="bid", target_usd=requested_notional_usd
    )
    ask_vwap, ask_impact, ask_filled = liquidity.quote_for_book_side(
        snapshot, book_side="ask", target_usd=requested_notional_usd
    )
    exit_book_side = liquidity.book_side_for(position_side=side, leg="exit")
    exit_vwap, exit_filled = (
        (bid_vwap, bid_filled) if exit_book_side == "bid" else (ask_vwap, ask_filled)
    )
    status = capture.status
    error = capture.error
    if status == "sampled" and exit_vwap is None:
        status = f"insufficient_{exit_book_side}_depth"
        error = f"visible {exit_book_side} depth cannot fill requested notional"
    observation = {
        "observed_at": datetime.fromtimestamp(capture.observed_at_ms / 1000, tz=UTC),
        "exchange": exchange,
        "symbol": symbol,
        "market_id": snapshot.get("market_id"),
        "status": status,
        "requested_notional_usd": requested_notional_usd,
        "filled_notional_usd": exit_filled,
        "best_bid": snapshot.get("best_bid"),
        "best_ask": snapshot.get("best_ask"),
        "mid": snapshot.get("mid"),
        "spread_bps": snapshot.get("spread_bps"),
        "bid_vwap": bid_vwap,
        "bid_impact_bps": bid_impact,
        "ask_vwap": ask_vwap,
        "ask_impact_bps": ask_impact,
        "contract_size": snapshot.get("contract_size"),
        "latency_ms": capture.latency_ms,
        "error": error,
    }
    fresh_exit_slippage_bps = 0.0 if exit_vwap is not None else None
    return observation, exit_vwap, fresh_exit_slippage_bps


async def run_paper_monitor(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
    tracker: Any = None,
) -> None:
    while True:
        if tracker:
            tracker.tick_started()
        await asyncio.sleep(_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg)
            if tracker:
                tracker.tick_succeeded()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if tracker:
                tracker.tick_failed(exc)
            log.error("paper_monitor.error", err=str(exc))


async def _tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    keys = [k async for k in rdb.scan_iter(f"{_KEY_PREFIX}*")]
    if not keys:
        return

    for key in keys:
        try:
            raw = await rdb.get(key)
            if not raw:
                continue
            try:
                pos = json.loads(raw)
            except Exception as exc:
                log.warning("paper.bad_payload", key=str(key), err=str(exc))
                continue

            base = pos["base"]
            symbol = pos.get("symbol")
            exchange = pos["exchange"]
            entry_price = float(pos["entry_price"])
            opened_at = float(pos.get("opened_at", 0))
            side = pos.get("side", "short")

            ex = exchanges.get(exchange)
            if not ex:
                continue

            if not symbol:
                try:
                    instrument = symbols.resolve_execution_instrument(ex, base)
                    symbol = instrument.symbol
                    pos["symbol"] = symbol
                except (RuntimeError, ValueError) as e:
                    log.error(
                        "paper.monitor.unresolved_legacy_symbol",
                        base=base,
                        err=str(e),
                    )
                    continue

            try:
                ticker = await ex.fetch_ticker(symbol)
                mark = float(ticker.get("last") or 0)
            except Exception as exc:
                log.warning("paper.ticker_failed", symbol=symbol, exchange=exchange, err=str(exc))
                continue

            if mark <= 0:
                continue

            params = pos.get("exit_params") or exit_module.exit_params(None)
            bp_key = exit_module.best_price_key(exchange, base, paper=True)

            reason = await exit_module.check_exit(
                side=side,
                entry_price=entry_price,
                current_price=mark,
                opened_at=opened_at,
                params=params,
                rdb=rdb,
                bp_key=bp_key,
            )

            if reason:
                await rdb.delete(bp_key)
                await close_paper(
                    rdb,
                    pos=pos,
                    current_price=mark,
                    reason=reason,
                    cfg=cfg,
                    exchange_client=ex,
                )
        except Exception as exc:
            log.error("paper.trade_error", key=str(key), err=str(exc))
            continue
