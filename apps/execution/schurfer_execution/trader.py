from __future__ import annotations

import asyncio
import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from . import decisions, execution_intent, journal, liquidity, notify, risk, symbols
from . import exit as exit_module
from .account import fetch_margin_balance
from .execution_intent import Broker, ExecutionIntent, StrategyIdentity
from .order_lock import OrderLockLostError
from .orders import place_order

_FUNDING_FETCH_TIMEOUT = 5  # seconds

if TYPE_CHECKING:
    from .config import Config
    from .supervisor import WorkerReadinessGate

log = structlog.get_logger()

_INTERVAL_SECONDS = 60
_MEASUREMENT_PUMPS_KEY = "pumps:measurement"
_PUBLIC_PUMPS_KEY = "pumps:latest"
_SIGNALS_KEY = "signals:{base}"
_SIGNAL_READINESS_KEY = "execution:signal_readiness"
_SIGNAL_READINESS_TTL = 180  # 3 trader ticks; absence means telemetry is stale
_SEEN_KEY = "trader:seen:{base}"
_SEEN_TTL_TRADED = 86400  # 24h — don't re-enter the same token after a trade
_SEEN_TTL_SKIP = 1800  # 30min — recheck sooner when skipped
_SEEN_TTL_ENTRY_WAIT = 300  # 5min — recheck after one 5m candle when entry quality fails
_SEEN_TTL_SIGNAL_RETRY = 60  # transient signal cache/persistence race: retry next tick
_SEEN_TTL_MEASUREMENT_RECHECK = 60  # detect crossing the entry floor on the next scan
_SEEN_TTL_SCORE_RECHECK = 300  # a valid score can change on the next 5m candle
_SIGNALS_MAX_AGE = 90  # reject cached score older than 1.5x ticker interval
_ENTRY_CANDLE_TIMEOUT = 5
_ENTRY_CANDLE_COUNT = 6  # fetch 6 x 5m candles; last may be forming, [-2] is last closed


@dataclass(frozen=True)
class SignalResult:
    score: int | None
    payload: dict[str, Any] | None
    status: str


async def run_signal_trader(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
    broker: Broker | None = None,
    tracker: Any = None,
    *,
    worker_gate: WorkerReadinessGate,
) -> None:
    while True:
        if tracker:
            tracker.tick_started()
        await asyncio.sleep(_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg, broker, worker_gate=worker_gate)
            if tracker:
                tracker.tick_succeeded()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if tracker:
                tracker.tick_failed(e)
            log.error("trader.error", err=str(e))


async def _tick(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
    broker: Broker | None = None,
    *,
    worker_gate: WorkerReadinessGate,
) -> None:
    # Resolved here (not just once in run_signal_trader) so a direct _tick()
    # call -- every existing test, plus any future caller -- gets the same
    # safe default without having to build a broker itself. resolve_mode/
    # build_broker are pure and cheap; re-resolving every tick when the
    # caller didn't already pin a broker costs nothing that matters at
    # _INTERVAL_SECONDS=60 cadence.
    if broker is None:
        mode = execution_intent.resolve_mode(cfg, execution_intent.STRATEGY_PUMP_SHORT)
        broker = execution_intent.build_broker(mode, exchanges=exchanges, gate=worker_gate)

    # PUMP_SHORT_MODE=disabled must actually stop the strategy, not just
    # make the eventual broker.open() call reject while everything around
    # it (signal readiness, decision writes) proceeds as if nothing
    # happened (colleague review, P1). Only reachable when cfg.dry_run=True
    # -- Config already refuses this override under AUTO_TRADE=true, where
    # broker.mode is always PAPER (see resolve_mode) and this branch is a
    # harmless no-op check.
    if broker.mode is execution_intent.TradingMode.DISABLED:
        log.info("trader.disabled")
        return

    raw = await rdb.get(_MEASUREMENT_PUMPS_KEY)
    if not raw:
        # Rolling-deploy compatibility until analytics publishes the private feed.
        raw = await rdb.get(_PUBLIC_PUMPS_KEY)
    if not raw:
        await _publish_signal_readiness(rdb, pump_count=0, evaluated=0, ready=0)
        return

    pumps_data = json.loads(raw)
    pumps = pumps_data.get("pumps", [])
    if not pumps:
        await _publish_signal_readiness(rdb, pump_count=0, evaluated=0, ready=0)
        return

    effective_entry_floor, entry_floor_valid = _effective_entry_floor(
        pumps_data.get("entry_min_change_pct"),
        cfg.entry_min_pct,
    )
    log.debug(
        "trader.tick",
        pump_count=len(pumps),
        entry_min_pct=effective_entry_floor,
        entry_floor_valid=entry_floor_valid,
    )
    evaluated = 0
    ready = 0
    deferral_reasons: Counter[str] = Counter()

    for pump in pumps:
        base = pump.get("base", "")
        if not base:
            continue

        seen_key = _SEEN_KEY.format(base=base)
        if await rdb.get(seen_key):
            continue

        pump_event_id = _pump_event_id(pump)
        pump_pct = _finite_float(pump.get("max_change_pct"))
        measurement_reason: str | None = None
        if not entry_floor_valid:
            measurement_reason = "entry_floor_invalid"
        elif pump_pct is None:
            measurement_reason = "pump_change_unavailable"
        elif pump_pct < effective_entry_floor:
            measurement_reason = "pump_below_entry_floor"
        evaluated += 1
        signal = await _fetch_signal(
            base,
            rdb,
            expected_pump_event_id=pump_event_id,
            require_entry_qualified=measurement_reason is None,
        )
        score = signal.score
        if signal.status != "ok" or score is None or signal.payload is None:
            # A missing, stale, malformed, or previous-episode signal means the
            # strategy does not yet have the inputs required to make a decision.
            # Keep this as an operational deferral instead of polluting the durable
            # decision dataset with a synthetic skip that invalidates the episode.
            reason = signal.status if signal.status != "ok" else "signal_invalid_ready_state"
            deferral_reasons[reason] += 1
            log.info(
                "trader.defer.signal_unavailable",
                base=base,
                pump_event_id=pump_event_id,
                reason=reason,
            )
            await rdb.set(seen_key, "1", ex=_SEEN_TTL_SIGNAL_RETRY)
            continue

        ready += 1

        # Per-decision measurement context is created only after all strategy inputs
        # are ready, so every durable row represents an actual evaluation.
        decision_id = str(uuid4())

        exchange = _pick_exchange(pump.get("exchanges", []), exchanges)
        ex = exchanges.get(exchange) if exchange else None
        instrument: symbols.ExecutionInstrument | None = None
        symbol: str | None = None
        if ex:
            try:
                instrument = symbols.resolve_execution_instrument(ex, base)
                symbol = instrument.symbol
            except (RuntimeError, ValueError) as e:
                log.warning("trader.unresolved_symbol", base=base, err=str(e))

        features = _decision_features(
            signal,
            pump,
            cfg,
            effective_entry_floor=effective_entry_floor,
            measurement_only=measurement_reason is not None,
        )
        decision_price = _decision_price(pump, exchange)

        # Order-book liquidity at decision time, captured for every candidate that
        # has a configured exchange (not only tradeable ones) so the threshold
        # itself can be evaluated later. Non-recoverable, so sampled live. The
        # status makes an absent snapshot unambiguous.
        depth_target = liquidity.depth_target_usd(
            cfg.signal_position_usd,
            cfg.liquidity_depth_multiplier,
        )
        quality: liquidity.MarketQualityCheck | None = None
        liq: dict[str, Any]
        if ex is None:
            liq = {"status": "no_exchange"}
        else:
            snap = (
                await liquidity.snapshot(ex, symbol, required_depth_usd=depth_target)
                if symbol
                else None
            )
            quality = liquidity.check_market_quality(
                snap,
                target_usd=depth_target,
                max_spread_bps=cfg.max_spread_bps,
                max_impact_bps=cfg.max_liquidity_impact_bps,
            )
            liq = {"status": "sampled", **snap} if snap else {"status": "fetch_failed"}
            liq["quality"] = asdict(quality)

        if measurement_reason is not None:
            log.info(
                "trader.measurement_only",
                base=base,
                reason=measurement_reason,
                pump_pct=pump_pct,
                entry_min_pct=effective_entry_floor,
            )
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange or "",
                action="skipped",
                reason=measurement_reason,
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.measurement_strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_MEASUREMENT_RECHECK,
            )
            continue

        if not exchange:
            log.info("trader.skip.no_exchange", base=base)
            await decisions.write_decision(
                rdb,
                base=base,
                exchange="",
                action="skipped",
                reason="no_configured_exchange",
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )
            continue

        if instrument is None or symbol is None:
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="skipped",
                reason="execution_instrument_unresolved",
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )
            continue

        mad_score = features.get("mad_score")
        if mad_score is not None and mad_score < 1.5:
            log.info("trader.skip.mad_score", base=base, mad_score=mad_score)
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange or "",
                action="skipped",
                reason="mad_score_too_low",
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )
            continue

        if score < cfg.score_threshold:
            log.info("trader.skip.score", base=base, score=score, threshold=cfg.score_threshold)
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="skipped",
                reason=f"score {score} < threshold {cfg.score_threshold}",
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SCORE_RECHECK,
            )
            continue

        if cfg.require_market_quality and (quality is None or not quality.allowed):
            reason = (
                quality.reason if quality is not None else "market_quality_snapshot_unavailable"
            )
            log.info(
                "trader.skip.market_quality",
                base=base,
                reason=reason,
                depth_target_usd=depth_target,
                spread_bps=quality.spread_bps if quality else None,
                bid_impact_bps=quality.bid_impact_bps if quality else None,
                ask_impact_bps=quality.ask_impact_bps if quality else None,
            )
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="skipped",
                reason=reason,
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_ENTRY_WAIT,
            )
            continue

        funding_rate_pct = await _fetch_funding_rate_pct(ex, symbol) if ex and symbol else None

        if funding_rate_pct is None and cfg.require_funding_rate:
            reason = "funding_rate_unavailable"
            log.info("trader.skip.funding_rate_unavailable", base=base)
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="skipped",
                reason=reason,
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )
            continue

        if funding_rate_pct is not None:
            fr_check = risk.check_funding_rate(funding_rate_pct, cfg.min_funding_rate_pct)
            if not fr_check.allowed:
                log.info("trader.skip.funding_rate", base=base, reason=fr_check.reason)
                await decisions.write_decision(
                    rdb,
                    base=base,
                    exchange=exchange,
                    action="skipped",
                    reason=fr_check.reason,
                    score=score,
                    pump_pct=pump_pct,
                    decision_id=decision_id,
                    strategy_version=cfg.strategy_version,
                    features=features,
                    liquidity=liq,
                    price=decision_price,
                    pump_event_id=pump_event_id,
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_SKIP,
                )
                continue

        entry_check: risk.EntryCheck | None = None
        if cfg.require_red_candle or cfg.min_retrace_pct > 0:
            candles = await _fetch_entry_candles(ex, symbol) if ex and symbol else None
            if candles is None:
                log.warning("trader.skip.entry_candles_unavailable", base=base)
                await decisions.write_decision(
                    rdb,
                    base=base,
                    exchange=exchange,
                    action="skipped",
                    reason="entry_candles_unavailable",
                    score=score,
                    pump_pct=pump_pct,
                    decision_id=decision_id,
                    strategy_version=cfg.strategy_version,
                    features=features,
                    liquidity=liq,
                    price=decision_price,
                    pump_event_id=pump_event_id,
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_ENTRY_WAIT,
                )
                continue
            entry_check = risk.check_entry_candles(
                candles,
                require_red_candle=cfg.require_red_candle,
                min_retrace_pct=cfg.min_retrace_pct,
            )
            if not entry_check.allowed:
                log.info("trader.skip.entry_quality", base=base, reason=entry_check.reason)
                await decisions.write_decision(
                    rdb,
                    base=base,
                    exchange=exchange,
                    action="skipped",
                    reason=entry_check.reason,
                    score=score,
                    pump_pct=pump_pct,
                    decision_id=decision_id,
                    strategy_version=cfg.strategy_version,
                    features=features,
                    liquidity=liq,
                    price=decision_price,
                    pump_event_id=pump_event_id,
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_ENTRY_WAIT,
                )
                continue

        exit_params = exit_module.exit_params(pump_pct)
        liq_check = risk.check_liquidation_distance(
            exit_params["initial_sl_pct"],
            cfg.signal_leverage,
            cfg.liquidation_buffer_pct,
        )
        if not liq_check.allowed:
            log.info("trader.skip.liquidation_guard", base=base, reason=liq_check.reason)
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="skipped",
                reason=liq_check.reason,
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )
            continue

        equity_usd: float | None = None
        sizing_mode = "fixed"
        if cfg.risk_per_trade_pct > 0:
            equity_usd = await _fetch_equity_usd(exchanges, exchange)
            if equity_usd is None:
                log.warning("trader.skip.equity_unavailable", base=base, exchange=exchange)
                await decisions.write_decision(
                    rdb,
                    base=base,
                    exchange=exchange,
                    action="skipped",
                    reason="equity_unavailable_for_risk_sizing",
                    score=score,
                    pump_pct=pump_pct,
                    decision_id=decision_id,
                    strategy_version=cfg.strategy_version,
                    features=features,
                    liquidity=liq,
                    price=decision_price,
                    pump_event_id=pump_event_id,
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_SKIP,
                )
                continue
            computed = risk.compute_position_size_usd(
                equity_usd,
                cfg.risk_per_trade_pct,
                exit_params["initial_sl_pct"],
                cfg.signal_position_usd,
            )
            if computed is None:
                skip_reason = (
                    f"risk_sized_position_below_min_notional "
                    f"(equity={equity_usd:.0f}, risk={cfg.risk_per_trade_pct}%, "
                    f"sl={exit_params['initial_sl_pct']}%)"
                )
                log.info("trader.skip.size_below_min", base=base, reason=skip_reason)
                await decisions.write_decision(
                    rdb,
                    base=base,
                    exchange=exchange,
                    action="skipped",
                    reason=skip_reason,
                    score=score,
                    pump_pct=pump_pct,
                    decision_id=decision_id,
                    strategy_version=cfg.strategy_version,
                    features=features,
                    liquidity=liq,
                    price=decision_price,
                    pump_event_id=pump_event_id,
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_SKIP,
                )
                continue
            size_usd = computed
            sizing_mode = "risk_pct"
            log.info(
                "trader.risk_sizing",
                base=base,
                equity_usd=round(equity_usd, 2),
                size_usd=round(size_usd, 2),
                risk_pct=cfg.risk_per_trade_pct,
            )
        else:
            size_usd = cfg.signal_position_usd

        setup_context = {
            "decision_id": decision_id,
            "pump_event_id": pump_event_id,
            "strategy_version": cfg.strategy_version,
            "score": score,
            "pump_pct": pump_pct,
            "funding_rate_pct": funding_rate_pct,
            "exchanges": pump.get("exchanges", []),
            "signals_ts": pumps_data.get("ts"),
            "sizing_mode": sizing_mode,
            "equity_usd": equity_usd,
            "risk_per_trade_pct": cfg.risk_per_trade_pct if cfg.risk_per_trade_pct > 0 else None,
            "initial_sl_pct": exit_params["initial_sl_pct"],
            "size_usd": size_usd,
            "entry_require_red_candle": cfg.require_red_candle,
            "entry_min_retrace_pct": cfg.min_retrace_pct if cfg.min_retrace_pct > 0 else None,
            "entry_closed_red": entry_check.closed_red if entry_check else None,
            "entry_retrace_pct": entry_check.retrace_pct if entry_check else None,
            "market_quality": asdict(quality) if quality else None,
        }

        if cfg.dry_run:
            if not ex:
                await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
                continue
            try:
                ticker = await ex.fetch_ticker(instrument.symbol)
                entry_price = float(ticker["last"])
            except Exception as exc:
                log.warning("trader.dry_run.price_failed", base=base, err=str(exc))
                await decisions.write_decision(
                    rdb,
                    base=base,
                    exchange=exchange,
                    action="skipped",
                    reason="dry_run_price_unavailable",
                    score=score,
                    pump_pct=pump_pct,
                    decision_id=decision_id,
                    strategy_version=cfg.strategy_version,
                    features=features,
                    liquidity=liq,
                    price=decision_price,
                    pump_event_id=pump_event_id,
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_SKIP,
                )
                continue

            # journal.strategy_identity(setup_context) is the SAME pure
            # parser journal.open_trade will use to register this trade's
            # app.strategies row -- deriving StrategyIdentity from it
            # directly (rather than hand-building name/version here) is
            # what keeps the two from silently disagreeing. Hand-building
            # the identity from cfg.strategy_version directly was wrong: it
            # is the whole raw string (e.g. "pump_short_v1_market_quality"),
            # while journal.strategy_identity parses it down to the correct
            # ("pump_short", "1_market_quality") pair (colleague review).
            strategy_name, strategy_version = journal.strategy_identity(setup_context)
            intent = ExecutionIntent(
                strategy=StrategyIdentity(name=strategy_name, version=strategy_version),
                instrument=instrument,
                side="short",
                size_usd=size_usd,
                leverage=cfg.signal_leverage,
                score=score,
                setup_context=setup_context,
                idempotency_key=decision_id,
                price=entry_price,
            )
            # Named distinctly from `result` below (the live place_order()
            # dict) -- same function scope, different type; mypy correctly
            # flags a name collision between the two.
            open_result = await broker.open(intent, cfg=cfg, rdb=rdb)
            # Never claim "opened_dry_run" unless the broker actually
            # opened something -- checked against the specific expected
            # status, not the coarser `committed` bool, so a future status
            # this call site was never written to expect (e.g. a live
            # broker's EMERGENCY_CLOSED, which is also "committed") can't
            # silently be reported as a normal paper open (colleague
            # review).
            if open_result.status is not execution_intent.ExecutionStatus.PAPER_OPENED:
                await decisions.write_decision(
                    rdb,
                    base=base,
                    exchange=exchange,
                    action="skipped",
                    reason=f"broker_rejected:{open_result.reason}",
                    score=score,
                    pump_pct=pump_pct,
                    decision_id=decision_id,
                    strategy_version=cfg.strategy_version,
                    features=features,
                    liquidity=liq,
                    price=decision_price,
                    pump_event_id=pump_event_id,
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_SKIP,
                )
                continue
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="opened_dry_run",
                reason="paper trade",
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_TRADED,
            )
            continue

        try:
            result = await place_order(
                base=base,
                symbol=symbol,
                exchange=exchange,
                side="short",
                size_usd=size_usd,
                leverage=cfg.signal_leverage,
                exchanges=exchanges,
                rdb=rdb,
                max_positions=cfg.max_positions,
                max_position_usd=cfg.max_position_usd,
                daily_loss_limit_usd=cfg.daily_loss_limit_usd,
                liquidity_checked_usd=depth_target,
                exit_params=exit_params,
                liquidation_buffer_pct=cfg.liquidation_buffer_pct,
                cfg=cfg,
                setup_context=setup_context,
                worker_gate=worker_gate,
            )
        except OrderLockLostError as exc:
            # Exclusivity became uncertain mid-operation (see order_lock.py). The
            # exchange-side outcome is unverified here, not necessarily a skip — but
            # one candidate losing its lease must not abort evaluation of every other
            # pump still queued in this tick.
            log.critical(
                "trader.order_lock_lost",
                base=base,
                exchange=exchange,
                err=str(exc),
            )
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="skipped",
                reason="order_lock_lost_outcome_uncertain",
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )
            continue

        if result.get("allowed") and result.get("fill_status") == "unresolved":
            # Order is confirmed placed on the exchange; the fill price is not.
            # place_order already created a durable incident and revoked PnL
            # readiness (fill_price.py / incidents.py). Recording a trade here
            # would need a price we don't have — the incident worker completes
            # journal.complete_open (journal write + exit tracking) once
            # resolve_fill_price confirms a real price for this same order id.
            # seen_key is still marked "traded": a real position exists, so
            # this token must not be re-entered.
            log.warning(
                "trader.open_fill_unresolved",
                base=base,
                exchange=exchange,
                incident_id=result.get("incident_id"),
            )
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="opened_pending_fill",
                reason="fill_unresolved",
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_TRADED,
            )
        elif result.get("allowed"):
            log.info(
                "trader.opened",
                base=base,
                exchange=exchange,
                score=score,
                order_id=result.get("order_id"),
            )
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="opened",
                reason="ok",
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_TRADED,
            )

            # place_order already completed the journal write and every
            # Redis exit-tracking key (exit_params/entry/side/size_usd,
            # plus the trade:id pointer) via journal.complete_open, inside
            # the same lock and the same function call as the exchange
            # order itself -- doing that here instead, a few awaits after
            # place_order returned, used to be exactly the crash window
            # that could leave a real, SL-protected position with no
            # app.trades row (colleague review candidate on an earlier
            # draft). Nothing left for this branch but decision logging and
            # the notification.
            entry_price = result["price"]

            creds = notify.credentials(cfg)
            if creds:
                await notify.notify_open(
                    *creds,
                    base=base,
                    exchange=exchange,
                    size_usd=size_usd,
                    leverage=cfg.signal_leverage,
                    price=result["price"],
                    score=score,
                    paper=False,
                )
        else:
            blocked_reason = result.get("reason", "unknown")
            log.info("trader.blocked", base=base, reason=blocked_reason)
            await decisions.write_decision(
                rdb,
                base=base,
                exchange=exchange,
                action="skipped",
                reason=blocked_reason,
                score=score,
                pump_pct=pump_pct,
                decision_id=decision_id,
                strategy_version=cfg.strategy_version,
                features=features,
                liquidity=liq,
                price=decision_price,
                pump_event_id=pump_event_id,
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )

    await _publish_signal_readiness(
        rdb,
        pump_count=len(pumps),
        evaluated=evaluated,
        ready=ready,
        deferral_reasons=deferral_reasons,
    )


async def _publish_signal_readiness(
    rdb: Any,
    *,
    pump_count: int,
    evaluated: int,
    ready: int,
    deferral_reasons: Counter[str] | None = None,
) -> None:
    """Publish ephemeral operational telemetry without adding decision rows."""
    reasons = deferral_reasons or Counter()
    await rdb.hset(
        _SIGNAL_READINESS_KEY,
        mapping={
            "updated_at_ms": time.time_ns() // 1_000_000,
            "pump_count": pump_count,
            "evaluated": evaluated,
            "ready": ready,
            "deferred": sum(reasons.values()),
            "reasons": json.dumps(dict(sorted(reasons.items())), separators=(",", ":")),
        },
    )
    await rdb.expire(_SIGNAL_READINESS_KEY, _SIGNAL_READINESS_TTL)


_EIGHT_HOURS_MS = 8 * 3600 * 1000  # reference period for normalization


async def _fetch_funding_rate_pct(ex: Any, symbol: str) -> float | None:
    """Fetch current funding rate normalized to % per 8h equivalent.

    ccxt returns fundingRate as a fraction and fundingInterval in ms.
    When fundingInterval is present we normalize so a 4h rate is doubled
    and a 1h rate is multiplied by 8 before comparison with the threshold.
    If fundingInterval is absent we assume the standard 8h period.
    """
    try:
        data = await asyncio.wait_for(
            ex.fetch_funding_rate(symbol),
            timeout=_FUNDING_FETCH_TIMEOUT,
        )
        rate = data.get("fundingRate")
        if rate is None:
            return None
        interval_ms = data.get("fundingInterval")
        if interval_ms and interval_ms > 0:
            rate_8h = float(rate) * (_EIGHT_HOURS_MS / float(interval_ms))
        else:
            rate_8h = float(rate)  # assume 8h (Binance/Bybit/OKX standard)
        return rate_8h * 100
    except Exception as exc:
        log.warning("trader.funding_rate.fetch_failed", symbol=symbol, err=str(exc))
        return None


async def _fetch_equity_usd(exchanges: dict[str, Any], exchange: str) -> float | None:
    """Return total USDT wallet balance as equity proxy. None if unavailable."""
    try:
        balances = await fetch_margin_balance(exchanges, exchange)
        for b in balances:
            if b.get("exchange") == exchange and b.get("asset") == "USDT":
                return float(b.get("total", 0) or 0) or None
    except Exception as exc:
        log.warning("trader.equity.fetch_failed", exchange=exchange, err=str(exc))
    return None


async def _fetch_entry_candles(ex: Any, symbol: str) -> list[list[float]] | None:
    """Fetch recent 5m OHLCV candles. Returns None on any error or malformed data."""
    try:
        candles = await asyncio.wait_for(
            ex.fetch_ohlcv(symbol, "5m", limit=_ENTRY_CANDLE_COUNT),
            timeout=_ENTRY_CANDLE_TIMEOUT,
        )
        if not candles or not all(isinstance(c, list | tuple) and len(c) >= 6 for c in candles):
            log.warning("trader.entry_candles.malformed", symbol=symbol)
            return None
        validated: list[list[float]] = [list(c[:6]) for c in candles]
        return validated
    except Exception as exc:
        log.warning("trader.entry_candles.fetch_failed", symbol=symbol, err=str(exc))
        return None


async def _fetch_signal(
    base: str,
    rdb: Any,
    expected_pump_event_id: int | None = None,
    *,
    require_entry_qualified: bool = False,
) -> SignalResult:
    """Return a valid fresh signal or an explicit unavailable status.

    A real computed score of zero is valid and distinct from missing, malformed, or
    stale data. Callers can therefore choose a short retry for infrastructure timing
    without contaminating the low-score cohort with synthetic zeros.
    """
    raw = await rdb.get(_SIGNALS_KEY.format(base=base))
    if not raw:
        return SignalResult(None, None, "signal_missing")
    try:
        data = json.loads(raw)
    except Exception:
        return SignalResult(None, None, "signal_invalid_json")
    # Guard against valid-but-unexpected JSON: a list, or {"score": null}, etc.
    if not isinstance(data, dict):
        log.warning("trader.signal.not_an_object", base=base)
        return SignalResult(None, None, "signal_invalid_payload")
    try:
        raw_score = data["score"]
        if isinstance(raw_score, bool) or not isinstance(raw_score, int):
            raise TypeError
        score = raw_score
    except (TypeError, ValueError):
        return SignalResult(None, data, "signal_invalid_score")
    except KeyError:
        return SignalResult(None, data, "signal_missing_score")
    episode = data.get("episode")
    if expected_pump_event_id is not None:
        signal_pump_event_id = (
            _positive_int(episode.get("id")) if isinstance(episode, dict) else None
        )
        if signal_pump_event_id != expected_pump_event_id:
            log.warning(
                "trader.signal.episode_mismatch",
                base=base,
                expected=expected_pump_event_id,
                actual=signal_pump_event_id,
            )
            return SignalResult(None, data, "signal_episode_mismatch")
    if require_entry_qualified:
        entry_qualified_at = (
            _positive_int(episode.get("entry_qualified_at")) if isinstance(episode, dict) else None
        )
        if entry_qualified_at is None:
            log.info("trader.signal.entry_not_qualified", base=base)
            return SignalResult(None, data, "signal_entry_not_qualified")
    # Freshness is a safety gate, so fail closed (return 0 = skip) whenever it cannot
    # be verified: a missing, non-numeric, or non-finite computed_at, a stale signal,
    # or a timestamp from the future (clock skew or junk).
    computed_at = data.get("computed_at")
    if (
        not isinstance(computed_at, int | float)
        or isinstance(computed_at, bool)
        or not math.isfinite(computed_at)
        or computed_at <= 0
    ):
        log.warning("trader.signal.invalid_computed_at", base=base)
        return SignalResult(None, data, "signal_invalid_timestamp")
    age = time.time() - computed_at
    if age > _SIGNALS_MAX_AGE:
        log.warning("trader.signal.unfresh", base=base, age=int(age))
        return SignalResult(None, data, "signal_stale")
    if age < -5:
        log.warning("trader.signal.unfresh", base=base, age=int(age))
        return SignalResult(None, data, "signal_future_timestamp")
    return SignalResult(score, data, "ok")


async def _fetch_score(base: str, rdb: Any) -> int:
    result = await _fetch_signal(base, rdb)
    return result.score if result.score is not None else 0


def _decision_features(
    signal: SignalResult,
    pump: dict[str, Any],
    cfg: Config,
    *,
    effective_entry_floor: float,
    measurement_only: bool,
) -> dict[str, Any]:
    """Full decision-input context stored on every decision.

    Bundles the signal snapshot, the candidate exchanges, and a fingerprint of
    the effective config. strategy_version is a coarse label; this fingerprint is
    the actual settings in force, so decisions stay comparable across rule changes.
    """
    mad_score = None
    if signal.payload and "components" in signal.payload:
        mad_score = signal.payload["components"].get("mad_score")

    return {
        "mad_score": mad_score,
        "signal": signal.payload,
        "signal_status": signal.status,
        "measurement_only": measurement_only,
        "candidate_exchanges": pump.get("exchanges", []),
        "config": {
            "entry_min_pct": effective_entry_floor,
            "score_threshold": cfg.score_threshold,
            "signal_leverage": cfg.signal_leverage,
            "signal_position_usd": cfg.signal_position_usd,
            "risk_per_trade_pct": cfg.risk_per_trade_pct,
            "require_funding_rate": cfg.require_funding_rate,
            "min_funding_rate_pct": cfg.min_funding_rate_pct,
            "require_market_quality": cfg.require_market_quality,
            "max_spread_bps": cfg.max_spread_bps,
            "max_liquidity_impact_bps": cfg.max_liquidity_impact_bps,
            "liquidity_depth_multiplier": cfg.liquidity_depth_multiplier,
            "require_red_candle": cfg.require_red_candle,
            "min_retrace_pct": cfg.min_retrace_pct,
            "liquidation_buffer_pct": cfg.liquidation_buffer_pct,
        },
    }


def _pump_event_id(pump: dict[str, Any]) -> int | None:
    """Return a trusted positive episode id from the scanner payload."""
    return _positive_int(pump.get("pump_event_id"))


def _positive_int(value: object) -> int | None:
    """Accept JSON integers used as database ids, excluding bool and coercion."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _safe_float(v: Any) -> float:
    """Parse to a float usable as a max() sort key, sending anything unusable to the
    bottom. A corrupted or non-numeric field cannot raise and abort the whole trader
    tick, and NaN/Infinity (which float() accepts) cannot win the sort: both map to
    -inf so a valid entry always outranks a garbage one."""
    value = _finite_float(v)
    return value if value is not None else -math.inf


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _effective_entry_floor(
    published_floor: Any,
    configured_floor: float,
) -> tuple[float, bool]:
    """Use the stricter floor and fail closed on an explicitly invalid feed value."""
    if published_floor is None:
        return configured_floor, True
    parsed = _finite_float(published_floor)
    if parsed is None or parsed <= 0 or parsed > 5_000:
        return configured_floor, False
    return max(configured_floor, parsed), True


def _decision_price(pump: dict[str, Any], exchange: str | None) -> float | None:
    """Reference price of the token at decision time.

    Uses the chosen exchange's last price, or the top-moving exchange's when no
    exchange was picked (no_configured_exchange), so a skip still records the price
    we saw. Returns None if there is no usable, finite, positive price.
    """
    exchanges = pump.get("exchanges", [])
    chosen: dict[str, Any] | None = None
    if exchange:
        chosen = next((e for e in exchanges if e.get("exchange") == exchange), None)
    if chosen is None and exchanges:
        chosen = max(exchanges, key=lambda e: _safe_float(e.get("change_pct")))
    if chosen is None:
        return None
    raw = chosen.get("price")
    if raw is None:
        return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _pick_exchange(
    pump_exchanges: list[dict[str, Any]],
    configured: dict[str, Any],
) -> str | None:
    """Return the highest-volume pump exchange that is configured in the execution service."""
    candidates = [e for e in pump_exchanges if e.get("exchange") in configured]
    if not candidates:
        return None
    best = max(candidates, key=lambda e: _safe_float(e.get("volume_24h_usd")))
    return str(best["exchange"])
