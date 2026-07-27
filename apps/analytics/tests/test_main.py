from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        patch("schurfer_analytics.main.fetch_oi_for_pumps", AsyncMock(return_value=[])),
        patch("schurfer_analytics.main.fetch_funding_rates_for_pumps", AsyncMock(return_value=[])),
        patch("schurfer_analytics.main.get_open_episode_ids", AsyncMock(return_value={})),
        patch("schurfer_analytics.main.insert_oi_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.insert_funding_rate_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.take_due_snapshots", AsyncMock()),
    ):
        await _run(once=True)

    assert events == ["persist", "publish"]
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

    with (
        patch("schurfer_analytics.main.Config", return_value=_config()),
        patch("schurfer_analytics.main.aioredis.from_url", return_value=rdb),
        patch("schurfer_analytics.main.get_tracked_bases", AsyncMock(return_value=frozenset())),
        patch("schurfer_analytics.main.run_once", AsyncMock(return_value=batch)),
        # A same-sized but wrong mapping must also fail closed, not raise a KeyError
        # or publish a pump attributed to another base.
        patch("schurfer_analytics.main.upsert_pumps", AsyncMock(return_value={"ETH": 99})),
        patch("schurfer_analytics.main.publish", publish),
        patch("schurfer_analytics.main.fetch_oi_for_pumps", AsyncMock(return_value=[])),
        patch("schurfer_analytics.main.fetch_funding_rates_for_pumps", AsyncMock(return_value=[])),
        patch("schurfer_analytics.main.get_open_episode_ids", AsyncMock(return_value={})),
        patch("schurfer_analytics.main.insert_oi_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.insert_funding_rate_snapshots", AsyncMock()),
        patch("schurfer_analytics.main.take_due_snapshots", AsyncMock()),
    ):
        await _run(once=True)

    publish.assert_not_awaited()
    rdb.aclose.assert_awaited_once()
