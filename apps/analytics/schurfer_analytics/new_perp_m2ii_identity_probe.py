"""Proposed, separately approved pre-cut filename evidence for two opaque I25 positions."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economics as publication
from . import new_perp_m2ii as m
from . import new_perp_stage1 as s
from .bybit_carry_feasibility import publish_once

if TYPE_CHECKING:
    from collections.abc import Callable

VERSION = "new_perp_m2ii_identity_probe_v1"
V2_REPORT = "0590a619d8bc5ddac11e928a958038f360ad4616d3685b11c53dc11be31a1f23"
V2_INPUTS = "751a2f1bb1af744d26df25e68b928e729da93e122477abba0513b5ca9da408fe"
V2_REVISION = "7e958b82105675409e0cad8f297aaaac9dc0bf36"
ORDINALS = (80, 245)
MAX_BYTES = 20 * s.MIB
DOC = "docs/research/new-perp-m2ii-identity-probe-v1.md"
CONTRACT = "docs/research/evidence/new-perp-m2ii-identity-probe-v1/contract.json"
PUBLISHED = (
    *m.PUBLISHED,
    "apps/analytics/schurfer_analytics/new_perp_m2ii_identity_probe.py",
    DOC,
    CONTRACT,
)


def specs() -> dict[str, s.ClassSpec]:
    return {
        name: replace(
            spec,
            max_requests=1 if name == "C3" else 2,
            max_class_bytes=MAX_BYTES,
            expected=1 if name == "C3" else 2,
            params="none" if name == "C3" else "two fresh ordinal positions",
            note="fixed current positions; no identity inference, search fallback or resume",
            parsed="root identities in RAM, or private pre-cut filename prefix evidence",
            discarded="post-cut identity/date/count, raw HTML, URLs, schema keys and body hashes",
        )
        for name, spec in m.class_specs().items()
    }


def build_contract() -> dict[str, Any]:
    contract: dict[str, Any] = {
        "rules": VERSION,
        "source_report_sha256": V2_REPORT,
        "source_evidence_map_sha256": V2_INPUTS,
        "source_revision": V2_REVISION,
        "base_input_map_sha256": m.SOURCE_INPUTS,
        "ordinals": list(ORDINALS),
        "max_unknown_directories": m.MAX_DIRECTORIES,
        "classes": [asdict(c) for c in specs().values()],
        "go_limits": {"1": {"max_attempts": 9, "max_bytes": MAX_BYTES}},
        "target_guard": (
            "fresh sorted root minus frozen 263; known directories present; no search fallback"
        ),
        "retention": (
            "private pre-cut filename prefixes and first/last day/count, "
            "never verified asset identity"
        ),
        "directory_name": (
            "only with an exact matching pre-cut filename; otherwise opaque token only"
        ),
        "continuity": (
            "ordinal similarity proves no linkage to old directories; old M2 remains incomplete"
        ),
        "failure": (
            "missing ordinal, changed target, invalid schema, integrity or budget stops diagnostic"
        ),
        "restart": "fresh sibling root only; no resume or modification of sealed roots",
        "publication": "aggregates only; private prefix evidence stays outside Git",
    }
    contract["request_list_sha256"] = s.contract_hash(contract)
    return contract


@dataclass(frozen=True)
class Inputs:
    base: m.Source
    v2: Path


def load_inputs(base: Path, v2: Path) -> Inputs:
    frozen = m.load_source(base)
    report_bytes = (v2 / "combined-stage1-report.json").read_bytes()
    map_bytes = (v2 / "evidence-hashes.json").read_bytes()
    if s.digest(report_bytes) != V2_REPORT or s.digest(map_bytes) != V2_INPUTS:
        raise ValueError("v2 fingerprints differ")
    hashes = json.loads(map_bytes)
    for name, expected in hashes.items():
        path = v2 / name
        if (
            not path.resolve().is_relative_to(v2.resolve())
            or s.digest(path.read_bytes()) != expected
        ):
            raise ValueError("v2 input differs")
    report = json.loads(report_bytes)
    if (
        report["outcome"] != "stage1_incomplete"
        or report["not_assessed"] != ["m2_archive_filename_identity_unverified"]
        or report["supplement"]["source"] != frozen.binding
        or report["supplement"]["code"]["revision"] != V2_REVISION
        or not report["supplement"]["complete"]
        or report["supplement"]["identity_check_complete"]
        or (v2 / "stop.json").exists()
    ):
        raise ValueError("v2 is not the frozen identity-incomplete scan")
    for folder in ("receipts", "identity-unverified"):
        actual = {str(p.relative_to(v2)) for p in (v2 / folder).glob("*.json")}
        recorded = {name for name in hashes if name.startswith(folder + "/")}
        if actual != recorded:
            raise ValueError("v2 captured file set differs")
    keys = {p.stem for p in (v2 / "identity-unverified").glob("*.json")}
    receipts = [json.loads(p.read_text()) for p in sorted((v2 / "receipts").glob("*.json"))]
    d1 = [r for r in receipts if r["cls"] == "D1"]
    positions = tuple(i for i, r in enumerate(d1, 1) if r["key"] in keys)
    if (
        len(keys) != 2
        or positions != ORDINALS
        or len(receipts) != report["supplement"]["requests"]["attempts"]
        or len(d1) != report["supplement"]["requests"]["attempts_by_class"]["D1"]
        or len({r["key"] for r in receipts}) != len(receipts)
        or any(r["outcome"] != "ok" or r["http_status"] != 200 for r in receipts)
    ):
        raise ValueError("opaque ordinal evidence differs")
    return Inputs(frozen, v2.resolve())


def filename_evidence(symbol: str, body: bytes) -> dict[str, Any]:
    if m.listing(symbol, body).get("identity_status") != "unverified_pre_cut_filename":
        raise s.SchemaMismatchError("target_drift_unproven")
    before: dict[str, set[str]] = {}
    for href in m.links(body):
        if href == "../":
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(\d{4}-\d{2}-\d{2})\.csv\.gz", href)
        if match is None:
            raise s.SchemaMismatchError("unsupported_file_anchor")
        if match[2] < s.CUT_DAY:
            before.setdefault(match[1], set()).add(match[2])
    facts: dict[str, Any] = {
        "evidence_status": "unverified_pre_cut_filename_prefixes",
        "prefixes": [
            {
                "prefix": prefix,
                "first_day": min(days),
                "last_day": max(days),
                "file_count": len(days),
            }
            for prefix, days in sorted(before.items())
        ],
    }
    if symbol in before:
        facts["directory_name"] = symbol
    return facts


def run(
    inputs: Inputs,
    root: Path,
    client: httpx.Client,
    *,
    contract: dict[str, Any],
    code: dict[str, Any],
    sleep: Callable[[float], None] | None = None,
    validate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if any(root.resolve().is_relative_to(p.resolve()) for p in (inputs.base.root, inputs.v2)):
        raise ValueError("output must be outside both sealed sources")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    options: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    fetcher = m.PrivateFetcher(root, contract, client, specs=specs(), code=code, **options)
    complete = False
    intact = True
    observed = 0
    with m.private_logs():
        try:
            response = fetcher.run_request(
                m.OpaqueRequest("C3", "GET", s.ARCHIVE_ROOT), m.root_symbols, keep_facts=False
            )
            if response["outcome"] != "ok":
                raise s.FetchStoppedError("root_unavailable")
            current = set(response["facts"]["symbols"])
            if not inputs.base.required_root <= current:
                raise s.FetchStoppedError("known_directory_missing")
            names = sorted(current - inputs.base.symbols)
            if len(names) > contract["max_unknown_directories"] or len(names) < max(ORDINALS):
                raise s.FetchStoppedError("target_plan_unproven")
            for position in ORDINALS:
                symbol = names[position - 1]
                request = m.OpaqueRequest("D1", "GET", s.ARCHIVE_ROOT + symbol + "/")
                response = fetcher.run_request(
                    request, partial(filename_evidence, symbol), keep_facts=False
                )
                if response["outcome"] != "ok":
                    raise s.FetchStoppedError("target_unavailable")
                publish_once(root / "private-evidence" / f"{request.key}.json", response["facts"])
                observed += 1
            complete = True
        except (s.FetchStoppedError, s.BudgetExceededError):
            fetcher.record_stop("diagnostic_incomplete")
        except KeyboardInterrupt:
            fetcher.record_stop("interrupted")
        except Exception:
            fetcher.record_stop("unexpected_error")
        if validate is not None:
            try:
                validate()
            except Exception:
                intact = False
                complete = False
                fetcher.record_stop("source_integrity_error")
    evidence = {
        str(p.relative_to(root)): s.digest(p.read_bytes()) for p in sorted(root.rglob("*.json"))
    }
    evidence_sha = publish_once(root / "evidence-hashes.json", evidence)
    report = {
        "version": VERSION,
        "complete": complete,
        "outcome": "diagnostic_complete_unlinked" if complete else "diagnostic_incomplete",
        "source_integrity_valid": intact,
        "identity_continuity": "not_proven",
        "source_m2_outcome": "stage1_incomplete",
        "private_observations": observed,
        "requests": fetcher.summary(),
        "code": code,
        "source_report_sha256": V2_REPORT,
        "source_evidence_map_sha256": V2_INPUTS,
        "request_list_sha256": contract["request_list_sha256"],
        "evidence_sha256": evidence_sha,
    }
    if not intact:
        report["outcome"] = "source_integrity_error"
    publish_once(root / "diagnostic-report.json", report)
    if complete:
        publish_once(
            root / "diagnostic_done.json", {"complete": True, "identity_continuity": "not_proven"}
        )
    return report


def require_merged(revision: str) -> None:
    if re.fullmatch(r"[a-f0-9]{40}", revision) is None:
        raise ValueError("invalid code revision")
    git = shutil.which("git")
    if git is None:
        raise ValueError("git is required to verify merged code")
    # Fixed argv, absolute executable, validated revision and no shell expansion.
    merged = subprocess.run(  # noqa: S603
        [git, "merge-base", "--is-ancestor", revision, "origin/main"],
        capture_output=True,
        check=False,
    )
    if merged.returncode != 0:
        raise ValueError("diagnostic code must be merged into origin/main")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    fetch = sub.add_parser("fetch")
    for name in ("base", "v2", "root"):
        fetch.add_argument("--" + name, type=Path, required=True)
    fetch.add_argument("--approved-sha256", required=True)
    args = parser.parse_args(argv)
    contract = build_contract()
    if args.command == "plan":
        print(json.dumps(contract, indent=2))  # noqa: T201
        return 0
    if args.approved_sha256 != contract["request_list_sha256"]:
        raise ValueError("approval differs from diagnostic contract")
    if json.loads(Path(CONTRACT).read_text()) != contract:
        raise ValueError("checked-in diagnostic contract differs")
    inputs = load_inputs(args.base, args.v2)
    revision = transport.clean_revision()
    require_merged(revision)
    code = {
        "revision": revision,
        "git_clean_before": True,
        "publication": publication.publication(list(PUBLISHED)),
    }

    def validate() -> None:
        load_inputs(args.base, args.v2)
        if (
            transport.clean_revision() != revision
            or publication.publication(list(PUBLISHED)) != code["publication"]
        ):
            raise ValueError("diagnostic code changed")

    with httpx.Client(timeout=60, follow_redirects=False) as client:
        report = run(inputs, args.root, client, contract=contract, code=code, validate=validate)
    print(json.dumps({"complete": report["complete"], "outcome": report["outcome"]}))  # noqa: T201
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
