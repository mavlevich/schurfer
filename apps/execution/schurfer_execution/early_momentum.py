"""Early-momentum breakout detection: candidate -> durable episode -> claim -> paper open.

The full lifecycle contract this module orchestrates lives in episodes.py:
armed -> claimed -> opened/expired/rejected/suppressed, with Postgres as the
source of truth and Redis's WATCH cache as a repairable, self-healing view
of it -- never the only place a live episode can be found. See
episodes.py's module docstring and migration 0032 for the full reasoning.

v4 adds a frozen input-quality contract (EARLY_MOMENTUM_V4_QUALITY_POLICY,
schurfer_market_quality) the scanner window must satisfy before a candidate
is even signal-evaluated, full quality evidence persisted on every armed
episode, worker heartbeats, and an independent health monitor. See
fix/early-momentum-input-quality-v1's plan for the full reasoning and the
real production calibration behind the quality thresholds below.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import structlog
from psycopg.rows import dict_row
from schurfer_market_quality import (
    Capability,
    SeriesIdentity,
    WindowQualityEvidence,
    WindowQualityPolicy,
    WindowQualityReason,
    WindowQualityResult,
)
from schurfer_market_quality import validate as validate_window_quality

from . import (
    early_momentum_health,
    episodes,
    journal,
    liquidity,
    notify,
    paper,
    symbols,
    worker_health,
)

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_EXECUTION_EXCHANGE = "bybit"  # Hardcoded for now since momentum flow is Bybit only
_SOURCE_EXCHANGES = ["bybit", "binance"]
_WATCH_KEY_PREFIX = "market:early_momentum:v4:watch:"
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
_STRATEGY_VERSION = "4"

# Hardcoded best parameters from backtest -- unchanged from v1/v2/v3. This
# PR does not touch the signal, TP/SL, max-hold, or these thresholds; only
# the trustworthiness of the window the signal is computed over.
_EXIT_PARAMS = {
    "initial_sl_pct": 10.0,
    "activation_pct": 10.0,  # Not used since TP hits first, but required
    "trail_pct": 10.0,
    "trail_tighten_pct": 10.0,
    "tighten_after_min": 240.0,
    "max_hold_min": 240.0,  # 4 hours
    "take_profit_pct": 4.0,  # The holy grail parameter
}
_SIGNAL_OI_GROWTH_MIN_PCT = 0.05
_SIGNAL_PRICE_RANGE_MAX_PCT = 0.03

# The frozen input-quality contract every scanner window must satisfy
# before it's trusted enough to even evaluate the signal against. Values
# calibrated against real production data -- see
# docs/research/early-momentum-v4-oi-freshness-calibration.md for the full
# calibration queries/results (a versioned record, not just a comment,
# so a future threshold change has a traceable origin to update):
#   - required_bucket_count=121 / max_bucket_lag_seconds=180: a 3-day
#     historical measurement of collector write-lag showed ~65-70s
#     p50=p95=p99=max for both exchanges, zero minutes over 180s in 3 full
#     days -- wide margin, not an operational guess.
#   - max_oi_age_seconds_by_exchange: binance re-polls OI on a fixed REST
#     cadence (event_at always advances even when the value doesn't
#     change), so 180s is tight and safe there. Bybit is delta-push --
#     event_at only advances when the OI field is actually present in a
#     message, so an old event_at does NOT by itself mean a broken feed.
#     A live REST cross-check (ground truth) for the worst observed case
#     confirmed a "stale" WS reading was simply unchanged, not wrong;
#     ticker/price feeds stayed fresh on every such symbol. 600s is the
#     conservative starting point pending a longer observation window --
#     any future tightening changes this policy's hash and therefore the
#     strategy cohort, never a silent runtime tweak.
#   - allowed_capture_versions={"v1"}: the only value either exchange has
#     ever written. An explicit allowlist, not just count(DISTINCT)==1 --
#     a future v2 capture format must not silently pass as "uniform".
EARLY_MOMENTUM_V4_QUALITY_POLICY = WindowQualityPolicy(
    cadence_seconds=60,
    required_bucket_count=121,
    max_bucket_lag_seconds=180,
    max_oi_age_seconds_by_exchange=(("binance", 180), ("bybit", 600)),
    required_capabilities=(Capability.PRICE, Capability.TRADES, Capability.OPEN_INTEREST),
    allowed_market_types=("linear",),
    allowed_capture_versions=frozenset({"v1"}),
    require_single_capture_version=True,
    require_single_universe_version=True,
    future_timestamp_tolerance_seconds=5,
)
_QUALITY_POLICY_HASH = hashlib.sha256(
    json.dumps(EARLY_MOMENTUM_V4_QUALITY_POLICY.to_canonical_dict(), sort_keys=True).encode()
).hexdigest()

# Canonical serialization of everything an episode is armed under: the
# frozen quality policy plus the signal thresholds and exit contract.
# Sorted keys and only fixed literal values make this deterministic across
# runs and machines.
_CONTRACT_PAYLOAD: dict[str, Any] = {
    "scanner": {"quality": EARLY_MOMENTUM_V4_QUALITY_POLICY.to_canonical_dict()},
    "signal": {
        "oi_growth_min_pct": _SIGNAL_OI_GROWTH_MIN_PCT,
        "price_range_max_pct": _SIGNAL_PRICE_RANGE_MAX_PCT,
    },
    "exit_params": _EXIT_PARAMS,
    "size_usd": _SIZE_USD,
    "leverage": _LEVERAGE,
}
CONTRACT_SHA256 = hashlib.sha256(json.dumps(_CONTRACT_PAYLOAD, sort_keys=True).encode()).digest()

# Worker heartbeats (see worker_health.py) -- TTL is deliberately generous
# rather than optimistic: a single post-tick write with a short TTL can
# expire mid-tick on a slow iteration (many episodes, a CCXT timeout) even
# though the worker is alive. 360s needs its own observation period once
# deployed; start conservative.
_SCANNER_HEARTBEAT_KEY = "worker:early_momentum:v4:scanner:heartbeat"
_TRIGGER_HEARTBEAT_KEY = "worker:early_momentum:v4:trigger:heartbeat"
_HEARTBEAT_TTL_SECONDS = 360

_HEALTH_MONITOR_INTERVAL_SECONDS = 30
_HEALTH_MONITOR_STARTUP_GRACE_SECONDS = 180
_HEALTH_ZERO_QUALITY_READY_ERROR_THRESHOLD = 3
_HEALTH_ZERO_QUALITY_READY_COUNTER_KEY = "early_momentum:v4:health:consecutive_zero_quality_ready"
_HEALTH_ALERT_STATUS_KEY = "early_momentum:v4:health:last_alerted_status"
_HEALTH_ALERT_COOLDOWN_KEY_PREFIX = "early_momentum:v4:health:alert_cooldown:"

# Only the scope this window is evaluated over -- exchange (implicit via
# PARTITION), market_type, and time bounds -- is filtered in SQL. Every
# per-row defect (completeness, gaps, staleness, duplicate buckets, future
# timestamps, invalid price/OI, version mixing) is computed as a *column*
# on the evidence row instead, so the pure validator can name the exact
# reason -- hiding a bad row in a WHERE clause would make it
# indistinguishable from a row that was simply never written (colleague
# review).
_SQL_SCANNER = """
WITH scoped_bars AS (
    SELECT
        exchange, market_type, symbol, bucket_start, close_price, open_interest,
        capture_version, universe_version, price_complete, trades_complete,
        open_interest_complete, open_interest_event_at, unbackfilled_gap_minutes,
        buy_total_notional_usd, sell_total_notional_usd
    FROM timeseries.bybit_momentum_bars_1m
    WHERE bucket_start >= now() - make_interval(mins := %(fetch_lookback_minutes)s)
      AND market_type = ANY(%(allowed_market_types)s)
),
ranked AS (
    SELECT *,
        row_number() OVER (
            PARTITION BY exchange, market_type, symbol ORDER BY bucket_start DESC
        ) AS rn
    FROM scoped_bars
),
window_rows AS (
    -- The most recent required_bucket_count rows per series -- may include
    -- duplicate bucket_start entries from a capture-version seam; that's
    -- intentional, has_duplicate_bucket below must see them.
    SELECT * FROM ranked WHERE rn <= %(required_bucket_count)s
),
gapped AS (
    SELECT *,
        bucket_start - lag(bucket_start) OVER (
            PARTITION BY exchange, market_type, symbol ORDER BY bucket_start
        ) AS gap_to_prev
    FROM window_rows
)
SELECT
    exchange, market_type, symbol,
    min(bucket_start) AS window_start,
    max(bucket_start) AS window_end,
    count(*) AS raw_row_count,
    count(DISTINCT bucket_start) AS distinct_bucket_count,
    COALESCE(EXTRACT(EPOCH FROM max(gap_to_prev)), 0) AS max_gap_seconds,
    array_agg(DISTINCT capture_version ORDER BY capture_version) AS capture_versions,
    array_agg(DISTINCT universe_version ORDER BY universe_version) AS universe_versions,
    count(*) FILTER (WHERE price_complete) AS price_complete_count,
    count(*) FILTER (WHERE trades_complete) AS trades_complete_count,
    count(*) FILTER (WHERE open_interest_complete) AS oi_complete_count,
    min(open_interest_event_at) AS first_oi_event_at,
    max(open_interest_event_at) AS latest_oi_event_at,
    COALESCE(sum(unbackfilled_gap_minutes), 0) AS unbackfilled_gap_minutes_sum,
    bool_or(
        bucket_start > now() + make_interval(secs := %(future_tolerance_seconds)s)
    ) AS has_future_timestamp,
    bool_or(close_price IS NULL) AS has_invalid_price,
    bool_or(open_interest IS NULL OR open_interest <= 0) AS has_invalid_open_interest,
    (count(*) > count(DISTINCT bucket_start)) AS has_duplicate_bucket,
    (array_agg(open_interest ORDER BY bucket_start ASC))[1] AS oi_start,
    (array_agg(open_interest ORDER BY bucket_start DESC))[1] AS oi_latest,
    max(close_price) AS price_max,
    min(close_price) AS price_min,
    COALESCE(sum(buy_total_notional_usd), 0) AS buy_vol,
    COALESCE(sum(sell_total_notional_usd), 0) AS sell_vol
FROM gapped
GROUP BY exchange, market_type, symbol;
"""


def _scanner_sql_params(policy: WindowQualityPolicy) -> dict[str, Any]:
    return {
        # Comfortable slack over required_bucket_count so a genuinely
        # gappy window still has enough raw history to prove the gap,
        # rather than the fetch itself truncating the evidence.
        "fetch_lookback_minutes": policy.required_bucket_count + 20,
        "allowed_market_types": list(policy.allowed_market_types),
        "required_bucket_count": policy.required_bucket_count,
        "future_tolerance_seconds": policy.future_timestamp_tolerance_seconds,
    }


def _row_to_evidence(row: dict[str, Any]) -> WindowQualityEvidence:
    return WindowQualityEvidence(
        identity=SeriesIdentity(
            exchange=row["exchange"], market_type=row["market_type"], symbol=row["symbol"]
        ),
        window_start=row["window_start"],
        window_end=row["window_end"],
        raw_row_count=row["raw_row_count"],
        distinct_bucket_count=row["distinct_bucket_count"],
        max_gap_seconds=float(row["max_gap_seconds"]),
        latest_bucket_start=row["window_end"],
        capture_versions=tuple(row["capture_versions"] or ()),
        universe_versions=tuple(row["universe_versions"] or ()),
        price_complete_count=row["price_complete_count"],
        trades_complete_count=row["trades_complete_count"],
        oi_complete_count=row["oi_complete_count"],
        first_oi_event_at=row["first_oi_event_at"],
        latest_oi_event_at=row["latest_oi_event_at"],
        unbackfilled_gap_minutes_sum=int(row["unbackfilled_gap_minutes_sum"]),
        has_future_timestamp=bool(row["has_future_timestamp"]),
        has_invalid_price=bool(row["has_invalid_price"]),
        has_invalid_open_interest=bool(row["has_invalid_open_interest"]),
        has_duplicate_bucket=bool(row["has_duplicate_bucket"]),
    )


# --- strategy-owned signal features -- deliberately NOT in
# schurfer_market_quality, which knows nothing about OI growth, price
# range, or buy-dominance (colleague review: quality and signal are
# different concerns, kept in different types). ---


@dataclass(frozen=True)
class EarlyMomentumSignalFeatures:
    oi_growth_pct: float
    price_range_pct: float
    net_taker_flow_usd: float
    ceiling: float
    bucket_start: datetime


@dataclass(frozen=True)
class EarlyMomentumSignalResult:
    features: EarlyMomentumSignalFeatures
    qualified: bool


def _compute_signal(row: dict[str, Any]) -> EarlyMomentumSignalResult:
    oi_start = row["oi_start"] or 0.0
    oi_latest = row["oi_latest"] or 0.0
    price_min = row["price_min"] or 0.0
    price_max = row["price_max"] or 0.0
    buy_vol = float(row["buy_vol"] or 0.0)
    sell_vol = float(row["sell_vol"] or 0.0)
    net_taker_flow_usd = buy_vol - sell_vol

    if oi_start <= 0 or price_min <= 0:
        oi_growth_pct = 0.0
        price_range_pct = 0.0
        qualified = False
    else:
        oi_growth_pct = (oi_latest - oi_start) / oi_start
        price_range_pct = (price_max - price_min) / price_min
        qualified = (
            oi_growth_pct > _SIGNAL_OI_GROWTH_MIN_PCT
            and net_taker_flow_usd > 0
            and price_range_pct < _SIGNAL_PRICE_RANGE_MAX_PCT
        )

    features = EarlyMomentumSignalFeatures(
        oi_growth_pct=oi_growth_pct,
        price_range_pct=price_range_pct,
        net_taker_flow_usd=net_taker_flow_usd,
        ceiling=price_max or 0.0,
        bucket_start=row["window_end"],
    )
    return EarlyMomentumSignalResult(features=features, qualified=qualified)


def _episode_features(
    quality_result: WindowQualityResult, signal: EarlyMomentumSignalResult
) -> dict[str, Any]:
    """Full quality + signal evidence persisted on every armed (and every
    rejected-past-quality-gate) episode -- so a later performance report
    can prove the exact provenance of every trade, not just its outcome."""
    evidence = quality_result.evidence
    return {
        "quality_policy_version": _QUALITY_POLICY_HASH,
        "window_start": evidence.window_start.isoformat(),
        "window_end": evidence.window_end.isoformat(),
        "bucket_count": evidence.raw_row_count,
        "distinct_bucket_count": evidence.distinct_bucket_count,
        "max_gap_seconds": evidence.max_gap_seconds,
        "market_type": evidence.identity.market_type,
        "capture_version": evidence.capture_versions[0] if evidence.capture_versions else None,
        "universe_version": evidence.universe_versions[0] if evidence.universe_versions else None,
        "first_oi_age_seconds": (
            (evidence.window_end - evidence.first_oi_event_at).total_seconds()
            if evidence.first_oi_event_at
            else None
        ),
        "last_oi_age_seconds": (
            (evidence.window_end - evidence.latest_oi_event_at).total_seconds()
            if evidence.latest_oi_event_at
            else None
        ),
        "price_complete_count": evidence.price_complete_count,
        "trades_complete_count": evidence.trades_complete_count,
        "oi_complete_count": evidence.oi_complete_count,
        "quality_reasons": [reason.value for reason in quality_result.reasons],
        "oi_growth_pct": round(signal.features.oi_growth_pct * 100, 2),
        "price_range_pct": round(signal.features.price_range_pct * 100, 2),
        "net_taker_flow_usd": round(signal.features.net_taker_flow_usd, 2),
        "bucket_start": signal.features.bucket_start.isoformat(),
    }


# One rollup counter per WindowQualityReason, plus the pipeline-stage
# counters -- so "zero candidates_found" is always explainable (quality
# gates rejecting everything vs. quality-clean windows just not
# signal-qualifying vs. the scanner not running at all) instead of an
# opaque zero (colleague review).
_REASON_COUNTER_KEYS: dict[WindowQualityReason, str] = {
    WindowQualityReason.INCOMPLETE_PRICE: "rejected_incomplete",
    WindowQualityReason.INCOMPLETE_TRADES: "rejected_incomplete",
    WindowQualityReason.INCOMPLETE_OI: "rejected_incomplete",
    WindowQualityReason.GAP: "rejected_gap",
    WindowQualityReason.STALE_BUCKET: "rejected_stale_bucket",
    WindowQualityReason.STALE_OI: "rejected_stale_oi",
    WindowQualityReason.CAPTURE_VERSION_NOT_ALLOWED: "rejected_capture_version_not_allowed",
    WindowQualityReason.MULTIPLE_CAPTURE_VERSIONS: "rejected_multiple_capture_versions",
    WindowQualityReason.MULTIPLE_UNIVERSE_VERSIONS: "rejected_multiple_universe_versions",
    WindowQualityReason.DUPLICATE_BUCKET: "rejected_duplicate_bucket",
    WindowQualityReason.WRONG_MARKET_TYPE: "rejected_wrong_market_type",
    WindowQualityReason.FUTURE_TIMESTAMP: "rejected_future_timestamp",
    WindowQualityReason.INVALID_PRICE: "rejected_invalid_price",
    WindowQualityReason.INVALID_OPEN_INTEREST: "rejected_invalid_open_interest",
    WindowQualityReason.INSUFFICIENT_ROWS: "rejected_insufficient_rows",
}
_SCANNER_COUNTER_KEYS = (
    "symbols_total",
    "quality_ready",
    "rejected_signal",
    "candidates_found",
    *sorted(set(_REASON_COUNTER_KEYS.values())),
)


def _new_scanner_counters() -> dict[str, int]:
    return dict.fromkeys(_SCANNER_COUNTER_KEYS, 0)


def _tally_rejection_counters(
    counters: dict[str, int], reasons: tuple[WindowQualityReason, ...]
) -> None:
    for reason in reasons:
        counters[_REASON_COUNTER_KEYS[reason]] += 1


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
            async with worker_health.track_tick(
                rdb,
                key=_SCANNER_HEARTBEAT_KEY,
                worker_name="early_momentum_scanner",
                worker_version=_STRATEGY_VERSION,
                ttl_seconds=_HEARTBEAT_TTL_SECONDS,
            ) as tick:
                tick.counters.update(await _scan_once(rdb, cfg))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("early_momentum.scanner_error", err=str(exc))

        await asyncio.sleep(_SCAN_INTERVAL)


async def _scan_once(rdb: Any, cfg: Config) -> dict[str, int]:
    assert cfg.db_url is not None
    counters = _new_scanner_counters()

    async with (
        await psycopg.AsyncConnection.connect(cfg.db_url) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(_SQL_SCANNER, _scanner_sql_params(EARLY_MOMENTUM_V4_QUALITY_POLICY))
        rows = await cur.fetchall()
        # bucket-lag/OI-staleness are judged against the DATABASE's own
        # clock, not the execution app's -- the evidence timestamps
        # (bucket_start, open_interest_event_at) are DB-written, so
        # comparing them against the app host's local clock would make
        # ordinary NTP drift between the two hosts show up as false
        # staleness (or mask real staleness), even with both nominally on
        # UTC (colleague review).
        await cur.execute("SELECT now()")
        now_row = await cur.fetchone()
        now = now_row["now"] if now_row else datetime.now(tz=UTC)

    qualified: list[tuple[dict[str, Any], WindowQualityResult, EarlyMomentumSignalResult]] = []
    for row in rows:
        evidence = _row_to_evidence(row)
        result = validate_window_quality(
            evidence, EARLY_MOMENTUM_V4_QUALITY_POLICY, evaluated_at=now
        )
        counters["symbols_total"] += 1
        _tally_rejection_counters(counters, result.reasons)
        if not result.qualified:
            continue
        counters["quality_ready"] += 1

        signal = _compute_signal(row)
        if not signal.qualified:
            counters["rejected_signal"] += 1
            continue
        counters["candidates_found"] += 1
        qualified.append((row, result, signal))

    if not qualified:
        return counters

    strategy_id = await journal.ensure_strategy(
        cfg.db_url, name=_STRATEGY_NAME, version=_STRATEGY_VERSION
    )
    if strategy_id is None:
        log.error("early_momentum.scanner_strategy_upsert_failed")
        return counters

    # Batch by source exchange: one staleness check and one route-resolution
    # query per exchange present this tick, instead of N round trips.
    by_source_exchange: dict[
        str, list[tuple[dict[str, Any], WindowQualityResult, EarlyMomentumSignalResult]]
    ] = {}
    for item in qualified:
        by_source_exchange.setdefault(item[0]["exchange"], []).append(item)

    max_age_seconds = cfg.identity_snapshot_max_age_hours * 3600
    for source_exchange, items in by_source_exchange.items():
        age = await episodes.identity_snapshot_age_seconds(cfg.db_url, exchange=source_exchange)
        catalog_stale = age is None or age > max_age_seconds

        routes = await episodes.resolve_routes_batch(
            cfg.db_url,
            source_exchange=source_exchange,
            source_native_ids=[row["symbol"] for row, _, _ in items],
            execution_exchange=_EXECUTION_EXCHANGE,
        )
        for row, quality_result, signal in items:
            await _process_candidate(
                rdb,
                cfg,
                row=row,
                quality_result=quality_result,
                signal=signal,
                strategy_id=strategy_id,
                source_exchange=source_exchange,
                route=routes.get(row["symbol"]),
                catalog_stale=catalog_stale,
            )
    return counters


async def _process_candidate(
    rdb: Any,
    cfg: Config,
    *,
    row: dict[str, Any],
    quality_result: WindowQualityResult,
    signal: EarlyMomentumSignalResult,
    strategy_id: int,
    source_exchange: str,
    route: episodes.BatchRoute | None,
    catalog_stale: bool,
) -> None:
    assert cfg.db_url is not None
    raw_symbol = row["symbol"]
    ceiling = signal.features.ceiling
    features = _episode_features(quality_result, signal)

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
            async with worker_health.track_tick(
                rdb,
                key=_TRIGGER_HEARTBEAT_KEY,
                worker_name="early_momentum_trigger",
                worker_version=_STRATEGY_VERSION,
                ttl_seconds=_HEARTBEAT_TTL_SECONDS,
            ):
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
        # v4: durable episode lifecycle + input-quality evidence (frozen
        # route/identity, atomic claim, idempotent open/close, quality
        # policy hash). Same signal/exit_params as v1/v2/v3 -- this is a
        # measurement/reliability change, not a trading rule change.
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


# --- Independent health monitor -----------------------------------------
#
# Runs as its own asyncio task, deliberately separate from the scanner and
# trigger loops it watches: if trigger deadlocks, this task keeps ticking
# and can still report it (today nothing would notice a hung trigger loop
# at all). Scope note: running in the same process/container, it detects a
# hung scanner/trigger *task* -- it cannot detect a crashed container, an
# event-loop deadlock, a dead host, or Redis/Telegram itself being
# unreachable. This is not a full production monitoring system; an
# external watchdog/Docker healthcheck is later, separate work.


_HEALTH_LAST_COUNTED_HEARTBEAT_KEY = "early_momentum:v4:health:last_counted_scanner_heartbeat"

# gather_health_status runs on every independent-monitor tick (~30s) AND
# every HTTP GET /health/early-momentum -- a plain check-then-increment
# would double-count the same completed scanner tick under concurrent or
# just frequent reads (colleague review: polling the endpoint must never
# inflate this counter). Dedupe by the tick's own completed_at, atomically
# via Lua so two concurrent readers can't both win the "first to see this
# tick" race. A 'started' (still mid-tick) heartbeat is never passed in
# here at all -- see the caller below -- so it can neither reset nor
# advance this.
_COUNT_ZERO_QUALITY_READY_TICK_ONCE = """
local last_counted = redis.call("get", KEYS[1])
if last_counted == ARGV[1] then
    local val = redis.call("get", KEYS[2])
    if val then return tonumber(val) else return 0 end
end
redis.call("set", KEYS[1], ARGV[1])
if ARGV[2] == "1" then
    return redis.call("incr", KEYS[2])
else
    redis.call("del", KEYS[2])
    return 0
end
"""


async def _read_zero_quality_ready_counter(rdb: Any) -> int:
    try:
        raw = await rdb.get(_HEALTH_ZERO_QUALITY_READY_COUNTER_KEY)
        return int(raw) if raw else 0
    except Exception as exc:
        log.error("early_momentum.zero_quality_ready_counter_read_failed", err=str(exc))
        return 0


async def _update_zero_quality_ready_counter(
    rdb: Any, *, scanner_heartbeat: worker_health.WorkerHeartbeat | None
) -> int:
    """Tracks consecutive *completed* scanner ticks with symbols_total > 0
    but quality_ready == 0 -- a single noisy tick must read as `degraded`,
    not `error` (see early_momentum_health.compute_status's debounce). A
    missing heartbeat, a still-running ('started') one, or one with no
    completed_at yet just reports the current count unchanged -- only a
    freshly-completed tick can advance or reset it."""
    if scanner_heartbeat is None or scanner_heartbeat.state != worker_health.STATE_COMPLETED:
        return await _read_zero_quality_ready_counter(rdb)
    if scanner_heartbeat.completed_at is None:
        return await _read_zero_quality_ready_counter(rdb)

    marker = scanner_heartbeat.completed_at.isoformat()
    counters = scanner_heartbeat.counters
    symbols_total = counters.get("symbols_total", 0)
    quality_ready = counters.get("quality_ready")
    is_zero_bad_tick = symbols_total > 0 and quality_ready == 0
    try:
        result = await rdb.eval(
            _COUNT_ZERO_QUALITY_READY_TICK_ONCE,
            2,
            _HEALTH_LAST_COUNTED_HEARTBEAT_KEY,
            _HEALTH_ZERO_QUALITY_READY_COUNTER_KEY,
            marker,
            "1" if is_zero_bad_tick else "0",
        )
        return int(result or 0)
    except Exception as exc:
        log.error("early_momentum.zero_quality_ready_counter_failed", err=str(exc))
        return 0


async def gather_health_status(
    rdb: Any, cfg: Config, *, startup_at: datetime
) -> tuple[early_momentum_health.Status, tuple[str, ...], dict[str, Any]]:
    """Gathers every input `early_momentum_health.compute_status` needs and
    calls it -- the one function both the HTTP health endpoint and the
    independent monitor task use, so the read path and the alerting path
    can never disagree about what the status is. Returns (status, reasons,
    raw_metrics) -- raw_metrics is everything the HTTP endpoint additionally
    wants to display beyond the verdict itself."""
    assert cfg.db_url is not None
    now = datetime.now(tz=UTC)

    scanner_heartbeat = await worker_health.read_heartbeat(rdb, key=_SCANNER_HEARTBEAT_KEY)
    trigger_heartbeat = await worker_health.read_heartbeat(rdb, key=_TRIGGER_HEARTBEAT_KEY)

    lifecycle_metrics = await episodes.health_metrics(cfg.db_url)
    source_freshness_by_exchange = await episodes.source_freshness(
        cfg.db_url,
        exchanges=_SOURCE_EXCHANGES,
        market_type=EARLY_MOMENTUM_V4_QUALITY_POLICY.allowed_market_types[0],
        capture_versions=EARLY_MOMENTUM_V4_QUALITY_POLICY.allowed_capture_versions,
    )
    identity_health_by_exchange = await episodes.identity_health(
        cfg.db_url, exchanges=_SOURCE_EXCHANGES, max_age_hours=cfg.identity_snapshot_max_age_hours
    )

    # Read-only lookup -- never journal.ensure_strategy here. This function
    # runs on every health read (the monitor every ~30s, plus every HTTP
    # GET), and ensure_strategy's ON CONFLICT DO UPDATE SET updated_at =
    # now() would make a health check itself a write, silently destroying
    # what updated_at means on app.strategies (colleague review). The
    # strategy row is created by the scanner's own ensure_strategy call the
    # first time it actually has a candidate to arm -- until then, None
    # here correctly means "no v4 activity has ever happened yet".
    v4_strategy_id = await journal.find_strategy_id(
        cfg.db_url, name=_STRATEGY_NAME, version=_STRATEGY_VERSION
    )
    last_open_at = (
        await episodes.last_successful_open_at(cfg.db_url, strategy_id=v4_strategy_id)
        if v4_strategy_id is not None
        else None
    )

    consecutive_zero = await _update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=scanner_heartbeat
    )

    status, reasons = early_momentum_health.compute_status(
        now=now,
        startup_at=startup_at,
        grace_period_seconds=_HEALTH_MONITOR_STARTUP_GRACE_SECONDS,
        scanner_heartbeat=scanner_heartbeat,
        trigger_heartbeat=trigger_heartbeat,
        heartbeat_ttl_seconds=_HEARTBEAT_TTL_SECONDS,
        source_max_lag_seconds={
            exchange: data.get("lag_seconds")
            for exchange, data in source_freshness_by_exchange.items()
        },
        source_lag_limit_seconds=EARLY_MOMENTUM_V4_QUALITY_POLICY.max_bucket_lag_seconds,
        overdue_armed=lifecycle_metrics.get("overdue_armed"),
        expired_claims=lifecycle_metrics.get("expired_claims"),
        consecutive_zero_quality_ready_ticks=consecutive_zero,
        zero_quality_ready_error_threshold=_HEALTH_ZERO_QUALITY_READY_ERROR_THRESHOLD,
        identity_health=identity_health_by_exchange,
    )
    raw_metrics: dict[str, Any] = {
        "scanner_heartbeat": scanner_heartbeat,
        "trigger_heartbeat": trigger_heartbeat,
        "source_freshness": source_freshness_by_exchange,
        "identity_health": identity_health_by_exchange,
        "lifecycle_metrics": lifecycle_metrics,
        "last_successful_open_at": last_open_at,
        "consecutive_zero_quality_ready_ticks": consecutive_zero,
    }
    return status, reasons, raw_metrics


def _format_health_alert(*, status: str, reasons: tuple[str, ...], recovered: bool) -> str:
    if recovered:
        return "early_momentum_v4 health recovered to ok"
    reason_text = ", ".join(reasons) if reasons else "unknown"
    return f"early_momentum_v4 health is {status}: {reason_text}"


async def _maybe_alert(rdb: Any, cfg: Config, *, status: str, reasons: tuple[str, ...]) -> None:
    """Immediate alert on any transition (including recovery to ok); a
    reminder at most once per cooldown while a bad status persists
    unchanged; never silent forever, never spammed every monitor tick.
    Alert delivery failure must never kill the monitor loop -- but it also
    must never be treated as delivered: `last_alerted_status` is only
    written once notify_alert actually confirms Telegram accepted the
    message, and a reminder's cooldown reservation is released again on
    failure so the very next tick can retry instead of waiting out the
    full window for a message that never went out (colleague review)."""
    creds = notify.credentials(cfg)
    if not creds:
        return

    try:
        last_status_raw = await rdb.get(_HEALTH_ALERT_STATUS_KEY)
        last_status = (
            last_status_raw.decode() if isinstance(last_status_raw, bytes) else last_status_raw
        )
    except Exception as exc:
        log.error("early_momentum.health_alert_state_read_failed", err=str(exc))
        return

    transitioned = last_status != status
    # A never-recorded previous status (the very first health tick this
    # process has ever run) is "no signal yet", not "we just recovered" --
    # only a genuine prior non-ok reading turning into ok is a recovery.
    recovered = transitioned and status == "ok" and last_status is not None

    should_send = transitioned
    cooldown_key = f"{_HEALTH_ALERT_COOLDOWN_KEY_PREFIX}{status}"
    cooldown_reserved = False
    if not transitioned and status != "ok":
        try:
            acquired = await rdb.set(
                cooldown_key, "1", nx=True, ex=cfg.early_momentum_health_alert_cooldown_seconds
            )
            should_send = bool(acquired)
            cooldown_reserved = should_send
        except Exception as exc:
            log.error("early_momentum.health_alert_cooldown_failed", err=str(exc))
            return

    if not should_send:
        return

    text = _format_health_alert(status=status, reasons=reasons, recovered=recovered)
    delivered = False
    try:
        delivered = await notify.notify_alert(*creds, text=text)
    except Exception as exc:
        log.error("early_momentum.health_alert_delivery_failed", err=str(exc))

    if not delivered:
        log.error("early_momentum.health_alert_not_delivered", status=status)
        if cooldown_reserved:
            try:
                await rdb.delete(cooldown_key)
            except Exception as exc:
                log.error("early_momentum.health_alert_cooldown_release_failed", err=str(exc))
        return

    try:
        await rdb.set(_HEALTH_ALERT_STATUS_KEY, status)
    except Exception as exc:
        log.error("early_momentum.health_alert_state_write_failed", err=str(exc))


async def _health_monitor_tick(rdb: Any, cfg: Config, *, startup_at: datetime) -> None:
    status, reasons, _raw_metrics = await gather_health_status(rdb, cfg, startup_at=startup_at)
    await _maybe_alert(rdb, cfg, status=status, reasons=reasons)


async def run_early_momentum_health_monitor(rdb: Any, cfg: Config, *, startup_at: datetime) -> None:
    """`startup_at` is passed in (not sampled internally) so the monitor's
    grace-period clock and the HTTP health endpoint's (routers/health.py,
    reading the same timestamp off app.state) can never disagree about
    when the process actually started."""
    if not cfg.db_url:
        log.warning("early_momentum.health_monitor_disabled", reason="no db_url")
        return

    while True:
        try:
            await _health_monitor_tick(rdb, cfg, startup_at=startup_at)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("early_momentum.health_monitor_error", err=str(exc))

        await asyncio.sleep(_HEALTH_MONITOR_INTERVAL_SECONDS)
