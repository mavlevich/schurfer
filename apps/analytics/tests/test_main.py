from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_analytics.main import _run
from schurfer_analytics.scanner import ScanBatch


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        exchanges=["binance"],
        measurement_min_pct=20.0,
        entry_min_pct=30.0,
        interval=60,
        db_url="postgresql://test",
        redis_addr="redis:6379",
        close_after_misses=3,
        source_lead_capture_enabled=True,
        source_lead_targets=("binance", "bybit"),
        source_lead_notional_usd=50.0,
        source_lead_timeout_seconds=5.0,
        source_lead_batch_size=8,
        source_lead_queue_size=16,
        source_lead_shutdown_timeout_seconds=10.0,
    )


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        start=MagicMock(),
        submit=AsyncMock(),
        close=AsyncMock(),
    )


async def test_run_persists_and_attributes_before_publish() -> None:
    events: list[str] = []
    rdb = AsyncMock()
    batch = ScanBatch(
        pumps=[{"base": "BTC", "max_change_pct": 50.0, "exchanges": []}],
        errors={"okx": "timeout"},
        below_updates={},
        tracked_pumps=[],
        scanned=("binance",),
    )

    async def persist(
        _db_url: str,
        _pumps: list[dict[str, object]],
        entry_min_pct: float,
    ) -> dict[str, int]:
        events.append("persist")
        assert entry_min_pct == 30
        return {"BTC": 42}

    async def publish(
        published: ScanBatch,
        measurement_min_pct: float,
        entry_min_pct: float,
        _rdb: object,
    ) -> None:
        events.append("publish")
        assert published.pumps[0]["pump_event_id"] == 42
        assert measurement_min_pct == 20
        assert entry_min_pct == 30

    claims = tuple(object() for _ in range(9))

    async def prepare(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        events.append("claim")
        return claims

    worker = _worker()

    async def submit(_claimed: tuple[object, ...]) -> None:
        events.append("enqueue")

    with (
        patch("schurfer_analytics.main.Config", return_value=_config()),
        patch("schurfer_analytics.main.aioredis.from_url", return_value=rdb),
        patch("schurfer_analytics.main.get_tracked_bases", AsyncMock(return_value=frozenset())),
        patch(
            "schurfer_analytics.main.run_once",
            AsyncMock(return_value=batch),
        ) as run_once,
        patch("schurfer_analytics.main.upsert_pumps", side_effect=persist),
        patch("schurfer_analytics.main.publish", side_effect=publish),
        patch("schurfer_analytics.main.SourceLeadCaptureWorker", return_value=worker),
        patch("schurfer_analytics.main.prepare_source_lead_captures", side_effect=prepare) as claim,
        patch("schurfer_analytics.main.fetch_oi_for_pumps", AsyncMock(return_value=[])),
        patch("schurfer_analytics.main.fetch_funding_rates_for_pumps", AsyncMock(return_value=[])),
        patch("schurfer_analytics.main.get_open_episode_ids", AsyncMock(return_value={})),
        patch("schurfer_analytics.main.insert_oi_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.insert_funding_rate_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.take_due_snapshots", AsyncMock()),
    ):
        worker.submit.side_effect = submit
        await _run(once=True)

    assert events == ["persist", "publish", "claim", "enqueue", "enqueue"]
    worker.start.assert_called_once_with()
    assert worker.submit.await_count == 2
    assert worker.submit.await_args_list[0].args == (claims[:8],)
    assert worker.submit.await_args_list[1].args == (claims[8:],)
    worker.close.assert_awaited_once_with()
    claim.assert_awaited_once()
    run_once.assert_awaited_once_with(["binance"], 20.0, frozenset())
    rdb.aclose.assert_awaited_once()


async def test_run_preserves_last_snapshot_when_episode_attribution_is_incomplete() -> None:
    rdb = AsyncMock()
    batch = ScanBatch(
        pumps=[{"base": "BTC", "max_change_pct": 50.0, "exchanges": []}],
        errors={"okx": "timeout"},
        below_updates={},
        tracked_pumps=[],
        scanned=("binance",),
    )
    publish = AsyncMock()
    worker = _worker()

    with (
        patch("schurfer_analytics.main.Config", return_value=_config()),
        patch("schurfer_analytics.main.aioredis.from_url", return_value=rdb),
        patch("schurfer_analytics.main.get_tracked_bases", AsyncMock(return_value=frozenset())),
        patch("schurfer_analytics.main.run_once", AsyncMock(return_value=batch)),
        # A same-sized but wrong mapping must also fail closed, not raise a KeyError
        # or publish a pump attributed to another base.
        patch("schurfer_analytics.main.upsert_pumps", AsyncMock(return_value={"ETH": 99})),
        patch("schurfer_analytics.main.publish", publish),
        patch("schurfer_analytics.main.SourceLeadCaptureWorker", return_value=worker),
        patch("schurfer_analytics.main.prepare_source_lead_captures", AsyncMock()) as claim,
        patch("schurfer_analytics.main.fetch_oi_for_pumps", AsyncMock(return_value=[])),
        patch("schurfer_analytics.main.fetch_funding_rates_for_pumps", AsyncMock(return_value=[])),
        patch("schurfer_analytics.main.get_open_episode_ids", AsyncMock(return_value={})),
        patch("schurfer_analytics.main.insert_oi_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.insert_funding_rate_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.take_due_snapshots", AsyncMock()),
    ):
        await _run(once=True)

    publish.assert_not_awaited()
    claim.assert_not_awaited()
    worker.submit.assert_not_awaited()
    worker.close.assert_awaited_once_with()
    rdb.aclose.assert_awaited_once()


async def test_run_does_not_capture_when_scan_has_no_events() -> None:
    rdb = AsyncMock()
    batch = ScanBatch(
        pumps=[],
        errors={},
        below_updates={},
        tracked_pumps=[],
        scanned=("binance",),
    )
    worker = _worker()

    with (
        patch("schurfer_analytics.main.Config", return_value=_config()),
        patch("schurfer_analytics.main.aioredis.from_url", return_value=rdb),
        patch("schurfer_analytics.main.get_tracked_bases", AsyncMock(return_value=frozenset())),
        patch("schurfer_analytics.main.run_once", AsyncMock(return_value=batch)),
        patch("schurfer_analytics.main.publish", AsyncMock()),
        patch("schurfer_analytics.main.SourceLeadCaptureWorker", return_value=worker),
        patch("schurfer_analytics.main.prepare_source_lead_captures", AsyncMock()) as claim,
        patch("schurfer_analytics.main.take_due_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.close_retrace", AsyncMock()),
    ):
        await _run(once=True)

    claim.assert_not_awaited()
    worker.submit.assert_not_awaited()
    worker.close.assert_awaited_once_with()
    rdb.aclose.assert_awaited_once()
