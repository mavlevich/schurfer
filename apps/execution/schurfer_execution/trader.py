from __future__ import annotations

import asyncio
import json
import math
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from . import decisions, journal, liquidity, notify, paper, risk
from . import exit as exit_module
from .account import fetch_margin_balance
from .orders import place_order

_FUNDING_FETCH_TIMEOUT = 5  # seconds

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_INTERVAL_SECONDS = 60
_PUMPS_KEY = "pumps:latest"
_SIGNALS_KEY = "signals:{base}"
_SEEN_KEY = "trader:seen:{base}"
_TRADE_ID_KEY = "trade:id:{exchange}:{base}"
_SEEN_TTL_TRADED = 86400  # 24h — don't re-enter the same token after a trade
_SEEN_TTL_SKIP = 1800  # 30min — recheck sooner when skipped
_SEEN_TTL_ENTRY_WAIT = 300  # 5min — recheck after one 5m candle when entry quality fails
_SIGNALS_MAX_AGE = 90  # reject cached score older than 1.5x ticker interval
_ENTRY_CANDLE_TIMEOUT = 5
_ENTRY_CANDLE_COUNT = 6  # fetch 6 x 5m candles; last may be forming, [-2] is last closed


async def run_signal_trader(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
) -> None:
    while True:
        await asyncio.sleep(_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("trader.error", err=str(e))


async def _tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    raw = await rdb.get(_PUMPS_KEY)
    if not raw:
        return

    pumps_data = json.loads(raw)
    pumps = pumps_data.get("pumps", [])
    if not pumps:
        return

    log.debug("trader.tick", pump_count=len(pumps))

    for pump in pumps:
        base = pump.get("base", "")
        if not base:
            continue

        seen_key = _SEEN_KEY.format(base=base)
        if await rdb.get(seen_key):
            continue

        # Per-decision measurement context, resolved up front so every decision
        # (including no_configured_exchange) carries the full context.
        decision_id = str(uuid4())
        pump_pct: float | None = pump.get("max_change_pct")
        score, signal_payload = await _fetch_signal(base, rdb)
        exchange = _pick_exchange(pump.get("exchanges", []), exchanges)
        ex = exchanges.get(exchange) if exchange else None
        features = _decision_features(signal_payload, pump, cfg)
        decision_price = _decision_price(pump, exchange)

        # Order-book liquidity at decision time, captured for every candidate that
        # has a configured exchange (not only tradeable ones) so the threshold
        # itself can be evaluated later. Non-recoverable, so sampled live. The
        # status makes an absent snapshot unambiguous.
        liq: dict[str, Any]
        if ex is None:
            liq = {"status": "no_exchange"}
        else:
            snap = await liquidity.snapshot(ex, base)
            liq = {"status": "sampled", **snap} if snap else {"status": "fetch_failed"}

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
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )
            continue

        funding_rate_pct = await _fetch_funding_rate_pct(ex, base) if ex else None

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
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_SKIP,
                )
                continue

        entry_check: risk.EntryCheck | None = None
        if cfg.require_red_candle or cfg.min_retrace_pct > 0:
            candles = await _fetch_entry_candles(ex, base) if ex else None
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
        }

        if cfg.dry_run:
            if not ex:
                await rdb.set(seen_key, "1", ex=_SEEN_TTL_SKIP)
                continue
            try:
                ticker = await ex.fetch_ticker(f"{base.upper()}/USDT:USDT")
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
                    seen_key=seen_key,
                    seen_ttl=_SEEN_TTL_SKIP,
                )
                continue

            await paper.open_paper(
                rdb,
                base=base,
                exchange=exchange,
                price=entry_price,
                size_usd=size_usd,
                leverage=cfg.signal_leverage,
                score=score,
                setup_context=setup_context,
                cfg=cfg,
            )
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
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_TRADED,
            )
            continue

        result = await place_order(
            base=base,
            exchange=exchange,
            side="short",
            size_usd=size_usd,
            leverage=cfg.signal_leverage,
            exchanges=exchanges,
            rdb=rdb,
            max_positions=cfg.max_positions,
            max_position_usd=cfg.max_position_usd,
            daily_loss_limit_usd=cfg.daily_loss_limit_usd,
            initial_sl_pct=exit_params["initial_sl_pct"],
            liquidation_buffer_pct=cfg.liquidation_buffer_pct,
            cfg=cfg,
        )

        if result.get("allowed"):
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
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_TRADED,
            )

            entry_price = result.get("price", 0)
            await rdb.set(
                exit_module.params_key(exchange, base),
                json.dumps(exit_params),
                ex=_SEEN_TTL_TRADED,
            )
            await rdb.set(
                exit_module.entry_key(exchange, base),
                str(entry_price),
                ex=_SEEN_TTL_TRADED,
            )
            await rdb.set(
                exit_module.side_key(exchange, base),
                "short",
                ex=_SEEN_TTL_TRADED,
            )

            if cfg.db_url:
                trade_id = await journal.open_trade(
                    cfg.db_url,
                    base=base,
                    exchange=exchange,
                    order_id=result.get("order_id"),
                    size_usd=size_usd,
                    leverage=cfg.signal_leverage,
                    entry_price=entry_price,
                    setup_context=setup_context,
                )
                if trade_id:
                    await rdb.set(
                        _TRADE_ID_KEY.format(exchange=exchange, base=base.upper()),
                        str(trade_id),
                        ex=_SEEN_TTL_TRADED,
                    )

            creds = notify.credentials(cfg)
            if creds:
                await notify.notify_open(
                    *creds,
                    base=base,
                    exchange=exchange,
                    size_usd=size_usd,
                    leverage=cfg.signal_leverage,
                    price=result.get("price", 0),
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
                seen_key=seen_key,
                seen_ttl=_SEEN_TTL_SKIP,
            )


_EIGHT_HOURS_MS = 8 * 3600 * 1000  # reference period for normalization


async def _fetch_funding_rate_pct(ex: Any, base: str) -> float | None:
    """Fetch current funding rate normalized to % per 8h equivalent.

    ccxt returns fundingRate as a fraction and fundingInterval in ms.
    When fundingInterval is present we normalize so a 4h rate is doubled
    and a 1h rate is multiplied by 8 before comparison with the threshold.
    If fundingInterval is absent we assume the standard 8h period.
    """
    try:
        data = await asyncio.wait_for(
            ex.fetch_funding_rate(f"{base.upper()}/USDT:USDT"),
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
        log.warning("trader.funding_rate.fetch_failed", base=base, err=str(exc))
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


async def _fetch_entry_candles(ex: Any, base: str) -> list[list[float]] | None:
    """Fetch recent 5m OHLCV candles. Returns None on any error or malformed data."""
    try:
        candles = await asyncio.wait_for(
            ex.fetch_ohlcv(f"{base.upper()}/USDT:USDT", "5m", limit=_ENTRY_CANDLE_COUNT),
            timeout=_ENTRY_CANDLE_TIMEOUT,
        )
        if not candles or not all(isinstance(c, list | tuple) and len(c) >= 6 for c in candles):
            log.warning("trader.entry_candles.malformed", base=base)
            return None
        validated: list[list[float]] = [list(c[:6]) for c in candles]
        return validated
    except Exception as exc:
        log.warning("trader.entry_candles.fetch_failed", base=base, err=str(exc))
        return None


async def _fetch_signal(base: str, rdb: Any) -> tuple[int, dict[str, Any] | None]:
    """Return (score, payload) for base.

    payload is the full `signals:{base}` snapshot (score, verdict, components,
    computed_at) recorded as decision features. score is 0 when the signal is
    missing, unparseable, or stale, which forces a skip while still keeping the
    payload for the record when we have one.
    """
    raw = await rdb.get(_SIGNALS_KEY.format(base=base))
    if not raw:
        return 0, None
    try:
        data = json.loads(raw)
    except Exception:
        return 0, None
    # Guard against valid-but-unexpected JSON: a list, or {"score": null}, etc.
    if not isinstance(data, dict):
        log.warning("trader.signal.not_an_object", base=base)
        return 0, None
    try:
        score = int(data.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
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
        return 0, data
    age = time.time() - computed_at
    if age > _SIGNALS_MAX_AGE or age < -5:
        log.warning("trader.signal.unfresh", base=base, age=int(age))
        return 0, data
    return score, data


async def _fetch_score(base: str, rdb: Any) -> int:
    score, _ = await _fetch_signal(base, rdb)
    return score


def _decision_features(
    signal_payload: dict[str, Any] | None,
    pump: dict[str, Any],
    cfg: Config,
) -> dict[str, Any]:
    """Full decision-input context stored on every decision.

    Bundles the signal snapshot, the candidate exchanges, and a fingerprint of
    the effective config. strategy_version is a coarse label; this fingerprint is
    the actual settings in force, so decisions stay comparable across rule changes.
    """
    return {
        "signal": signal_payload,
        "candidate_exchanges": pump.get("exchanges", []),
        "config": {
            "score_threshold": cfg.score_threshold,
            "signal_leverage": cfg.signal_leverage,
            "signal_position_usd": cfg.signal_position_usd,
            "risk_per_trade_pct": cfg.risk_per_trade_pct,
            "require_funding_rate": cfg.require_funding_rate,
            "min_funding_rate_pct": cfg.min_funding_rate_pct,
            "require_red_candle": cfg.require_red_candle,
            "min_retrace_pct": cfg.min_retrace_pct,
            "liquidation_buffer_pct": cfg.liquidation_buffer_pct,
        },
    }


def _safe_float(v: Any) -> float:
    """Parse to a float usable as a max() sort key, sending anything unusable to the
    bottom. A corrupted or non-numeric field cannot raise and abort the whole trader
    tick, and NaN/Infinity (which float() accepts) cannot win the sort: both map to
    -inf so a valid entry always outranks a garbage one."""
    try:
        value = float(v)
    except (TypeError, ValueError):
        return -math.inf
    return value if math.isfinite(value) else -math.inf


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
