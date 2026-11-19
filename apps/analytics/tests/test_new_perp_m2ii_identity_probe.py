"""Synthetic-only checks of private filename evidence and refusal to promote M2."""

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
from schurfer_analytics import new_perp_stage1 as s

if TYPE_CHECKING:
    from collections.abc import Callable

OLD = "OLDUSDT"
FUTURE = "FUTURESECRETUSDT"
ALIAS = "ALIASUSDT"


def html(*links: str) -> bytes:
    return ("<html>" + "".join(f'<a href="{link}">item</a>' for link in links) + "</html>").encode()


def inputs(tmp_path: Path) -> p.Inputs:
    return p.Inputs(m.Source(tmp_path / "base", frozenset(), {}, {}), tmp_path / "v2")


def execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, bytes],
    *,
    contract: dict[str, Any] | None = None,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    validate: Any = None,
    snapshot: p.Inputs | None = None,
) -> tuple[dict[str, Any], list[httpx.Request]]:
    monkeypatch.setattr(p, "ORDINALS", (1, 2))
    calls: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        logging.getLogger("httpx").warning("must not log %s", request.url)
        if handler is not None:
            return handler(request)
        body = (
            html(*(name + "/" for name in sorted(pages)))
            if request.url.path == "/trading/"
            else pages[request.url.path.split("/")[-2]]
        )
        return httpx.Response(200, stream=httpx.ByteStream(body))

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        report = p.run(
            snapshot or inputs(tmp_path),
            tmp_path / "diagnostic",
            client,
            contract=contract or p.build_contract(),
            code={"synthetic": True},
            sleep=lambda _: None,
            validate=validate,
        )
    return report, calls


def test_three_requests_keep_only_pre_cut_prefix_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pages = {
        OLD: html(
            OLD + "2025-02-01.csv.gz", ALIAS + "2025-02-02.csv.gz", FUTURE + "2026-10-01.csv.gz"
        ),
        FUTURE: html(ALIAS + "2024-01-01.csv.gz", FUTURE + "2026-10-01.csv.gz"),
    }
    report, calls = execute(tmp_path, monkeypatch, pages)
    root = tmp_path / "diagnostic"
    assert len(calls) == 3 and report["complete"]
    assert report["outcome"] == "diagnostic_complete_unlinked"
    assert report["source_m2_outcome"] == "stage1_incomplete"
    assert report["identity_continuity"] == "not_proven"
    assert FUTURE not in "".join(f.read_text() for f in root.rglob("*.json"))
    assert FUTURE not in caplog.text and ALIAS not in caplog.text
    private = [json.loads(f.read_text()) for f in (root / "private-evidence").glob("*.json")]
    assert len(private) == 2
    assert sum("directory_name" in row for row in private) == 1
    assert all(row.get("directory_name", OLD) == OLD for row in private)
    assert "2026-10-01" not in json.dumps(private)
    public = (root / "diagnostic-report.json").read_text()
    assert ALIAS not in public and OLD not in public and "2025-02-02" not in public
    for folder in ("attempts", "receipts"):
        for f in (root / folder).glob("*.json"):
            r = json.loads(f.read_text())
            assert r["symbol"] == "" and r["meta"] == {}
            assert not {"facts", "url", "schema", "body_sha256", "ret_msg"} & r.keys()


@pytest.mark.parametrize(
    "body",
    [
        html(OLD + "2025-02-01.csv.gz"),
        html(ALIAS + "2026-10-01.csv.gz"),
        html("../"),
        html(ALIAS + "2025-02-30.csv.gz"),
        html("not-a-file"),
        b'{"FUTURESECRETUSDT":1}',
    ],
)
def test_changed_or_unverifiable_target_never_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    report, calls = execute(
        tmp_path, monkeypatch, {OLD: body, "ZZZUSDT": html(ALIAS + "2025-02-01.csv.gz")}
    )
    assert len(calls) == 2 and not report["complete"]
    assert report["source_m2_outcome"] == "stage1_incomplete"
    assert not (tmp_path / "diagnostic/diagnostic_done.json").exists()
    assert not (tmp_path / "diagnostic/private-evidence").exists()


def test_duplicate_days_and_noncanonical_prefix_remain_unverified() -> None:
    facts = p.filename_evidence(
        OLD,
        html(
            "other_prefix-2024-01-01.csv.gz",
            "other_prefix-2024-01-01.csv.gz",
            OLD + "2026-10-01.csv.gz",
        ),
    )
    assert facts == {
        "evidence_status": "unverified_pre_cut_filename_prefixes",
        "prefixes": [
            {
                "prefix": "other_prefix-",
                "first_day": "2024-01-01",
                "last_day": "2024-01-01",
                "file_count": 1,
            }
        ],
    }


def test_missing_known_directory_stops_before_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = inputs(tmp_path)
    snap = replace(snap, base=replace(snap.base, required_root=frozenset({"MISSINGUSDT"})))
    report, calls = execute(
        tmp_path,
        monkeypatch,
        {OLD: html(ALIAS + "2025-02-01.csv.gz"), FUTURE: html(ALIAS + "2025-02-01.csv.gz")},
        snapshot=snap,
    )
    assert len(calls) == 1 and not report["complete"]


def test_existing_root_and_sealed_root_refused_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = {OLD: html(ALIAS + "2025-02-01.csv.gz"), FUTURE: html(ALIAS + "2025-02-01.csv.gz")}
    execute(tmp_path, monkeypatch, pages)
    with pytest.raises(FileExistsError):
        execute(tmp_path, monkeypatch, pages)
    snap = inputs(tmp_path)
    for root in (snap.base.root / "child", snap.v2 / "child"):
        with (
            httpx.Client(
                transport=httpx.MockTransport(lambda _: pytest.fail("HTTP forbidden"))
            ) as client,
            pytest.raises(ValueError, match="outside both"),
        ):
            p.run(snap, root, client, contract=p.build_contract(), code={})


def test_source_mutation_never_publishes_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid() -> None:
        raise ValueError("source changed")

    report, _ = execute(
        tmp_path,
        monkeypatch,
        {OLD: html(ALIAS + "2025-02-01.csv.gz"), FUTURE: html(ALIAS + "2025-02-01.csv.gz")},
        validate=invalid,
    )
    assert report["outcome"] == "source_integrity_error" and not report["complete"]
    assert not (tmp_path / "diagnostic/diagnostic_done.json").exists()


def test_attempt_budget_does_not_promote_partial_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(p, "ORDINALS", (1, 2))
    c = p.build_contract()
    c["go_limits"]["1"]["max_attempts"] = 2
    c["request_list_sha256"] = s.contract_hash(c)
    report, calls = execute(
        tmp_path,
        monkeypatch,
        {OLD: html(ALIAS + "2025-02-01.csv.gz"), FUTURE: html(ALIAS + "2025-02-01.csv.gz")},
        contract=c,
    )
    assert len(calls) == 2 and not report["complete"] and report["private_observations"] == 1


def test_byte_reservation_before_any_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(p, "ORDINALS", (1, 2))
    c = p.build_contract()
    c["go_limits"]["1"]["max_bytes"] = s.reserve(p.specs()["C3"]) - 1
    c["request_list_sha256"] = s.contract_hash(c)
    report, calls = execute(tmp_path, monkeypatch, {}, contract=c)
    assert not calls and not report["complete"]


def test_previous_approval_cannot_authorize_probe() -> None:
    with pytest.raises(ValueError, match="approval differs"):
        p.main(
            [
                "fetch",
                "--base",
                "/unused",
                "--v2",
                "/unused",
                "--root",
                "/unused",
                "--approved-sha256",
                "88bebd47b124f1492f049a89ed428a78a2a9f1b03b22d285b92eba88736e6834",
            ]
        )


def test_checked_in_contract_and_hard_caps() -> None:
    c = p.build_contract()
    assert c["go_limits"]["1"] == {"max_attempts": 9, "max_bytes": 20 * s.MIB}
    assert p.ORDINALS == (80, 245)
    assert json.loads(Path(p.CONTRACT).read_text()) == c
    assert sum(s.reserve(spec) * spec.max_requests * 3 for spec in p.specs().values()) <= 20 * s.MIB


def test_postcut_addition_changes_no_retained_fact() -> None:
    before = html(OLD + "2025-02-01.csv.gz", ALIAS + "2025-02-02.csv.gz")
    after = html(
        OLD + "2025-02-01.csv.gz", ALIAS + "2025-02-02.csv.gz", FUTURE + "2026-10-01.csv.gz"
    )
    assert p.filename_evidence(OLD, before) == p.filename_evidence(OLD, after)


@pytest.mark.parametrize("status", [403, 404, 500, 301])
def test_error_responses_cannot_complete_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trading/":
            return httpx.Response(200, stream=httpx.ByteStream(html(OLD + "/", FUTURE + "/")))
        return httpx.Response(status)

    report, calls = execute(tmp_path, monkeypatch, {}, handler=serve)
    assert not report["complete"]
    assert len(calls) == (4 if status == 500 else 2)
    assert not (tmp_path / "diagnostic/private-evidence").exists()


def test_frozen_v2_inputs_and_opaque_ordinals_are_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = inputs(tmp_path)
    snap.v2.mkdir()
    (snap.v2 / "receipts").mkdir()
    (snap.v2 / "identity-unverified").mkdir()
    for index in range(246):
        token = f"{index:064x}"
        receipt = {
            "seq": index + 1,
            "cls": "C3" if index == 0 else "D1",
            "key": token,
            "outcome": "ok",
            "http_status": 200,
        }
        (snap.v2 / "receipts" / f"{index + 1:05}.json").write_text(json.dumps(receipt))
        if index in p.ORDINALS:
            (snap.v2 / "identity-unverified" / f"{token}.json").write_text(
                json.dumps({"identity_status": "unverified_pre_cut_filename"})
            )
    report = {
        "outcome": "stage1_incomplete",
        "not_assessed": ["m2_archive_filename_identity_unverified"],
        "supplement": {
            "source": snap.base.binding,
            "code": {"revision": p.V2_REVISION},
            "complete": True,
            "identity_check_complete": False,
            "requests": {"attempts": 246, "attempts_by_class": {"C3": 1, "D1": 245}},
        },
    }
    raw = json.dumps(report).encode()
    (snap.v2 / "combined-stage1-report.json").write_bytes(raw)
    hashes = {
        str(f.relative_to(snap.v2)): s.digest(f.read_bytes())
        for folder in ("receipts", "identity-unverified")
        for f in (snap.v2 / folder).glob("*.json")
    }
    encoded = json.dumps(hashes).encode()
    (snap.v2 / "evidence-hashes.json").write_bytes(encoded)
    monkeypatch.setattr(m, "load_source", lambda _: snap.base)
    monkeypatch.setattr(p, "V2_REPORT", s.digest(raw))
    monkeypatch.setattr(p, "V2_INPUTS", s.digest(encoded))
    assert p.load_inputs(snap.base.root, snap.v2) == snap
    extra = snap.v2 / "receipts/00999.json"
    extra.write_text("{}")
    with pytest.raises(ValueError, match="file set differs"):
        p.load_inputs(snap.base.root, snap.v2)
    extra.unlink()
    (snap.v2 / "receipts/00081.json").write_text("{}")
    with pytest.raises(ValueError, match="v2 input differs"):
        p.load_inputs(snap.base.root, snap.v2)


@pytest.mark.parametrize("revision", ["--help", "", "x" * 40])
def test_merge_guard_rejects_nonrevision_before_any_subprocess(revision: str) -> None:
    with pytest.raises(ValueError, match="invalid code revision"):
        p.require_merged(revision)


def test_all_three_requests_with_two_retries_complete_at_nine_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts: dict[str, int] = {}

    def serve(request: httpx.Request) -> httpx.Response:
        key = request.url.path
        counts[key] = counts.get(key, 0) + 1
        if counts[key] < 3:
            return httpx.Response(500)
        body = (
            html(OLD + "/", FUTURE + "/")
            if key == "/trading/"
            else html(ALIAS + "2025-02-01.csv.gz")
        )
        return httpx.Response(200, stream=httpx.ByteStream(body))

    report, calls = execute(tmp_path, monkeypatch, {}, handler=serve)
    assert len(calls) == report["requests"]["attempts"] == 9
    assert report["complete"] and report["identity_continuity"] == "not_proven"
    assert report["source_m2_outcome"] == "stage1_incomplete"


def test_unknown_directory_cap_stops_before_selected_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(p, "ORDINALS", (1, 2))
    contract = p.build_contract()
    contract["max_unknown_directories"] = 1
    contract["request_list_sha256"] = s.contract_hash(contract)
    report, calls = execute(
        tmp_path,
        monkeypatch,
        {OLD: html(ALIAS + "2025-02-01.csv.gz"), FUTURE: html(ALIAS + "2025-02-01.csv.gz")},
        contract=contract,
    )
    assert len(calls) == 1 and not report["complete"]
    assert not (tmp_path / "diagnostic/private-evidence").exists()
