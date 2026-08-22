"""Early-momentum breakout detection: candidate -> durable episode -> claim -> paper open.

The full lifecycle contract this module orchestrates lives in episodes.py:
armed -> claimed -> opened/expired/rejected/suppressed, with Postgres as the
source of truth and Redis's WATCH cache as a repairable, self-healing view
of it -- never the only place a live episode can be found. See
episodes.py's module docstring and migration 0032 for the full reasoning.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import structlog
from psycopg.rows import dict_row

from . import episodes, journal, liquidity, paper, symbols

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_EXECUTION_EXCHANGE = "bybit"  # Hardcoded for now since momentum flow is Bybit only
_WATCH_KEY_PREFIX = "market:early_momentum:v3:watch:"
_SCAN_INTERVAL = 60
_TRIGGER_INTERVAL = 60
_LIST_ACTIONABLE_BATCH_SIZE = 200
_MAX_CLAIM_ATTEMPTS = 5
_CLAIM_LEASE_SECONDS = 30
_RESERVATION_TOKEN_TTL_SECONDS = 30
# Size of the paper trade this strategy opens on every breakout. Kept as a
# module constant (rather than only inline in the open call) since the
# entry liquidity gate needs to size its depth check to the exact same
# notional, and the contract hash below needs a fixed value to hash.
_SIZE_USD = 100.0
_LEVERAGE = 5
_STRATEGY_NAME = "early_momentum"
_STRATEGY_VERSION = "3"

# Hardcoded best parameters from backtest -- unchanged from v1/v2. This PR
# does not touch the signal, TP/SL, or max-hold; only the lifecycle around
# deciding to open one.
_EXIT_PARAMS = {
    "initial_sl_pct": 10.0,
    "activation_pct": 10.0,  # Not used since TP hits first, but required
    "trail_pct": 10.0,
    "trail_tighten_pct": 10.0,
    "tighten_after_min": 240.0,
    "max_hold_min": 240.0,  # 4 hours
    "take_profit_pct": 4.0,  # The holy grail parameter
}

# Canonical serialization of everything an episode is armed under: the
# scanner's own thresholds (fixed literals below, matching _SQL_SCANNER --
# not env-configurable, so there is nothing to read back out of the SQL
# text itself) plus the exit contract and sizing. Sorted keys and only
# fixed literal values make this deterministic across runs and machines.
_CONTRACT_PAYLOAD: dict[str, Any] = {
    "scanner": {
        "lookback_minutes": 125,
        "oi_growth_min_pct": 0.05,
        "price_range_max_pct": 0.03,
    },
    "exit_params": _EXIT_PARAMS,
    "size_usd": _SIZE_USD,
    "leverage": _LEVERAGE,
}
CONTRACT_SHA256 = hashlib.sha256(json.dumps(_CONTRACT_PAYLOAD, sort_keys=True).encode()).digest()

# We need a robust CTE to find accumulation candidates for the last 120 minutes.
_SQL_SCANNER = """
WITH recent_bars AS (
    SELECT
        exchange,
        symbol,
        bucket_start,
        close_price,
        open_interest,
        buy_total_notional_usd,
        sell_total_notional_usd
    FROM timeseries.bybit_momentum_bars_1m
    WHERE bucket_start >= NOW() - INTERVAL '125 minutes'
      AND open_interest IS NOT NULL
),
rolling AS (
    SELECT
        exchange,
        symbol,
        bucket_start,
        close_price,
        open_interest,
        FIRST_VALUE(open_interest) OVER w AS oi_start_2h,
        MAX(close_price) OVER w AS price_max_2h,
        MIN(close_price) OVER w AS price_min_2h,
        SUM(buy_total_notional_usd) OVER w AS buy_vol_2h,
        SUM(sell_total_notional_usd) OVER w AS sell_vol_2h
    FROM recent_bars
    WINDOW w AS (
        PARTITION BY exchange, symbol
        ORDER BY bucket_start
        ROWS BETWEEN 120 PRECEDING AND CURRENT ROW
    )
),
latest AS (
    -- Only take the single most recent row for each exchange+symbol to evaluate current state
    SELECT DISTINCT ON (exchange, symbol) *
    FROM rolling
    ORDER BY exchange, symbol, bucket_start DESC
)
SELECT *
FROM latest
WHERE oi_start_2h > 0
  AND price_min_2h > 0
  AND (open_interest - oi_start_2h) / oi_start_2h > 0.05
  AND (buy_vol_2h - sell_vol_2h) > 0
  AND (price_max_2h - price_min_2h) / price_min_2h < 0.03;
"""


def _watch_key(episode_id: str) -> str:
    return f"{_WATCH_KEY_PREFIX}{episode_id}"


def _watch_payload(ep: episodes.Episode) -> dict[str, Any]:
    return {
        "episode_id": ep.episode_id,
        "ceiling": ep.ceiling,
        "native_market_id": ep.native_market_id,
        "source_exchange": ep.source_exchange,
        "source_native_id": ep.source_native_id,
    }


async def _write_watch_cache(rdb: Any, ep: episodes.Episode) -> None:
    ttl = max(5, int((ep.expires_at - datetime.now(tz=UTC)).total_seconds()))
    await rdb.set(_watch_key(ep.episode_id), json.dumps(_watch_payload(ep)), ex=ttl)


async def run_early_momentum_scanner(rdb: Any, cfg: Config) -> None:
    """Scans for accumulation candidates and arms a durable episode for each."""
    if not cfg.db_url:
        log.warning("early_momentum.scanner_disabled", reason="no db_url")
        return

    while True:
        try:
            await _scan_once(rdb, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("early_momentum.scanner_error", err=str(exc))

        await asyncio.sleep(_SCAN_INTERVAL)


async def _scan_once(rdb: Any, cfg: Config) -> None:
    assert cfg.db_url is not None
    async with (
        await psycopg.AsyncConnection.connect(cfg.db_url) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(_SQL_SCANNER)
        candidates = await cur.fetchall()
    if not candidates:
        return

    strategy_id = await journal.ensure_strategy(
        cfg.db_url, name=_STRATEGY_NAME, version=_STRATEGY_VERSION
    )
    if strategy_id is None:
        log.error("early_momentum.scanner_strategy_upsert_failed")
        return

    # Batch by source exchange: one staleness check and one route-resolution
    # query per exchange present this tick, instead of N round trips.
    by_source_exchange: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        by_source_exchange.setdefault(c["exchange"], []).append(c)

    max_age_seconds = cfg.identity_snapshot_max_age_hours * 3600
    for source_exchange, rows in by_source_exchange.items():
        age = await episodes.identity_snapshot_age_seconds(cfg.db_url, exchange=source_exchange)
        catalog_stale = age is None or age > max_age_seconds

        routes = await episodes.resolve_routes_batch(
            cfg.db_url,
            source_exchange=source_exchange,
            source_native_ids=[row["symbol"] for row in rows],
            execution_exchange=_EXECUTION_EXCHANGE,
        )
        for c in rows:
            await _process_candidate(
                rdb,
                cfg,
                candidate=c,
                strategy_id=strategy_id,
                source_exchange=source_exchange,
                route=routes.get(c["symbol"]),
                catalog_stale=catalog_stale,
            )


async def _process_candidate(
    rdb: Any,
    cfg: Config,
    *,
    candidate: dict[str, Any],
    strategy_id: int,
    source_exchange: str,
    route: episodes.BatchRoute | None,
    catalog_stale: bool,
) -> None:
    assert cfg.db_url is not None
    raw_symbol = candidate["symbol"]
    ceiling = float(candidate["price_max_2h"])
    features = {
        "oi_growth_pct": round(
            (candidate["open_interest"] - candidate["oi_start_2h"])
            / candidate["oi_start_2h"]
            * 100,
            2,
        ),
        "bucket_start": str(candidate["bucket_start"]),
    }

    if catalog_stale:
        await episodes.create_rejected_episode(
            cfg.db_url,
            strategy_id=strategy_id,
            contract_sha256=CONTRACT_SHA256,
            source_exchange=source_exchange,
            source_native_id=raw_symbol,
            exchange=_EXECUTION_EXCHANGE,
            native_market_id="",
            ceiling=ceiling,
            features=features,
            reason=episodes.REASON_IDENTITY_CATALOG_STALE,
        )
        return

    if route is None:
        await episodes.create_rejected_episode(
            cfg.db_url,
            strategy_id=strategy_id,
            contract_sha256=CONTRACT_SHA256,
            source_exchange=source_exchange,
            source_native_id=raw_symbol,
            exchange=_EXECUTION_EXCHANGE,
            native_market_id="",
            ceiling=ceiling,
            features=features,
            reason=episodes.REASON_IDENTITY_UNRESOLVED,
        )
        return

    if await episodes.within_rearm_cooldown(
        cfg.db_url,
        exchange=_EXECUTION_EXCHANGE,
        native_market_id=route.execution_native_id,
        cooldown_seconds=cfg.early_momentum_rearm_cooldown_seconds,
    ):
        await episodes.create_rejected_episode(
            cfg.db_url,
            strategy_id=strategy_id,
            contract_sha256=CONTRACT_SHA256,
            source_exchange=source_exchange,
            source_native_id=raw_symbol,
            exchange=_EXECUTION_EXCHANGE,
            native_market_id=route.execution_native_id,
            execution_identity_key=route.execution_identity_key,
            source_identity_key=route.source_identity_key,
            cluster_key=route.cluster_key,
            ceiling=ceiling,
            features=features,
            reason=episodes.REASON_REARM_COOLDOWN,
        )
        return

    ep = await episodes.create_episode(
        cfg.db_url,
        strategy_id=strategy_id,
        contract_sha256=CONTRACT_SHA256,
        source_exchange=source_exchange,
        source_native_id=raw_symbol,
        exchange=_EXECUTION_EXCHANGE,
        native_market_id=route.execution_native_id,
        execution_symbol=None,  # not known without a live exchange client yet
        execution_identity_key=route.execution_identity_key,
        source_identity_key=route.source_identity_key,
        cluster_key=route.cluster_key,
        ceiling=ceiling,
        features=features,
        ttl_seconds=3600,
    )
    if ep is None:
        # Already watching this instrument (armed or claimed) -- the
        # immutable-WATCH rule, enforced by the partial unique index rather
        # than an app-level check: no new ceiling, no new expiry, no second
        # episode.
        return

    log.info(
        "early_momentum.armed",
        episode_id=ep.episode_id,
        base=raw_symbol,
        ceiling=ceiling,
        oi_growth=features["oi_growth_pct"],
    )
    await _write_watch_cache(rdb, ep)


async def run_early_momentum_trigger(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    """Reaps overdue episodes, repairs the Redis WATCH cache from Postgres
    (the source of truth), then polls the cache for breakouts to claim and
    open."""
    if not cfg.db_url:
        log.warning("early_momentum.trigger_disabled", reason="no db_url")
        return

    while True:
        try:
            await _trigger_tick(exchanges, rdb, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("early_momentum.trigger_error", err=str(exc))

        await asyncio.sleep(_TRIGGER_INTERVAL)


async def _trigger_tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    assert cfg.db_url is not None

    # 1. Reap only the truly-dead cases (see episodes.reap_overdue) --
    # runs every tick unconditionally, even with zero new candidates.
    await episodes.reap_overdue(cfg.db_url, max_claim_attempts=_MAX_CLAIM_ATTEMPTS)

    # 2. DB-driven discovery: Postgres is the source of truth, never only
    # the Redis scan below -- an episode INSERT that committed while the
    # Redis SET failed, a flushed/restarted Redis, or a WATCH key that
    # expired before recovery must all still be found here and have their
    # cache entry repaired.
    actionable = await episodes.list_actionable(
        cfg.db_url, batch_size=_LIST_ACTIONABLE_BATCH_SIZE, max_claim_attempts=_MAX_CLAIM_ATTEMPTS
    )
    for ep in actionable:
        if not await rdb.exists(_watch_key(ep.episode_id)):
            await _write_watch_cache(rdb, ep)

    # 3. Same reasoning, for the trade itself: a crash between
    # open_trade_for_episode's commit and paper.py's own rdb.set(...) would
    # otherwise orphan an already-'opened' trade forever -- it's already
    # past list_actionable's armed/claimed scope, so nothing else would
    # ever notice it needs a position:paper:* key to be monitored.
    await paper.reconcile_missing_positions(rdb, cfg)

    ex = exchanges.get(_EXECUTION_EXCHANGE)
    if not ex:
        log.error("early_momentum.exchange_not_found", exchange=_EXECUTION_EXCHANGE)
        return

    keys = [k async for k in rdb.scan_iter(f"{_WATCH_KEY_PREFIX}*")]
    if not keys:
        return

    tickers = await ex.fetch_tickers()
    for key in keys:
        raw = await rdb.get(key)
        if not raw:
            continue
        cached = json.loads(raw)
        await _check_breakout(ex, rdb, cfg, cached=cached, tickers=tickers)


async def _check_breakout(
    ex: Any, rdb: Any, cfg: Config, *, cached: dict[str, Any], tickers: dict[str, Any]
) -> None:
    assert cfg.db_url is not None
    episode_id = cached["episode_id"]
    ceiling = float(cached["ceiling"])
    native_market_id = cached["native_market_id"]

    try:
        instrument = symbols.resolve_execution_instrument(ex, native_market_id)
    except (RuntimeError, ValueError):
        # Not resolvable against the currently-loaded markets on this poll
        # (e.g. not loaded yet) -- try again next tick, the cache entry
        # (or its DB row) is still there either way.
        return

    ticker = tickers.get(instrument.symbol)
    if not ticker:
        return
    last_price = float(ticker.get("last") or 0)
    if last_price <= ceiling:
        return

    # Breakout! Stop tracking it via the fast path regardless of what
    # happens below -- list_actionable will re-derive the cache entry next
    # tick if the episode is still armed (claim attempt failed) or reclaim
    # a claimed-but-expired one; it will not resurrect a terminal episode.
    await rdb.delete(_watch_key(episode_id))
    log.info(
        "early_momentum.breakout", episode_id=episode_id, base=native_market_id, ceiling=ceiling
    )

    # Belt-and-suspenders: the partial unique index only blocks a *second*
    # armed/claimed episode -- it doesn't span all the way through an
    # already-opened, still-open position. Reuses the same open-position
    # dedup pump-short/liquidation_cascade already rely on.
    open_id = await journal.find_open_trade_id(
        cfg.db_url, exchange=_EXECUTION_EXCHANGE, symbol=instrument.symbol
    )
    if open_id:
        await episodes.terminate_episode(
            cfg.db_url,
            episode_id=episode_id,
            reason=episodes.REASON_ALREADY_OPEN,
            status=episodes.STATUS_SUPPRESSED,
        )
        return

    claim = await episodes.claim_episode(
        cfg.db_url, episode_id=episode_id, lease_seconds=_CLAIM_LEASE_SECONDS
    )
    if not claim.claimed or claim.episode is None or claim.claim_token is None:
        # Already claimed (or gone) -- nothing to do; list_actionable
        # reconciles this on a later tick if it's still genuinely actionable.
        return
    ep = claim.episode
    claim_token = claim.claim_token

    await episodes.set_execution_symbol(
        cfg.db_url, episode_id=episode_id, execution_symbol=instrument.symbol
    )

    # Route re-check immediately before spending the entry quote: a route
    # that went stale between ARM and claim must terminate, never be
    # silently re-resolved to a different one.
    if not await episodes.route_still_confirmed(
        cfg.db_url,
        cluster_key=ep.cluster_key,
        exchange=ep.exchange,
        native_market_id=ep.native_market_id,
    ):
        await episodes.terminate_episode(
            cfg.db_url,
            episode_id=episode_id,
            claim_token=claim_token,
            reason=episodes.REASON_ROUTE_INVALIDATED,
        )
        return

    reservation_token = str(uuid.uuid4())
    reserved = await paper.reserve_position(
        rdb,
        exchange=_EXECUTION_EXCHANGE,
        base=instrument.base,
        token=reservation_token,
        ttl_seconds=_RESERVATION_TOKEN_TTL_SECONDS,
    )
    if not reserved:
        await episodes.terminate_episode(
            cfg.db_url,
            episode_id=episode_id,
            claim_token=claim_token,
            reason=episodes.REASON_POSITION_EXISTS,
            status=episodes.STATUS_SUPPRESSED,
        )
        return

    try:
        await _quote_and_open(
            ex,
            rdb,
            cfg,
            instrument=instrument,
            episode_id=episode_id,
            claim_token=claim_token,
            source_exchange=ep.source_exchange,
            source_native_id=ep.source_native_id,
            ceiling=ceiling,
        )
    finally:
        await paper.release_reservation(
            rdb, exchange=_EXECUTION_EXCHANGE, base=instrument.base, token=reservation_token
        )


async def _quote_and_open(
    ex: Any,
    rdb: Any,
    cfg: Config,
    *,
    instrument: symbols.ExecutionInstrument,
    episode_id: str,
    claim_token: str,
    source_exchange: str,
    source_native_id: str,
    ceiling: float,
) -> None:
    assert cfg.db_url is not None

    # LONG entry buys, so it prices off the ask side of a fresh order book
    # at the actual requested notional -- never the last-trade ticker
    # print, which says nothing about what this size could actually fill
    # at. depth_target (a multiple of the real size) is only the
    # market-quality gate's safety margin; the VWAP actually priced below
    # is measured at _SIZE_USD, the real trade size.
    depth_target = liquidity.depth_target_usd(_SIZE_USD, cfg.liquidity_depth_multiplier)
    snap = await liquidity.snapshot(ex, instrument.symbol, required_depth_usd=depth_target)
    quality = liquidity.check_market_quality(
        snap,
        target_usd=depth_target,
        max_spread_bps=cfg.max_spread_bps,
        max_impact_bps=cfg.max_liquidity_impact_bps,
    )
    if not quality.allowed:
        log.info(
            "early_momentum.market_quality_gate_skip",
            episode_id=episode_id,
            symbol=instrument.symbol,
            reason=quality.reason,
        )
        await episodes.terminate_episode(
            cfg.db_url,
            episode_id=episode_id,
            claim_token=claim_token,
            reason=_market_quality_reason(quality.reason),
        )
        return

    entry_vwap, entry_impact_bps, entry_filled_usd = liquidity.quote_for_side(
        snap, position_side="long", leg="entry", target_usd=_SIZE_USD
    )
    if entry_vwap is None:
        log.warning(
            "early_momentum.entry_quote_unavailable",
            episode_id=episode_id,
            symbol=instrument.symbol,
        )
        await episodes.terminate_episode(
            cfg.db_url,
            episode_id=episode_id,
            claim_token=claim_token,
            reason=episodes.REASON_INVALID_ORDER_BOOK,
        )
        return

    setup_context = {
        # v3: durable episode lifecycle (frozen route/identity, atomic
        # claim, idempotent open/close). Same signal/exit_params as v1/v2
        # -- this is a measurement/reliability change, not a trading rule
        # change.
        "strategy": f"{_STRATEGY_NAME}_v{_STRATEGY_VERSION}",
        "episode_id": episode_id,
        "breakout_price": ceiling,
        "signal_source": source_exchange,
        "source_symbol": source_native_id,
        # quality/market_quality reflects the gate's safety-margined
        # depth_target notional; these two fields are the actual
        # entry-side reading at the real trade size (_SIZE_USD), which
        # entry_vwap above was priced from -- kept as first-class evidence.
        "market_quality": asdict(quality),
        "entry_vwap_impact_bps": entry_impact_bps,
        "entry_vwap_filled_usd": entry_filled_usd,
        # entry_vwap already walked the ask book to this notional, so its
        # gap from mid IS the entry impact cost -- accounting_contract must
        # not also charge market_quality's ask_impact_bps a second time.
        "entry_price_includes_impact": True,
    }

    outcome = await paper.open_paper_for_episode(
        rdb,
        instrument=instrument,
        price=entry_vwap,
        size_usd=_SIZE_USD,
        leverage=_LEVERAGE,
        score=100,  # Synthetic score
        setup_context=setup_context,
        cfg=cfg,
        side="long",
        exit_params=_EXIT_PARAMS,
        episode_id=episode_id,
        claim_token=claim_token,
        entry_idempotency_key=f"{episode_id}:entry:base",
    )
    if outcome.trade_id is None:
        log.error(
            "early_momentum.open_trade_for_episode_failed",
            episode_id=episode_id,
            claim_valid=outcome.claim_valid,
        )
        if outcome.claim_valid:
            await episodes.terminate_episode(
                cfg.db_url,
                episode_id=episode_id,
                claim_token=claim_token,
                reason=episodes.REASON_INFRASTRUCTURE_FAILURE,
            )
        # else: the claim was already invalid (reclaimed/expired under us)
        # -- nothing left to terminate with our own stale token.


def _market_quality_reason(reason: str) -> str:
    return {
        "market_quality_snapshot_unavailable": episodes.REASON_INVALID_ORDER_BOOK,
        "market_quality_invalid_spread": episodes.REASON_INVALID_ORDER_BOOK,
        "market_quality_spread_too_wide": episodes.REASON_SPREAD_TOO_WIDE,
        "market_quality_invalid_depth": episodes.REASON_INVALID_ORDER_BOOK,
        "market_quality_insufficient_bid_depth": episodes.REASON_INSUFFICIENT_DEPTH,
        "market_quality_insufficient_ask_depth": episodes.REASON_INSUFFICIENT_DEPTH,
        "market_quality_entry_impact_too_high": episodes.REASON_IMPACT_TOO_HIGH,
        "market_quality_exit_impact_too_high": episodes.REASON_IMPACT_TOO_HIGH,
    }.get(reason, episodes.REASON_INSUFFICIENT_DEPTH)
