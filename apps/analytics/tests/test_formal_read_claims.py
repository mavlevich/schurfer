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
    FormalReadLeaseHeldError,
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


def _claim_kwargs(study: str) -> dict[str, Any]:
    start = datetime(2026, 9, 29, tzinfo=UTC)
    return {
        "study_id": study,
        "contract_version": "v2",
        "cohort_start": start,
        "database_now": start + timedelta(days=30),
        "code_revision": "abc",
        "working_tree_dirty": False,
    }


def test_only_one_run_owns_a_claim_and_resume_waits_for_the_lease() -> None:
    _db_or_skip()
    study = f"TEST-{uuid.uuid4().hex[:8]}"
    kwargs = _claim_kwargs(study)

    async def two_at_once() -> tuple[Any, ...]:
        return await asyncio.gather(
            open_claim(TEST_DATABASE_URL, candidate_ids=[3, 1, 2], **kwargs),
            open_claim(TEST_DATABASE_URL, candidate_ids=[3, 1, 2], **kwargs),
            return_exceptions=True,
        )

    try:
        results = asyncio.run(two_at_once())
        owners = [r for r in results if isinstance(r, FormalReadClaim)]
        refused = [r for r in results if isinstance(r, FormalReadLeaseHeldError)]
        assert len(owners) == 1 and len(refused) == 1  # review repro: both used to proceed
        first = owners[0]
        assert first.candidate_ids == (3, 1, 2) and not first.resumed

        # The lease expires (the first run died): a new run takes over the SAME ids.
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(
                "UPDATE app.formal_read_claims SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = %s",
                (first.id,),
            )
        second = asyncio.run(open_claim(TEST_DATABASE_URL, candidate_ids=[9], **kwargs))
        assert second.id == first.id and second.candidate_ids == (3, 1, 2) and second.resumed
        assert second.owner != first.owner

        # The previous owner can no longer complete; the current owner can, once.
        with pytest.raises(FormalReadAlreadyClaimedError):
            asyncio.run(complete_claim(TEST_DATABASE_URL, first.id, "f" * 64, owner=first.owner))
        asyncio.run(complete_claim(TEST_DATABASE_URL, first.id, "f" * 64, owner=second.owner))
        with pytest.raises(FormalReadAlreadyClaimedError):
            asyncio.run(open_claim(TEST_DATABASE_URL, candidate_ids=[3, 1, 2], **kwargs))
        existing = asyncio.run(
            existing_claim(
                TEST_DATABASE_URL,
                study_id=study,
                contract_version="v2",
                cohort_start=kwargs["cohort_start"],
            )
        )
        assert existing is not None and existing.status == "completed"
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute("DELETE FROM app.formal_read_claims WHERE study_id = %s", (study,))


def test_the_blind_status_never_computes_a_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review repro: the blind search reached calculate_performance."""
    from schurfer_analytics import source_lead_forward_cohort as contract
    from schurfer_analytics.ohlcv import Candle

    entry = SOURCE_LEAD_FORWARD_COHORT_START + timedelta(days=1)
    boundary = contract.expected_exit_boundary_ms(entry)
    episode = _episode(1, "ABC", entry)
    bars: list[Candle | None] = [
        Candle(ts_ms=boundary, open=1.0, high=1.0, low=1.0, close=1.1, volume=1.0),
        None,
        Candle(ts_ms=boundary - 60_000, open=1.0, high=1.0, low=1.0, close=1.1, volume=1.0),
        Candle(ts_ms=boundary + 3 * 60_000, open=1.0, high=1.0, low=1.0, close=1.1, volume=1.0),
        Candle(ts_ms=boundary, open=1.0, high=1.0, low=1.0, close=float("nan"), volume=1.0),
    ]
    # The full resolution (which does compute a return) defines the expected flags.
    expected = [
        report_mod._resolve_one(episode, bar, exit_slippage_bps=15.0).resolved for bar in bars
    ]

    def forbidden(**_: Any) -> Any:
        raise AssertionError("the blind search computed a return")

    monkeypatch.setattr(contract, "calculate_performance", forbidden)
    assert [report_mod.episode_resolved_blind(episode, bar) for bar in bars] == expected
    assert expected == [True, False, False, False, False]


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
            id=7, candidate_ids=tuple(candidate_ids), status="claimed", resumed=False, owner="me"
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
