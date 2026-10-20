from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics.momentum_flow_hold12h_v2_terminal import (
    V2TerminalPin,
    check_v2_closed_windows,
    load_v2_stop,
    watch_dependency,
)
from schurfer_analytics.momentum_flow_watch_contract import FROZEN_WATCH_CONTRACT
from schurfer_analytics.source_lead_forward_cohort import (
    CONTRACT_VERSION,
    SOURCE_LEAD_FORWARD_COHORT_START,
)

if TYPE_CHECKING:
    from pathlib import Path

START = datetime(2026, 11, 9, tzinfo=UTC)


def _stop(tmp_path: Path, *ends: datetime, **overrides: Any) -> V2TerminalPin:
    payload = {
        "study_id": "HYP-012",
        "contract_version": CONTRACT_VERSION,
        "cohort_start": SOURCE_LEAD_FORWARD_COHORT_START.isoformat(),
        "decision": "stop",
        "closed_windows": [
            {"capture_id": i, "canonical_asset_id": f"asset:{i}",
             "start": (end - timedelta(minutes=30)).isoformat(), "end": end.isoformat()}
            for i, end in enumerate(ends)
        ],
    } | overrides  # fmt: skip
    body = json.dumps(payload).encode()
    (tmp_path / "checkpoint-2026-10-31.json").write_bytes(body)
    return V2TerminalPin("checkpoint-2026-10-31.json", hashlib.sha256(body).hexdigest())


def test_a_watch_decision_depends_on_its_state_lookback_and_oi_staleness() -> None:
    # cooldown 360 (longer than the 5-minute re-arm) + lookback 60 + OI staleness 30
    assert watch_dependency(FROZEN_WATCH_CONTRACT) == timedelta(minutes=450)


def test_windows_ending_before_the_earliest_data_used_pass(tmp_path: Path) -> None:
    earliest = START - timedelta(minutes=450)
    pin = _stop(tmp_path, datetime(2026, 10, 30, tzinfo=UTC), earliest)
    summary = check_v2_closed_windows(load_v2_stop(tmp_path, pin), earliest_use=earliest, pin=pin)
    assert summary == {
        "v2_terminal_artifact": pin.file_name,
        "v2_terminal_sha256": pin.sha256,
        "closed_windows": 2,
        "latest_closed_window_end": earliest.isoformat(),
        "earliest_data_used": earliest.isoformat(),
    }


def test_a_stop_with_no_closed_windows_passes(tmp_path: Path) -> None:
    pin = _stop(tmp_path)
    summary = check_v2_closed_windows(load_v2_stop(tmp_path, pin), earliest_use=START, pin=pin)
    assert summary["closed_windows"] == 0 and summary["latest_closed_window_end"] is None


def test_a_window_reaching_into_the_data_used_refuses(tmp_path: Path) -> None:
    earliest = START - timedelta(minutes=450)
    pin = _stop(tmp_path, earliest + timedelta(seconds=1))
    with pytest.raises(SystemExit, match="1 v2 closed windows end after"):
        check_v2_closed_windows(load_v2_stop(tmp_path, pin), earliest_use=earliest, pin=pin)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"study_id": "HYP-015"}, "not the HYP-012 v2 administrative stop"),
        ({"contract_version": "other"}, "not the HYP-012 v2 administrative stop"),
        ({"cohort_start": "2026-10-01T00:00:00+00:00"}, "not the HYP-012 v2 administrative stop"),
        ({"decision": "continue"}, "not the HYP-012 v2 administrative stop"),
        ({"closed_windows": None}, "no closed_windows list"),
    ],
)
def test_anything_but_the_v2_stop_refuses(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(SystemExit, match=message):
        load_v2_stop(tmp_path, _stop(tmp_path, **overrides))


def test_another_file_than_the_pinned_one_refuses(tmp_path: Path) -> None:
    pin = _stop(tmp_path)
    with pytest.raises(SystemExit, match="not the pinned v2 terminal artifact"):
        load_v2_stop(tmp_path, V2TerminalPin(pin.file_name, "0" * 64))
    with pytest.raises(SystemExit, match="is missing"):
        load_v2_stop(tmp_path, V2TerminalPin("other.json", pin.sha256))
