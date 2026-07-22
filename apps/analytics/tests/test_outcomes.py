import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from schurfer_analytics.ohlcv import Candle
from schurfer_analytics.outcome_models import Decision
from schurfer_analytics.outcome_worker import run_outcome_resolver
from schurfer_analytics.outcomes import (
    OutcomeConfig,
    compute_outcome,
    exchange_candidates,
    price_anchor_exchange,
    resolve_once,
)


def _decision(
    *,
    ts: datetime | None = None,
    exchange: str = "binance",
    price: float | None = 100.0,
    features: dict[str, object] | None = None,
    horizons: tuple[int, ...] = (15,),
) -> Decision:
    return Decision(
        decision_id="00000000-0000-0000-0000-000000000001",
        ts=ts or datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        base="ERA",
        exchange=exchange,
        price=price,
        features=features,
        horizons=horizons,
    )


def _candle(minute: int, *, high: float, low: float, close: float) -> Candle:
    ts = int(
        (datetime(2026, 7, 22, 12, 0, tzinfo=UTC) + timedelta(minutes=minute)).timestamp() * 1000
    )
    return Candle(ts, 100.0, high, low, close, 1_000.0)


def _store(decisions: list[Decision]) -> AsyncMock:
    store = AsyncMock()
    store.load_due_decisions.return_value = decisions
    return store


def test_compute_outcome_for_short() -> None:
    result = compute_outcome(
        _decision(),
        15,
        [
            _candle(0, high=102, low=95, close=97),
            _candle(5, high=105, low=90, close=92),
            _candle(10, high=101, low=91, close=93),
        ],
        anchor="binance",
        source="binance",
    )

    assert result.status == "complete"
    assert result.expected_bars == 3
    assert result.bars_count == 3
    assert result.coverage_ratio == 1
    assert result.forward_price == 93
    assert result.mfe_pct == 10
    assert result.mae_pct == 5
    assert result.short_return_pct == pytest.approx(7)


def test_compute_outcome_excludes_candle_opened_before_decision() -> None:
    decision = _decision(ts=datetime(2026, 7, 22, 12, 2, tzinfo=UTC))
    result = compute_outcome(
        decision,
        15,
        [
            _candle(0, high=200, low=1, close=50),  # contains pre-decision prices
            _candle(5, high=102, low=98, close=99),
            _candle(10, high=103, low=97, close=98),
            _candle(15, high=110, low=90, close=95),  # closes after the horizon
        ],
        anchor="binance",
        source="binance",
    )

    assert result.status == "complete"
    assert result.expected_bars == 2
    assert result.bars_count == 2
    assert result.mfe_pct == 3
    assert result.mae_pct == 3
    assert result.forward_price == 98


def test_compute_outcome_marks_partial_coverage_for_retry() -> None:
    result = compute_outcome(
        _decision(),
        15,
        [_candle(0, high=101, low=99, close=100), _candle(10, high=102, low=98, close=99)],
        anchor="binance",
        source="binance",
    )

    assert result.status == "partial"
    assert result.coverage_ratio == pytest.approx(2 / 3)


def test_compute_outcome_requires_full_coverage_for_extreme_metrics() -> None:
    candles = [
        _candle(minute, high=101, low=99, close=100) for minute in range(0, 100, 5) if minute != 50
    ]

    result = compute_outcome(
        _decision(horizons=(100,)),
        100,
        candles,
        anchor="binance",
        source="binance",
    )

    assert result.coverage_ratio == pytest.approx(0.95)
    assert result.status == "partial"


def test_compute_outcome_marks_cross_exchange_fallback() -> None:
    result = compute_outcome(
        _decision(),
        15,
        [
            _candle(0, high=101, low=99, close=100),
            _candle(5, high=101, low=99, close=100),
            _candle(10, high=101, low=99, close=100),
        ],
        anchor="binance",
        source="bybit",
    )

    assert result.status == "complete_fallback"


def test_exchange_candidates_prefers_price_anchor_then_fallbacks() -> None:
    decision = _decision(
        exchange="",
        features={
            "candidate_exchanges": [
                {"exchange": "bybit", "change_pct": 40},
                {"exchange": "binance", "change_pct": 65},
                {"exchange": "unsupported", "change_pct": 100},
            ]
        },
    )

    assert exchange_candidates(decision, {"binance", "bybit"}) == ["binance", "bybit"]


def test_price_anchor_is_preserved_when_only_fallback_is_supported() -> None:
    decision = _decision(
        exchange="mexc",
        features={
            "candidate_exchanges": [
                {"exchange": "mexc", "change_pct": 65},
                {"exchange": "binance", "change_pct": 40},
            ]
        },
    )

    assert price_anchor_exchange(decision) == "mexc"
    assert exchange_candidates(decision, {"binance"}) == ["binance"]


def test_non_finite_candidate_change_cannot_become_inferred_anchor() -> None:
    decision = _decision(
        exchange="",
        features={
            "candidate_exchanges": [
                {"exchange": "bad", "change_pct": float("nan")},
                {"exchange": "binance", "change_pct": 40},
            ]
        },
    )

    assert price_anchor_exchange(decision) == "binance"


@pytest.mark.parametrize(
    ("override", "env_name"),
    [
        ({"poll_interval_seconds": 0}, "OUTCOME_POLL_INTERVAL"),
        ({"retry_after_seconds": 0}, "OUTCOME_RETRY_AFTER"),
        ({"batch_size": 0}, "OUTCOME_BATCH_SIZE"),
        ({"max_attempts": 0}, "OUTCOME_MAX_ATTEMPTS"),
    ],
)
def test_outcome_config_rejects_non_positive_values(
    override: dict[str, int], env_name: str
) -> None:
    with pytest.raises(ValueError, match=env_name):
        OutcomeConfig("postgresql://test", ("binance",), **override)


async def test_resolve_once_records_unsupported_exchange_without_fetch() -> None:
    cfg = OutcomeConfig("postgresql://test", ("binance",))
    decision = _decision(exchange="unknown", features=None)
    store = _store([decision])

    count = await resolve_once(cfg, {"binance": AsyncMock()}, store)

    assert count == 1
    saved = store.persist_outcomes.await_args.args[0]
    assert saved[0].status == "unsupported_exchange"
    assert saved[0].anchor_exchange == "unknown"
    assert saved[0].error == "no supported candidate exchange"


async def test_resolve_once_records_bounded_error_when_all_fetches_fail() -> None:
    cfg = OutcomeConfig("postgresql://test", ("binance",))
    decision = _decision(
        features={"candidate_exchanges": [{"exchange": "binance", "change_pct": 40}]}
    )
    store = _store([decision])

    with patch(
        "schurfer_analytics.outcomes.fetch_candles",
        AsyncMock(side_effect=RuntimeError("x" * 2000)),
    ):
        count = await resolve_once(cfg, {"binance": AsyncMock()}, store)

    assert count == 1
    saved = store.persist_outcomes.await_args.args[0]
    assert saved[0].status == "fetch_failed"
    assert saved[0].source_exchange is None
    assert saved[0].error is not None
    assert len(saved[0].error) == 1000


async def test_resolve_once_persists_missing_price_without_fetching() -> None:
    cfg = OutcomeConfig("postgresql://test", ("binance",), batch_size=10)
    decision = _decision(price=None, horizons=(15, 60))
    exchange = AsyncMock()
    store = _store([decision])

    count = await resolve_once(cfg, {"binance": exchange}, store)

    assert count == 2
    exchange.fetch_ohlcv.assert_not_called()
    assert store.persist_outcomes.await_args is not None
    saved = store.persist_outcomes.await_args.args[0]
    assert [outcome.status for outcome in saved] == ["missing_price", "missing_price"]


async def test_resolve_once_uses_complete_fallback_when_anchor_is_partial() -> None:
    cfg = OutcomeConfig("postgresql://test", ("binance", "bybit"), batch_size=10)
    decision = _decision(
        features={
            "candidate_exchanges": [
                {"exchange": "binance", "change_pct": 60},
                {"exchange": "bybit", "change_pct": 55},
            ]
        }
    )
    anchor_candles = [_candle(0, high=101, low=99, close=100)]
    fallback_candles = [
        _candle(0, high=101, low=99, close=100),
        _candle(5, high=101, low=98, close=99),
        _candle(10, high=100, low=97, close=98),
    ]
    store = _store([decision])

    with patch(
        "schurfer_analytics.outcomes.fetch_candles",
        AsyncMock(side_effect=[anchor_candles, fallback_candles]),
    ):
        count = await resolve_once(cfg, {"binance": AsyncMock(), "bybit": AsyncMock()}, store)

    assert count == 1
    assert store.persist_outcomes.await_args is not None
    saved = store.persist_outcomes.await_args.args[0]
    assert saved[0].source_exchange == "bybit"
    assert saved[0].status == "complete_fallback"


async def test_resolve_once_labels_unsupported_anchor_as_fallback() -> None:
    cfg = OutcomeConfig("postgresql://test", ("binance",), batch_size=10)
    decision = _decision(
        exchange="mexc",
        features={
            "candidate_exchanges": [
                {"exchange": "mexc", "change_pct": 60},
                {"exchange": "binance", "change_pct": 55},
            ]
        },
    )
    candles = [
        _candle(0, high=101, low=99, close=100),
        _candle(5, high=101, low=98, close=99),
        _candle(10, high=100, low=97, close=98),
    ]
    store = _store([decision])

    with patch(
        "schurfer_analytics.outcomes.fetch_candles",
        AsyncMock(return_value=candles),
    ):
        count = await resolve_once(cfg, {"binance": AsyncMock()}, store)

    assert count == 1
    assert store.persist_outcomes.await_args is not None
    saved = store.persist_outcomes.await_args.args[0]
    assert saved[0].anchor_exchange == "mexc"
    assert saved[0].source_exchange == "binance"
    assert saved[0].status == "complete_fallback"


async def test_resolve_once_selects_exchange_independently_per_horizon() -> None:
    cfg = OutcomeConfig("postgresql://test", ("binance", "bybit"), batch_size=10)
    decision = _decision(
        horizons=(15, 30),
        features={
            "candidate_exchanges": [
                {"exchange": "binance", "change_pct": 60},
                {"exchange": "bybit", "change_pct": 55},
            ]
        },
    )
    anchor_candles = [
        _candle(0, high=101, low=99, close=100),
        _candle(5, high=101, low=98, close=99),
        _candle(10, high=100, low=97, close=98),
    ]
    fallback_candles = [
        _candle(minute, high=101, low=97, close=98) for minute in (0, 5, 10, 15, 20, 25)
    ]
    store = _store([decision])

    with patch(
        "schurfer_analytics.outcomes.fetch_candles",
        AsyncMock(side_effect=[anchor_candles, fallback_candles]),
    ):
        count = await resolve_once(cfg, {"binance": AsyncMock(), "bybit": AsyncMock()}, store)

    assert count == 2
    assert store.persist_outcomes.await_args is not None
    saved = store.persist_outcomes.await_args.args[0]
    assert [
        (outcome.horizon_minutes, outcome.source_exchange, outcome.status) for outcome in saved
    ] == [
        (15, "binance", "complete"),
        (30, "bybit", "complete_fallback"),
    ]


async def test_run_once_propagates_failure_to_cli() -> None:
    cfg = OutcomeConfig("postgresql://test", ("test",))
    exchange = AsyncMock()
    store = _store([])

    with (
        patch.dict(
            "schurfer_analytics.outcome_worker.EXCHANGE_FACTORIES",
            {"test": lambda: exchange},
            clear=True,
        ),
        patch(
            "schurfer_analytics.outcome_worker.resolve_once",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        await run_outcome_resolver(cfg, once=True, store=store)

    exchange.close.assert_awaited_once()


async def test_run_once_success_closes_exchange() -> None:
    cfg = OutcomeConfig("postgresql://test", ("test",))
    exchange = AsyncMock()
    store = _store([])

    with (
        patch.dict(
            "schurfer_analytics.outcome_worker.EXCHANGE_FACTORIES",
            {"test": lambda: exchange},
            clear=True,
        ),
        patch(
            "schurfer_analytics.outcome_worker.resolve_once",
            AsyncMock(return_value=cfg.batch_size),
        ) as resolve,
    ):
        await run_outcome_resolver(cfg, once=True, store=store)

    resolve.assert_awaited_once()
    exchange.close.assert_awaited_once()


async def test_runner_drains_full_batches_before_sleeping() -> None:
    cfg = OutcomeConfig(
        "postgresql://test",
        ("test",),
        poll_interval_seconds=300,
        batch_size=50,
    )
    exchange = AsyncMock()
    store = _store([])

    with (
        patch.dict(
            "schurfer_analytics.outcome_worker.EXCHANGE_FACTORIES",
            {"test": lambda: exchange},
            clear=True,
        ),
        patch(
            "schurfer_analytics.outcome_worker.resolve_once",
            AsyncMock(side_effect=[50, 50, 7]),
        ) as resolve,
        patch(
            "schurfer_analytics.outcome_worker.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_outcome_resolver(cfg, store=store)

    assert resolve.await_count == 3
    sleep.assert_awaited_once_with(300)
    exchange.close.assert_awaited_once()


async def test_runner_sleeps_after_failed_batch_instead_of_spinning() -> None:
    cfg = OutcomeConfig("postgresql://test", ("test",), poll_interval_seconds=300)
    exchange = AsyncMock()
    store = _store([])

    with (
        patch.dict(
            "schurfer_analytics.outcome_worker.EXCHANGE_FACTORIES",
            {"test": lambda: exchange},
            clear=True,
        ),
        patch(
            "schurfer_analytics.outcome_worker.resolve_once",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ) as resolve,
        patch(
            "schurfer_analytics.outcome_worker.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_outcome_resolver(cfg, store=store)

    resolve.assert_awaited_once()
    sleep.assert_awaited_once_with(300)
    exchange.close.assert_awaited_once()


async def test_runner_closes_owned_repository() -> None:
    cfg = OutcomeConfig("postgresql://test", ("test",))
    exchange = AsyncMock()
    repository = AsyncMock()

    with (
        patch.dict(
            "schurfer_analytics.outcome_worker.EXCHANGE_FACTORIES",
            {"test": lambda: exchange},
            clear=True,
        ),
        patch(
            "schurfer_analytics.outcome_worker.OutcomeRepository.from_url",
            return_value=repository,
        ),
        patch(
            "schurfer_analytics.outcome_worker.resolve_once",
            AsyncMock(return_value=0),
        ),
    ):
        await run_outcome_resolver(cfg, once=True)

    repository.close.assert_awaited_once()
    exchange.close.assert_awaited_once()


async def test_runner_closes_exchanges_after_partial_factory_failure() -> None:
    cfg = OutcomeConfig("postgresql://test", ("first", "broken"))
    first = AsyncMock()

    def broken_factory() -> None:
        raise RuntimeError("factory failed")

    with (
        patch.dict(
            "schurfer_analytics.outcome_worker.EXCHANGE_FACTORIES",
            {"first": lambda: first, "broken": broken_factory},
            clear=True,
        ),
        pytest.raises(RuntimeError, match="factory failed"),
    ):
        await run_outcome_resolver(cfg, once=True, store=_store([]))

    first.close.assert_awaited_once()
