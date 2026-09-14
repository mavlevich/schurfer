"""Coverage for the PURE gated-deletion decision logic.

No database, Borg repo, or filesystem: the decision must be provable in
isolation because it guards irreversible production deletion. The impure
collectors are exercised separately; here every safety rule is a table row.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from schurfer_analytics.cold_bar_gated_deletion import (
    BLOCK,
    DROP,
    EXPECTED_SCHEMA_VERSION,
    DayEvidence,
    DropReceipt,
    drop_decision,
    is_eligible,
    plan_drops,
)


def _receipt(**over: object) -> DropReceipt:
    base = {
        "day": "2026-08-01",
        "archive_name": "bars-2026-08-02T04:00:00",
        "parquet_path": "bars-2026-08-01.parquet",
        "parquet_sha256": "pq-sha",
        "manifest_sha256": "mf-sha",
        "row_count": 1_000_000,
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "source_fingerprint": "cbfp_v1:src-abc",
        "file_fingerprint": "cbfp_v1:file-abc",
        "fidelity_verified": True,
    }
    base.update(over)
    return DropReceipt(**base)  # type: ignore[arg-type]


def _evidence(**over: object) -> DayEvidence:
    """A fully-passing evidence bundle; each test perturbs one field.

    The receipt defaults to the SAME day as the evidence (unless a test passes its
    own receipt), so the passing case and multi-day plans are not accidentally
    blocked by the receipt-day guard.
    """
    day = over.get("day", "2026-08-01")
    base = {
        "day": "2026-08-01",
        "eligible": True,
        "manifest_present": True,
        "receipt": _receipt(day=day),
        "receipt_offsite_confirmed": True,
        "archive_present": True,
        "extracted_parquet_sha256": "pq-sha",
        "extracted_manifest_sha256": "mf-sha",
        "recomputed_fingerprint": "cbfp_v1:src-abc",
        "recomputed_file_fingerprint": "cbfp_v1:file-abc",
    }
    base.update(over)
    return DayEvidence(**base)  # type: ignore[arg-type]


def test_all_gates_pass_drops() -> None:
    decision, reason = drop_decision(_evidence())
    assert decision == DROP, reason


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("eligible", False, "not yet eligible"),
        ("manifest_present", False, "manifest missing"),
        ("receipt", None, "receipt missing"),
        ("receipt_offsite_confirmed", False, "not itself confirmed"),
        ("archive_present", False, "archive"),
        ("extracted_parquet_sha256", None, "could not be extracted"),
        ("extracted_parquet_sha256", "WRONG", "parquet sha256 does not match"),
        ("extracted_manifest_sha256", None, "manifest could not be extracted"),
        ("extracted_manifest_sha256", "WRONG", "manifest sha256 does not match"),
        ("recomputed_file_fingerprint", None, "could not recompute the file fingerprint"),
        ("recomputed_file_fingerprint", "cbfp_v1:other", "file fingerprint mismatch"),
        ("recomputed_fingerprint", None, "could not recompute the source fingerprint"),
        ("recomputed_fingerprint", "cbfp_v1:changed", "source changed since export"),
    ],
)
def test_each_missing_or_wrong_proof_blocks(field: str, value: object, needle: str) -> None:
    decision, reason = drop_decision(_evidence(**{field: value}))
    assert decision == BLOCK
    assert needle in reason


def test_receipt_schema_mismatch_blocks() -> None:
    decision, reason = drop_decision(_evidence(receipt=_receipt(schema_version="cold_bars_v2")))
    assert decision == BLOCK
    assert "schema_version" in reason


def test_receipt_without_fingerprint_blocks() -> None:
    decision, reason = drop_decision(_evidence(receipt=_receipt(source_fingerprint="")))
    assert decision == BLOCK
    assert "no fingerprints" in reason


def test_unverified_fidelity_blocks() -> None:
    # a day whose export could not prove the file captured the source is not droppable
    decision, reason = drop_decision(_evidence(receipt=_receipt(fidelity_verified=False)))
    assert decision == BLOCK
    assert "fidelity" in reason


def test_fingerprint_mismatch_signals_reexport() -> None:
    decision, reason = drop_decision(_evidence(recomputed_fingerprint="fp-different"))
    assert decision == BLOCK
    assert "needs_versioned_reexport" in reason


def test_receipt_for_a_different_day_blocks() -> None:
    # A valid receipt for another day must never license dropping this day.
    ev = _evidence(day="2026-08-02", receipt=_receipt(day="2026-08-01"))
    decision, reason = drop_decision(ev)
    assert decision == BLOCK
    assert "not the candidate" in reason


# --- is_eligible -----------------------------------------------------------


def test_eligibility_uses_utc_midnight_minus_buffer() -> None:
    now = datetime(2026, 9, 14, 18, 30, tzinfo=UTC)  # cutoff = 2026-08-05 00:00
    # a chunk ending exactly at the cutoff is eligible (range_end is exclusive)
    assert is_eligible(datetime(2026, 8, 5, 0, 0, tzinfo=UTC), now, 40) is True
    # one minute later is still inside the buffer
    assert is_eligible(datetime(2026, 8, 5, 0, 1, tzinfo=UTC), now, 40) is False
    # the run's time of day does not move the cutoff
    later_same_day = datetime(2026, 9, 14, 23, 59, tzinfo=UTC)
    assert is_eligible(datetime(2026, 8, 5, 0, 0, tzinfo=UTC), later_same_day, 40) is True


def test_eligibility_anchors_to_utc_not_the_argument_offset() -> None:
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    end = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
    # Same instant expressed in UTC-07 and UTC+05 must give the SAME cutoff (UTC midnight).
    now_utc = datetime(2026, 9, 14, 3, 0, tzinfo=UTC)  # cutoff 2026-08-05 00:00
    now_west = now_utc.astimezone(_tz(-_td(hours=7)))  # 2026-09-13 20:00 -07
    now_east = now_utc.astimezone(_tz(_td(hours=5)))
    assert is_eligible(end, now_utc, 40) is True
    assert is_eligible(end, now_west, 40) is True
    assert is_eligible(end, now_east, 40) is True


def test_eligibility_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_eligible(datetime(2026, 8, 5, 0, 0, tzinfo=UTC), datetime(2026, 9, 14, 3, 0), 40)
    with pytest.raises(ValueError, match="timezone-aware"):
        is_eligible(datetime(2026, 8, 5, 0, 0), datetime(2026, 9, 14, 3, 0, tzinfo=UTC), 40)


# --- plan_drops: contiguous prefix ----------------------------------------


def test_plan_drops_all_pass() -> None:
    days = tuple(_evidence(day=f"2026-08-0{n}") for n in (1, 2, 3))
    plan = plan_drops(days)
    assert plan.to_drop == ("2026-08-01", "2026-08-02", "2026-08-03")
    assert plan.blocked_at is None
    assert plan.held_after_block == ()


def test_plan_drops_stops_at_first_block_and_holds_rest() -> None:
    days = (
        _evidence(day="2026-08-01"),
        _evidence(day="2026-08-02", recomputed_fingerprint="changed"),  # blocks
        _evidence(day="2026-08-03"),  # would pass alone, but is held
    )
    plan = plan_drops(days)
    assert plan.to_drop == ("2026-08-01",)
    assert plan.blocked_at is not None
    assert plan.blocked_at[0] == "2026-08-02"
    assert "source changed" in plan.blocked_at[1]
    assert plan.held_after_block == ("2026-08-03",)


def test_plan_drops_blocks_on_the_oldest_touches_nothing() -> None:
    days = (
        _evidence(day="2026-08-01", receipt=None),  # oldest blocks
        _evidence(day="2026-08-02"),
    )
    plan = plan_drops(days)
    assert plan.to_drop == ()
    assert plan.blocked_at[0] == "2026-08-01"  # type: ignore[index]
    assert plan.held_after_block == ("2026-08-02",)


def test_plan_drops_halts_on_a_calendar_gap() -> None:
    # Aug 02 is missing from the candidate list; the frontier must not jump the gap.
    days = (
        _evidence(day="2026-08-01"),
        _evidence(day="2026-08-03"),  # gap: not consecutive after Aug 01
    )
    plan = plan_drops(days)
    assert plan.to_drop == ("2026-08-01",)
    assert plan.blocked_at is not None
    assert plan.blocked_at[0] == "2026-08-03"
    assert "not consecutive" in plan.blocked_at[1]


def test_plan_drops_halts_on_out_of_order_days() -> None:
    days = (
        _evidence(day="2026-08-02"),
        _evidence(day="2026-08-01"),  # out of order
    )
    plan = plan_drops(days)
    assert plan.to_drop == ("2026-08-02",)
    assert plan.blocked_at is not None
    assert plan.blocked_at[0] == "2026-08-01"
    assert "not consecutive" in plan.blocked_at[1]
