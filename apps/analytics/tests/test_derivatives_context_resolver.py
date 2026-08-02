from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_analytics.derivatives_context import (
    DerivativesContextTarget,
    DerivativesContextWork,
)
from schurfer_analytics.derivatives_context_resolver import (
    DEFAULT_COHORT_START,
    DERIVATIVES_CONTEXT_RESOLVER_VERSION,
    LONG_HORIZON_FUNDING_COHORT_START,
    LONG_HORIZON_FUNDING_RESOLVER_VERSION,
    OPEN_ENDED_MARGIN_FUNDING_COHORT_START,
    OPEN_ENDED_MARGIN_FUNDING_RESOLVER_VERSION,
    PERSISTED_METHODS_BY_EXCHANGE,
    DerivativesContextResolverConfig,
    long_horizon_funding_config_from_env,
    open_ended_margin_funding_config_from_env,
    resolve_derivatives_context_once,
)

ANCHOR = datetime(2026, 7, 27, 12, tzinfo=UTC)
SINCE_MS = int((ANCHOR - timedelta(minutes=240)).timestamp() * 1000)
UNTIL_MS = int((ANCHOR + timedelta(minutes=480)).timestamp() * 1000)


def _target(exchange: str = "binance") -> DerivativesContextTarget:
    return DerivativesContextTarget(
        event_id=42,
        exchange=exchange,
        base="ERA",
        unified_symbol="ERA/USDT:USDT",
        market_id="ERAUSDT",
        identity_key=f"{exchange}:unknown:ERAUSDT:unknown",
        anchor_at=ANCHOR,
    )


def _store(work: tuple[DerivativesContextWork, ...]) -> AsyncMock:
    store = AsyncMock()
    store.load_due_work.return_value = work
    return store


def test_persistence_allowlist_contains_only_proven_non_price_series() -> None:
    persisted_methods = {
        method for methods in PERSISTED_METHODS_BY_EXCHANGE.values() for method in methods
    }

    assert persisted_methods == {
        "funding_rate_history",
        "open_interest_history",
        "long_short_ratio_history",
        "liquidations",
    }
    assert PERSISTED_METHODS_BY_EXCHANGE["binance"] == (
        "funding_rate_history",
        "open_interest_history",
        "long_short_ratio_history",
    )
    assert PERSISTED_METHODS_BY_EXCHANGE["htx"] == (
        "funding_rate_history",
        "open_interest_history",
        "liquidations",
    )
    assert DERIVATIVES_CONTEXT_RESOLVER_VERSION == "derivatives_context_v2"


def test_config_parses_forward_cohort_and_validates_bounds() -> None:
    with patch.dict(
        "os.environ",
        {
            "DERIVATIVES_CONTEXT_ENABLED": "false",
            "DERIVATIVES_CONTEXT_SINCE": "2026-08-01T00:00:00Z",
            "DERIVATIVES_CONTEXT_BATCH_SIZE": "4",
        },
        clear=False,
    ):
        cfg = DerivativesContextResolverConfig.from_env()

    assert cfg.enabled is False
    assert cfg.cohort_start == datetime(2026, 8, 1, tzinfo=UTC)
    assert cfg.batch_size == 4
    assert datetime(2026, 7, 27, tzinfo=UTC) == DEFAULT_COHORT_START
    with pytest.raises(ValueError, match="FETCH_LIMIT"):
        DerivativesContextResolverConfig(fetch_limit=0)
    with pytest.raises(ValueError, match="cannot exceed"):
        DerivativesContextResolverConfig(after_minutes=10_081)
    with pytest.raises(ValueError, match="anchor mode"):
        DerivativesContextResolverConfig(anchor_mode="future")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown derivatives context methods"):
        DerivativesContextResolverConfig(method_names=("unknown",))


def test_long_horizon_funding_config_is_closed_anchor_and_funding_only() -> None:
    cfg = long_horizon_funding_config_from_env()

    assert cfg.enabled is True
    assert cfg.cohort_start == LONG_HORIZON_FUNDING_COHORT_START
    assert cfg.before_minutes == 1_440
    assert cfg.after_minutes == 10_080
    assert cfg.anchor_mode == "closed"
    assert cfg.method_names == ("funding_rate_history",)
    assert cfg.supported_pairs(("binance", "htx")) == (
        ("binance", "funding_rate_history"),
        ("htx", "funding_rate_history"),
    )
    assert LONG_HORIZON_FUNDING_RESOLVER_VERSION == "long_horizon_funding_v1"


def test_open_ended_margin_funding_is_a_separate_28_day_lane() -> None:
    cfg = open_ended_margin_funding_config_from_env()

    assert cfg.enabled is True
    assert cfg.cohort_start == OPEN_ENDED_MARGIN_FUNDING_COHORT_START
    assert cfg.cohort_start == datetime(2026, 8, 3, tzinfo=UTC)
    assert cfg.before_minutes == 1_440
    assert cfg.after_minutes == 40_320
    assert cfg.batch_size == 4
    assert cfg.anchor_mode == "closed"
    assert cfg.method_names == ("funding_rate_history",)
    assert cfg.maximum_window_minutes == 40_320
    assert OPEN_ENDED_MARGIN_FUNDING_RESOLVER_VERSION == "open_ended_margin_funding_v1"


async def test_resolver_reuses_loaded_client_and_persists_samples() -> None:
    target = _target()
    work = (
        DerivativesContextWork(target, "funding_rate_history"),
        DerivativesContextWork(target, "long_short_ratio_history"),
    )
    store = _store(work)
    exchange = MagicMock()
    exchange.has = {
        "fetchFundingRateHistory": True,
        "fetchLongShortRatioHistory": True,
    }
    exchange.markets = {"ERA/USDT:USDT": {"id": "ERAUSDT"}}
    exchange.load_markets = AsyncMock()
    exchange.fetch_funding_rate_history = AsyncMock(
        return_value=[
            {"timestamp": SINCE_MS + 60_000, "fundingRate": 0.01},
            {"timestamp": UNTIL_MS, "fundingRate": 0.02},
        ]
    )
    exchange.fetch_long_short_ratio_history = AsyncMock(
        return_value=[
            {
                "timestamp": timestamp,
                "longShortRatio": 1.2,
            }
            for timestamp in range(SINCE_MS, UNTIL_MS + 1, 5 * 60_000)
        ]
    )
    cfg = DerivativesContextResolverConfig()

    count = await resolve_derivatives_context_once(
        cfg,
        {"binance": exchange},
        store,
    )

    assert count == 2
    exchange.load_markets.assert_awaited_once_with()
    store.load_due_work.assert_awaited_once()
    persisted = store.persist_observations.await_args.args[0]
    assert len(persisted) == 2
    assert {observation.result.status for observation in persisted} == {"sampled"}
    assert sum(len(observation.samples) for observation in persisted) == 145
    assert store.persist_observations.await_args.kwargs["resolver_version"] == (
        DERIVATIVES_CONTEXT_RESOLVER_VERSION
    )


async def test_resolver_persists_load_market_failure_without_fetching() -> None:
    target = _target()
    store = _store((DerivativesContextWork(target, "funding_rate_history"),))
    exchange = MagicMock()
    exchange.load_markets = AsyncMock(side_effect=RuntimeError("venue unavailable"))
    exchange.fetch_funding_rate_history = AsyncMock()

    count = await resolve_derivatives_context_once(
        DerivativesContextResolverConfig(),
        {"binance": exchange},
        store,
    )

    assert count == 1
    exchange.fetch_funding_rate_history.assert_not_awaited()
    persisted = store.persist_observations.await_args.args[0]
    assert persisted[0].result.status == "load_markets_failed"
    assert persisted[0].result.error == "venue unavailable"
    assert persisted[0].samples == ()


async def test_resolver_passes_anchor_mode_and_explicit_version_to_store() -> None:
    store = _store(())
    cfg = DerivativesContextResolverConfig(
        anchor_mode="closed",
        method_names=("funding_rate_history",),
    )

    count = await resolve_derivatives_context_once(
        cfg,
        {"binance": MagicMock()},
        store,
        resolver_version=LONG_HORIZON_FUNDING_RESOLVER_VERSION,
    )

    assert count == 0
    assert store.load_due_work.await_args.kwargs["anchor_mode"] == "closed"
    store.persist_observations.assert_awaited_once()
    assert store.persist_observations.await_args.args == ((),)
    assert store.persist_observations.await_args.kwargs["resolver_version"] == (
        LONG_HORIZON_FUNDING_RESOLVER_VERSION
    )
