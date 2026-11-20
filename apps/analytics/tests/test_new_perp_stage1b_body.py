"""I25 Stage 1b body phase: contract, reducers, requests, sealed inputs, fetcher and runs.

Every test runs against a synthetic sealed metadata root and a mock Bybit (`new_perp_world`,
`new_perp_body_world`): no network, no holdout data. Each test names the defect it fails on.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import re
import zlib
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import new_perp_stage1 as s
from schurfer_analytics import new_perp_stage1b as meta
from schurfer_analytics import new_perp_stage1b_body as body
from schurfer_analytics import new_perp_stage1b_body_verdict as verdict
from schurfer_analytics.new_perp_window import HOLDOUT

from .new_perp_body_world import (
    Sealed,
    all_text,
    body_fetcher,
    contract_for,
    holdout_world,
    launch_at,
    request_numbers,
    run_both,
    sealed_row,
    slot_of,
    write_sealed,
)
from .new_perp_world import PRICE, RATE, Clock, Launch, World, ms, streamed

if TYPE_CHECKING:
    from pathlib import Path

CUT = HOLDOUT.cut_ms
DAY = s.DAY_MS
MIB = s.MIB
T_KNOWN = 2_323_690_559


# -- the contract and its numbers


def test_pins_are_the_public_record_and_never_a_sealed_file() -> None:
    """A changed pin would silently bind the body phase to a different metadata run."""
    assert dataclasses.asdict(body.PINS) == {
        "metadata_contract_sha256": (
            "31ae1a57970e7af98d2e52fd75534cf2604cd4a79bb889f25cd2f6a90fbada96"
        ),
        "metadata_report_sha256": (
            "173347ae6785dd2fe5a5bbbce7e6fdfc10b82db37d98908e7ba0fc6f64458352"
        ),
        "manifest_sha256": "f3098e0840be489df640fbdab66c464257bac3d6aa5315d58e70d86d594d1341",
        "catalog_slots": 254,
        "first_files": 247,
        "first_bytes": T_KNOWN,
    }
    assert body.PINS.metadata_contract_sha256 == meta.build_contract()["request_list_sha256"]


def test_budget_numbers_are_recomputed_from_the_formulas() -> None:
    """Every ceiling is a formula of the pins and the retry rules, not a typed number."""
    contract = body.build_contract()
    limits = contract["go_limits"]
    reserve, cap = s.READ_RESERVE_BYTES, s.PER_FILE_CAP_BYTES
    assert limits["1"]["max_attempts"] == 2 * 247 == 494
    assert limits["1"]["max_bytes"] == 5_185_208_320 == 4945 * MIB
    assert 2 * T_KNOWN + cap + reserve == 5_184_317_566 <= limits["1"]["max_bytes"]
    assert limits["1"]["max_bytes"] - (2 * T_KNOWN + cap + reserve) < MIB
    classes = {c["cls"]: c for c in contract["classes"]}
    assert [classes[k]["max_requests"] for k in ("D2", "D3", "D4", "D5", "D6")] == [
        247,
        247,
        247,
        247,
        988,
    ]
    assert [body.api_sends(n) for n in (247, 988)] == [309, 1235]
    assert [classes[k]["max_class_bytes"] // MIB for k in ("D3", "D4", "D5", "D6")] == [
        20,
        155,
        155,
        309,
    ]
    assert limits["2"]["max_attempts"] == 3 * 309 + 1235 == 2162
    assert limits["2"]["max_bytes"] == sum(
        classes[k]["max_class_bytes"] for k in classes if k != "D2"
    )
    assert limits["2"]["max_bytes"] == 639 * MIB
    # the worst case written in the document: three full sends of every file is not covered,
    # and the run then stops cleanly on the class cap instead of overshooting
    assert 3 * T_KNOWN + cap + reserve > limits["1"]["max_bytes"]
    assert classes["D2"]["go"] == 1
    assert {classes[k]["go"] for k in ("D3", "D4", "D5", "D6")} == {2}


def test_go_b_class_caps_cover_every_allowed_send_at_its_response_cap() -> None:
    """A class cap below its sends would hide a retry budget that cannot be spent."""
    for spec in body.class_specs().values():
        if spec.go == 2:
            sends = body.api_sends(spec.max_requests)
            assert spec.max_class_bytes >= sends * spec.max_response_bytes + s.READ_RESERVE_BYTES
            assert (
                spec.max_class_bytes - (sends * spec.max_response_bytes + s.READ_RESERVE_BYTES)
                < MIB
            )


def test_thresholds_are_the_protocol_numbers_and_not_tuned_by_the_holdout() -> None:
    """The holdout pass thresholds are the ones fixed in the protocol (section 4 and 8)."""
    t = body.build_contract()["thresholds"]
    assert t == s.THRESHOLDS
    assert (t["m1_ambiguous_max"], t["m1_readable_min"], t["lifecycle_unrebuildable_max"]) == (
        0.10,
        0.90,
        0.15,
    )
    assert (t["m2_no_file_near_launch_max"], t["m2_archive_only_max"]) == (0.05, 0.05)
    assert (t["m3_unverifiable_max"], t["m3_contradicting_max"]) == (0.20, 0.10)
    assert (t["m4_funding_uncovered_max"], t["m5_bars_min"], t["m6_feasible_min"]) == (
        0.05,
        0.95,
        0.90,
    )
    assert (t["min_episodes"], t["min_weeks"]) == (100, 26)
    assert body.build_contract()["stop_codes"] == list(s.STOP_CODES)


def test_contract_hashes_bind_pins_and_go_limits() -> None:
    """A changed pin or cap must change the approved hash."""
    base = body.build_contract()
    assert base["request_list_sha256"] == s.contract_hash(base)
    other = body.build_contract(replace(body.PINS, first_bytes=body.PINS.first_bytes + 1))
    assert other["request_list_sha256"] != base["request_list_sha256"]
    assert other["go_sha256"]["1"] != base["go_sha256"]["1"]
    assert other["go_sha256"]["2"] != base["go_sha256"]["2"]  # the pins are shared rules
    changed = json.loads(json.dumps(base))
    changed["go_limits"]["2"]["max_attempts"] += 1
    assert body.go_hash(changed, 2) != base["go_sha256"]["2"]
    assert body.go_hash(changed, 1) == base["go_sha256"]["1"]


def test_checked_in_contract_equals_build_contract() -> None:
    from pathlib import Path

    saved = json.loads(Path(body.CONTRACT_PATH).read_text())
    assert saved == body.build_contract()
    assert body.main(["write-contract", "--check"]) == 0


def test_cli_refuses_an_unapproved_hash_before_any_io(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approved hash"):
        body.main(
            [
                "fetch",
                "--go",
                "1",
                "--approved-sha256",
                "0" * 64,
                "--metadata-root",
                str(tmp_path / "m"),
                "--root",
                str(tmp_path / "r"),
            ]
        )
    assert not (tmp_path / "r").exists()
    good = body.build_contract()["go_sha256"]["2"]
    with pytest.raises(ValueError, match="queue-sha256"):
        body.main(
            [
                "fetch",
                "--go",
                "2",
                "--approved-sha256",
                good,
                "--metadata-root",
                str(tmp_path / "m"),
                "--root",
                str(tmp_path / "r"),
            ]
        )
    with pytest.raises(ValueError, match="disjoint"):
        body.main(
            [
                "fetch",
                "--go",
                "1",
                "--approved-sha256",
                body.build_contract()["go_sha256"]["1"],
                "--metadata-root",
                str(tmp_path / "m"),
                "--root",
                str(tmp_path / "m" / "inside"),
            ]
        )


# -- the clock, in nanoseconds


def test_timestamp_ns_keeps_the_fraction_and_rejects_everything_else() -> None:
    assert body.timestamp_ns(b"1704067200") == 1_704_067_200_000_000_000
    assert body.timestamp_ns(b"1704067200.123456789") == 1_704_067_200_123_456_789
    assert body.timestamp_ns(b"1704067200.5") == 1_704_067_200_500_000_000
    for bad in (b"", b"abc", b"1704067200.", b"-1704067200", b"1704067200.1234567890", b"17.0"):
        with pytest.raises(s.SchemaMismatchError, match="bad:timestamp"):
            body.timestamp_ns(bad)


def test_entry_clock_rounds_up_exactly_even_below_a_millisecond() -> None:
    """Truncating T0 to milliseconds first would move E a whole minute for a fraction."""
    boundary = ms("2023-06-01T10:00:00") * 1_000_000
    entry = ms("2023-06-08T10:00:00")
    assert body.entry_clock_ns(boundary) == (entry, entry + 7 * DAY)
    assert body.entry_clock_ns(boundary + 1)[0] == entry + s.MINUTE_MS
    sub_ms = boundary + 400_000  # 0.4 ms after the minute
    assert sub_ms // 1_000_000 == boundary // 1_000_000  # the truncated T0 is on the minute
    assert body.entry_clock_ns(sub_ms)[0] == entry + s.MINUTE_MS
    assert s.entry_clock(sub_ms // 1_000_000)[0] == entry  # what a truncated clock would give
    assert body.entry_clock_ns(boundary - 1)[0] == entry


def test_cuts_agree_with_the_metadata_window_for_whole_millisecond_stamps() -> None:
    """Same X and lookback rules as the metadata contract: only X > cut censors."""
    for offset in range(0, 570, 3):
        t0 = HOLDOUT.start_ms + offset * DAY + 17_250
        got = body.cuts_ns(t0 * 1_000_000)
        assert got == HOLDOUT.cuts(t0)
    t0 = CUT - 14 * DAY
    assert body.cuts_ns(t0 * 1_000_000)["exit_ms"] == CUT
    assert body.cuts_ns(t0 * 1_000_000)["window_censored"] is False
    assert body.cuts_ns((t0 + s.MINUTE_MS) * 1_000_000)["window_censored"] is True
    assert body.cuts_ns((t0 + 1) * 1_000_000)["window_censored"] is True  # E rounds up
    start = HOLDOUT.start_ms + 23 * DAY
    assert body.cuts_ns(start * 1_000_000)["lookback_censored"] is False
    assert body.cuts_ns((start - s.MINUTE_MS) * 1_000_000)["lookback_censored"] is True


def test_m1_class_boundaries_are_exact_in_nanoseconds() -> None:
    launch = ms("2023-06-01T10:00:00")
    base = launch * 1_000_000
    five, hour, day = 5 * 60_000_000_000, 3_600_000_000_000, 86_400_000_000_000
    assert body.t0_class_ns(base - five, launch) == "aligned"
    assert body.t0_class_ns(base - five - 1, launch) == "premarket_or_ambiguous"
    assert body.t0_class_ns(base + hour, launch) == "aligned"
    assert body.t0_class_ns(base + hour + 1, launch) == "delayed_open"
    assert body.t0_class_ns(base + day, launch) == "delayed_open"
    assert body.t0_class_ns(base + day + 1, launch) == "premarket_or_ambiguous"


# -- reducers


def trade_gz(rows: list[str], header: bool = True) -> bytes:
    lines = (["timestamp,symbol,side,size,price,tickDirection,trdMatchID"] if header else []) + rows
    return gzip.compress(("\n".join(lines) + "\n").encode())


def feed(data: bytes, step: int = 7) -> dict[str, Any]:
    reducer = body.FirstFileWindowReducer()
    for i in range(0, len(data), step):
        reducer.feed(data[i : i + step])
    return reducer.finish()


def row(stamp: str) -> str:
    return f"{stamp},SYMUSDT,Buy,1,{PRICE},PlusTick,{RATE}"


def test_first_file_reducer_keeps_column_one_only_and_a_minimum() -> None:
    """Prices, sizes and ids in columns 2+ must never reach a fact; T0 is the minimum."""
    data = trade_gz([row("1685613601.000100"), row("1685613600.123456789"), row("1685613602")])
    facts = feed(data)
    assert facts == {
        "rows": 3,
        "min_ns": 1_685_613_600_123_456_789,
        "max_ns": 1_685_613_602_000_000_000,
    }
    assert PRICE not in json.dumps(facts) and RATE not in json.dumps(facts)
    assert feed(trade_gz([row("1685613600.5")], header=False))["rows"] == 1


def test_first_file_reducer_enforces_the_holdout_window() -> None:
    """A row at or after the cut (or before the start) is a fatal schema stop, never recorded."""
    last_ok = (CUT - 1) // 1000
    assert feed(trade_gz([row(f"{last_ok}.999")]))["rows"] == 1
    for stamp, reason in (
        (f"{CUT // 1000}.000", "timestamp_after_cut"),
        (f"{CUT // 1000 + 1}", "timestamp_after_cut"),
        (f"{HOLDOUT.start_ms // 1000 - 1}.999", "timestamp_before_window"),
    ):
        with pytest.raises(s.SchemaMismatchError, match=reason):
            feed(trade_gz([row("1685613600"), row(stamp)]))


def test_first_file_reducer_fails_closed_on_bad_shapes() -> None:
    for blob, reason in (
        (b"not gzip at all", "not_gzip"),
        (gzip.compress(b"timestamp,x\n")[:-6], "truncated_gzip"),
        (trade_gz([]), "no_rows"),
        (trade_gz(["junk,1,2"], header=False), "bad:timestamp"),
    ):
        with pytest.raises(s.SchemaMismatchError, match=reason):
            feed(blob)


def kline_payload(symbol: str, starts: list[int], width: int = 7) -> dict[str, Any]:
    rows = [[str(t), *([PRICE] * (width - 1))] for t in starts]
    return {"retCode": 0, "result": {"symbol": symbol, "list": rows}}


def test_kline_reducer_drops_rows_from_the_cut_uncounted() -> None:
    """The discovery reducer cuts at 2025-10-01; the holdout cut is 2024-09-16."""
    last = CUT - s.HOUR_MS
    starts = [last - 2 * s.HOUR_MS, last - s.HOUR_MS, last, CUT, CUT + s.HOUR_MS]
    facts = body.reduce_kline_window(
        kline_payload("SYMUSDT", starts), "SYMUSDT", 7, s.HOUR_MS, 1000
    )
    assert facts["n"] == 3 and facts["newest_ms"] == last
    assert facts["full"] is False and PRICE not in json.dumps(facts)
    discovery = s.reduce_kline(kline_payload("SYMUSDT", starts), "SYMUSDT", 7, s.HOUR_MS, 1000)
    assert discovery["n"] == 5  # the discovery cut would have counted the rows from the cut
    # a page that is full by its raw row count may be cut by its limit, even if rows were dropped
    full = body.reduce_kline_window(kline_payload("SYMUSDT", starts), "SYMUSDT", 7, s.HOUR_MS, 5)
    assert full["full"] is True and full["n"] == 3
    with pytest.raises(s.SchemaMismatchError, match="symbol"):
        body.reduce_kline_window(kline_payload("OTHERUSDT", starts), "SYMUSDT", 7, s.HOUR_MS, 5)


def funding_payload(symbol: str, stamps: list[int]) -> dict[str, Any]:
    items = [
        {"symbol": symbol, "fundingRate": RATE, "fundingRateTimestamp": str(t)} for t in stamps
    ]
    return {"retCode": 0, "result": {"list": items}}


def test_funding_reducer_drops_rows_from_the_cut_and_never_keeps_a_rate() -> None:
    eight = 8 * s.HOUR_MS
    stamps = [CUT - 2 * eight, CUT - eight, CUT, CUT + eight]
    facts = body.reduce_funding_window(funding_payload("SYMUSDT", stamps), "SYMUSDT")
    assert facts["n"] == 2 and facts["newest_ms"] == CUT - eight and facts["max_gap_ms"] == eight
    assert RATE not in json.dumps(facts)
    assert s.reduce_funding(funding_payload("SYMUSDT", stamps), "SYMUSDT")["n"] == 4


# -- requests


def make_request(cls: str = "D4", **change: Any) -> body.BodyRequest:
    slot = slot_of("T001USDT")
    start, end = HOLDOUT.start_ms + 5 * DAY, CUT - 1
    spec: dict[str, Any] = {
        "D2": dict(
            url=s.first_file_url("T001USDT", "2023-04-01"), params=(), meta=(("slot", slot),)
        ),
        "D3": dict(
            url=s.KLINE_URL,
            params=body._kline_params("T001USDT", "1", start, start, 1),
            meta=(("slot", slot),),
        ),
        "D4": dict(
            url=s.MARK_URL,
            params=body._kline_params("T001USDT", "60", start, end, 1000),
            meta=(("slot", slot),),
        ),
        "D6": dict(
            url=s.FUNDING_URL,
            params=(
                ("category", "linear"),
                ("symbol", "T001USDT"),
                ("startTime", str(start)),
                ("endTime", str(end)),
                ("limit", "200"),
            ),
            meta=(("slot", slot), ("page", "1")),
        ),
    }[cls]
    spec.update(change)
    return body.BodyRequest(cls, "GET", spec.pop("url"), spec.pop("params"), meta=spec.pop("meta"))


def test_requests_outside_the_window_or_the_contract_cannot_be_built() -> None:
    """The cut is enforced where a request is made, not only where it is planned."""
    for cls in ("D2", "D3", "D4", "D6"):
        make_request(cls)  # the valid forms exist
    slot = slot_of("T001USDT")
    cut_day = HOLDOUT.cut_day
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D2", url=s.first_file_url("T001USDT", cut_day))
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D2", url=s.first_file_url("T001USDT", "2023-01-31"))
    ends = body._kline_params("T001USDT", "60", HOLDOUT.start_ms + DAY, CUT, 1000)
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D4", params=ends)  # end == cut
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D3", params=body._kline_params("T001USDT", "1", CUT, CUT, 1))
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D3", params=body._kline_params("T001USDT", "1", CUT - 1, CUT - 1, 1))
    early = body._kline_params("T001USDT", "60", HOLDOUT.start_ms - 1, CUT - 1, 1000)
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D4", params=early)
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D6", meta=(("slot", slot), ("page", "5")))
    with pytest.raises(ValueError, match="outside the body contract"):
        body.BodyRequest(
            "D2",
            "GET",
            s.first_file_url("T001USDT", "2023-04-01"),
            symbol="T001USDT",
            meta=(("slot", slot),),
        )
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D4", meta=(("slot", "not-a-slot"),))
    with pytest.raises(ValueError, match="outside the body contract"):
        body.BodyRequest("D1", "GET", s.ARCHIVE_ROOT, meta=(("slot", slot),))
    with pytest.raises(ValueError, match="outside the body contract"):
        body.BodyRequest(
            "D2", "HEAD", s.first_file_url("T001USDT", "2023-04-01"), meta=(("slot", slot),)
        )
    with pytest.raises(ValueError, match="outside the body contract"):
        make_request("D4", url="https://api.bybit.com/v5/market/other")


def test_request_keys_never_hash_a_symbol_and_the_repr_hides_it() -> None:
    a, b = make_request("D4"), make_request("D4")
    assert a.key == b.key
    params = body._kline_params("OTHERUSDT", "60", HOLDOUT.start_ms + 5 * DAY, CUT - 1, 1000)
    renamed = make_request("D4", params=params)
    assert renamed.key == a.key  # the key is of the opaque slot, not of the ticker
    later = make_request(
        "D4", params=body._kline_params("T001USDT", "60", HOLDOUT.start_ms + 6 * DAY, CUT - 1, 1000)
    )
    assert later.key != a.key
    assert "USDT" not in repr(a) and "USDT" not in str(a)
    assert make_request("D6").key != make_request("D6", meta=(("slot", a.slot), ("page", "2"))).key


# -- sealed inputs


def small_world(count: int = 6) -> World:
    return holdout_world(count)


def test_load_sealed_accepts_only_the_exact_finished_metadata_root(tmp_path: Path) -> None:
    sealed = write_sealed(tmp_path, small_world())
    assert len(sealed.sealed.rows) == 6
    root, pins = sealed.root, sealed.pins
    body.load_sealed(root, pins)
    with pytest.raises(ValueError, match="differs from the public record"):
        body.load_sealed(root, replace(pins, manifest_sha256="0" * 64))
    with pytest.raises(ValueError, match="differs from the public record"):
        body.load_sealed(root, replace(pins, metadata_report_sha256="0" * 64))
    with pytest.raises(ValueError, match="differs from the public record"):
        body.load_sealed(root, replace(pins, metadata_contract_sha256="d" * 64))
    with pytest.raises(ValueError, match="differs from the public record"):
        # an incomplete or unassessed metadata outcome is not an input of the body phase
        bad = write_sealed(
            tmp_path,
            small_world(),
            name="m2",
            tweak_report={"outcome": "metadata_complete_unassessed"},
        )
        body.load_sealed(bad.root, bad.pins)
    with pytest.raises(ValueError, match="differs from the public record"):
        stopped = write_sealed(tmp_path, small_world(), name="m3")
        (stopped.root / "stop.json").write_text("{}")
        body.load_sealed(stopped.root, stopped.pins)
    with pytest.raises(ValueError, match="differs from the public record"):
        open_phase = write_sealed(
            tmp_path, small_world(), name="m4", tweak_report={"body_phase_authorized": True}
        )
        body.load_sealed(open_phase.root, open_phase.pins)


def test_load_sealed_detects_changed_extra_and_missing_files(tmp_path: Path) -> None:
    sealed = write_sealed(tmp_path, small_world())
    first = next((sealed.root / "catalog").glob("*.json"))
    original = first.read_text()
    first.write_text(original.replace("csv_pending", "no_archive"))
    with pytest.raises(ValueError, match="evidence differs"):
        body.load_sealed(sealed.root, sealed.pins)
    first.write_text(original)
    (sealed.root / "catalog" / ("e" * 64 + ".json")).write_text("{}")
    with pytest.raises(ValueError, match="file set differs"):
        body.load_sealed(sealed.root, sealed.pins)
    (sealed.root / "catalog" / ("e" * 64 + ".json")).unlink()
    first.unlink()
    with pytest.raises(ValueError, match="file set differs"):
        body.load_sealed(sealed.root, sealed.pins)


def test_preflight_binds_the_queue_to_the_public_record(tmp_path: Path) -> None:
    """Count, bytes and the per-file cap are checked before a single body byte is read."""
    world = small_world()
    sealed = write_sealed(tmp_path, world, sizes={"T000USDT": 10_000, "T001USDT": 20_000})
    plan = body.preflight(sealed.sealed.rows, sealed.pins)
    assert plan["ok"] and plan["files"] == 6 and plan["bytes"] == 10_000 + 20_000 + 4 * 4096
    assert plan["matches_public_record"] and plan["within_cap"] and plan["over_per_file_cap"] == 0
    wrong = replace(sealed.pins, first_bytes=sealed.pins.first_bytes + 1)
    assert body.preflight(sealed.sealed.rows, wrong)["ok"] is False
    assert body.preflight(sealed.sealed.rows, replace(sealed.pins, first_files=7))["ok"] is False
    assert body.preflight(sealed.sealed.rows, replace(sealed.pins, catalog_slots=7))["ok"] is False
    big = write_sealed(tmp_path, world, name="big", sizes={"T000USDT": s.PER_FILE_CAP_BYTES + 1})
    over = body.preflight(big.sealed.rows, big.pins)
    assert over["over_per_file_cap"] == 1 and over["ok"] is False
    edge = write_sealed(tmp_path, world, name="edge", sizes={"T000USDT": s.PER_FILE_CAP_BYTES})
    assert body.preflight(edge.sealed.rows, edge.pins)["ok"] is True
    rows = {slot: dict(r) for slot, r in sealed.sealed.rows.items()}
    next(iter(rows.values()))["head"] = {
        "key": "0" * 64,
        "outcome": "absent",
        "content_length": None,
    }
    missing = body.preflight(rows, sealed.pins)
    assert missing["no_size"] == 1 and missing["ok"] is False


def test_a_failed_preflight_sends_nothing_and_seals_the_root(tmp_path: Path) -> None:
    world = small_world()
    sealed = write_sealed(tmp_path, world, sizes={"T000USDT": s.PER_FILE_CAP_BYTES + 1})
    fetcher, _ = body_fetcher(world, tmp_path / "body", sealed)
    with pytest.raises(s.FetchStoppedError, match="preflight_failed"):
        body.run_a(fetcher, sealed.sealed, sealed.pins)
    assert world.requests == []
    assert json.loads((tmp_path / "body/stop.json").read_text())["reason"] == "preflight_failed"
    with pytest.raises(s.FetchStoppedError, match="stopped"):
        body_fetcher(world, tmp_path / "body", sealed)


# -- Go A


def receipts(root: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((root / "receipts").glob("*.json"))]


def test_go_a_requests_only_sealed_first_files_in_slot_order(tmp_path: Path) -> None:
    world = small_world()
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    fetcher, _ = body_fetcher(world, root, sealed)
    report = body.run_a(fetcher, sealed.sealed, sealed.pins)
    paths = [r.url.path for r in world.requests]
    assert len(paths) == 6 and all(
        re.fullmatch(r"/trading/T\d{3}USDT/T\d{3}USDT\d{4}-\d{2}-\d{2}\.csv\.gz", p) for p in paths
    )
    assert all(r.method == "GET" and r.url.host == "public.bybit.com" for r in world.requests)
    assert all(r.headers["accept-encoding"] == "identity" for r in world.requests)
    order = [r["meta"]["slot"] for r in receipts(root)]
    assert order == sorted(order) and set(order) == set(sealed.sealed.rows)
    assert report["phase"] == "a" and report["launches"] == 6 and report["n_primary"] is None
    assert report["requests"]["by_class"] == {"D2": 6}
    # a six-launch world cannot reach the floors, so Go B is refused by definitive stop codes
    assert report["go_b_allowed"] is False
    assert report["stop_codes"][0] in ("identity_unresolved", "insufficient_events")
    assert not (root / "queue_b.json").exists()
    assert json.loads((root / "go_a_done.json").read_text()) == {"go": 1, "go_b_allowed": False}


def test_receipts_hold_no_symbol_url_body_hash_or_value(tmp_path: Path) -> None:
    world = small_world()
    sealed = write_sealed(tmp_path, world)
    fetcher, _ = body_fetcher(world, tmp_path / "body", sealed)
    body.run_a(fetcher, sealed.sealed, sealed.pins)
    text = all_text(tmp_path / "body")
    for launch in world.launches:
        assert launch.symbol not in text
    assert "USDT" not in text and PRICE not in text and RATE not in text
    for r in receipts(tmp_path / "body"):
        assert r["symbol"] == "" and "body_sha256" not in r and set(r["meta"]) == {"slot"}
        assert set(r["facts"]) == {"rows", "min_ns", "max_ns"}
    for p in (tmp_path / "body/attempts").glob("*.json"):
        assert json.loads(p.read_text())["symbol"] == ""


def test_t0_ambiguous_launch_gets_no_request_after_go_a(tmp_path: Path) -> None:
    """An ambiguous T0 is never re-anchored and never asked for a bar, mark or funding row."""
    ambiguous = launch_at("TAMBUSDT", "2023-04-01T10:00:00", delay_min=30 * 60)  # 30 h later
    world = World([launch_at("TOKUSDT", "2023-04-01T10:00:00"), ambiguous])
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    fetcher, _ = body_fetcher(world, root, sealed)
    body.run_a(fetcher, sealed.sealed, sealed.pins)
    data = verdict.load_run(root, sealed.sealed)
    queued = {slot for slot, r in sealed.sealed.rows.items() if body.has_size(r)}
    state = body.t0_state(
        sealed.sealed.rows[sealed.slots["TAMBUSDT"]], data.firsts[sealed.slots["TAMBUSDT"]], True
    )
    assert state == (None, "premarket_or_ambiguous")
    cands = body.entry_candidates(sealed.sealed.rows, data.firsts, queued)
    assert [c.slot for c in cands] == [sealed.slots["TOKUSDT"]]
    requests = body.bar_requests(cands, sealed.sealed.rows)
    assert all(r.slot == sealed.slots["TOKUSDT"] for r in requests)


def test_metadata_ambiguous_and_unreadable_launches_are_never_requested(tmp_path: Path) -> None:
    world = small_world(3)
    rows = {
        "T000USDT": sealed_row(world.launches[0], t0_status="premarket_or_ambiguous"),
    }
    rows["T000USDT"].pop("first_file_day")
    rows["T000USDT"].pop("head")
    rows["T001USDT"] = {
        "launch_ms": world.launches[1].launch_ms,
        "t0_status": "no_archive",
        "reason": "t0_unavailable",
        "listing_state": "absent",
        "first_file_within_1d_of_launch": False,
        "filename_identity_unverified": False,
        "catalog_identity": "not_contradicted",
    }
    sealed = write_sealed(tmp_path, world, rows=rows)
    fetcher, _ = body_fetcher(world, tmp_path / "body", sealed)
    body.run_a(fetcher, sealed.sealed, sealed.pins)
    assert [r.url.path.split("/")[2] for r in world.requests] == ["T002USDT"]


def test_go_a_http_outcomes(tmp_path: Path) -> None:
    """404 is a result, 503 is retried three times, 403 and a bad body seal the root."""
    world = small_world(3)
    sealed = write_sealed(tmp_path, world)
    world.fail["T000USDT"] = [503, 503, 503]
    world.fail["T001USDT"] = [429]
    world.retry_after = 7
    root = tmp_path / "body"
    fetcher, clock = body_fetcher(world, root, sealed)
    body.run_a(fetcher, sealed.sealed, sealed.pins)
    outcomes = {r["meta"]["slot"]: r["outcome"] for r in fetcher.final.values()}
    assert outcomes[sealed.slots["T000USDT"]] == "unavailable"
    assert outcomes[sealed.slots["T001USDT"]] == "ok" and outcomes[sealed.slots["T002USDT"]] == "ok"
    assert 7 in clock.sleeps and max(clock.sleeps) >= 10
    assert fetcher.spacing > s.SPACING_S  # an HTTP 429 doubles the spacing
    report = verdict.load_run(root, sealed.sealed)
    states = {
        slot: body.t0_state(sealed.sealed.rows[slot], report.firsts.get(slot), True)[1]
        for slot in sealed.sealed.rows
    }
    assert states[sealed.slots["T000USDT"]] == "t0_unreadable"


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, "HTTP 403"), (418, "schema mismatch")],
)
def test_go_a_fatal_status_seals_the_root(tmp_path: Path, status: int, reason: str) -> None:
    world = small_world(3)
    sealed = write_sealed(tmp_path, world)
    world.fail["T001USDT"] = [status]
    root = tmp_path / "body"
    fetcher, _ = body_fetcher(world, root, sealed)
    with pytest.raises(s.FetchStoppedError):
        body.run_a(fetcher, sealed.sealed, sealed.pins)
    stop = json.loads((root / "stop.json").read_text())
    assert reason in stop["reason"] and "T001" not in json.dumps(stop)
    count = len(world.requests)
    with pytest.raises(s.FetchStoppedError, match="stopped"):
        body_fetcher(world, root, sealed)
    assert len(world.requests) == count  # a sealed root sends nothing more


def test_go_a_file_from_the_cut_or_a_bad_gzip_stops_with_a_fixed_reason(tmp_path: Path) -> None:
    for blob, reason in (
        (trade_gz([row(f"{CUT // 1000}.5")]), "timestamp_after_cut"),
        (b"plainly not gzip", "not_gzip"),
        (gzip.compress(b"timestamp\n1685613600.1,x\n")[:-4], "truncated_gzip"),
    ):
        world = small_world(2)
        sealed = write_sealed(tmp_path, world, name=f"m{reason}")
        world.raw["T000USDT"] = blob
        fetcher, _ = body_fetcher(world, tmp_path / f"b{reason}", sealed)
        with pytest.raises(s.FetchStoppedError):
            body.run_a(fetcher, sealed.sealed, sealed.pins)
        stop = json.loads((tmp_path / f"b{reason}/stop.json").read_text())["reason"]
        assert stop == f"D2: schema mismatch ({reason})"
        assert str(CUT // 1000) not in stop


def scripted(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_go_a_byte_rules_declared_length_chunks_encoding_and_class_cap(tmp_path: Path) -> None:
    cap = s.PER_FILE_CAP_BYTES
    good = trade_gz([row("1685613600")])
    cases = {
        "declared": lambda: streamed(200, content=b"x", headers={"content-length": str(cap + 1)}),
        "encoded": lambda: streamed(200, content=good, headers={"content-encoding": "gzip"}),
    }
    for name, make in cases.items():
        world = small_world(1)
        sealed = write_sealed(tmp_path, world, name=f"m{name}")
        client = scripted(lambda request, make=make: make())
        fetcher, _ = body_fetcher(world, tmp_path / f"b{name}", sealed, client=client)
        if name == "declared":
            body.run_a(fetcher, sealed.sealed, sealed.pins)
            result = next(iter(fetcher.final.values()))
            assert result["outcome"] == "too_large" and result["body_bytes"] == 0
        else:
            with pytest.raises(s.FetchStoppedError):
                body.run_a(fetcher, sealed.sealed, sealed.pins)
            assert (
                "content_encoding"
                in json.loads((tmp_path / f"b{name}/stop.json").read_text())["reason"]
            )


def test_go_a_transport_chunk_over_the_reserve_stops_the_run(tmp_path: Path) -> None:
    world = small_world(1)
    sealed = write_sealed(tmp_path, world)
    big = trade_gz([row("1685613600")])
    client = scripted(
        lambda request: httpx.Response(
            200, stream=httpx.ByteStream(big + b"\0" * (s.READ_RESERVE_BYTES + 1))
        )
    )
    fetcher, _ = body_fetcher(world, tmp_path / "body", sealed, client=client)
    with pytest.raises(s.FetchStoppedError, match="larger than the read reserve"):
        body.run_a(fetcher, sealed.sealed, sealed.pins)


def test_go_a_stops_on_the_attempt_cap_and_on_the_class_byte_cap(tmp_path: Path) -> None:
    world = small_world(4)
    sealed = write_sealed(tmp_path, world)
    contract = contract_for(sealed.pins, go1={"max_attempts": 2})
    fetcher, _ = body_fetcher(world, tmp_path / "attempts", sealed, contract=contract)
    with pytest.raises(s.BudgetExceededError, match="attempt cap"):
        body.run_a(fetcher, sealed.sealed, sealed.pins)
    assert len(world.requests) == 2
    assert "attempt cap" in json.loads((tmp_path / "attempts/stop.json").read_text())["reason"]
    # the class byte cap: used + response cap + reserve must fit before a request starts
    world2 = small_world(4)
    sealed2 = write_sealed(tmp_path, world2, name="m2")
    specs = body.class_specs(sealed2.pins)
    tight = replace(specs["D2"], max_class_bytes=s.PER_FILE_CAP_BYTES + s.READ_RESERVE_BYTES + 100)
    contract2 = body.build_contract(sealed2.pins)
    contract2["classes"][0]["max_class_bytes"] = tight.max_class_bytes
    contract2["go_limits"]["1"]["max_bytes"] = 10 * 1024**3
    contract2["request_list_sha256"] = s.contract_hash(contract2)
    clock = Clock()
    root = tmp_path / "bytes"
    root.mkdir(mode=0o700)
    fetcher2 = body.BodyFetcher(
        root,
        contract2,
        httpx.Client(transport=world2.transport()),
        go=1,
        code={"synthetic": True},
        specs={**specs, "D2": tight},
        now=clock.now,
        sleep=clock.sleep,
        wall=clock.wall,
    )
    with pytest.raises(s.BudgetExceededError, match="class byte cap"):
        body.run_a(fetcher2, sealed2.sealed, sealed2.pins)
    assert 1 <= len(world2.requests) < 4


def test_a_crash_resumes_in_the_same_root_under_the_same_binding_only(tmp_path: Path) -> None:
    world = small_world(3)
    # sealed sizes are numbers, not bytes: they size the class cap so that a crashed attempt,
    # charged its whole reserve, leaves room to resume
    sealed = write_sealed(tmp_path, world, sizes={f"T00{i}USDT": 200 * MIB for i in range(3)})
    root = tmp_path / "body"
    world.crash = ["T001USDT"]
    fetcher, _ = body_fetcher(world, root, sealed)
    with pytest.raises(s.FetchStoppedError, match="unexpected_error"):
        body.run_a(fetcher, sealed.sealed, sealed.pins)
    # an unexpected error is sealed and leaves no trace of the exception text
    assert json.loads((root / "stop.json").read_text())["reason"] == "unexpected_error"
    # a hard crash (no stop file) resumes under the same code and refuses another binding
    (root / "stop.json").unlink()
    (root / "go_a_report.json").unlink()  # a real hard crash writes neither file
    again, _ = body_fetcher(world, root, sealed)
    body.run_a(again, sealed.sealed, sealed.pins)
    assert {r["outcome"] for r in again.final.values()} == {"ok"}
    with pytest.raises(ValueError, match="run binding differs"):
        body_fetcher(world, root, sealed, code={"synthetic": False})


def test_a_fatal_receipt_without_a_stop_file_still_stops_a_recovering_fetcher(
    tmp_path: Path,
) -> None:
    world = small_world(2)
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    world.fail["T000USDT"] = [403]
    fetcher, _ = body_fetcher(world, root, sealed)
    with pytest.raises(s.FetchStoppedError):
        body.run_a(fetcher, sealed.sealed, sealed.pins)
    (root / "stop.json").unlink()
    with pytest.raises(s.FetchStoppedError, match="recovered from its receipt"):
        body_fetcher(world, root, sealed)
    assert (root / "stop.json").exists()


def test_go_b_needs_a_finished_go_a_and_the_approved_queue_hash(tmp_path: Path) -> None:
    world = small_world(3)
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    b, _ = body_fetcher(world, root, sealed, go=2)
    with pytest.raises(s.FetchStoppedError, match="finished Go A"):
        body.run_b(b, sealed.sealed, "0" * 64)
    assert world.requests == []


# -- the full two-go run on a synthetic holdout


def edge_launches() -> list[Launch]:
    """Launches that pin the clock rules at both window ends, with first trade at T0."""
    return [
        # T0 = cut - 14 d exactly: E on a minute, X == cut, a candidate (only X > cut censors)
        launch_at("XEQCUTUSDT", "2024-09-01T23:50:00", first_file_offset_days=1),
        # T0 one minute later: X = cut + 1 minute, window censored
        launch_at("XOVERUSDT", "2024-09-01T23:51:00", first_file_offset_days=1),
        # T0 = 2023-02-24T00:00: E - 30 d == window start exactly, not censored
        launch_at("LBOKUSDT", "2023-02-23T23:50:00", first_file_offset_days=1),
        # T0 = 2023-02-23T23:59: E - 30 d is a minute before the start, lookback censored
        launch_at("LBCUTUSDT", "2023-02-23T23:49:00"),
    ]


def full_world() -> tuple[World, dict[str, dict[str, Any]]]:
    world = holdout_world(130)
    by = {launch.symbol: launch for launch in world.launches}
    by["T010USDT"].funding_mode = "hole"  # funding_uncovered
    by["T020USDT"].entry_bar = False  # entry_unavailable
    by["T030USDT"].hourly_gap_hours = (100,)  # interior gap in both hourly series
    by["T040USDT"].first_trade_delay_ms = 30 * 3_600_000  # first trade a day after the file day
    world.launches.extend(edge_launches())
    rows = {
        "T050USDT": sealed_row(by["T050USDT"], catalog_identity="relist"),
        "T051USDT": sealed_row(by["T051USDT"], ticker_reuse_suspected=True),
    }
    return world, rows


@pytest.fixture(scope="module")
def full(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    tmp = tmp_path_factory.mktemp("full")
    world, rows = full_world()
    sealed = write_sealed(tmp, world, rows=rows)
    report_a, report, root = run_both(world, tmp, sealed)
    return {"world": world, "sealed": sealed, "a": report_a, "report": report, "root": root}


def test_full_run_reasons_add_up_and_every_edge_is_classified(full: dict[str, Any]) -> None:
    report = full["report"]
    assert report["launches"] == 134
    assert report["reasons"] == {
        "t0_unavailable": 0,
        "t0_ambiguous": 1,
        "identity_excluded": 2,
        "window_censored": 1,
        "lookback_censored": 1,
        "funding_uncovered": 1,
        "entry_unavailable": 1,
        "primary": 127,
    }
    assert sum(report["reasons"].values()) == report["launches"]
    assert report["n_primary"] == 127 and report["iso_weeks"] >= 26
    assert report["outcome"] == "stage1b_pass_partial" and report["stop_codes"] == []
    assert report["not_assessed"] == [] and report["phase"] == "final"
    assert "economic" in report["statement"]


def test_full_run_checks_m1_to_m6_from_receipts(full: dict[str, Any]) -> None:
    r = full["report"]
    assert r["m1"]["classes"] == {"aligned": 133, "premarket_or_ambiguous": 1}
    assert r["m1"]["pass"] and r["m1"]["shares"]["ambiguous"] == 1 / 134
    assert r["m2"]["counts"]["no_file_near_launch"] == 0 and r["m2"]["pass"]
    assert r["m2"]["counts"]["path_days_candidates"] == 129 and r["m2"]["metadata_report_agrees"]
    assert r["m3"]["survivors"] == 133 and r["m3"]["statuses"]["relist"] == 1
    assert (
        r["m3"]["statuses"]["ticker_reuse_suspected"] == 1 and r["m3"]["episodes_remaining"] == 131
    )
    assert r["m4"]["eligible"] == 129 and r["m4"]["funding_uncovered"] == 1
    assert r["m4"]["status"] == "partial_pass" and r["m4"]["by_reason"] == {"gap_inside": 1}
    assert r["m4"]["covered_by_interval_snapshot"] == {
        "snapshot_is_8h": 128,
        "snapshot_shorter_or_unknown": 0,
    }
    assert r["m5"]["episodes"] == 128 and r["m5"]["with_bars"] == 126
    assert r["m5"]["states"]["entry"] == {"absent": 1, "present": 127}
    assert r["m5"]["states"]["mark"] == {"covered": 127, "interior_gap": 1}
    assert r["m5"]["minute_level_path"] == "not_assessed_at_stage_1b"
    assert r["m6"]["counts"] == {"feasible": 128, "infeasible": 0, "unknown": 0}
    assert any("minute-level" in x for x in r["partial_checks"])
    assert any("current catalog" in x for x in r["partial_checks"])


def test_full_run_request_counts_and_ceilings(full: dict[str, Any]) -> None:
    a, report, world = full["a"], full["report"], full["world"]
    assert a["requests"]["by_class"] == {"D2": 134}
    assert a["queue_b"] == {
        "candidates": 129,
        "d3_requests": 129,
        "d4_requests": 129,
        "d5_requests": 129,
        "d6_first_pages": 129,
        "d6_worst_case_pages": 516,
        "within_ceiling": True,
    }
    by = report["requests"]["by_class"]
    assert by == {"D2": 134, "D3": 129, "D4": 129, "D5": 129, "D6": 129}
    assert report["requests"]["attempts"] == 134 + 4 * 129
    limits = body.build_contract(full["sealed"].pins)["go_limits"]
    assert limits["1"]["max_attempts"] >= 134 and limits["2"]["max_attempts"] >= 4 * 129
    assert len(world.requests) == 134 + 4 * 129
    # the ambiguous launch was fetched once, in Go A, and never again
    amb = [r for r in world.requests if "T040USDT" in str(r.url)]
    assert len(amb) == 1 and amb[0].url.host == "public.bybit.com"
    assert not [
        r for r in world.requests if "T050USDT" in str(r.url) and r.url.host == "api.bybit.com"
    ]
    assert not [
        r for r in world.requests if "XOVERUSDT" in str(r.url) and r.url.host == "api.bybit.com"
    ]
    assert not [
        r for r in world.requests if "LBCUTUSDT" in str(r.url) and r.url.host == "api.bybit.com"
    ]


def test_nothing_from_the_cut_is_requested_or_recorded(full: dict[str, Any]) -> None:
    world: World = full["world"]
    stamps = request_numbers(world)
    assert stamps and max(stamps) <= CUT - 1 and min(stamps) >= HOLDOUT.start_ms
    # the launch whose exit is exactly the cut got bars; its series end is clipped to cut - 1 ms
    eq = [r for r in world.requests if "XEQCUTUSDT" in str(r.url) and r.url.host == "api.bybit.com"]
    assert len(eq) == 4
    assert max(int(r.url.params["end"]) for r in eq if "end" in r.url.params) == CUT - 1
    assert str(CUT) not in all_text(full["root"])


def test_full_run_writes_no_symbol_value_url_or_slot_into_the_public_report(
    full: dict[str, Any],
) -> None:
    text = json.dumps(full["report"]) + json.dumps(full["a"])
    for launch in full["world"].launches:
        assert launch.symbol not in text
    assert "USDT" not in text and PRICE not in text and RATE not in text
    assert not any(slot in text for slot in full["sealed"].slots.values())
    assert re.search(r"\b1[67]\d{11}\b", text) is None  # no millisecond stamp
    assert re.search(r"\b1[67]\d{8}\b", text) is None  # no second stamp
    root_text = all_text(full["root"])
    for launch in full["world"].launches:
        assert launch.symbol not in root_text
    assert PRICE not in root_text and RATE not in root_text and "USDT" not in root_text


def test_the_two_reports_are_published_once_in_the_sealed_root(full: dict[str, Any]) -> None:
    root: Path = full["root"]
    assert json.loads((root / "body_report.json").read_text()) == full["report"]
    assert (root / "go_a_report.json").exists() and (root / "go_b_done.json").exists()
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert body.queue_b_sha256(root) == full["a"]["queue_b_sha256"]
    assert full["report"]["provenance"]["manifest_sha256"] == body.PINS.manifest_sha256


def test_go_b_refuses_a_queue_hash_it_was_not_given(full: dict[str, Any], tmp_path: Path) -> None:
    world, rows = full_world()
    sealed = write_sealed(tmp_path, world, rows=rows)
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed)
    report = body.run_a(a, sealed.sealed, sealed.pins)
    assert report["go_b_allowed"] is True and report["outcome"] == "stage1b_a_complete"
    count = len(world.requests)
    b, _ = body_fetcher(world, root, sealed, go=2)
    with pytest.raises(s.FetchStoppedError, match="not the one that was approved"):
        body.run_b(b, sealed.sealed, "1" * 64)
    assert len(world.requests) == count
    # a derived queue that differs from the approved one is sealed
    queue = json.loads((root / "queue_b.json").read_text())
    (root / "queue_b.json").write_text(json.dumps({**queue, "keys_sha256": "2" * 64}))
    with pytest.raises(s.FetchStoppedError, match="queue_b_differs"):
        body.run_b(b, sealed.sealed, body.queue_b_sha256(root))
    assert len(world.requests) == count
    assert json.loads((root / "stop.json").read_text())["reason"] == "queue_b_differs"


def test_go_a_stops_the_stage_when_a_definitive_check_fails(tmp_path: Path) -> None:
    """More than 10% ambiguous launches is `not_reconstructible`: Go B is refused, no request."""
    world = holdout_world(130)
    for launch in world.launches[:20]:
        launch.first_trade_delay_ms = 30 * 3_600_000
    sealed = write_sealed(tmp_path, world)
    report_a, final, root = run_both(world, tmp_path, sealed)
    assert report_a["outcome"] == "not_reconstructible" and report_a["go_b_allowed"] is False
    assert report_a["m1"]["fail"] and report_a["stop_codes"][0] == "not_reconstructible"
    assert final is report_a and not (root / "queue_b.json").exists()
    count = len(world.requests)
    b, _ = body_fetcher(world, root, sealed, go=2)
    with pytest.raises(s.FetchStoppedError, match="finished Go A"):
        body.run_b(b, sealed.sealed, "0" * 64)
    assert len(world.requests) == count == 130


def test_a_stopped_go_a_reports_a_stop_not_a_pass_and_keeps_known_failures(tmp_path: Path) -> None:
    world = holdout_world(130)
    sealed = write_sealed(tmp_path, world)
    world.fail["/trading/"] = [403]  # the first request is refused
    fetcher, _ = body_fetcher(world, tmp_path / "body", sealed)
    with pytest.raises(s.FetchStoppedError):
        body.run_a(fetcher, sealed.sealed, sealed.pins)
    data = verdict.from_fetcher(fetcher, sealed.sealed)
    report = verdict.evaluate(data, "a")
    assert report["outcome"] == "stage1b_stopped" and report["m1"]["pending"] == 130
    assert "m1_not_run" in report["not_assessed"] and report["m1"]["pass"] is False
    assert "insufficient_events" not in report["stop_codes"]  # pending launches are not known
    assert report["go_b_allowed"] is False
    assert report["stop"]["reason"] == "D2: HTTP 403"


# -- Go B transport and error envelopes


def go_b_fixture(tmp_path: Path, world: World | None = None) -> tuple[Any, Any, Any, Sealed]:
    world = world or holdout_world(3)
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed)
    body.run_a(a, sealed.sealed, sealed.pins)
    firsts = body.results_by_slot(a, "D2")
    queued = {slot for slot, r in sealed.sealed.rows.items() if body.has_size(r)}
    cands = body.entry_candidates(sealed.sealed.rows, firsts, queued)
    requests = body.bar_requests(cands, sealed.sealed.rows)
    b, clock = body_fetcher(world, root, sealed, go=2)
    return b, clock, requests, sealed


def d4_request(requests: list[body.BodyRequest]) -> body.BodyRequest:
    return next(r for r in requests if r.cls == "D4")


def reducer_for(request: body.BodyRequest) -> Any:
    return body.kline_reducer(dict(request.params)["symbol"], 5, s.HOUR_MS, 1000)


def reducer_any(request: body.BodyRequest) -> Any:
    width, step, limit = {"D3": (7, s.MINUTE_MS, 1), "D4": (5, s.HOUR_MS, 1000)}.get(
        request.cls, (7, s.HOUR_MS, 1000)
    )
    return body.kline_reducer(dict(request.params)["symbol"], width, step, limit)


def test_go_b_error_envelopes_keep_only_a_code_and_a_short_message(tmp_path: Path) -> None:
    """Rate-limit and transient codes are retried; any other code is fatal and named."""
    world = holdout_world(3)
    b, clock, requests, _sealed = go_b_fixture(tmp_path, world)
    request = d4_request(requests)
    long_message = "x" * 500
    world.api_errors = [(("mark-price-kline",), 10006, long_message)]
    result = b.run_request(request, reducer_for(request))
    assert result["outcome"] == "ok"
    first = receipts(tmp_path / "body")[-2]
    assert first["outcome"] == "error" and first["ret_code"] == 10006
    assert first["ret_msg"] == "x" * s.MAX_RET_MSG and "body_sha256" not in first
    assert b.spacing > s.SPACING_S and 10 in clock.sleeps
    for code in (10018, 10000, 10016):
        world.api_errors = [(("mark-price-kline",), code, "retry")]
        other = body.BodyRequest(
            "D4",
            "GET",
            s.MARK_URL,
            body._kline_params(
                dict(request.params)["symbol"],
                "60",
                int(dict(request.params)["start"]) + code,
                int(dict(request.params)["end"]),
                1000,
            ),
            meta=request.meta,
        )
        assert b.run_request(other, reducer_for(other))["outcome"] == "ok"


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (10001, "params error"),
        (10019, "ws only"),
        (0.0, "x"),
        (False, "x"),
        (True, "x"),
        ("10006", "x"),
    ],
)
def test_go_b_other_codes_and_a_non_integer_code_are_fatal(
    tmp_path: Path, code: Any, message: str
) -> None:
    world = holdout_world(3)
    b, _, requests, _ = go_b_fixture(tmp_path, world)
    request = d4_request(requests)
    envelope = {"retCode": code, "retMsg": message, "result": {}, "retExtInfo": {}, "time": 1}
    payload = json.dumps(envelope).encode()
    client = scripted(lambda r: streamed(200, content=payload))
    b.client = client
    with pytest.raises(s.FetchStoppedError):
        b.run_request(request, reducer_for(request))
    reason = json.loads((tmp_path / "body/stop.json").read_text())["reason"]
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        assert reason == f"D4: api error retCode {code}"
    else:
        assert reason == "D4: schema mismatch (envelope)"
    assert receipts(tmp_path / "body")[-1]["outcome"] in ("api_error", "schema_mismatch")


def test_go_b_three_failed_sends_make_a_request_unavailable_not_covered(tmp_path: Path) -> None:
    world = holdout_world(3)
    b, _, requests, _ = go_b_fixture(tmp_path, world)
    request = d4_request(requests)
    world.fail["mark-price-kline"] = [503, 503, 503]
    result = b.run_request(request, reducer_for(request))
    assert result["outcome"] == "unavailable" and result["facts"] is None
    assert sum(1 for a in b.attempts if a["key"] == request.key) == 3


def test_go_b_attempt_cap_stops_without_overshoot(tmp_path: Path) -> None:
    world = holdout_world(3)
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    contract = contract_for(sealed.pins, go2={"max_attempts": 3})
    a, _ = body_fetcher(world, root, sealed, contract=contract)
    body.run_a(a, sealed.sealed, sealed.pins)
    b, _ = body_fetcher(world, root, sealed, go=2, contract=contract)
    firsts = body.results_by_slot(a, "D2")
    queued = {slot for slot, r in sealed.sealed.rows.items() if body.has_size(r)}
    cands = body.entry_candidates(sealed.sealed.rows, firsts, queued)
    before = len(world.requests)
    with pytest.raises(s.BudgetExceededError, match="attempt cap"):
        for request in body.bar_requests(cands, sealed.sealed.rows):
            b.run_request(request, reducer_any(request))
    assert len(world.requests) - before == 3


def test_funding_pages_follow_the_pagination_rule_and_stop_at_four(tmp_path: Path) -> None:
    """A full page covers only from its oldest row; four full pages without the start are
    pagination_exhausted, never covered."""
    world = holdout_world(3)
    for launch in world.launches:
        launch.funding_interval_min = 60
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed)
    body.run_a(a, sealed.sealed, sealed.pins)
    firsts = body.results_by_slot(a, "D2")
    queued = {slot for slot, r in sealed.sealed.rows.items() if body.has_size(r)}
    cands = body.entry_candidates(sealed.sealed.rows, firsts, queued)
    c = cands[0]
    rows = sealed.sealed.rows
    start, end = HOLDOUT.funding_span(c.t0_ms)
    first = body.funding_round([c], rows, {})
    assert len(first) == 1 and first[0].meta_dict()["page"] == "1"
    assert dict(first[0].params)["endTime"] == str(end) and dict(first[0].params)[
        "startTime"
    ] == str(start)

    def page_run(count: int) -> list[dict[str, Any]]:
        out, newest = [], end
        for _ in range(count):
            oldest = newest - 199 * 60_000
            facts = {
                "n": 200,
                "oldest_ms": oldest,
                "newest_ms": newest,
                "max_gap_ms": 60_000,
                "full": True,
            }
            out.append({"outcome": "ok", "facts": facts})
            newest = oldest - 60_000
        return out

    pages = page_run(1)
    second = body.funding_round([c], rows, {c.slot: pages})
    assert second[0].meta_dict()["page"] == "2"
    assert dict(second[0].params)["endTime"] == str(end - 199 * 60_000 - 1)
    exhausted = page_run(4)
    assert body.funding_round([c], rows, {c.slot: exhausted}) == []
    state = s.funding_state(exhausted, start, end, None)
    assert state["reason"] == "pagination_exhausted" and not state["covered"]


def test_public_symbols_and_nothing_else_appear_in_urls_only(tmp_path: Path) -> None:
    world = holdout_world(3)
    _b, _, requests, sealed = go_b_fixture(tmp_path, world)
    assert len(requests) == 9
    for request in requests:
        assert request.symbol == "" and "USDT" in (request.url + str(request.params))
        assert request.slot in sealed.slots.values()
        assert "USDT" not in repr(request)


def test_ledger_totals_charge_an_unreceipted_attempt_its_full_reserve(tmp_path: Path) -> None:
    world = small_world(2)
    sealed = write_sealed(tmp_path, world)
    root = tmp_path / "body"
    world.crash = ["T001USDT"]
    fetcher, _ = body_fetcher(world, root, sealed)
    with pytest.raises(s.FetchStoppedError):
        body.run_a(fetcher, sealed.sealed, sealed.pins)
    data = verdict.load_run(root, sealed.sealed)
    spec = body.class_specs(sealed.pins)["D2"]
    unreceipted = s.reserve(spec)
    assert data.requests["attempts"] == 2
    assert data.requests["bytes"] >= unreceipted
    assert unreceipted == s.PER_FILE_CAP_BYTES + s.READ_RESERVE_BYTES


def test_the_first_file_reducer_reads_gzip_members_only() -> None:
    """A zlib stream without the gzip header is not a daily archive file."""
    with pytest.raises(s.SchemaMismatchError, match="not_gzip"):
        feed(zlib.compress(b"timestamp\n1685613600,x\n"))


def test_http_logging_is_disabled_during_a_run_and_restored_after(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A library log line would carry a request URL, and so a sealed symbol, to the console."""
    import logging

    world = small_world(2)
    sealed = write_sealed(tmp_path, world)

    def handler(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpx").warning("private %s", request.url)
        return world.handle(request)

    fetcher, _ = body_fetcher(world, tmp_path / "body", sealed, client=scripted(handler))
    caplog.set_level(logging.DEBUG)
    body.run_a(fetcher, sealed.sealed, sealed.pins)
    assert not [r for r in caplog.records if "USDT" in r.getMessage()]
    assert logging.root.manager.disable == 0
    logging.getLogger("httpx").warning("after %s", "run")
    assert [r for r in caplog.records if r.getMessage() == "after run"]


def test_a_finished_go_cannot_be_run_again_in_its_root(full: dict[str, Any]) -> None:
    root: Path = full["root"]
    sealed: Sealed = full["sealed"]
    world: World = full["world"]
    count = len(world.requests)
    a, _ = body_fetcher(world, root, sealed)
    with pytest.raises(s.FetchStoppedError, match="go A is already done"):
        body.run_a(a, sealed.sealed, sealed.pins)
    b, _ = body_fetcher(world, root, sealed, go=2)
    with pytest.raises(s.FetchStoppedError, match="go B is already done"):
        body.run_b(b, sealed.sealed, body.queue_b_sha256(root))
    assert len(world.requests) == count


def test_root_rules_resume_only_a_crashed_go_one_root(tmp_path: Path) -> None:
    """An existing root is acceptable only if it holds a run that never finished Go A."""
    fresh = tmp_path / "fresh"
    assert body.root_state(1, fresh) == "new"
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a resumable go 1 root"):
        body.root_state(1, plain)
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    (crashed / "run.json").write_text("{}")
    assert body.root_state(1, crashed) == "resume"
    (crashed / "go_a_done.json").write_text("{}")
    with pytest.raises(ValueError, match="not a resumable go 1 root"):
        body.root_state(1, crashed)
    assert body.root_state(2, crashed) == "resume"
    with pytest.raises(ValueError, match="finished go 1"):
        body.root_state(2, plain)


def test_document_and_registry_carry_the_hashes_and_the_recomputed_numbers() -> None:
    """The text a reviewer reads must say the numbers the code computes, once and the same."""
    from pathlib import Path

    contract = body.build_contract()
    doc = Path(body.DOC).read_text()
    registry = Path(s.REGISTRY).read_text()
    for text in (doc, registry):
        assert contract["request_list_sha256"] in text
        assert contract["go_sha256"]["1"] in text and contract["go_sha256"]["2"] in text
        assert "5,185,208,320" in text and "670,040,064" in text
    limits = contract["go_limits"]
    assert f"{limits['1']['max_bytes']:,}" in doc and f"{limits['2']['max_bytes']:,}" in doc
    assert str(limits["1"]["max_attempts"]) in doc and f"{limits['2']['max_attempts']:,}" in doc
    for pin in body.PINS.__dict__.values():
        text = f"{pin:,}" if isinstance(pin, int) else pin
        assert text in doc, text
    assert "2,656" in doc and "5,855,248,384" in doc and "1,976" in doc
    assert (
        494 + 2162 == 2656 and limits["1"]["max_bytes"] + limits["2"]["max_bytes"] == 5_855_248_384
    )
    assert 247 + 1729 == 1976
    protocol = Path(s.PROTOCOL).read_text()
    assert "Proposed Amendment 6" in protocol and "new-perp-stage1b-body-v1.md" in protocol
    assert "—" not in doc + registry + protocol + json.dumps(contract)


def test_a_stopped_run_publishes_its_aggregate_report_once(tmp_path: Path) -> None:
    """The owner needs the counts of a stopped root too; a stop must never hide behind them."""
    world = holdout_world(130)
    sealed = write_sealed(tmp_path, world)
    world.fail["/trading/"] = [403]
    root = tmp_path / "body"
    fetcher, _ = body_fetcher(world, root, sealed)
    with pytest.raises(s.FetchStoppedError, match="HTTP 403"):
        body.run_a(fetcher, sealed.sealed, sealed.pins)
    report = json.loads((root / "go_a_report.json").read_text())
    assert report["outcome"] == "stage1b_stopped" and report["stop"]["reason"] == "D2: HTTP 403"
    assert report["go_b_allowed"] is False and not (root / "go_a_done.json").exists()
    assert "USDT" not in json.dumps(report)
    assert body.main(["report", "--root", str(root), "--phase", "a"]) == 0


def test_a_stopped_go_b_publishes_the_final_aggregate_report(tmp_path: Path) -> None:
    world, rows = full_world()
    sealed = write_sealed(tmp_path, world, rows=rows)
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed)
    body.run_a(a, sealed.sealed, sealed.pins)
    world.fail["kline"] = [403]
    b, _ = body_fetcher(world, root, sealed, go=2)
    with pytest.raises(s.FetchStoppedError, match="HTTP 403"):
        body.run_b(b, sealed.sealed, body.queue_b_sha256(root))
    report = json.loads((root / "body_report.json").read_text())
    assert report["phase"] == "final" and report["outcome"] == "stage1b_stopped"
    assert report["not_assessed"] and report["n_primary"] is not None
    assert not (root / "go_b_done.json").exists()


def test_a_crash_between_two_outputs_republishes_the_same_content_only(tmp_path: Path) -> None:
    """Go A writes the queue, the report and the done marker in turn; a crash after the first
    must resume to the same bytes, and different bytes under a name are never accepted."""
    world, rows = full_world()
    sealed = write_sealed(tmp_path, world, rows=rows)
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed)
    report = body.run_a(a, sealed.sealed, sealed.pins)
    (root / "go_a_done.json").unlink()
    (root / "go_a_report.json").unlink()  # a crash after queue_b.json
    again, _ = body_fetcher(world, root, sealed)
    count = len(world.requests)
    assert body.run_a(again, sealed.sealed, sealed.pins) == report
    assert len(world.requests) == count  # nothing is sent twice
    queue = json.loads((root / "queue_b.json").read_text())
    with pytest.raises(s.FetchStoppedError, match="differs from the earlier one"):
        body.publish_or_same(root / "queue_b.json", {**queue, "candidates": 1})
