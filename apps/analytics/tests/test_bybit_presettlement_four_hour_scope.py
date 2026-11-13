from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from schurfer_analytics import bybit_presettlement_feasibility as b1
from schurfer_analytics.bybit_post_funding_screen import (
    DAY_MS,
    HOUR_MS,
    Index,
    Instrument,
    SnapshotWriter,
)

if TYPE_CHECKING:
    from pathlib import Path

T0 = b1.FIRST_MS + 10 * DAY_MS


def mixed_world() -> tuple[Index, list[b1.Interval], b1.MinutePremium]:
    """75 off-scope validation intervals really meet C and the minute trigger."""
    rows = [b1.Interval("FOURUSDT", T0, T0 + 4 * HOUR_MS, 240, False, T0 + 4 * HOUR_MS)]
    rows.extend(
        b1.Interval(f"ONE{k}USDT", T0, T0 + HOUR_MS, 60, False, T0 + HOUR_MS) for k in range(75)
    )
    instruments = {
        iv.symbol: Instrument(
            b1.LAST_MS,
            {T0 - DAY_MS: -0.004, iv.predicted: -0.0039},
            {T0 + 3 * HOUR_MS - k * HOUR_MS: 200_000.0 for k in range(40)},
        )
        for iv in rows
    }
    minutes = b1.MinutePremium(
        {iv.symbol: dict.fromkeys(b1.minute_starts(iv), -0.0045) for iv in rows}, ()
    )
    return Index("discovery", instruments, ()), rows, minutes


def test_validation_candidates_cannot_create_off_scope_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index, rows, minutes = mixed_world()
    monkeypatch.setattr(b1, "intervals", lambda _index: rows)
    monkeypatch.setattr(b1, "validation_sample", lambda _every: rows[1:])
    # A completely different hourly envelope cannot change either S1 or S3 scope.
    hourly = b1.HourlyPremium(
        {iv.symbol: {T0: 1.0} for iv in rows},
        {iv.symbol: {T0: 2.0} for iv in rows},
        (),
    )
    report = b1.build_report(index, hourly, minutes)
    assert report["event_superset"] == 1
    assert report["s1"]["pool"] == 76
    assert report["pooled_s3_verdict"] == "not_fetched"
    for d in ("d30", "d15", "d10"):
        pooled = report["s3"][d]
        assert pooled["status"] == "not_fetched" and pooled["floor"] is None
        assert pooled["by_interval"]["4h"]["admitted"]["events"] == 1
        for name in ("1h", "2h", "8h", "other"):
            assert pooled["by_interval"][name]["status"] == "not_fetched"
            assert pooled["by_interval"][name]["admitted"] is None
    for name in ("1h", "2h", "8h"):
        assert report["by_interval"][name]["verdict"] == "not_fetched"
        assert report["by_interval"][name]["passing_decision_offsets"] == []


def test_validation_membership_does_not_remove_an_independent_four_hour_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index, rows, _ = mixed_world()
    monkeypatch.setattr(b1, "intervals", lambda _index: rows)
    monkeypatch.setattr(b1, "validation_sample", lambda _every: rows)
    events, pool = b1.report_scope(index)
    assert events == rows[:1]
    assert len(pool) == len(rows)  # overlap counted once in S1, qualifies independently for S3


def test_incomplete_prefix_cannot_hide_behind_an_event_floor() -> None:
    index, rows, _ = mixed_world()
    result = b1._decision_block(index, rows[:1], b1.MinutePremium({}, ()), 30)
    assert result["input_intervals"] == 1
    assert result["complete_prefixes"] == 0
    assert result["prefix_complete_share"] == 0.0
    assert not result["data_complete"]


def test_hourly_failure_is_diagnostic_after_the_registered_amendment() -> None:
    check = {"status": "pass", "hourly_range_exceeded": True}
    assert b1._verdict(check, ["d30"]) == "pass"
    assert (
        b1._verdict({"status": "fail", "hourly_range_exceeded": True}, ["d30"])
        == "not_reconstructible"
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_closes_never_use_the_last_file(reverse: bool) -> None:
    parts = [
        b1.MinutePremium({"AAAUSDT": {T0: -0.004}}, ()),
        b1.MinutePremium({"AAAUSDT": {T0: -0.005}}, ()),
    ]
    if reverse:
        parts.reverse()
    with pytest.raises(ValueError, match="conflicting"):
        b1.merge_minutes(*parts)


def test_equal_duplicate_minutes_are_idempotent_and_do_not_mutate_inputs() -> None:
    a = b1.MinutePremium({"AAAUSDT": {T0: -0.004}}, ())
    b = b1.MinutePremium({"AAAUSDT": {T0: -0.004, T0 + 60_000: -0.005}}, ())
    merged = b1.merge_minutes(a, b, a)
    assert len(merged.closes["AAAUSDT"]) == 2
    assert a.closes["AAAUSDT"] == {T0: -0.004}


def write_snapshot(
    path: Path, *, high: str, close: str = "-0.004", repeated_high: str | None = None
) -> None:
    writer = SnapshotWriter(path)
    try:
        writer.record("version", None, {}, {"version": b1.VERSION})
        items = [[str(T0), "-0.004", high, "-0.006", close]]
        if repeated_high is not None:
            items.append([str(T0), "-0.004", repeated_high, "-0.006", close])
        writer.record("premium_1", "AAAUSDT", {}, {"result": {"symbol": "AAAUSDT", "list": items}})
    finally:
        writer.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_real_snapshot_merge_rejects_changed_ohlc_even_when_close_matches(
    tmp_path: Path, reverse: bool
) -> None:
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    write_snapshot(a, high="-0.003")
    write_snapshot(b, high="-0.002")
    parts = [b1.load_minutes(a), b1.load_minutes(b)]
    if reverse:
        parts.reverse()
    with pytest.raises(ValueError, match=r"conflicting.*OHLC"):
        b1.merge_minutes(*parts)


def test_single_snapshot_cannot_overwrite_conflicting_duplicate_ohlc(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.jsonl.gz"
    write_snapshot(path, high="-0.003", repeated_high="-0.002")
    with pytest.raises(ValueError, match=r"conflicting.*OHLC"):
        b1.load_minutes(path)


def test_equal_native_snapshot_duplicate_keeps_provenance(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    write_snapshot(a, high="-0.003")
    write_snapshot(b, high="-0.003")
    merged = b1.merge_minutes(b1.load_minutes(a), b1.load_minutes(b))
    assert merged.closes == {"AAAUSDT": {T0: -0.004}}
    assert merged.source_paths == (a, b)


def test_full_scope_auditor_uses_classify_and_catches_an_omitted_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from schurfer_analytics import bybit_presettlement_scope_audit as audit

    index, rows, _ = mixed_world()
    monkeypatch.setattr(b1, "intervals", lambda _index: rows)
    good = audit.audit_scope(index)
    assert good["status"] == "pass"
    assert good["classifications"] == len(rows) * 3
    assert good["admitted_by_d"] == {"d10": len(rows), "d15": len(rows), "d30": len(rows)}
    monkeypatch.setattr(b1, "event_superset", lambda _index, _rows: rows[1:])
    bad = audit.audit_scope(index)
    assert bad["status"] == "fail"
    assert bad["admitted_outside_superset"] == 3
    assert {r["symbol"] for r in bad["omitted_ids"]} == {"FOURUSDT"}


@pytest.mark.parametrize("command", ["fetch-minutes", "fetch-minutes-extra"])
def test_unbudgeted_minute_download_is_disabled_before_loading_inputs(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    def unexpected(*_args: object) -> None:
        pytest.fail("disabled download must not load inputs or create a client")

    monkeypatch.setattr(b1, "load_index", unexpected)
    monkeypatch.setattr(b1, "_run_fetch", unexpected)
    argv = [command, "--index", "unread", "--snapshot", "not-created"]
    if command.endswith("extra"):
        argv.extend(["--minutes", "unread", "--minutes-sha256", "unread"])
    with pytest.raises(SystemExit, match="downloads are disabled"):
        b1.main(argv)
