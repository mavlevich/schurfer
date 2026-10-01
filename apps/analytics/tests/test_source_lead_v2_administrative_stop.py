from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import source_lead_forward_cohort_repository as repository_mod
from schurfer_analytics import source_lead_v2_administrative_stop as stop
from schurfer_analytics.formal_read_claims import AdministrativeStop, FormalReadClaim
from schurfer_analytics.source_lead_forward_cohort import (
    COHORT_CAPTURE_DEADLINE,
    SOURCE_LEAD_FORWARD_COHORT_START,
)
from schurfer_analytics.source_lead_forward_cohort_repository import (
    AccrualSnapshotRow,
    OpenTransactions,
)

if TYPE_CHECKING:
    from pathlib import Path

START = SOURCE_LEAD_FORWARD_COHORT_START
FIRST, SECOND, THIRD = stop.CHECKPOINTS[:3]


def _row(i: int, entry: datetime, venue: str = "bybit") -> AccrualSnapshotRow:
    return AccrualSnapshotRow(
        capture_id=i,
        source_first_observed_at=entry - timedelta(seconds=30),
        canonical_asset_id=f"asset:{i % 9}",
        target_exchange=venue,
        observed_at=entry,
    )


def _rows(count: int, before: datetime, *, first_id: int = 1) -> list[AccrualSnapshotRow]:
    return [_row(first_id + i, before - timedelta(hours=2 + i)) for i in range(count)]


# --- the frozen table ----------------------------------------------------------------


def test_the_frozen_table_matches_its_poisson_justification() -> None:
    assert stop.CHECKPOINTS[-1] == COHORT_CAPTURE_DEADLINE == datetime(2027, 3, 31, tzinfo=UTC)
    assert [c.date().isoformat() for c in stop.CHECKPOINTS] == [
        "2026-10-31",
        "2026-11-30",
        "2026-12-31",
        "2027-01-31",
        "2027-02-28",
        "2027-03-31",
    ]
    for checkpoint, minimum in stop.MIN_QUALIFIED_TO_CONTINUE.items():
        assert stop.justified_minimum(checkpoint) == minimum, checkpoint
    assert list(stop.MIN_QUALIFIED_TO_CONTINUE.values()) == [12, 28, 45, 64, 81, 100]


def test_poisson_upper_bound_known_values() -> None:
    assert stop.poisson_upper_mean(0) == pytest.approx(-math.log(0.05), abs=1e-6)
    assert stop.poisson_upper_mean(10) == pytest.approx(17.0, abs=0.05)
    with pytest.raises(ValueError, match="invalid"):
        stop.poisson_upper_mean(-1)


# --- one decision --------------------------------------------------------------------


def test_only_episodes_matured_at_the_checkpoint_count() -> None:
    matured = _rows(11, FIRST)
    # Entered 20 minutes before C: its exit bar has not closed at C.
    late = _row(99, FIRST - timedelta(minutes=20))
    decision = stop.decide(FIRST, [*matured, late])
    assert (decision.qualified_in_snapshot, decision.matured_tradable) == (12, 11)
    assert decision.decision == stop.DECISION_STOP
    # Every qualified capture before C is closed, the immature one included.
    assert set(decision.closed_capture_ids) == {r.capture_id for r in [*matured, late]}
    assert stop.terminal_reason(decision) == "accrual_below_rule:2026-10-31"

    enough = stop.decide(FIRST, _rows(12, FIRST))
    assert enough.decision == stop.DECISION_CONTINUE and enough.closed_capture_ids == ()


def test_a_snapshot_outside_the_window_or_venue_is_refused() -> None:
    with pytest.raises(ValueError, match="outside the checkpoint snapshot"):
        stop.decide(FIRST, [_row(1, FIRST + timedelta(hours=1))])
    with pytest.raises(ValueError, match="non-tradable"):
        stop.decide(FIRST, [_row(1, FIRST - timedelta(hours=3), venue="binance")])
    with pytest.raises(ValueError, match="not a registered checkpoint"):
        stop.decide(FIRST + timedelta(days=1), [])


def test_the_snapshot_query_selects_no_price_or_book() -> None:
    sql = str(repository_mod._ACCRUAL_SNAPSHOT_SQL).lower()
    for column in ("liquidity", "instrument", "ticker", "vwap", "price", "close", "impact"):
        assert column not in sql, column
    assert "q.created_at < :as_of" in sql
    assert "c.source_first_observed_at < :as_of" in sql
    assert not hasattr(stop, "calculate_performance")


# --- artifacts -----------------------------------------------------------------------


def test_a_decision_artifact_is_written_once_and_replayed(tmp_path: Path) -> None:
    decision = stop.decide(FIRST, _rows(3, FIRST))
    payload = stop.decision_payload(decision)
    kwargs: dict[str, Any] = {
        "code_revision": "a" * 40,
        "working_tree_dirty": False,
        "evaluated_at": FIRST + timedelta(days=2),
    }
    first = stop.persist_decision(tmp_path, "c.json", payload, **kwargs)
    # A later run on another revision replays the same decision: same artifact.
    again = stop.persist_decision(
        tmp_path, "c.json", payload, **{**kwargs, "code_revision": "b" * 40}
    )
    assert first == again
    stored = json.loads((tmp_path / "c.json").read_text())
    assert stored["code_revision"] == "a" * 40 and stored["decision"] == "stop"
    changed = {**payload, "matured_tradable": 4}
    with pytest.raises(stop.AdministrativeStopIntegrityError):
        stop.persist_decision(tmp_path, "c.json", changed, **kwargs)


# --- the run -------------------------------------------------------------------------


class _Repository:
    def __init__(
        self,
        now: datetime,
        by_checkpoint: dict[datetime, list[AccrualSnapshotRow]],
        open_before: dict[datetime, OpenTransactions] | None = None,
    ):
        self.now = now
        self.by_checkpoint = by_checkpoint
        self.open_before = open_before or {}
        self.snapshots: list[datetime] = []

    async def database_now(self) -> datetime:
        return self.now

    async def open_transactions_started_before(self, as_of: datetime) -> OpenTransactions:
        return self.open_before.get(as_of, OpenTransactions(0, 0))

    async def fetch_accrual_snapshot(self, *, as_of: datetime, **_: Any) -> Any:
        self.snapshots.append(as_of)
        return self.by_checkpoint.get(as_of, [])


def _patch_claims(
    monkeypatch: pytest.MonkeyPatch, prior: FormalReadClaim | None
) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    async def existing(*_a: Any, **_k: Any) -> FormalReadClaim | None:
        return prior

    async def record(_db: str, **kwargs: Any) -> AdministrativeStop:
        recorded.append(kwargs)
        return AdministrativeStop(
            1,
            tuple(kwargs["closed_ids"]),
            kwargs["terminal_reason"],
            kwargs["result_fingerprint"],
            newly_recorded=prior is None,
        )

    monkeypatch.setattr(stop, "existing_claim", existing)
    monkeypatch.setattr(stop, "record_administrative_stop", record)
    return recorded


def _run(repository: _Repository, tmp_path: Path) -> stop.RunOutcome:
    return asyncio.run(
        stop.evaluate_due_checkpoints(
            repository,  # type: ignore[arg-type]
            "postgresql://unused",
            artifact_dir=tmp_path,
            code_revision="a" * 40,
            working_tree_dirty=False,
        )
    )


def test_nothing_is_due_before_the_first_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = _patch_claims(monkeypatch, None)
    repository = _Repository(FIRST - timedelta(seconds=1), {})
    outcome = _run(repository, tmp_path)
    assert outcome.status == stop.DECISION_CONTINUE and outcome.decisions == ()
    assert repository.snapshots == [] and recorded == []


def test_a_late_run_stops_at_the_earliest_failing_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missed runs: evaluated in order, each on its own snapshot; the first stop wins."""
    recorded = _patch_claims(monkeypatch, None)
    data = {
        FIRST: _rows(12, FIRST),  # continues
        SECOND: _rows(20, SECOND, first_id=100),  # below 28: stops here
        THIRD: _rows(1, THIRD, first_id=500),  # never evaluated
    }
    repository = _Repository(THIRD + timedelta(days=3), data)
    outcome = _run(repository, tmp_path)
    assert outcome.status == "stopped"
    assert repository.snapshots == [FIRST, SECOND]
    assert [d.decision for d in outcome.decisions] == ["continue", "stop"]
    (record,) = recorded
    assert record["terminal_reason"] == "accrual_below_rule:2026-11-30"
    assert record["closed_ids"] == [r.capture_id for r in data[SECOND]]
    assert (tmp_path / "checkpoint-2026-10-31.json").exists()
    assert (tmp_path / "checkpoint-2026-11-30.json").exists()
    assert not (tmp_path / "checkpoint-2026-12-31.json").exists()

    # A rerun replays both artifacts and confirms the same stop.
    stopped = FormalReadClaim(id=1, candidate_ids=(), status="admin_stopped", resumed=True)
    _patch_claims(monkeypatch, stopped)
    assert _run(_Repository(THIRD + timedelta(days=9), data), tmp_path).status == (
        "already_stopped"
    )


def test_a_started_formal_read_is_never_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    claimed = FormalReadClaim(id=3, candidate_ids=(1,), status="claimed", resumed=True)
    recorded = _patch_claims(monkeypatch, claimed)
    repository = _Repository(THIRD, {})
    outcome = _run(repository, tmp_path)
    assert outcome.status == "formal_read_started"
    assert repository.snapshots == [] and recorded == []


def test_a_recorded_stop_that_no_checkpoint_replays_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stopped = FormalReadClaim(id=1, candidate_ids=(), status="admin_stopped", resumed=True)
    _patch_claims(monkeypatch, stopped)
    with pytest.raises(stop.AdministrativeStopIntegrityError):
        _run(_Repository(FIRST + timedelta(days=1), {FIRST: _rows(12, FIRST)}), tmp_path)


def test_the_deadline_needs_the_full_floor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded = _patch_claims(monkeypatch, None)
    data = {c: _rows(stop.MIN_QUALIFIED_TO_CONTINUE[c], c, first_id=1) for c in stop.CHECKPOINTS}
    data[COHORT_CAPTURE_DEADLINE] = _rows(99, COHORT_CAPTURE_DEADLINE)
    outcome = _run(_Repository(COHORT_CAPTURE_DEADLINE + timedelta(hours=1), data), tmp_path)
    assert outcome.status == "stopped"
    assert recorded[0]["terminal_reason"] == "accrual_below_rule:2027-03-31"


def test_deadline_settlement() -> None:
    assert not stop.deadline_stop_due(COHORT_CAPTURE_DEADLINE + timedelta(hours=23))
    assert stop.deadline_stop_due(COHORT_CAPTURE_DEADLINE + stop.DEADLINE_SETTLEMENT)


@pytest.mark.parametrize("open_tx", [OpenTransactions(1, 0), OpenTransactions(0, 1)])
def test_a_checkpoint_is_decided_only_on_a_final_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, open_tx: OpenTransactions
) -> None:
    """Review: a transaction begun before C can still commit rows stamped before C."""
    recorded = _patch_claims(monkeypatch, None)
    data = {FIRST: _rows(12, FIRST), SECOND: []}
    repository = _Repository(SECOND + timedelta(hours=1), data, {SECOND: open_tx})
    outcome = _run(repository, tmp_path)
    assert outcome.status == stop.SNAPSHOT_NOT_FINAL
    assert repository.snapshots == [FIRST]  # the unsettled checkpoint is not even read
    assert recorded == [] and not (tmp_path / "checkpoint-2026-11-30.json").exists()
    # Once it settles, the same run decides it.
    repository.open_before = {}
    assert _run(repository, tmp_path).status == "stopped"


def test_a_stop_lists_the_closed_data_windows() -> None:
    entry = FIRST - timedelta(hours=5, seconds=10)
    decision = stop.decide(FIRST, [_row(7, entry)])
    (window,) = decision.closed_windows
    assert window.capture_id == 7 and window.start == entry
    # Exit bar: first 1m boundary at or after entry + 30m, closed one minute later.
    assert window.end == entry.replace(second=0) + timedelta(minutes=32)
    payload = stop.decision_payload(decision)
    assert payload["closed_windows"][0]["canonical_asset_id"] == "asset:7"
    assert stop.decision_payload(stop.decide(FIRST, _rows(12, FIRST)))["closed_windows"] == []
