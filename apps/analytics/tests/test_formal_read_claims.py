from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from schurfer_analytics import source_lead_forward_cohort_report as report_mod
from schurfer_analytics.formal_read_claims import (
    FormalReadAlreadyClaimedError,
    candidate_ids_sha256,
    claim_formal_read,
)
from schurfer_analytics.source_lead_forward_cohort import SOURCE_LEAD_FORWARD_COHORT_START
from schurfer_analytics.source_lead_forward_cohort_repository import (
    RawQualifiedEpisode,
    SourceLeadForwardCohortRepository,
)

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"


def test_candidate_hash_ignores_order() -> None:
    assert candidate_ids_sha256([3, 1, 2]) == candidate_ids_sha256([1, 2, 3])
    assert candidate_ids_sha256([1, 2]) != candidate_ids_sha256([1, 2, 3])


def _db_or_skip() -> None:
    try:
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            row = conn.execute("SELECT to_regclass('app.formal_read_claims')").fetchone()
            if not row or row[0] is None:
                pytest.skip("migration 0054 is not applied")
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise
        pytest.skip(f"no local postgres: {exc}")


def test_a_cohort_can_be_claimed_exactly_once() -> None:
    _db_or_skip()
    study = f"TEST-{uuid.uuid4().hex[:8]}"
    start = datetime(2026, 9, 29, tzinfo=UTC)
    kwargs: dict[str, Any] = {
        "study_id": study,
        "cohort_start": start,
        "database_now": start + timedelta(days=30),
        "candidate_ids": [3, 1, 2],
        "code_revision": "abc",
        "working_tree_dirty": False,
    }
    try:
        claim_id = asyncio.run(
            claim_formal_read(TEST_DATABASE_URL, contract_version="v2", **kwargs)
        )
        assert claim_id > 0
        with pytest.raises(FormalReadAlreadyClaimedError):
            asyncio.run(claim_formal_read(TEST_DATABASE_URL, contract_version="v2", **kwargs))
        # Another contract version is another cohort.
        asyncio.run(claim_formal_read(TEST_DATABASE_URL, contract_version="v3", **kwargs))
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            row = conn.execute(
                "SELECT candidate_count, candidate_ids_sha256 FROM app.formal_read_claims "
                "WHERE study_id = %s AND contract_version = 'v2'",
                (study,),
            ).fetchone()
        assert row == (3, candidate_ids_sha256([1, 2, 3]))
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute("DELETE FROM app.formal_read_claims WHERE study_id = %s", (study,))


# --- reader ordering -------------------------------------------------------------


def _episode(i: int, asset: str, when: datetime) -> RawQualifiedEpisode:
    return RawQualifiedEpisode(
        capture_id=i,
        base=asset,
        canonical_asset_id=f"asset:{asset}",
        target_exchange="bybit",
        observed_at=when,
        requested_notional_usd=50.0,
        liquidity={"ask_vwap": 1.0},
        instrument={"identity_key": f"bybit:swap:{asset}USDT:1"},
    )


class _Repo:
    def __init__(self, episodes: list[RawQualifiedEpisode], now: datetime) -> None:
        self.episodes, self.now = episodes, now

    async def database_now(self) -> datetime:
        return self.now

    async def fetch_qualified_episodes(self, **_: Any) -> list[RawQualifiedEpisode]:
        return self.episodes


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        since=SOURCE_LEAD_FORWARD_COHORT_START,
        code_revision="a" * 40,
        working_tree_dirty=False,
        max_qualified_episodes=10_000,
        max_concurrent_exchange_fetches=1,
        exchange_fetch_wall_seconds=1.0,
    )


def _patch(monkeypatch: pytest.MonkeyPatch, repo: _Repo, events: list[str]) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(
        SourceLeadForwardCohortRepository, "from_url", staticmethod(lambda _u: repo)
    )

    async def claim(*_a: Any, **_k: Any) -> int:
        events.append("claim")
        return 1

    async def fetch(*_a: Any, **_k: Any) -> list[None]:
        events.append("fetch_exit_bars")
        raise RuntimeError("stop after ordering is observed")

    monkeypatch.setattr(report_mod, "claim_formal_read", claim)
    monkeypatch.setattr(report_mod, "_fetch_exit_bars_bounded", fetch)
    monkeypatch.setattr(report_mod, "EXCHANGE_FACTORIES", {})


def test_the_reader_refuses_before_the_floors_without_claiming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = SOURCE_LEAD_FORWARD_COHORT_START
    repo = _Repo([_episode(1, "ABC", start + timedelta(days=1))], start + timedelta(days=3))
    events: list[str] = []
    _patch(monkeypatch, repo, events)
    with pytest.raises(ValueError, match="before claiming"):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == []


def test_the_reader_claims_before_fetching_any_exit_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    start = SOURCE_LEAD_FORWARD_COHORT_START
    episodes = [
        _episode(i, f"A{i % 8}", start + timedelta(days=1 + (i % 28), hours=i % 24))
        for i in range(120)
    ]
    repo = _Repo(episodes, start + timedelta(days=40))
    events: list[str] = []
    _patch(monkeypatch, repo, events)
    with pytest.raises(RuntimeError, match="ordering"):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == ["claim", "fetch_exit_bars"]
