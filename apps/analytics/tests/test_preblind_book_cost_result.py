"""A failed one-time read resumes only from its frozen pre-blind rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import preblind_book_cost_baseline as baseline
from schurfer_analytics import preblind_book_cost_result as reader
from schurfer_analytics.source_lead_multi_source_report import load_verified

if TYPE_CHECKING:
    from pathlib import Path

_Rows = tuple[datetime, list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]
_DB_NOW = datetime(2026, 9, 30, tzinfo=UTC)


def _source_row(impact: float = 10.0) -> dict[str, Any]:
    return {
        "capture_id": 1,
        "capture_version": "source_lead_prospective_capture_v1",
        "source_exchange": "gate",
        "source_first_observed_at": datetime(2026, 9, 28, 11, tzinfo=UTC),
        "target_id": 2,
        "target_exchange": "bybit",
        "target_status": "sampled",
        "observed_at": datetime(2026, 9, 28, 12, tzinfo=UTC),
        "requested_notional_usd": 50.0,
        "spread_bps": 8.0,
        "ask_impact_bps": impact,
        "bid_impact_bps": 12.0,
        "ask_filled_notional_usd": 50.0,
        "bid_filled_notional_usd": 50.0,
        "book_age_ms": 100,
    }


@pytest.mark.asyncio
async def test_completed_result_is_verified_without_another_database_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def load(_db_url: str) -> _Rows:
        nonlocal calls
        calls += 1
        return _DB_NOW, [], [_source_row()], {"paper_updated_after_cutoff": 136}

    monkeypatch.setattr(baseline, "load_registered_rows", load)
    first, first_sha = await reader.report_once(
        "unused", code_revision="first", working_tree_dirty=False, artifact_dir=tmp_path
    )
    second, second_sha = await reader.report_once(
        "unused", code_revision="later", working_tree_dirty=True, artifact_dir=tmp_path
    )
    assert calls == 1
    assert first == second
    assert first_sha == second_sha
    assert first["inputs_sha256"] == load_verified(tmp_path / "inputs.json")[1]
    assert first["database_read_at_utc"] == _DB_NOW.isoformat()
    assert first["excluded_from_preblind_snapshot"]["paper_updated_after_cutoff"] == 136


@pytest.mark.asyncio
async def test_crash_after_freeze_replays_original_rows_and_pins_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def load(_db_url: str) -> _Rows:
        nonlocal calls
        calls += 1
        return _DB_NOW, [], [_source_row(10.0 if calls == 1 else 100.0)], {}

    monkeypatch.setattr(baseline, "load_registered_rows", load)
    original = reader._result_from_inputs

    def interrupted(_inputs: dict[str, Any], _digest: str) -> dict[str, Any]:
        raise RuntimeError("simulated crash after freeze")

    monkeypatch.setattr(reader, "_result_from_inputs", interrupted)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await reader.report_once(
            "unused", code_revision="first", working_tree_dirty=False, artifact_dir=tmp_path
        )
    monkeypatch.setattr(reader, "_result_from_inputs", original)
    with pytest.raises(ValueError, match="another code revision"):
        await reader.report_once(
            "unused", code_revision="changed", working_tree_dirty=False, artifact_dir=tmp_path
        )
    report, _ = await reader.report_once(
        "unused", code_revision="first", working_tree_dirty=False, artifact_dir=tmp_path
    )
    assert calls == 1
    assert report["groups"][0]["entry_ask_impact_bps"]["mean"] == 10.0


@pytest.mark.asyncio
async def test_interrupted_result_digest_requires_exact_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(_db_url: str) -> _Rows:
        return _DB_NOW, [], [_source_row()], {}

    monkeypatch.setattr(baseline, "load_registered_rows", load)
    await reader.report_once(
        "unused", code_revision="first", working_tree_dirty=False, artifact_dir=tmp_path
    )
    (tmp_path / "result.json.sha256").unlink()
    (tmp_path / "result.json").write_text("{}\n")
    with pytest.raises(ValueError, match="differs from frozen-input replay"):
        await reader.report_once(
            "unused", code_revision="first", working_tree_dirty=False, artifact_dir=tmp_path
        )
    assert not (tmp_path / "result.json.sha256").exists()
