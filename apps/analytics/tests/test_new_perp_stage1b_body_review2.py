"""Second review of PR 628: each finding reproduced by a synthetic test, then fixed.

628-1 a success report published before the final source check; 628-2 a second gzip member
ignored; 628-4 API reducers without the lower bound or the hourly grid; 628-5 an output root
inside the Git tree; 628-6 a known M4 or M5 failure lost while launches are pending.
No network, no sealed file: a synthetic sealed root and a mock server.
"""

from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import new_perp_stage1 as s
from schurfer_analytics import new_perp_stage1b_body as body
from schurfer_analytics import new_perp_stage1b_body_verdict as v
from schurfer_analytics.new_perp_window import HOLDOUT

from .new_perp_body_world import body_fetcher, holdout_world, write_sealed
from .new_perp_world import PRICE, RATE
from .test_new_perp_stage1b_body import (
    feed,
    full_world,
    funding_payload,
    kline_payload,
    row,
    trade_gz,
)
from .test_new_perp_stage1b_body_verdict import (
    EIGHT,
    HOUR,
    _candidate,
    evaluate,
    funding_page,
    hourly,
    make,
    slots,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CUT = HOLDOUT.cut_ms
DAY = s.DAY_MS
GOOD = row("1685613600.5")


# -- 628-2: exactly one gzip member and no trailing bytes


def test_a_second_gzip_member_is_never_ignored() -> None:
    """Member 2 holds a 2025 stamp: the old reducer returned one row from member 1."""
    late = f"{CUT // 1000 + 400}.5"
    blob = trade_gz([GOOD]) + trade_gz([row(late)], header=False)
    for step in (1, 7, len(blob)):
        with pytest.raises(s.SchemaMismatchError, match="gzip_trailing_data"):
            feed(blob, step)


def test_a_second_member_with_an_earlier_trade_cannot_change_the_minimum_silently() -> None:
    early = row("1685613000.0")  # earlier than the first member's trade
    for step in (3, 10_000):
        with pytest.raises(s.SchemaMismatchError, match="gzip_trailing_data"):
            feed(trade_gz([GOOD]) + gzip.compress((early + "\n").encode()), step)


@pytest.mark.parametrize("trailer", [b"\0", b"\0" * 20, b"junk", b"\x1f\x8b"])
def test_arbitrary_trailing_bytes_after_the_gzip_member_stop_the_run(trailer: bytes) -> None:
    for step in (5, 10_000):
        with pytest.raises(s.SchemaMismatchError, match="gzip_trailing_data"):
            feed(trade_gz([GOOD]) + trailer, step)


def test_a_single_clean_member_still_reads_in_any_chunking() -> None:
    blob = trade_gz([GOOD, row("1685613599.25")])
    for step in (1, 2, 64, len(blob)):
        assert feed(blob, step)["rows"] == 2


# -- 628-4: exact request bounds, the hourly grid, duplicates


START = HOLDOUT.start_ms + 10 * DAY
END = START + 40 * HOUR
H = s.HOUR_MS


def bars(starts: list[int], symbol: str = "SYMUSDT") -> dict[str, Any]:
    return kline_payload(symbol, starts, width=5)


def reduce_bars(starts: list[int], *, start: int = START, end: int = END) -> dict[str, Any]:
    return body.reduce_kline_window(bars(starts), "SYMUSDT", 5, H, 1000, start, end)


def test_a_bar_below_the_requested_lower_bound_is_never_kept() -> None:
    """A 2021 bar was kept in holdout coverage facts."""
    ok = START - START % H + H
    stale = ok - 1000 * DAY
    with pytest.raises(s.SchemaMismatchError, match="bar_out_of_range"):
        reduce_bars([stale - stale % H, ok])
    with pytest.raises(s.SchemaMismatchError, match="bar_out_of_range"):
        reduce_bars([ok, END - END % H + H])  # above the requested end, below the cut
    assert reduce_bars([ok, ok + H])["n"] == 2


def test_the_hour_containing_the_start_is_allowed_and_nothing_earlier() -> None:
    """A bar that starts before an unaligned start but contains it is the bar of that hour."""
    containing = START - START % H
    assert reduce_bars([containing, containing + H])["oldest_ms"] == containing
    with pytest.raises(s.SchemaMismatchError, match="bar_out_of_range"):
        reduce_bars([containing - H, containing])


def test_an_hourly_series_shifted_by_a_minute_is_not_covered() -> None:
    """Every timestamp one minute off the hour grid used to give bar_state covered."""
    entry = START + 6 * H
    exit_ = entry + 7 * DAY
    shifted = [t + s.MINUTE_MS for t in range(START - START % H, END, H)]
    with pytest.raises(s.SchemaMismatchError, match="bar_off_grid"):
        reduce_bars(shifted)
    assert entry < exit_


def test_duplicate_bar_timestamps_are_a_schema_stop() -> None:
    first = START - START % H + H
    with pytest.raises(s.SchemaMismatchError, match="bar_duplicate"):
        reduce_bars([first, first + H, first + H])


def test_rows_from_the_cut_are_still_dropped_uncounted_when_the_request_reaches_it() -> None:
    last = CUT - H
    facts = body.reduce_kline_window(
        bars([last - H, last, CUT, CUT + H]), "SYMUSDT", 5, H, 1000, last - 5 * H, CUT - 1
    )
    assert facts["n"] == 2 and facts["newest_ms"] == last


def test_the_minute_entry_bar_must_be_exactly_the_requested_minute() -> None:
    entry = START - START % s.MINUTE_MS
    payload = kline_payload("SYMUSDT", [entry])
    ok = body.reduce_kline_window(payload, "SYMUSDT", 7, s.MINUTE_MS, 1, entry, entry)
    assert ok["n"] == 1 and ok["oldest_ms"] == entry
    for other in (entry - s.MINUTE_MS, entry + s.MINUTE_MS, entry + 1):
        with pytest.raises(s.SchemaMismatchError):
            body.reduce_kline_window(
                kline_payload("SYMUSDT", [other]), "SYMUSDT", 7, s.MINUTE_MS, 1, entry, entry
            )


def test_a_malformed_row_or_bad_symbol_is_checked_before_other_fields() -> None:
    first = START - START % H + H
    bad_row = {"retCode": 0, "result": {"symbol": "SYMUSDT", "list": [["x", *([PRICE] * 4)]]}}
    with pytest.raises(s.SchemaMismatchError, match="bad:start"):
        body.reduce_kline_window(bad_row, "SYMUSDT", 5, H, 1000, START, END)
    with pytest.raises(s.SchemaMismatchError, match="symbol"):
        body.reduce_kline_window(bars([first], "OTHERUSDT"), "SYMUSDT", 5, H, 1000, START, END)
    with pytest.raises(s.SchemaMismatchError, match="row"):
        body.reduce_kline_window(
            {"retCode": 0, "result": {"symbol": "SYMUSDT", "list": [[str(first)]]}},
            "SYMUSDT",
            5,
            H,
            1000,
            START,
            END,
        )


def test_funding_rows_must_lie_inside_the_page_bounds_without_duplicates() -> None:
    lo, hi = START, START + 5 * EIGHT
    inside = [lo + EIGHT, lo + 2 * EIGHT]
    good = body.reduce_funding_window(funding_payload("SYMUSDT", inside), "SYMUSDT", lo, hi)
    assert good["n"] == 2 and RATE not in json.dumps(good)
    for stamps, reason in (
        ([lo - EIGHT, lo + EIGHT], "funding_out_of_range"),
        ([lo + EIGHT, hi + EIGHT], "funding_out_of_range"),
        ([lo + EIGHT, lo + EIGHT], "funding_duplicate"),
    ):
        with pytest.raises(s.SchemaMismatchError, match=reason):
            body.reduce_funding_window(funding_payload("SYMUSDT", stamps), "SYMUSDT", lo, hi)
    cut = body.reduce_funding_window(
        funding_payload("SYMUSDT", [CUT - EIGHT, CUT, CUT + EIGHT]),
        "SYMUSDT",
        CUT - 3 * EIGHT,
        CUT - 1,
    )
    assert cut["n"] == 1 and cut["newest_ms"] == CUT - EIGHT


def test_an_out_of_range_response_seals_the_run_through_the_real_fetcher(tmp_path: Path) -> None:
    world = holdout_world(3)
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed)
    body.run_a(a, sealed.sealed, sealed.pins)
    queued = {slot for slot, r in sealed.sealed.rows.items() if body.has_size(r)}
    cands = body.entry_candidates(sealed.sealed.rows, body.results_by_slot(a, "D2"), queued)
    request = next(r for r in body.bar_requests(cands, sealed.sealed.rows) if r.cls == "D4")
    params = dict(request.params)
    stale = [str((int(params["start"]) - 1000 * DAY) // H * H), *([PRICE] * 4)]
    payload = json.dumps(
        {"retCode": 0, "result": {"symbol": params["symbol"], "list": [stale]}}
    ).encode()
    b, _ = body_fetcher(
        world,
        root,
        sealed,
        go=2,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, stream=httpx.ByteStream(payload))
            )
        ),
    )
    reducer = body.kline_reducer(
        params["symbol"], 5, H, 1000, int(params["start"]), int(params["end"])
    )
    with pytest.raises(s.FetchStoppedError, match="bar_out_of_range"):
        b.run_request(request, reducer)
    assert json.loads((root / "stop.json").read_text())["reason"].endswith("(bar_out_of_range)")


# -- 628-1: nothing positive is published before the final integrity check


def run_a_with(tmp_path: Path, verify: Any) -> tuple[Any, Path, Any, Any]:
    world, rows = full_world()
    sealed = write_sealed(tmp_path, world, rows=rows)
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed)
    return a, root, sealed, world


def fail() -> None:
    raise ValueError("source changed after the last response")


def test_go_a_publishes_no_done_marker_or_report_when_the_final_check_fails(
    tmp_path: Path,
) -> None:
    a, root, sealed, _ = run_a_with(tmp_path, fail)
    with pytest.raises(s.FetchStoppedError, match="source_integrity_error"):
        body.run_a(a, sealed.sealed, sealed.pins, verify=fail)
    assert not (root / "go_a_done.json").exists() and not (root / "queue_b.json").exists()
    assert json.loads((root / "stop.json").read_text())["reason"] == "source_integrity_error"
    stored = json.loads((root / "go_a_report.json").read_text())
    assert stored["outcome"] == "stage1b_stopped" and stored["go_b_allowed"] is False


def test_go_b_publishes_no_pass_or_done_marker_when_the_final_check_fails(tmp_path: Path) -> None:
    world, rows = full_world()
    sealed = write_sealed(tmp_path, world, rows=rows)
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed)
    body.run_a(a, sealed.sealed, sealed.pins)
    b, _ = body_fetcher(world, root, sealed, go=2)
    with pytest.raises(s.FetchStoppedError, match="source_integrity_error"):
        body.run_b(b, sealed.sealed, body.queue_b_sha256(root), verify=fail)
    assert not (root / "go_b_done.json").exists()
    stored = json.loads((root / "body_report.json").read_text())
    assert stored["outcome"] == "stage1b_stopped" and stored["outcome"] != "stage1b_pass_partial"
    assert json.loads((root / "stop.json").read_text())["reason"] == "source_integrity_error"


def test_the_final_check_covers_the_sealed_files_the_revision_and_the_publication(
    tmp_path: Path,
) -> None:
    sealed = write_sealed(tmp_path, holdout_world(3))
    bound = {"revision": "a" * 40, "publication": {"x": "1"}}
    good = body.make_verify(
        sealed.root,
        sealed.sealed,
        sealed.pins,
        bound,
        revision=lambda: "a" * 40,
        published=lambda paths: {"x": "1"},
    )
    good()

    def revision_of(value: str) -> Callable[[], str]:
        return lambda: value

    def published_of(value: dict[str, str]) -> Callable[[list[str]], dict[str, str]]:
        return lambda paths: value

    for revision, published in (("b" * 40, {"x": "1"}), ("a" * 40, {"x": "2"})):
        changed = body.make_verify(
            sealed.root,
            sealed.sealed,
            sealed.pins,
            bound,
            revision=revision_of(revision),
            published=published_of(published),
        )
        with pytest.raises(ValueError, match="changed"):
            changed()
    first = next((sealed.root / "catalog").glob("*.json"))
    saved = first.read_text()
    first.unlink()  # a sealed file deleted after the last response
    with pytest.raises(ValueError):
        good()
    first.write_text(saved.replace("csv_pending", "no_archive"))
    with pytest.raises(ValueError):
        good()


def test_report_refuses_without_a_done_marker_and_never_shows_a_pass_over_a_stop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "body"
    root.mkdir()
    positive = {"outcome": "stage1b_pass_partial", "stop_codes": []}
    (root / "body_report.json").write_text(json.dumps(positive))
    with pytest.raises(ValueError, match="not finished"):
        body.main(["report", "--root", str(root), "--phase", "final"])
    (root / "go_b_done.json").write_text("{}")
    assert body.main(["report", "--root", str(root), "--phase", "final"]) == 0
    assert "stage1b_pass_partial" in capsys.readouterr().out
    (root / "stop.json").write_text(json.dumps({"reason": "source_integrity_error"}))
    with pytest.raises(ValueError, match="sealed by a stop"):
        body.main(["report", "--root", str(root), "--phase", "final"])
    (root / "body_report.json").write_text(json.dumps({"outcome": "stage1b_stopped"}))
    assert body.main(["report", "--root", str(root), "--phase", "final"]) == 0
    assert "stage1b_stopped" in capsys.readouterr().out


# -- 628-5: the output root is outside the Git tree and every source root


def test_the_output_root_may_not_be_inside_the_git_tree_or_a_source_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    source = tmp_path / "metadata"
    source.mkdir()
    body.check_root_location(tmp_path / "private", source, repo)
    for bad in (repo / "docs" / "root", repo, repo / "x", source / "inside", source, tmp_path):
        with pytest.raises(ValueError, match="root"):
            body.check_root_location(bad, source, repo)
    link = tmp_path / "link"
    link.symlink_to(repo / "docs")
    with pytest.raises(ValueError, match="root"):
        body.check_root_location(link / "x", source, repo)


def test_the_cli_refuses_a_root_inside_the_repository_before_any_file_or_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("an HTTP client was created")

    monkeypatch.setattr(httpx, "Client", no_client)
    repo_root = body.repo_root()
    target = repo_root / "i25-body-root-test-never-created"
    with pytest.raises(ValueError, match="Git tree"):
        body.main(
            [
                "fetch",
                "--go",
                "1",
                "--approved-sha256",
                body.build_contract()["go_sha256"]["1"],
                "--metadata-root",
                str(tmp_path / "metadata"),
                "--root",
                str(target),
            ]
        )
    assert not target.exists()


# -- 628-6: a known failure survives pending evidence


def test_a_known_m4_failure_is_a_stop_code_even_while_launches_are_pending() -> None:
    """6 of 100 uncovered is known over the fixed denominator, whatever is still pending."""
    data = make(100)
    ordered = slots(data)
    for slot in ordered[:6]:
        data.funding[slot] = [funding_page(_candidate(data, slot), max_gap_ms=EIGHT + 1)]
    for slot in ordered[6:20]:
        del data.funding[slot]  # fourteen launches are still pending
    report = evaluate(data)
    assert report["m4"]["share"] == 0.06 and report["m4"]["assessed"] is False
    assert report["m4"]["fail"] is True and report["m4"]["status"] == "fail"
    assert "incomplete_coverage" in report["stop_codes"]
    assert report["outcome"] == "incomplete_coverage"
    data.stop = {"go": 2, "reason": "D6: HTTP 403"}
    assert evaluate(data)["outcome"] == "incomplete_coverage"


def test_pending_launches_never_invent_an_m4_failure_and_below_the_limit_still_passes() -> None:
    data = make(130)
    ordered = slots(data)
    for slot in ordered[:6]:  # 6 of 130 is below 5% of the fixed denominator
        data.funding[slot] = [funding_page(_candidate(data, slot), max_gap_ms=EIGHT + 1)]
    for slot in ordered[6:30]:
        del data.funding[slot]
    report = evaluate(data)
    assert report["m4"]["fail"] is False and report["m4"]["pass"] is False
    assert "incomplete_coverage" not in report["stop_codes"]
    assert report["outcome"] == "stage1b_incomplete"


def test_a_known_m5_failure_survives_pending_bars_and_a_stop() -> None:
    data = make(100)
    ordered = slots(data)
    for slot in ordered[:6]:
        c = _candidate(data, slot)
        data.mark_hourly[slot] = hourly(c, runs=[[c.entry_ms // HOUR * HOUR + 3 * HOUR, 1]])
    for slot in ordered[6:20]:
        del data.trade_hourly[slot]  # pending
    report = evaluate(data)
    assert report["m5"]["fail"] is True and report["m5"]["assessed"] is False
    assert "incomplete_coverage" in report["stop_codes"]
    data.stop = {"go": 2, "reason": "D5: HTTP 403"}
    assert evaluate(data)["outcome"] == "incomplete_coverage"


def test_m5_pending_or_unproven_episodes_never_invent_a_failure() -> None:
    data = make(100)
    ordered = slots(data)
    for slot in ordered[:5]:
        c = _candidate(data, slot)
        data.mark_hourly[slot] = hourly(c, runs=[[c.entry_ms // HOUR * HOUR + 3 * HOUR, 1]])
    for slot in ordered[5:25]:
        del data.trade_hourly[slot]
    report = evaluate(data)
    assert report["m5"]["fail"] is False and "incomplete_coverage" not in report["stop_codes"]
    unproven = make(100)
    for slot in slots(unproven)[:30]:
        unproven.mark_hourly[slot] = hourly(_candidate(unproven, slot), full=True)
    again = evaluate(unproven)
    assert again["m5"]["fail"] is False and again["outcome"] == "stage1b_incomplete"


def test_a_bad_episode_whose_funding_is_pending_may_still_leave_the_m5_denominator() -> None:
    """Counting it as a possible success is the optimistic bound, so no false failure."""
    data = make(100)
    ordered = slots(data)
    for slot in ordered[:8]:
        c = _candidate(data, slot)
        data.mark_hourly[slot] = hourly(c, runs=[[c.entry_ms // HOUR * HOUR + 3 * HOUR, 1]])
        del data.funding[slot]
    assert evaluate(data)["m5"]["fail"] is False
    for slot in ordered[:8]:
        data.funding[slot] = [funding_page(_candidate(data, slot), max_gap_ms=EIGHT + 1)]
    assert evaluate(data)["m5"]["fail"] is False  # all eight left through funding_uncovered


def test_verdict_module_names_m3_as_partial_and_wording_is_exact() -> None:
    report = evaluate(make(100))
    text = " ".join(report["partial_checks"])
    assert "absence of a contradiction" in text and "historical identity" in text
    assert v.STATEMENT.startswith("coverage level only")
