"""A proposed fresh I25 archive screen by unique canonical pre-cut filename prefix."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economics as publication
from . import new_perp_m2ii as m
from . import new_perp_m2ii_identity_probe as p
from . import new_perp_stage1 as s
from .bybit_carry_feasibility import publish_once

if TYPE_CHECKING:
    from collections.abc import Callable

VERSION = "new_perp_m2ii_v3"
UNIT = "observed_pre_cut_file_prefix_cohort"
MAX_PREFIX_SLOTS = 16384
DIAGNOSTIC_REPORT = "4449921d31dc9efdb61925d8f83b33c5ca0a39885f80de910cd5344b96017b49"
DIAGNOSTIC_INPUTS = "9a47070688e546eaf44715c37ed48cf6e9ac19407e0da7a9d37cecddd1ee8492"
DOC = "docs/research/new-perp-m2ii-file-prefix-v3.md"
CONTRACT = "docs/research/evidence/new-perp-m2ii-v3/contract.json"
PUBLISHED = tuple(
    dict.fromkeys(
        (*p.PUBLISHED, "apps/analytics/schurfer_analytics/new_perp_m2ii_v3.py", DOC, CONTRACT)
    )
)


def class_specs() -> dict[str, s.ClassSpec]:
    return {
        name: replace(
            spec,
            parsed="RAM-only pre-cut prefix facts; global admission after full scan",
            params="none" if name == "C3" else "one unknown directory GET, no query parameters",
            discarded="post-cut identities/dates/counts, raw HTML, URLs, schema and body hashes",
        )
        for name, spec in m.class_specs().items()
    }


def build_contract() -> dict[str, Any]:
    contract: dict[str, Any] = {
        "rules": VERSION,
        "unit": UNIT,
        "source_report_sha256": m.SOURCE_REPORT,
        "source_input_map_sha256": m.SOURCE_INPUTS,
        "prior_v2_report_sha256": p.V2_REPORT,
        "prior_v2_input_map_sha256": p.V2_INPUTS,
        "diagnostic_report_sha256": DIAGNOSTIC_REPORT,
        "diagnostic_input_map_sha256": DIAGNOSTIC_INPUTS,
        "window": [s.WINDOW_START_DAY, s.CUT_DAY],
        "classes": [asdict(spec) for spec in class_specs().values()],
        "go_limits": {"1": {"max_attempts": m.MAX_ATTEMPTS, "max_bytes": m.MAX_BYTES}},
        "max_directories": m.MAX_DIRECTORIES,
        "directory_scope": "unknown root directories only; frozen directory contents not rescanned",
        "max_pre_cut_prefix_slots": MAX_PREFIX_SLOTS,
        "admission": (
            "one canonical pre-cut prefix per unknown directory, absent from frozen catalog"
        ),
        "collision": (
            "any shared canonical pre-cut prefix blocks all owners; no merge or normalization"
        ),
        "threshold": "archive_only_in_window/(263+archive_only_in_window) <= 0.05",
        "retention": "private file_prefix, first/last pre-cut day and count after global admission",
        "directory_name": "only if exactly equal to the admitted pre-cut file_prefix",
        "blocked_facts": "opaque status only; no blocked prefix or directory name persists",
        "restart": "fresh sibling root; no resume or old report modification",
        "failure": "incomplete scan, invalid source/schema or any cap prevents a positive result",
        "partial": "observed archive filename cohort, not verified assets or all real launches",
    }
    contract["request_list_sha256"] = s.contract_hash(contract)
    return contract


@dataclass(frozen=True)
class Inputs:
    base: m.Source
    v2: Path
    diagnostic: Path


def load_inputs(base: Path, v2: Path, diagnostic: Path) -> Inputs:
    previous = p.load_inputs(base, v2)
    raw = (diagnostic / "diagnostic-report.json").read_bytes()
    map_bytes = (diagnostic / "evidence-hashes.json").read_bytes()
    if s.digest(raw) != DIAGNOSTIC_REPORT or s.digest(map_bytes) != DIAGNOSTIC_INPUTS:
        raise ValueError("diagnostic fingerprints differ")
    for name, expected in json.loads(map_bytes).items():
        path = diagnostic / name
        if (
            not path.resolve().is_relative_to(diagnostic.resolve())
            or s.digest(path.read_bytes()) != expected
        ):
            raise ValueError("diagnostic input differs")
    report = json.loads(raw)
    if (
        not report["complete"]
        or report["outcome"] != "diagnostic_complete_unlinked"
        or report["identity_continuity"] != "not_proven"
        or report["source_m2_outcome"] != "stage1_incomplete"
        or not report["source_integrity_valid"]
        or not (diagnostic / "diagnostic_done.json").exists()
        or (diagnostic / "stop.json").exists()
    ):
        raise ValueError("diagnostic is not the sealed unlinked observation")
    return Inputs(previous.base, previous.v2, diagnostic.resolve())


def listing(directory: str, body: bytes) -> dict[str, Any]:
    before: dict[str, set[str]] = {}
    recognized = False
    for href in m.links(body):
        if href == "../":
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(\d{4}-\d{2}-\d{2})\.csv\.gz", href)
        if match is None:
            raise s.SchemaMismatchError("unsupported_file_anchor")
        try:
            day = date.fromisoformat(match[2]).isoformat()
        except ValueError as exc:
            raise s.SchemaMismatchError("invalid_file_day") from exc
        recognized = True
        if day < s.CUT_DAY:
            before.setdefault(match[1], set()).add(day)
            if len(before) > MAX_PREFIX_SLOTS:
                raise s.SchemaMismatchError("pre_cut_prefix_budget")
    if not recognized:
        raise s.SchemaMismatchError("unverifiable_listing")
    if not before:
        return {}  # No post-cut identity, date, count or classification survives.
    canonical = sorted(prefix for prefix in before if s.SYMBOL.fullmatch(prefix))
    unverified = len(before) != 1 or len(canonical) != 1
    candidate: dict[str, Any] | None = None
    if not unverified:
        prefix = canonical[0]
        days = before[prefix]
        candidate = {
            "file_prefix": prefix,
            "first_day": min(days),
            "last_day_before_cut": max(days),
            "file_count_before_cut": len(days),
        }
        if directory == prefix:
            candidate["directory_name"] = directory
    return {
        "canonical_prefixes": canonical,
        "prefix_slots": len(before),
        "unverified": unverified,
        "candidate": candidate,
    }  # RAM only, never journalled.


def classify(
    observations: dict[str, dict[str, Any]], frozen: frozenset[str]
) -> tuple[dict[str, dict[str, Any]], set[str], set[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    unverified = {key for key, facts in observations.items() if facts["unverified"]}
    for key, facts in observations.items():
        for prefix in facts["canonical_prefixes"]:
            owners[prefix].add(key)
    collisions: set[str] = set()
    for prefix, keys in owners.items():
        if prefix in frozen or len(keys) > 1:
            collisions.update(keys)
    accepted = {
        key: facts["candidate"]
        for key, facts in observations.items()
        if key not in unverified | collisions and facts["candidate"] is not None
    }
    return accepted, unverified, collisions


def combined_report(
    inputs: Inputs,
    accepted: list[dict[str, Any]],
    complete: bool,
    unverified: int,
    collisions: int,
    usage: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    source = inputs.base
    report: dict[str, Any] = json.loads(json.dumps(source.report))
    count = sum(s.WINDOW_START_DAY <= row["first_day"] < s.CUT_DAY for row in accepted)
    share = count / (len(source.symbols) + count)
    report["version"] += "+m2ii_v3"
    if complete:
        blocked = bool(unverified or collisions)
        report["m2"]["counts"].update(
            archive_only_in_window=count,
            archive_only_pre_window=sum(row["first_day"] < s.WINDOW_START_DAY for row in accepted),
            admitted_pre_cut_series=len(accepted),
            unverified_pre_cut_filename=unverified,
            prefix_collision_unverified=collisions,
            archive_only_unlisted=None if blocked else 0,
            candidates_listed=None if blocked else count,
        )
        report["m2"]["shares"]["archive_only"] = None if blocked else share
        report["m2"].update(
            unit=UNIT,
            archive_only_assessed=not blocked,
            archive_only_proven=False,
            archive_only_status="observed_archive_file_prefix_partial",
            archive_only_scope=(
                "unique canonical pre-cut filename series in directories outside frozen catalog"
            ),
            partial=True,
            **{"pass": report["m2"]["pass"] and not blocked and share <= 0.05},
        )
        report["not_assessed"].remove("m2_archive_only_dates_absent")
        report["partial_checks"] = [
            x for x in report["partial_checks"] if not x.startswith("m2(ii)")
        ]
        report["partial_checks"].append(
            "m2(ii): observed canonical pre-cut filename series in unknown directories only; "
            "not verified "
            "instrument identity, renames, trading-open or all market launches"
        )
        if unverified:
            report["not_assessed"].append("m2_unverified_pre_cut_filename")
        if collisions:
            report["not_assessed"].append("m2_prefix_collision_unverified")
        if share > 0.05:
            report["outcome"] = "incomplete_coverage"
            report["stop_codes"].append("incomplete_coverage")
        elif not report["not_assessed"] and not report["stop_codes"] and report["m2"]["pass"]:
            report["outcome"] = "stage1_pass_partial"
    report["supplement"] = {
        "rules": VERSION,
        "complete": complete,
        "file_prefix_check_complete": complete and not (unverified or collisions),
        "unit": UNIT,
        "source": source.binding,
        "prior_v2_report_sha256": p.V2_REPORT,
        "diagnostic_report_sha256": DIAGNOSTIC_REPORT,
        "unverified_directories": unverified,
        "collision_directories": collisions,
        "request_list_sha256": contract["request_list_sha256"],
        "requests": usage,
    }
    report["base_requests"] = report["requests"]
    report["requests"] = {
        "attempts": report["base_requests"]["attempts"] + usage["attempts"],
        "bytes": report["base_requests"]["bytes"] + usage["bytes"],
    }
    return report


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
    if any(
        root.resolve().is_relative_to(path.resolve())
        for path in (inputs.base.root, inputs.v2, inputs.diagnostic)
    ):
        raise ValueError("output must be outside all sealed sources")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    options: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    fetcher = m.PrivateFetcher(root, contract, client, specs=class_specs(), code=code, **options)
    observations: dict[str, dict[str, Any]] = {}
    complete = False
    intact = True
    slots = 0
    with m.private_logs():
        try:
            response = fetcher.run_request(
                m.OpaqueRequest("C3", "GET", s.ARCHIVE_ROOT), m.root_symbols, keep_facts=False
            )
            if response["outcome"] != "ok":
                raise s.FetchStoppedError("root_unavailable")
            observed = set(response["facts"]["symbols"])
            if not inputs.base.required_root <= observed:
                raise s.FetchStoppedError("known_archive_directory_missing")
            names = sorted(observed - inputs.base.symbols)
            if len(names) > contract["max_directories"]:
                raise s.FetchStoppedError("plan_exceeds_cap")
            for directory in names:
                request = m.OpaqueRequest("D1", "GET", s.ARCHIVE_ROOT + directory + "/")
                response = fetcher.run_request(
                    request, partial(listing, directory), keep_facts=False
                )
                if response["outcome"] != "ok":
                    raise s.FetchStoppedError("listing_unavailable")
                if response["facts"]:
                    facts = response["facts"]
                    slots += facts["prefix_slots"]
                    if slots > contract["max_pre_cut_prefix_slots"]:
                        raise s.FetchStoppedError("pre_cut_prefix_budget")
                    observations[request.key] = facts
            complete = True
        except (s.FetchStoppedError, s.BudgetExceededError):
            fetcher.record_stop("supplement_incomplete")
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
    accepted, unverified, collisions = classify(observations, inputs.base.symbols)
    if not complete:
        accepted = {}  # Unseen directories could collide; partial candidates are not admitted.
    for key in sorted(unverified):
        publish_once(root / "unverified" / f"{key}.json", {"status": "unverified_pre_cut_filename"})
    for key in sorted(collisions):
        publish_once(root / "collisions" / f"{key}.json", {"status": "prefix_collision_unverified"})
    for key, facts in accepted.items():
        publish_once(root / "admitted" / f"{key}.json", facts)
    report = combined_report(
        inputs,
        list(accepted.values()),
        complete,
        len(unverified),
        len(collisions),
        fetcher.summary(),
        contract,
    )
    if not intact:
        report["outcome"] = "source_integrity_error"
    evidence = {
        str(path.relative_to(root)): s.digest(path.read_bytes())
        for path in sorted(root.rglob("*.json"))
    }
    evidence_sha = publish_once(root / "evidence-hashes.json", evidence)
    report["supplement"].update(
        code=code,
        binding_valid=intact,
        evidence_sha256=evidence_sha,
        finished_at_utc=datetime.now(UTC).isoformat(),
    )
    publish_once(root / "combined-stage1-report.json", report)
    if complete:
        publish_once(
            root / "supplement_done.json",
            {"complete": True, "file_prefix_check_complete": not (unverified or collisions)},
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    fetch = sub.add_parser("fetch")
    for name in ("base", "v2", "diagnostic", "root"):
        fetch.add_argument("--" + name, type=Path, required=True)
    fetch.add_argument("--approved-sha256", required=True)
    args = parser.parse_args(argv)
    contract = build_contract()
    if args.command == "plan":
        print(json.dumps(contract, indent=2))  # noqa: T201
        return 0
    if args.approved_sha256 != contract["request_list_sha256"]:
        raise ValueError("approval differs from v3 contract")
    if json.loads(Path(CONTRACT).read_text()) != contract:
        raise ValueError("checked-in v3 contract differs")
    inputs = load_inputs(args.base, args.v2, args.diagnostic)
    revision = transport.clean_revision()
    p.require_merged(revision)
    code = {
        "revision": revision,
        "git_clean_before": True,
        "publication": publication.publication(list(PUBLISHED)),
    }

    def validate() -> None:
        load_inputs(args.base, args.v2, args.diagnostic)
        if (
            transport.clean_revision() != revision
            or publication.publication(list(PUBLISHED)) != code["publication"]
        ):
            raise ValueError("code publication changed")

    with httpx.Client(timeout=60, follow_redirects=False) as client:
        report = run(inputs, args.root, client, contract=contract, code=code, validate=validate)
    print(json.dumps({"outcome": report["outcome"], "complete": report["supplement"]["complete"]}))  # noqa: T201
    return 0 if report["supplement"]["complete"] and not report["not_assessed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
