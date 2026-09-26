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
    FormalReadClaim,
    candidate_ids_sha256,
    complete_claim,
    existing_claim,
    open_claim,
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


def test_open_resume_complete_then_refuse() -> None:
    _db_or_skip()
    study = f"TEST-{uuid.uuid4().hex[:8]}"
    start = datetime(2026, 9, 29, tzinfo=UTC)
    kwargs: dict[str, Any] = {
        "study_id": study,
        "contract_version": "v2",
        "cohort_start": start,
        "database_now": start + timedelta(days=30),
        "code_revision": "abc",
        "working_tree_dirty": False,
    }
    try:
        first = asyncio.run(open_claim(TEST_DATABASE_URL, candidate_ids=[3, 1, 2], **kwargs))
        assert first.candidate_ids == (3, 1, 2) and not first.resumed
        # A failed run resumes the SAME claim, even if it would now pick other ids.
        again = asyncio.run(open_claim(TEST_DATABASE_URL, candidate_ids=[9], **kwargs))
        assert again.id == first.id and again.candidate_ids == (3, 1, 2) and again.resumed
        asyncio.run(complete_claim(TEST_DATABASE_URL, first.id, "f" * 64))
        with pytest.raises(FormalReadAlreadyClaimedError):
            asyncio.run(open_claim(TEST_DATABASE_URL, candidate_ids=[3, 1, 2], **kwargs))
        with pytest.raises(FormalReadAlreadyClaimedError):
            asyncio.run(complete_claim(TEST_DATABASE_URL, first.id, "f" * 64))
        existing = asyncio.run(
            existing_claim(
                TEST_DATABASE_URL, study_id=study, contract_version="v2", cohort_start=start
            )
        )
        assert existing is not None and existing.status == "completed"
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute("DELETE FROM app.formal_read_claims WHERE study_id = %s", (study,))


# --- reader protocol -------------------------------------------------------------


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


START = SOURCE_LEAD_FORWARD_COHORT_START
MANY = [
    _episode(i, f"A{i % 8}", START + timedelta(days=1 + (i % 28), hours=i % 24)) for i in range(120)
]


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    episodes: list[RawQualifiedEpisode],
    events: list[str],
    *,
    prior: FormalReadClaim | None = None,
    prefix: int | None = 100,
) -> None:
    repo = _Repo(episodes, START + timedelta(days=40))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(
        SourceLeadForwardCohortRepository, "from_url", staticmethod(lambda _u: repo)
    )
    monkeypatch.setattr(report_mod, "EXCHANGE_FACTORIES", {})

    async def existing(*_a: Any, **_k: Any) -> FormalReadClaim | None:
        return prior

    async def fetch(_clients: Any, candidates: list[Any], **_k: Any) -> list[None]:
        events.append(f"fetch:{len(candidates)}")
        return [None] * len(candidates)

    def blind(candidates: Any, _bars: Any) -> int | None:
        events.append("blind_checkpoint")
        return prefix

    async def open_(*_a: Any, candidate_ids: list[int], **_k: Any) -> FormalReadClaim:
        events.append(f"claim:{len(candidate_ids)}")
        if prior is not None:
            return prior
        return FormalReadClaim(
            id=7, candidate_ids=tuple(candidate_ids), status="claimed", resumed=False
        )

    def aggregate(**_k: Any) -> Any:
        events.append("aggregate")
        raise RuntimeError("stop after the protocol order is observed")

    monkeypatch.setattr(report_mod, "existing_claim", existing)
    monkeypatch.setattr(report_mod, "_fetch_exit_bars_bounded", fetch)
    monkeypatch.setattr(report_mod, "checkpoint_prefix_length_blind", blind)
    monkeypatch.setattr(report_mod, "open_claim", open_)
    monkeypatch.setattr(report_mod, "aggregate_cohort", aggregate)


def test_too_few_matured_episodes_refuses_before_any_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    _patch(monkeypatch, MANY[:50], events)
    with pytest.raises(ValueError, match="before fetching"):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == []


def test_an_unreached_checkpoint_refuses_without_claiming(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review: matured is not resolved; an unresolved prefix must not burn the read."""
    events: list[str] = []
    _patch(monkeypatch, MANY, events, prefix=None)
    with pytest.raises(ValueError, match="nothing was claimed"):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == ["fetch:120", "blind_checkpoint"]


def test_clusters_do_not_delay_the_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review: 100 resolved over 4 weeks with only 6 assets is read (insufficient_data)."""
    six_assets = [
        _episode(i, f"A{i % 6}", START + timedelta(days=1 + (i % 28))) for i in range(120)
    ]
    events: list[str] = []
    _patch(monkeypatch, six_assets, events)
    with pytest.raises(RuntimeError, match="protocol order"):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == ["fetch:120", "blind_checkpoint", "claim:100", "aggregate"]


def test_a_failed_run_resumes_the_stored_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = FormalReadClaim(
        id=7, candidate_ids=tuple(range(5, 105)), status="claimed", resumed=True
    )
    events: list[str] = []
    _patch(monkeypatch, MANY, events, prior=stored)
    with pytest.raises(RuntimeError, match="protocol order"):
        asyncio.run(report_mod.generate_report(_args()))
    # Exactly the stored ids are fetched; the checkpoint is not searched again.
    assert events == ["fetch:100", "claim:100", "aggregate"]


def test_a_completed_read_refuses_before_any_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    done = FormalReadClaim(id=7, candidate_ids=(1,), status="completed", resumed=True)
    events: list[str] = []
    _patch(monkeypatch, MANY, events, prior=done)
    with pytest.raises(FormalReadAlreadyClaimedError):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == []
