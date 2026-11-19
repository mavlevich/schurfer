"""Synthetic HTTP tests for the proposed pre-cut prefix cohort, with no real fetch."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import new_perp_m2ii as m
from schurfer_analytics import new_perp_m2ii_identity_probe as p
from schurfer_analytics import new_perp_m2ii_v3 as v
from schurfer_analytics import new_perp_stage1 as s

if TYPE_CHECKING:
    from collections.abc import Callable

OLD = "SYNTHOLDUSDT"
FUTURE = "SYNTHFUTUREUSDT"
PREFIX = "SYNTHPREFIXUSDT"


def html(*hrefs: str) -> bytes:
    return ("<html>" + "".join(f'<a href="{href}">item</a>' for href in hrefs) + "</html>").encode()


def inputs(tmp_path: Path) -> v.Inputs:
    report = {
        "version": "synthetic",
        "outcome": "stage1_incomplete",
        "launches": 263,
        "not_assessed": ["m2_archive_only_dates_absent"],
        "stop_codes": [],
        "m2": {"pass": True, "counts": {}, "shares": {"archive_only": None}},
        "partial_checks": ["m2(ii): old", "m3: original", "m4: coarse", "m5: hourly", "m6: static"],
        "requests": {"attempts": 1726, "bytes": 6764743564},
        "m3": {"change_known_at_entry": "not_observable_at_stage_1"},
        "m4": {"partial": True},
        "m5": {"minute_level_path": "not_assessed_at_stage_1"},
        "m6": {"partial": True},
    }
    base = m.Source(
        tmp_path / "sealed",
        frozenset(f"BASE{i:03}USDT" for i in range(263)),
        report,
        {"report_sha256": "synthetic"},
    )
    snap = v.Inputs(base, tmp_path / "v2", tmp_path / "diagnostic-source")
    for root in (snap.base.root, snap.v2, snap.diagnostic):
        root.mkdir(parents=True, exist_ok=True)
        (root / "sentinel").write_text("unchanged")
    return snap


def all_text(root: Path) -> str:
    return "".join(f.read_text() for f in root.rglob("*.json"))


def execute(
    tmp_path: Path,
    pages: dict[str, bytes],
    *,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    contract: dict[str, Any] | None = None,
    snapshot: v.Inputs | None = None,
    validate: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # No provisional series may have reached disk before the full snapshot ends.
        assert not (tmp_path / "run/admitted").exists()
        logging.getLogger("httpx").warning("must not persist %s", request.url)
        if handler is not None:
            return handler(request)
        body = (
            html("BASE000USDT/", *(name + "/" for name in pages))
            if request.url.path == "/trading/"
            else pages[request.url.path.split("/")[-2]]
        )
        return httpx.Response(200, stream=httpx.ByteStream(body))

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        report = v.run(
            snapshot or inputs(tmp_path),
            tmp_path / "run",
            client,
            contract=contract or v.build_contract(),
            code={"synthetic": True},
            sleep=lambda _: None,
            validate=validate,
        )
    return report, calls


def admitted(root: Path) -> list[dict[str, Any]]:
    return [json.loads(f.read_text()) for f in (root / "admitted").glob("*.json")]


def test_foreign_single_pre_cut_prefix_is_new_cohort_without_retaining_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    report, calls = execute(
        tmp_path, {FUTURE: html(PREFIX + "2025-02-01.csv.gz", FUTURE + "2026-10-01.csv.gz")}
    )
    facts = admitted(tmp_path / "run")
    assert len(calls) == 2 and len(facts) == 1
    assert facts[0] == {
        "file_prefix": PREFIX,
        "first_day": "2025-02-01",
        "last_day_before_cut": "2025-02-01",
        "file_count_before_cut": 1,
    }
    assert report["outcome"] == "stage1_pass_partial"
    assert report["m2"]["unit"] == v.UNIT
    assert report["m2"]["archive_only_status"] == "observed_archive_file_prefix_partial"
    assert report["m2"]["archive_only_proven"] is False
    assert FUTURE not in all_text(tmp_path / "run") and "2026-10-01" not in all_text(
        tmp_path / "run"
    )
    assert FUTURE not in caplog.text
    assert PREFIX not in (tmp_path / "run/combined-stage1-report.json").read_text()
    assert m.listing(FUTURE, html(PREFIX + "2025-02-01.csv.gz")) == {
        "identity_status": "unverified_pre_cut_filename"
    }


def test_directory_name_requires_exact_admitted_prefix(tmp_path: Path) -> None:
    report, _ = execute(tmp_path, {PREFIX: html(PREFIX + "2025-02-01.csv.gz")})
    assert report["outcome"] == "stage1_pass_partial"
    assert admitted(tmp_path / "run")[0]["directory_name"] == PREFIX


@pytest.mark.parametrize(
    "count,outcome",
    [(0, "stage1_pass_partial"), (13, "stage1_pass_partial"), (14, "incomplete_coverage")],
)
def test_threshold_and_original_partial_checks_unchanged(
    tmp_path: Path, count: int, outcome: str
) -> None:
    snap = inputs(tmp_path)
    before = json.dumps(snap.base.report, sort_keys=True)
    pages = {
        f"MISSING{i:02}USDT": html(f"MISSING{i:02}USDT2025-02-01.csv.gz") for i in range(count)
    }
    report, _ = execute(tmp_path, pages, snapshot=snap)
    assert report["outcome"] == outcome
    assert report["m2"]["counts"]["archive_only_in_window"] == count
    assert report["m2"]["shares"]["archive_only"] == count / (263 + count)
    assert json.dumps(snap.base.report, sort_keys=True) == before
    for field in ("m3", "m4", "m5", "m6"):
        assert report[field] == snap.base.report[field]
    assert report["partial_checks"][:4] == [
        "m3: original",
        "m4: coarse",
        "m5: hourly",
        "m6: static",
    ]
    assert all(
        (root / "sentinel").read_text() == "unchanged"
        for root in (snap.base.root, snap.v2, snap.diagnostic)
    )


def test_pre_window_first_day_and_duplicate_dates(tmp_path: Path) -> None:
    report, _ = execute(
        tmp_path,
        {
            FUTURE: html(
                PREFIX + "2023-01-01.csv.gz",
                PREFIX + "2023-01-01.csv.gz",
                PREFIX + "2025-02-01.csv.gz",
            )
        },
    )
    assert report["m2"]["counts"]["archive_only_in_window"] == 0
    assert report["m2"]["counts"]["archive_only_pre_window"] == 1
    assert admitted(tmp_path / "run")[0]["file_count_before_cut"] == 2


@pytest.mark.parametrize("day", ["2023-01-01", "2025-02-01"])
@pytest.mark.parametrize(
    "prefixes", [(PREFIX, OLD), (PREFIX, "lowercaseusdt"), ("not-canonical_",)]
)
def test_mixed_or_noncanonical_pre_cut_prefixes_block_with_opaque_facts(
    tmp_path: Path, prefixes: tuple[str, ...], day: str
) -> None:
    report, _ = execute(tmp_path, {FUTURE: html(*(x + day + ".csv.gz" for x in prefixes))})
    assert report["outcome"] == "stage1_incomplete" and report["m2"]["pass"] is False
    assert report["not_assessed"] == ["m2_unverified_pre_cut_filename"]
    assert report["m2"]["shares"]["archive_only"] is None and not admitted(tmp_path / "run")
    text = all_text(tmp_path / "run")
    assert FUTURE not in text and all(prefix not in text for prefix in prefixes)
    issues = [json.loads(f.read_text()) for f in (tmp_path / "run/unverified").glob("*.json")]
    assert issues == [{"status": "unverified_pre_cut_filename"}]


@pytest.mark.parametrize("day", ["2023-01-01", "2025-02-01"])
def test_interdirectory_collision_blocks_both_without_early_admission(
    tmp_path: Path, day: str
) -> None:
    report, _ = execute(
        tmp_path, {PREFIX: html(PREFIX + day + ".csv.gz"), FUTURE: html(PREFIX + day + ".csv.gz")}
    )
    assert report["outcome"] == "stage1_incomplete"
    assert report["not_assessed"] == ["m2_prefix_collision_unverified"]
    assert report["m2"]["counts"]["prefix_collision_unverified"] == 2
    assert not admitted(tmp_path / "run")
    assert PREFIX not in all_text(tmp_path / "run") and FUTURE not in all_text(tmp_path / "run")
    assert len(list((tmp_path / "run/collisions").glob("*.json"))) == 2


def test_collision_with_frozen_catalog_is_not_silently_deduplicated(tmp_path: Path) -> None:
    report, _ = execute(tmp_path, {FUTURE: html("BASE001USDT2023-01-01.csv.gz")})
    assert report["outcome"] == "stage1_incomplete"
    assert report["m2"]["counts"]["prefix_collision_unverified"] == 1
    assert "BASE001USDT" not in all_text(tmp_path / "run") and not admitted(tmp_path / "run")


def test_prefix_in_mixed_directory_also_blocks_other_owner(tmp_path: Path) -> None:
    report, _ = execute(
        tmp_path,
        {
            PREFIX: html(PREFIX + "2025-02-01.csv.gz"),
            FUTURE: html(PREFIX + "2025-02-02.csv.gz", OLD + "2025-02-02.csv.gz"),
        },
    )
    assert report["m2"]["counts"]["unverified_pre_cut_filename"] == 1
    assert report["m2"]["counts"]["prefix_collision_unverified"] == 2
    assert set(report["not_assessed"]) == {
        "m2_unverified_pre_cut_filename",
        "m2_prefix_collision_unverified",
    }
    assert not admitted(tmp_path / "run")
    assert all(name not in all_text(tmp_path / "run") for name in (PREFIX, FUTURE, OLD))


def test_postcut_prefixes_never_block_or_change_scientific_result(tmp_path: Path) -> None:
    a, _ = execute(tmp_path / "a", {PREFIX: html(PREFIX + "2025-02-01.csv.gz")})
    b, _ = execute(
        tmp_path / "b",
        {
            PREFIX: html(PREFIX + "2025-02-01.csv.gz", OLD + "2026-10-01.csv.gz"),
            FUTURE: html(PREFIX + "2026-10-01.csv.gz", "noncanonical_2026-10-01.csv.gz"),
        },
    )
    assert a["m2"] == b["m2"] and a["outcome"] == b["outcome"]
    assert b["supplement"]["requests"]["attempts"] == a["supplement"]["requests"]["attempts"] + 1
    assert FUTURE not in all_text(tmp_path) and OLD not in all_text(tmp_path)
    assert "2026-10-01" not in all_text(tmp_path)


@pytest.mark.parametrize("status", [403, 404, 500, 301])
def test_incomplete_scan_discards_all_provisional_candidates(tmp_path: Path, status: int) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trading/":
            return httpx.Response(200, stream=httpx.ByteStream(html("AAAUSDT/", "ZZZUSDT/")))
        if "/AAAUSDT/" in request.url.path:
            return httpx.Response(200, stream=httpx.ByteStream(html(PREFIX + "2025-02-01.csv.gz")))
        return httpx.Response(status)

    report, _ = execute(tmp_path, {}, handler=serve)
    assert not report["supplement"]["complete"] and report["outcome"] == "stage1_incomplete"
    assert not admitted(tmp_path / "run") and PREFIX not in all_text(tmp_path / "run")
    assert not (tmp_path / "run/supplement_done.json").exists()


def test_source_mutation_prevents_admission_and_completion(tmp_path: Path) -> None:
    def invalid() -> None:
        raise ValueError("source differs")

    report, _ = execute(tmp_path, {PREFIX: html(PREFIX + "2025-02-01.csv.gz")}, validate=invalid)
    assert report["outcome"] == "source_integrity_error"
    assert not report["supplement"]["binding_valid"] and not admitted(tmp_path / "run")
    assert not (tmp_path / "run/supplement_done.json").exists()


def test_known_threshold_failure_not_masked_by_unverified_folder(tmp_path: Path) -> None:
    pages = {f"MISSING{i:02}USDT": html(f"MISSING{i:02}USDT2025-02-01.csv.gz") for i in range(14)}
    pages[FUTURE] = html("noncanonical_2025-02-01.csv.gz")
    report, _ = execute(tmp_path, pages)
    assert report["outcome"] == "incomplete_coverage" and report["stop_codes"] == [
        "incomplete_coverage"
    ]
    assert report["m2"]["counts"]["archive_only_in_window"] == 14
    assert report["m2"]["shares"]["archive_only"] is None


def test_prefix_slot_cap_does_not_truncate_into_a_pass(tmp_path: Path) -> None:
    c = v.build_contract()
    c["max_pre_cut_prefix_slots"] = 1
    c["request_list_sha256"] = s.contract_hash(c)
    report, _ = execute(
        tmp_path,
        {PREFIX: html(PREFIX + "2025-02-01.csv.gz"), OLD: html(OLD + "2025-02-01.csv.gz")},
        contract=c,
    )
    assert not report["supplement"]["complete"] and report["outcome"] == "stage1_incomplete"
    assert not admitted(tmp_path / "run")


@pytest.mark.parametrize("kind", ["max_attempts", "max_bytes", "max_directories"])
def test_network_caps_stop_without_admission(tmp_path: Path, kind: str) -> None:
    c = v.build_contract()
    if kind == "max_directories":
        c[kind] = 0
    else:
        c["go_limits"]["1"][kind] = (
            1 if kind == "max_attempts" else s.reserve(v.class_specs()["C3"]) - 1
        )
    c["request_list_sha256"] = s.contract_hash(c)
    report, calls = execute(tmp_path, {PREFIX: html(PREFIX + "2025-02-01.csv.gz")}, contract=c)
    assert len(calls) == (0 if kind == "max_bytes" else 1)
    assert not admitted(tmp_path / "run") and not report["supplement"]["complete"]


@pytest.mark.parametrize(
    "body",
    [
        html("../"),
        html("bad.csv.gz"),
        html(PREFIX + "2025-02-30.csv.gz"),
        b'{"SYNTHFUTUREUSDT":1}',
        b"<html>truncated",
    ],
)
def test_invalid_listing_is_not_postcut_zero(tmp_path: Path, body: bytes) -> None:
    report, _ = execute(tmp_path, {FUTURE: body})
    assert report["outcome"] == "stage1_incomplete" and not report["supplement"]["complete"]
    assert FUTURE not in all_text(tmp_path / "run")


def test_protects_every_sealed_source_and_refuses_resume(tmp_path: Path) -> None:
    snap = inputs(tmp_path)
    execute(tmp_path, {PREFIX: html(PREFIX + "2025-02-01.csv.gz")}, snapshot=snap)
    with pytest.raises(FileExistsError):
        execute(tmp_path, {}, snapshot=snap)
    for source in (snap.base.root, snap.v2, snap.diagnostic):
        with (
            httpx.Client(
                transport=httpx.MockTransport(lambda _: pytest.fail("HTTP forbidden"))
            ) as client,
            pytest.raises(ValueError, match="outside all sealed"),
        ):
            v.run(snap, source / "child", client, contract=v.build_contract(), code={})


@pytest.mark.parametrize(
    "approval",
    [
        "88bebd47b124f1492f049a89ed428a78a2a9f1b03b22d285b92eba88736e6834",
        "617cb488517081071727108945afe7634345fcf1baec22ff05fbfe500cd4ee21",
    ],
)
def test_previous_approvals_never_authorize_v3(approval: str) -> None:
    with pytest.raises(ValueError, match="approval differs"):
        v.main(
            [
                "fetch",
                "--base",
                "/unused",
                "--v2",
                "/unused",
                "--diagnostic",
                "/unused",
                "--root",
                "/unused",
                "--approved-sha256",
                approval,
            ]
        )


def test_checked_in_contract_and_report_evidence_binding(tmp_path: Path) -> None:
    assert json.loads(Path(v.CONTRACT).read_text()) == v.build_contract()
    report, _ = execute(tmp_path, {PREFIX: html(PREFIX + "2025-02-01.csv.gz")})
    root = tmp_path / "run"
    assert (
        s.digest((root / "evidence-hashes.json").read_bytes())
        == report["supplement"]["evidence_sha256"]
    )
    hashes = json.loads((root / "evidence-hashes.json").read_text())
    assert all(s.digest((root / name).read_bytes()) == sha for name, sha in hashes.items())
    assert any(name.startswith("admitted/") for name in hashes)
    assert report["supplement"]["file_prefix_check_complete"]


def test_late_collision_removes_an_apparent_fourteenth_series(tmp_path: Path) -> None:
    pages = {f"MISSING{i:02}USDT": html(f"MISSING{i:02}USDT2025-02-01.csv.gz") for i in range(14)}
    pages[FUTURE] = html("MISSING00USDT2025-02-01.csv.gz", OLD + "2025-02-01.csv.gz")
    report, _ = execute(tmp_path, pages)
    assert report["outcome"] == "stage1_incomplete" and not report["stop_codes"]
    assert report["m2"]["counts"]["archive_only_in_window"] == 13
    assert report["m2"]["counts"]["prefix_collision_unverified"] == 2
    assert all(row["file_prefix"] != "MISSING00USDT" for row in admitted(tmp_path / "run"))


def test_known_directory_disappearance_stops_before_d1(tmp_path: Path) -> None:
    snap = inputs(tmp_path)
    snap = replace(snap, base=replace(snap.base, required_root=frozenset({"BASE001USDT"})))
    report, calls = execute(tmp_path, {PREFIX: html(PREFIX + "2025-02-01.csv.gz")}, snapshot=snap)
    assert len(calls) == 1 and not report["supplement"]["complete"]


def test_admission_does_not_put_prefix_facts_in_journals(tmp_path: Path) -> None:
    execute(tmp_path, {PREFIX: html(PREFIX + "2025-02-01.csv.gz")})
    for folder in ("attempts", "receipts"):
        for path in (tmp_path / "run" / folder).glob("*.json"):
            row = json.loads(path.read_text())
            assert row["symbol"] == "" and row["meta"] == {}
            assert (
                not {
                    "facts",
                    "url",
                    "schema",
                    "body_sha256",
                    "ret_msg",
                    "file_prefix",
                    "canonical_prefixes",
                }
                & row.keys()
            )


def test_diagnostic_source_binding_and_mutation_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = inputs(tmp_path)
    monkeypatch.setattr(p, "load_inputs", lambda *_: p.Inputs(snap.base, snap.v2))
    raw = json.dumps(
        {
            "complete": True,
            "outcome": "diagnostic_complete_unlinked",
            "identity_continuity": "not_proven",
            "source_m2_outcome": "stage1_incomplete",
            "source_integrity_valid": True,
        }
    ).encode()
    (snap.diagnostic / "diagnostic-report.json").write_bytes(raw)
    (snap.diagnostic / "diagnostic_done.json").write_text("{}")
    proof = snap.diagnostic / "captured.json"
    proof.write_text('{"synthetic":true}')
    encoded = json.dumps({"captured.json": s.digest(proof.read_bytes())}).encode()
    (snap.diagnostic / "evidence-hashes.json").write_bytes(encoded)
    monkeypatch.setattr(v, "DIAGNOSTIC_REPORT", s.digest(raw))
    monkeypatch.setattr(v, "DIAGNOSTIC_INPUTS", s.digest(encoded))
    assert v.load_inputs(snap.base.root, snap.v2, snap.diagnostic) == snap
    proof.write_text("changed")
    with pytest.raises(ValueError, match="diagnostic input differs"):
        v.load_inputs(snap.base.root, snap.v2, snap.diagnostic)


def test_frozen_directory_contents_are_not_rescanned(tmp_path: Path) -> None:
    report, calls = execute(
        tmp_path, {"BASE001USDT": b"must not be read", FUTURE: html(PREFIX + "2025-02-01.csv.gz")}
    )
    assert len(calls) == 2 and all("/BASE001USDT/" not in r.url.path for r in calls)
    assert report["m2"]["counts"]["archive_only_in_window"] == 1
    assert "directories outside frozen catalog" in report["m2"]["archive_only_scope"]
