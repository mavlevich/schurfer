"""Synthetic metadata-only supplement tests; no real network or private run is required."""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_presettlement_bounded_fetch as transport
from schurfer_analytics import bybit_presettlement_economics as publication
from schurfer_analytics import new_perp_m2ii as m
from schurfer_analytics import new_perp_stage1 as s

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


FUTURE = "SYNTHFUTUREUSDT"
OLD = "SYNTHOLDUSDT"


def html(*hrefs: str) -> bytes:
    return (
        "<html><body>" + "".join(f'<a href="{h}">item</a>' for h in hrefs) + "</body></html>"
    ).encode()


def source(tmp_path: Path) -> m.Source:
    names = frozenset(f"BASE{i:03}USDT" for i in range(263))
    report = {
        "version": "synthetic",
        "outcome": "stage1_incomplete",
        "launches": 263,
        "not_assessed": ["m2_archive_only_dates_absent"],
        "stop_codes": [],
        "partial_checks": ["m2(ii): old lower bound", "m4: coarse", "m5: hourly", "m6: static"],
        "m2": {"pass": True, "counts": {}, "shares": {"archive_only": None}},
        "requests": {"attempts": 1726, "bytes": 6764743564},
    }
    return m.Source(tmp_path / "sealed", names, report, {"report_sha256": "synthetic"})


def all_text(root: Path) -> str:
    return "\n".join(p.read_text() for p in root.rglob("*.json"))


def execute(
    tmp_path: Path,
    pages: dict[str, bytes],
    *,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    contract: dict[str, Any] | None = None,
    snapshot: m.Source | None = None,
    validate: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if handler is not None:
            return handler(request)
        body = (
            html("BASE000USDT/", *(name + "/" for name in pages))
            if request.url.path == "/trading/"
            else pages[request.url.path.split("/")[-2]]
        )
        return httpx.Response(200, stream=httpx.ByteStream(body))

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        report = m.run(
            snapshot or source(tmp_path),
            tmp_path / "supplement",
            client,
            contract=contract or m.build_contract(),
            code={"synthetic": True},
            sleep=lambda _: None,
            validate_source=validate,
        )
    return report, calls


def test_unknown_names_never_reach_any_artifact_before_admission(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "supplement"

    def serve(request: httpx.Request) -> httpx.Response:
        assert FUTURE not in all_text(root)
        logging.getLogger("httpx").warning("should not publish %s", request.url)
        body = (
            html(FUTURE + "/")
            if request.url.path == "/trading/"
            else html(FUTURE + "2026-10-01.csv.gz")
        )
        return httpx.Response(200, stream=httpx.ByteStream(body))

    report, calls = execute(tmp_path, {}, handler=serve)
    assert len(calls) == 2 and report["outcome"] == "stage1_pass_partial"
    assert FUTURE not in all_text(root) and "2026-10-01" not in all_text(root)
    assert FUTURE not in caplog.text
    receipts = [json.loads(p.read_text()) for p in (root / "receipts").glob("*.json")]
    assert all(
        r["symbol"] == "" and r["meta"] == {} and "schema" not in r and "body_sha256" not in r
        for r in receipts
    )


@pytest.mark.parametrize(
    "body", [html("../"), html("wrong.csv.gz"), b'{"SYNTHFUTUREUSDT":1}', b"<html>truncated"]
)
def test_unverifiable_page_is_not_postcut_and_cannot_pass(tmp_path: Path, body: bytes) -> None:
    report, _ = execute(tmp_path, {FUTURE: body})
    assert report["outcome"] == "stage1_incomplete"
    assert FUTURE not in all_text(tmp_path / "supplement")
    assert not (tmp_path / "supplement/supplement_done.json").exists()


@pytest.mark.parametrize(
    "count,expected",
    [(0, "stage1_pass_partial"), (13, "stage1_pass_partial"), (14, "incomplete_coverage")],
)
def test_frozen_263_and_five_percent_boundary(tmp_path: Path, count: int, expected: str) -> None:
    pages = {
        f"MISSING{i:02}USDT": html(f"MISSING{i:02}USDT2025-02-01.csv.gz") for i in range(count)
    }
    snap = source(tmp_path)
    before = json.dumps(snap.report, sort_keys=True)
    report, _ = execute(tmp_path, pages, snapshot=snap)
    assert report["outcome"] == expected
    assert report["m2"]["counts"]["archive_only_in_window"] == count
    assert report["m2"]["archive_only_proven"] is False
    assert json.dumps(snap.report, sort_keys=True) == before
    assert report["partial_checks"][:3] == ["m4: coarse", "m5: hourly", "m6: static"]


def test_frozen_known_member_is_not_counted_again_if_catalog_changes(tmp_path: Path) -> None:
    pages = {"BASE000USDT": b"must not be fetched", OLD: html(OLD + "2025-02-01.csv.gz")}
    report, calls = execute(tmp_path, pages)
    assert len(calls) == 2
    assert report["m2"]["counts"]["archive_only_in_window"] == 1
    assert all(r.url.host == "public.bybit.com" for r in calls)  # no fresh C1.


def test_postcut_relisted_ticker_does_not_hide_its_old_archive(tmp_path: Path) -> None:
    report, _ = execute(tmp_path, {OLD: html(OLD + "2025-02-01.csv.gz", OLD + "2026-10-01.csv.gz")})
    assert report["m2"]["counts"]["archive_only_in_window"] == 1
    assert "2026-10-01" not in all_text(tmp_path / "supplement")


def test_pre_window_first_archive_day_is_not_an_in_window_launch(tmp_path: Path) -> None:
    report, _ = execute(tmp_path, {OLD: html(OLD + "2024-01-01.csv.gz", OLD + "2025-02-01.csv.gz")})
    assert report["m2"]["counts"]["archive_only_in_window"] == 0


@pytest.mark.parametrize("status", [403, 404, 500, 301])
def test_errors_are_opaque_and_never_promote(tmp_path: Path, status: int) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, stream=httpx.ByteStream(html(FUTURE + "/")))
            if request.url.path == "/trading/"
            else httpx.Response(status, stream=httpx.ByteStream(b"error"))
        )

    report, calls = execute(tmp_path, {}, handler=serve)
    assert report["outcome"] == "stage1_incomplete"
    assert len(calls) == (4 if status == 500 else 2)
    assert FUTURE not in all_text(tmp_path / "supplement")


def test_interruption_after_charge_leaves_only_opaque_record_and_no_resume(tmp_path: Path) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trading/":
            return httpx.Response(200, stream=httpx.ByteStream(html(FUTURE + "/")))
        raise KeyboardInterrupt

    report, _ = execute(tmp_path, {}, handler=serve)
    assert report["outcome"] == "stage1_incomplete"
    assert FUTURE not in all_text(tmp_path / "supplement")
    with pytest.raises(FileExistsError):
        execute(tmp_path, {})


def test_attempt_cap_is_a_stop_not_a_completed_zero(tmp_path: Path) -> None:
    contract = m.build_contract()
    contract["go_limits"]["1"]["max_attempts"] = 1
    contract["request_list_sha256"] = s.contract_hash(contract)
    report, calls = execute(
        tmp_path, {FUTURE: html(FUTURE + "2026-10-01.csv.gz")}, contract=contract
    )
    assert len(calls) == 1 and report["outcome"] == "stage1_incomplete"


def test_directory_cap_stops_before_unadmitted_requests(tmp_path: Path) -> None:
    contract = m.build_contract()
    contract["max_directories"] = 0
    contract["request_list_sha256"] = s.contract_hash(contract)
    report, calls = execute(
        tmp_path, {FUTURE: html(FUTURE + "2026-10-01.csv.gz")}, contract=contract
    )
    assert len(calls) == 1 and report["outcome"] == "stage1_incomplete"


def test_byte_reservation_stops_before_http(tmp_path: Path) -> None:
    contract = m.build_contract()
    contract["go_limits"]["1"]["max_bytes"] = s.reserve(m.class_specs()["C3"]) - 1
    contract["request_list_sha256"] = s.contract_hash(contract)
    report, calls = execute(tmp_path, {}, contract=contract)
    assert not calls and report["outcome"] == "stage1_incomplete"


def test_custom_class_specs_are_bound_and_crash_reserve_matches_them(tmp_path: Path) -> None:
    contract = m.build_contract()
    specs = m.class_specs()
    specs["D1"] = replace(specs["D1"], max_response_bytes=12345)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client,
        pytest.raises(ValueError, match="specs differ"),
    ):
        s.Fetcher(tmp_path / "invalid", contract, client, specs=specs)
    attempts = [{"seq": 1, "cls": "D1"}]
    assert s.ledger_totals(attempts, [], specs=specs)[1] == 12345 + s.READ_RESERVE_BYTES


def test_source_mutation_cannot_publish_completion(tmp_path: Path) -> None:
    def invalid() -> None:
        raise ValueError("source changed")

    report, _ = execute(tmp_path, {}, validate=invalid)
    assert report["outcome"] == "source_integrity_error"
    assert not (tmp_path / "supplement/supplement_done.json").exists()


def test_known_directory_disappearance_keeps_result_incomplete(tmp_path: Path) -> None:
    snap = replace(source(tmp_path), required_root=frozenset({"BASE001USDT"}))
    report, _ = execute(tmp_path, {}, snapshot=snap)
    assert report["outcome"] == "stage1_incomplete"


def test_postcut_folder_addition_does_not_change_scientific_result(tmp_path: Path) -> None:
    a, _ = execute(tmp_path / "a", {OLD: html(OLD + "2025-02-01.csv.gz")})
    b, _ = execute(
        tmp_path / "b",
        {OLD: html(OLD + "2025-02-01.csv.gz"), FUTURE: html(FUTURE + "2026-10-01.csv.gz")},
    )
    assert a["m2"] == b["m2"] and a["outcome"] == b["outcome"]
    assert a["supplement"]["requests"]["attempts"] + 1 == b["supplement"]["requests"]["attempts"]
    assert FUTURE not in all_text(tmp_path)


def test_opaque_request_rejects_identity_metadata_and_other_routes() -> None:
    request = m.OpaqueRequest("D1", "GET", s.ARCHIVE_ROOT + FUTURE + "/")
    assert FUTURE not in repr(request) and len(request.key) == 64
    options: list[dict[str, Any]] = [
        dict(symbol=FUTURE),
        dict(meta=(("name", FUTURE),)),
        dict(token=FUTURE),
        dict(params=(("name", FUTURE),)),
    ]
    for fields in options:
        with pytest.raises(ValueError, match="outside"):
            m.OpaqueRequest("D1", "GET", s.ARCHIVE_ROOT + FUTURE + "/", **fields)
    with pytest.raises(ValueError, match="outside"):
        m.OpaqueRequest("D1", "GET", "https://example.org/" + FUTURE + "/")


def test_declared_oversized_listing_is_not_read(tmp_path: Path) -> None:
    pulled: list[int] = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            pulled.append(1)
            yield b"unread"

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trading/":
            return httpx.Response(200, stream=httpx.ByteStream(html(FUTURE + "/")))
        return httpx.Response(200, headers={"content-length": str(s.MIB + 1)}, stream=Stream())

    report, _ = execute(tmp_path, {}, handler=serve)
    assert not pulled and report["outcome"] == "stage1_incomplete"


def test_unknown_length_overrun_is_fully_charged_within_the_reserve(tmp_path: Path) -> None:
    received: list[int] = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _ in range(16):
                received.append(s.READ_RESERVE_BYTES)
                yield b"x" * s.READ_RESERVE_BYTES
            received.append(1)
            yield b"x"

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trading/":
            return httpx.Response(200, stream=httpx.ByteStream(html(FUTURE + "/")))
        return httpx.Response(200, stream=Stream())

    report, _ = execute(tmp_path, {}, handler=serve)
    r = json.loads((tmp_path / "supplement/receipts/00002.json").read_text())
    assert r["body_bytes"] == sum(received) == s.MIB + 1
    assert r["body_bytes"] <= s.reserve(m.class_specs()["D1"])
    assert report["outcome"] == "stage1_incomplete"


def test_rate_limit_retry_keeps_identity_private(tmp_path: Path) -> None:
    sent = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        if request.url.path == "/trading/":
            return httpx.Response(200, stream=httpx.ByteStream(html(FUTURE + "/")))
        sent += 1
        if sent == 1:
            return httpx.Response(
                429, headers={"retry-after": "300"}, stream=httpx.ByteStream(b"slow")
            )
        return httpx.Response(200, stream=httpx.ByteStream(html(FUTURE + "2026-10-01.csv.gz")))

    report, calls = execute(tmp_path, {}, handler=serve)
    assert len(calls) == 3 and report["outcome"] == "stage1_pass_partial"
    assert FUTURE not in all_text(tmp_path / "supplement")


def test_wrong_approval_is_refused_before_source_or_http() -> None:
    with pytest.raises(ValueError, match="approval differs"):
        m.main(["fetch", "--source", "/unused", "--root", "/unused", "--approved-sha256", "wrong"])


def test_output_inside_the_sealed_source_is_refused(tmp_path: Path) -> None:
    snap = source(tmp_path)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client,
        pytest.raises(ValueError, match="outside the sealed"),
    ):
        m.run(snap, snap.root / "child", client, contract=m.build_contract(), code={})


def test_load_source_validates_every_input_and_frozen_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sealed"
    root.mkdir()
    snap = source(tmp_path)
    files = {
        "stage1-report.json": snap.report,
        "manifest.json": {"launches": 263},
        "phase0.json": {"catalog": {"rows": [{"symbol": s} for s in sorted(snap.symbols)]}},
        "run.json": {"code": {"revision": m.SOURCE_REVISION}},
        "go2_done.json": {"go": 2},
    }
    for name, content in files.items():
        (root / name).write_text(json.dumps(content))
    (root / "receipts").mkdir()
    (root / "receipts/00001.json").write_text(
        json.dumps({"cls": "D1", "symbol": "BASE000USDT", "outcome": "ok"})
    )
    hashes = {str(f.relative_to(root)): s.digest(f.read_bytes()) for f in root.rglob("*.json")}
    (root / "stage1-input-hashes.json").write_text(json.dumps(hashes))
    monkeypatch.setattr(m, "SOURCE_REPORT", s.digest((root / "stage1-report.json").read_bytes()))
    monkeypatch.setattr(m, "SOURCE_MANIFEST", s.digest((root / "manifest.json").read_bytes()))
    monkeypatch.setattr(
        m,
        "SOURCE_INPUTS",
        s.digest(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()),
    )
    loaded = m.load_source(root)
    assert loaded.symbols == snap.symbols and loaded.required_root == frozenset({"BASE000USDT"})
    (root / "receipts/00001.json").write_text("{}")
    with pytest.raises(ValueError, match="source input differs"):
        m.load_source(root)


def test_checked_in_contract_and_public_aggregates_match_frozen_bindings() -> None:
    root = Path(__file__).resolve().parents[3]
    assert json.loads((root / m.CONTRACT_PATH).read_text()) == m.build_contract()
    evidence = root / "docs/research/evidence/new-perp-stage1-v3"
    for name, expected in [
        ("stage1-report.json.gz", m.SOURCE_REPORT),
        ("manifest.json.gz", m.SOURCE_MANIFEST),
    ]:
        raw = gzip.decompress((evidence / name).read_bytes())
        assert s.digest(raw) == expected and b"USDT" not in raw


def test_other_market_directories_do_not_break_the_root_parser() -> None:
    result = m.root_symbols(html("BTCUSDT/", "BTCUSDT-27DEC24/", "BTCUSD/", "spot/"))
    assert result == {"symbols": ["BTCUSDT"]}


def test_unrecognized_usdt_symbol_syntax_is_not_silently_skipped() -> None:
    with pytest.raises(s.SchemaMismatchError, match="unsupported_usdt_directory"):
        m.root_symbols(html("BTCUSDT/", "UNRECOGNIZED-USDT/"))


def test_companion_report_binds_its_own_code_and_captured_inputs(tmp_path: Path) -> None:
    report, _ = execute(tmp_path, {OLD: html(OLD + "2025-02-01.csv.gz")})
    root = tmp_path / "supplement"
    assert report["supplement"]["code"] == {"synthetic": True}
    assert report["supplement"]["binding_valid"] is True
    assert (
        s.digest((root / "evidence-hashes.json").read_bytes())
        == report["supplement"]["evidence_sha256"]
    )
    hashes = json.loads((root / "evidence-hashes.json").read_text())
    assert "run.json" in hashes and "combined-stage1-report.json" not in hashes
    assert all(s.digest((root / name).read_bytes()) == sha for name, sha in hashes.items())


def test_pre_cut_foreign_prefix_continues_scan_but_never_passes(tmp_path: Path) -> None:
    report, calls = execute(
        tmp_path,
        {FUTURE: html("FOREIGNUSDT2025-02-01.csv.gz"), OLD: html(OLD + "2025-03-01.csv.gz")},
    )
    root = tmp_path / "supplement"
    assert len(calls) == 3 and report["supplement"]["complete"] is True
    assert report["outcome"] == "stage1_incomplete" and report["m2"]["pass"] is False
    assert report["m2"]["archive_only_assessed"] is False
    assert report["m2"]["shares"]["archive_only"] is None
    assert report["not_assessed"] == ["m2_archive_filename_identity_unverified"]
    assert report["m2"]["counts"]["archive_only_in_window"] == 1
    assert report["m2"]["counts"]["archive_filename_identity_unverified"] == 1
    assert FUTURE not in all_text(root) and "FOREIGNUSDT" not in all_text(root)
    issues = list((root / "identity-unverified").glob("*.json"))
    assert len(issues) == 1
    assert json.loads(issues[0].read_text()) == {"identity_status": "unverified_pre_cut_filename"}
    assert "2025-02-01" not in all_text(root)


@pytest.mark.parametrize("foreign_day", ["2024-01-01", "2024-10-01", "2025-09-30"])
def test_mixed_prefixes_never_admit_either_name(tmp_path: Path, foreign_day: str) -> None:
    report, _ = execute(
        tmp_path,
        {FUTURE: html(FUTURE + "2025-03-01.csv.gz", "FOREIGNUSDT" + foreign_day + ".csv.gz")},
    )
    assert report["outcome"] == "stage1_incomplete"
    assert report["m2"]["counts"]["archive_only_in_window"] == 0
    assert not (tmp_path / "supplement/admitted").exists()
    assert FUTURE not in all_text(tmp_path / "supplement")
    assert "FOREIGNUSDT" not in all_text(tmp_path / "supplement")


def test_valid_postcut_foreign_prefix_addition_changes_no_scientific_fact(tmp_path: Path) -> None:
    base, _ = execute(tmp_path / "a", {OLD: html(OLD + "2025-02-01.csv.gz")})
    extra, _ = execute(
        tmp_path / "b", {OLD: html(OLD + "2025-02-01.csv.gz", "FOREIGNUSDT2026-10-01.csv.gz")}
    )
    assert base["m2"] == extra["m2"] and base["outcome"] == extra["outcome"]
    assert "FOREIGNUSDT" not in all_text(tmp_path) and "2026-10-01" not in all_text(tmp_path)


def test_only_postcut_foreign_files_do_not_admit_directory(tmp_path: Path) -> None:
    report, _ = execute(tmp_path, {FUTURE: html("FOREIGNUSDT2026-10-01.csv.gz")})
    assert report["outcome"] == "stage1_pass_partial"
    assert report["m2"]["counts"]["archive_only_in_window"] == 0
    assert not (tmp_path / "supplement/identity-unverified").exists()
    assert FUTURE not in all_text(tmp_path) and "FOREIGNUSDT" not in all_text(tmp_path)


@pytest.mark.parametrize(
    "bad",
    [
        "FOREIGNUSDT2025-02-30.csv.gz",
        "FOREIGNUSD2025-02-01.csv.gz",
        "FOREIGNUSDT2025-02-01.csv",
        "nested/FOREIGNUSDT2025-02-01.csv.gz",
    ],
)
def test_foreign_prefix_does_not_relax_other_schema_guards(tmp_path: Path, bad: str) -> None:
    report, _ = execute(tmp_path, {FUTURE: html(bad)})
    assert not report["supplement"]["complete"]
    assert report["outcome"] == "stage1_incomplete"
    assert not (tmp_path / "supplement/identity-unverified").exists()


def test_known_coverage_failure_not_masked_by_foreign_prefix_uncertainty(tmp_path: Path) -> None:
    pages = {f"MISSING{i:02}USDT": html(f"MISSING{i:02}USDT2025-02-01.csv.gz") for i in range(14)}
    pages[FUTURE] = html("FOREIGNUSDT2025-02-01.csv.gz")
    report, _ = execute(tmp_path, pages)
    assert report["outcome"] == "incomplete_coverage"
    assert "incomplete_coverage" in report["stop_codes"]
    assert report["m2"]["shares"]["archive_only"] is None
    assert report["m2"]["counts"]["archive_only_in_window"] == 14


def test_completed_scan_marker_does_not_claim_identity_complete(tmp_path: Path) -> None:
    report, _ = execute(tmp_path, {FUTURE: html("FOREIGNUSDT2025-02-01.csv.gz")})
    assert report["supplement"]["identity_check_complete"] is False
    marker = json.loads((tmp_path / "supplement/supplement_done.json").read_text())
    assert marker == {"complete": True, "identity_check_complete": False}
    hashes = json.loads((tmp_path / "supplement/evidence-hashes.json").read_text())
    assert any(name.startswith("identity-unverified/") for name in hashes)


def test_uncertainty_preserved_when_later_request_fails(tmp_path: Path) -> None:
    report, _ = execute(
        tmp_path, {"AAAUSDT": html("FOREIGNUSDT2025-02-01.csv.gz"), "ZZZUSDT": b"bad"}
    )
    assert report["supplement"]["identity_unverified_directories"] == 1
    assert report["supplement"]["identity_check_complete"] is False
    assert report["supplement"]["complete"] is False
    assert report["outcome"] == "stage1_incomplete"


def test_v1_approval_cannot_authorize_v2() -> None:
    with pytest.raises(ValueError, match="approval differs"):
        m.main(
            [
                "fetch",
                "--source",
                "/unused",
                "--root",
                "/unused",
                "--approved-sha256",
                "4ca2124139eb28e4c38dc00e40b4a245eb7a5c357c5729028842f61221854a7c",
            ]
        )


def test_cli_nonzero_when_scan_complete_but_identity_unassessed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(m, "load_source", lambda _: source(tmp_path))
    monkeypatch.setattr(transport, "clean_revision", lambda: "synthetic")
    monkeypatch.setattr(publication, "publication", lambda _: {})
    monkeypatch.setattr(
        m,
        "run",
        lambda *args, **kwargs: {
            "outcome": "stage1_incomplete",
            "supplement": {"complete": True},
            "not_assessed": ["m2_archive_filename_identity_unverified"],
        },
    )
    assert (
        m.main(
            [
                "fetch",
                "--source",
                str(tmp_path),
                "--root",
                str(tmp_path / "unused"),
                "--approved-sha256",
                m.build_contract()["request_list_sha256"],
            ]
        )
        == 1
    )
