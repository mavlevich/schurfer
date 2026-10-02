from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from schurfer_analytics import source_lead_forward_cohort_report as report_mod
from schurfer_analytics import source_lead_v2_administrative_stop as administrative_stop
from schurfer_analytics.formal_read_claims import (
    AdministrativeStop,
    FormalReadAdministrativelyStoppedError,
    FormalReadAlreadyClaimedError,
    FormalReadClaim,
    FormalReadLeaseHeldError,
    candidate_ids_sha256,
    complete_claim,
    existing_claim,
    open_claim,
)
from schurfer_analytics.source_lead_forward_cohort import (
    COHORT_CAPTURE_DEADLINE,
    SOURCE_LEAD_FORWARD_COHORT_START,
)
from schurfer_analytics.source_lead_forward_cohort_repository import (
    AccrualSnapshotRow,
    OpenTransactions,
    RawQualifiedEpisode,
    SourceLeadForwardCohortRepository,
)
from schurfer_journal.testing_database import integration_database_url

TEST_DATABASE_URL = integration_database_url()


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
    def __init__(
        self,
        episodes: list[RawQualifiedEpisode],
        now: Any,
        snapshot: list[RawQualifiedEpisode] | None = None,
    ) -> None:
        self.episodes, self.now = episodes, now
        # What the administrative snapshot sees; by default the same episodes.
        self.snapshot = episodes if snapshot is None else snapshot
        self.open = OpenTransactions(0, 0)
        # Qualification stamps (created_at); unlisted episodes are stamped at entry.
        self.qualified_at: dict[int, datetime] = {}
        self.fetch_kwargs: list[dict[str, Any]] = []

    def _stamp(self, e: RawQualifiedEpisode) -> datetime:
        return self.qualified_at.get(e.capture_id, e.observed_at)

    async def database_now(self) -> datetime:
        # A list of instants plays successive clock readings (the last one repeats).
        now: datetime = (
            (self.now.pop(0) if len(self.now) > 1 else self.now[0])
            if isinstance(self.now, list)
            else self.now
        )
        return now

    async def open_transactions_started_before(self, _as_of: datetime) -> OpenTransactions:
        return self.open

    async def fetch_accrual_snapshot(self, *, as_of: datetime, **_: Any) -> Any:
        return [
            AccrualSnapshotRow(
                capture_id=e.capture_id,
                source_first_observed_at=e.observed_at - timedelta(seconds=30),
                canonical_asset_id=e.canonical_asset_id,
                target_exchange=e.target_exchange,
                observed_at=e.observed_at,
            )
            for e in self.snapshot
            if e.observed_at - timedelta(seconds=30) < as_of and self._stamp(e) < as_of
        ]

    async def fetch_qualified_episodes(self, **kwargs: Any) -> list[RawQualifiedEpisode]:
        self.fetch_kwargs.append(kwargs)
        before = kwargs.get("qualified_before")
        return [e for e in self.episodes if before is None or self._stamp(e) < before]


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        since=SOURCE_LEAD_FORWARD_COHORT_START,
        code_revision="a" * 40,
        working_tree_dirty=False,
        max_qualified_episodes=10_000,
        max_concurrent_exchange_fetches=1,
        exchange_fetch_wall_seconds=1.0,
        administrative_stop_dir=Path(tempfile.mkdtemp(prefix="v2-stop-")),
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
    now: Any = None,
    snapshot: list[RawQualifiedEpisode] | None = None,
) -> _Repo:
    repo = _Repo(episodes, now or START + timedelta(days=40), snapshot)
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
    return repo


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


# --- administrative stop (source-lead-forward-cohort-v2-administrative-stop.md) ---------


def test_a_stopped_cohort_refuses_before_loading_any_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = FormalReadClaim(id=7, candidate_ids=(1,), status="admin_stopped", resumed=True)
    events: list[str] = []
    _patch(monkeypatch, MANY, events, prior=stopped)

    def no_repository(_url: str) -> Any:
        raise AssertionError("a stopped cohort loaded its episodes")

    monkeypatch.setattr(SourceLeadForwardCohortRepository, "from_url", staticmethod(no_repository))
    with pytest.raises(FormalReadAdministrativelyStoppedError):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == []


def test_captures_after_the_deadline_never_enter_the_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repo = _patch(monkeypatch, MANY[:50], events)
    with pytest.raises(ValueError, match="before fetching"):
        asyncio.run(report_mod.generate_report(_args()))
    assert repo.fetch_kwargs[0]["until"] == COHORT_CAPTURE_DEADLINE


def _deadline_args(tmp_path: Path) -> argparse.Namespace:
    args = _args()
    args.administrative_stop_dir = tmp_path
    return args


def _record_stops(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    async def record(_db: str, **kwargs: Any) -> Any:
        recorded.append(kwargs)
        return AdministrativeStop(
            1,
            tuple(kwargs["closed_ids"]),
            kwargs["terminal_reason"],
            kwargs["result_fingerprint"],
            newly_recorded=True,
        )

    monkeypatch.setattr(report_mod, "record_administrative_stop", record)
    monkeypatch.setattr(administrative_stop, "record_administrative_stop", record)
    return recorded


SETTLED = COHORT_CAPTURE_DEADLINE + timedelta(hours=24)


def test_a_low_count_after_the_deadline_is_closed_by_its_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """50 episodes pass 2026-10-31 to 2026-12-31 but not 2027-01-31 (64): the due
    checkpoint closes the cohort before the reader loads anything."""
    events: list[str] = []
    repo = _patch(monkeypatch, MANY[:50], events, now=SETTLED)
    recorded = _record_stops(monkeypatch)
    with pytest.raises(FormalReadAdministrativelyStoppedError):
        asyncio.run(report_mod.generate_report(_deadline_args(tmp_path)))
    assert events == [] and repo.fetch_kwargs == []
    (record,) = recorded
    assert record["terminal_reason"] == "accrual_below_rule:2027-01-31"
    assert len(record["closed_ids"]) == 50


def test_an_unreached_checkpoint_after_the_deadline_records_the_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _patch(monkeypatch, MANY, events, prefix=None, now=SETTLED)
    recorded = _record_stops(monkeypatch)
    with pytest.raises(FormalReadAdministrativelyStoppedError):
        asyncio.run(report_mod.generate_report(_deadline_args(tmp_path)))
    assert events == ["fetch:120", "blind_checkpoint"]  # resolution status only, no claim
    (record,) = recorded
    assert record["terminal_reason"] == "checkpoint_unreached_at_deadline"
    assert record["closed_ids"] == [e.capture_id for e in MANY]
    stored = json.loads((tmp_path / "deadline.json").read_text())
    assert len(stored["closed_windows"]) == 120


def test_before_settlement_the_deadline_only_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _patch(monkeypatch, MANY, events, prefix=None, now=SETTLED - timedelta(minutes=1))
    recorded = _record_stops(monkeypatch)
    with pytest.raises(ValueError, match="nothing was claimed"):
        asyncio.run(report_mod.generate_report(_deadline_args(tmp_path)))
    assert recorded == [] and not (tmp_path / "deadline.json").exists()


def test_a_missed_checkpoint_stops_the_reader_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review repro: zero episodes at 2026-10-31, then 100 resolved over four weeks.
    Running the reader first must still apply the 2026-10-31 checkpoint."""
    later = [_episode(i, f"A{i % 8}", START + timedelta(days=33 + (i % 28))) for i in range(120)]
    events: list[str] = []
    repo = _patch(monkeypatch, later, events, now=START + timedelta(days=70))
    recorded = _record_stops(monkeypatch)
    with pytest.raises(FormalReadAdministrativelyStoppedError):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == [] and repo.fetch_kwargs == []  # no episode, quote, bar or claim
    (record,) = recorded
    assert record["terminal_reason"] == "accrual_below_rule:2026-10-31"
    assert record["closed_ids"] == []


def test_an_unsettled_checkpoint_refuses_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    repo = _patch(monkeypatch, MANY, events)
    repo.open = OpenTransactions(1, 0)
    recorded = _record_stops(monkeypatch)
    with pytest.raises(ValueError, match="cannot be evaluated yet"):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == [] and repo.fetch_kwargs == [] and recorded == []


def test_a_resumed_claim_is_not_re_evaluated(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = FormalReadClaim(
        id=7, candidate_ids=tuple(range(5, 105)), status="claimed", resumed=True
    )
    events: list[str] = []
    repo = _patch(monkeypatch, MANY, events, prior=stored, snapshot=[])
    repo.open = OpenTransactions(1, 1)
    with pytest.raises(RuntimeError, match="protocol order"):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == ["fetch:100", "claim:100", "aggregate"]


# 90 episodes matured by the deadline that pass every earlier checkpoint, plus 10 that
# enter 30m30s before it: their exit bar closes one minute after the deadline.
_AT_DEADLINE = [
    _episode(i, f"A{i % 8}", START + timedelta(days=1 + 1.8 * i)) for i in range(90)
] + [
    _episode(90 + i, f"B{i}", COHORT_CAPTURE_DEADLINE - timedelta(minutes=30, seconds=30))
    for i in range(10)
]


def test_the_deadline_cannot_fall_between_the_checkpoints_and_maturity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review repro: checkpoints judged just before the deadline, maturity counted on a
    second clock reading just after it, opened a claim with 100 'matured' episodes
    although only 90 had matured at the deadline."""
    events: list[str] = []
    clock = [
        COHORT_CAPTURE_DEADLINE - timedelta(seconds=1),
        COHORT_CAPTURE_DEADLINE + timedelta(minutes=1),
    ]
    _patch(monkeypatch, _AT_DEADLINE, events, now=clock)
    recorded = _record_stops(monkeypatch)
    with pytest.raises(ValueError, match="before fetching"):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == [] and recorded == []  # no exit bar, no claim


def test_after_the_deadline_ninety_matured_episodes_stop_the_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repo = _patch(
        monkeypatch, _AT_DEADLINE, events, now=COHORT_CAPTURE_DEADLINE + timedelta(minutes=1)
    )
    recorded = _record_stops(monkeypatch)
    with pytest.raises(FormalReadAdministrativelyStoppedError):
        asyncio.run(report_mod.generate_report(_args()))
    assert events == [] and repo.fetch_kwargs == []
    (record,) = recorded
    assert record["terminal_reason"] == "accrual_below_rule:2027-03-31"
    assert len(record["closed_ids"]) == 100


def test_a_late_qualification_cannot_join_the_first_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review repro: the clock is read one second before the deadline; ten older,
    matured captures are qualified after it. They must not lift the first read to 100
    and open a claim that the deadline snapshot (still 90) would have to stop."""
    from schurfer_analytics import source_lead_forward_cohort as contract
    from schurfer_analytics.ohlcv import Candle

    early = MANY[:90]
    late = [
        _episode(90 + i, f"B{i}", COHORT_CAPTURE_DEADLINE - timedelta(hours=1)) for i in range(10)
    ]
    events: list[str] = []
    clock = COHORT_CAPTURE_DEADLINE - timedelta(seconds=1)
    repo = _patch(monkeypatch, early + late, events, now=clock)
    repo.qualified_at = {e.capture_id: COHORT_CAPTURE_DEADLINE + timedelta(seconds=1) for e in late}

    async def bars(_clients: Any, candidates: list[Any], **_k: Any) -> list[Candle]:
        events.append(f"fetch:{len(candidates)}")
        return [
            Candle(
                ts_ms=contract.expected_exit_boundary_ms(e.observed_at),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )
            for e in candidates
        ]

    monkeypatch.setattr(report_mod, "_fetch_exit_bars_bounded", bars)
    monkeypatch.setattr(
        report_mod, "checkpoint_prefix_length_blind", report_mod.checkpoint_prefix_length_blind
    )
    recorded = _record_stops(monkeypatch)
    with pytest.raises(ValueError, match="before fetching"):
        asyncio.run(report_mod.generate_report(_args()))
    assert repo.fetch_kwargs[0]["qualified_before"] == clock
    assert events == [] and recorded == []  # 90 members: no bar, no claim


def test_the_late_qualifications_are_stopped_by_the_deadline_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same data read after the deadline: its snapshot at the deadline holds 90."""
    late = [
        _episode(90 + i, f"B{i}", COHORT_CAPTURE_DEADLINE - timedelta(hours=1)) for i in range(10)
    ]
    events: list[str] = []
    repo = _patch(
        monkeypatch, MANY[:90] + late, events, now=COHORT_CAPTURE_DEADLINE + timedelta(minutes=5)
    )
    repo.qualified_at = {e.capture_id: COHORT_CAPTURE_DEADLINE + timedelta(seconds=1) for e in late}
    recorded = _record_stops(monkeypatch)
    with pytest.raises(FormalReadAdministrativelyStoppedError):
        asyncio.run(report_mod.generate_report(_args()))
    (record,) = recorded
    assert record["terminal_reason"] == "accrual_below_rule:2027-03-31"
    assert len(record["closed_ids"]) == 90 and repo.fetch_kwargs == []


def test_a_resumed_claim_keeps_its_prefix_without_a_membership_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = FormalReadClaim(
        id=7, candidate_ids=tuple(range(5, 105)), status="claimed", resumed=True
    )
    events: list[str] = []
    repo = _patch(monkeypatch, MANY, events, prior=stored)
    repo.qualified_at = {i: START + timedelta(days=60) for i in range(120)}
    with pytest.raises(RuntimeError, match="protocol order"):
        asyncio.run(report_mod.generate_report(_args()))
    assert repo.fetch_kwargs[0]["qualified_before"] is None
    assert events == ["fetch:100", "claim:100", "aggregate"]
