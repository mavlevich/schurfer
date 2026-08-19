from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from schurfer_analytics import momentum_universe_identity_matcher as matcher
from schurfer_analytics.momentum_universe_identity_classifier import (
    AssetCluster,
    ClusterMember,
)
from schurfer_analytics.momentum_universe_identity_repository import (
    MomentumUniverseIdentityRepository,
)


def _cluster() -> AssetCluster:
    return AssetCluster(
        cluster_key="BTC:linear_usdt_perpetual",
        base="BTC",
        canonical_market_type="linear_usdt_perpetual",
        members=(
            ClusterMember(
                exchange="bybit",
                native_market_id="BTCUSDT",
                identity_key="bybit:linear_usdt_perpetual:BTCUSDT:0",
                onboarded_at=datetime(2020, 3, 15, tzinfo=UTC),
                match_status="confirmed",
                match_reason="all 2 members established",
            ),
            ClusterMember(
                exchange="binance",
                native_market_id="BTCUSDT",
                identity_key="binance:linear_usdt_perpetual:BTCUSDT:0",
                onboarded_at=datetime(2019, 9, 8, tzinfo=UTC),
                match_status="confirmed",
                match_reason="all 2 members established",
            ),
        ),
    )


async def test_run_wires_repository_reads_into_classifier_and_persists() -> None:
    repository = AsyncMock()
    repository.latest_ready_instruments.side_effect = lambda exchange: ()
    repository.persist_clusters.return_value = 0

    with patch.object(MomentumUniverseIdentityRepository, "from_url", return_value=repository):
        summary = await matcher.run(
            database_url="postgresql://example",
            exchanges=("bybit", "binance"),
            resolved_at=datetime(2026, 8, 17, tzinfo=UTC),
        )

    assert repository.latest_ready_instruments.await_count == 2
    repository.persist_clusters.assert_awaited_once()
    repository.close.assert_awaited_once()
    assert summary["clusters_written"] == 0
    assert summary["exchanges"] == {"bybit": 0, "binance": 0}


async def test_run_counts_match_statuses_and_closes_repository_even_on_persist_failure() -> None:
    repository = AsyncMock()
    repository.latest_ready_instruments.side_effect = lambda exchange: ()
    repository.persist_clusters.side_effect = RuntimeError("boom")

    with (
        patch(
            "schurfer_analytics.momentum_universe_identity_matcher.classify",
            return_value=(_cluster(),),
        ),
        patch.object(MomentumUniverseIdentityRepository, "from_url", return_value=repository),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await matcher.run(
            database_url="postgresql://example",
            exchanges=("bybit", "binance"),
            resolved_at=datetime(2026, 8, 17, tzinfo=UTC),
        )

    # The repository's own async engine must be disposed even when persist_clusters
    # raises -- otherwise a repeated failing run leaks a connection pool each time.
    repository.close.assert_awaited_once()


def test_main_reads_database_url_from_env_and_defaults_to_both_captured_venues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"clusters_written": 0}

    monkeypatch.setattr(matcher, "run", fake_run)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(
        "sys.argv",
        [
            "momentum-universe-identity-match",
            "--code-revision",
            "abc123",
            "--no-working-tree-dirty",
        ],
    )

    matcher.main()

    assert len(calls) == 1
    assert calls[0]["exchanges"] == ("bybit", "binance")
    assert calls[0]["database_url"] == "postgresql://example"
