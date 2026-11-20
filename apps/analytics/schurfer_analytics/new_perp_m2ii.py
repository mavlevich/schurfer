"""A separately approved, identity-private archive metadata supplement for I25."""

from __future__ import annotations

import argparse
import json
import logging
import re
import secrets
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from functools import partial
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economics as publication
from . import new_perp_stage1 as s
from .bybit_carry_feasibility import publish_once

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

VERSION = "new_perp_m2ii_v2"
SOURCE_REVISION = "8748d9ef664b4b0c65f885420692166dd8ad7a54"
SOURCE_REPORT = "400f635ea29ac52dc5999caa866b152fa14cea1b82381ce91270012821a853ca"
SOURCE_MANIFEST = "759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d"
SOURCE_INPUTS = "cc20f7d5b06208a18772d3df4f0a3427e1975750aea6f94a17845e5f627b4c5e"
MAX_DIRECTORIES = 1200
MAX_ATTEMPTS = 1500
MAX_BYTES = 128 * s.MIB
CONTRACT_PATH = "docs/research/evidence/new-perp-m2ii-v1/contract.json"
DOC_PATH = "docs/research/new-perp-m2ii-supplement-v1.md"
PUBLISHED = (
    "apps/analytics/schurfer_analytics/new_perp_m2ii.py",
    "apps/analytics/schurfer_analytics/new_perp_stage1.py",
    "apps/analytics/schurfer_analytics/bybit_carry_feasibility.py",
    "apps/analytics/schurfer_analytics/bybit_presettlement_bounded_fetch.py",
    DOC_PATH,
    CONTRACT_PATH,
    "docs/research/new-perp-short-lab-protocol-v1.md",
    "docs/research/seen-windows-registry.md",
)


def class_specs() -> dict[str, s.ClassSpec]:
    return {
        "C3": replace(
            s.CLASS_BY_ID["C3"],
            name="opaque_archive_root",
            max_requests=1,
            max_class_bytes=MAX_BYTES,
            parsed="observed USDT directory names in RAM only",
            discarded="raw HTML, unadmitted identities, dates, schema keys and body hashes",
            note="single snapshot; no C1, persisted candidate-name plan or resume",
        ),
        "D1": replace(
            s.CLASS_BY_ID["D1"],
            name="opaque_archive_dates",
            max_requests=MAX_DIRECTORIES,
            max_class_bytes=MAX_BYTES,
            expected=0,
            parsed="exact-prefix pre-cut facts, or opaque pre-cut filename identity uncertainty",
            discarded="post-cut names, dates and counts; raw HTML, schema keys and body hashes",
            note="plan size unknown before the root; frozen catalog subtraction, no resume",
        ),
    }


def build_contract() -> dict[str, Any]:
    body: dict[str, Any] = {
        "rules": VERSION,
        "source_revision": SOURCE_REVISION,
        "source_report_sha256": SOURCE_REPORT,
        "source_manifest_sha256": SOURCE_MANIFEST,
        "source_input_map_sha256": SOURCE_INPUTS,
        "window": [s.WINDOW_START_DAY, s.CUT_DAY],
        "classes": [asdict(c) for c in class_specs().values()],
        "go_limits": {"1": {"max_attempts": MAX_ATTEMPTS, "max_bytes": MAX_BYTES}},
        "max_directories": MAX_DIRECTORIES,
        "threshold": "archive_only/(frozen_catalog+archive_only) <= 0.05",
        "catalog_policy": "frozen window symbols only; no fresh C1 subtraction",
        "privacy": "random opaque request keys; no unadmitted names, raw bodies or body hashes",
        "identity": "exact directory/file prefix only; first archived day is not a launch date",
        "foreign_prefix": "valid pre-cut foreign-prefix file blocks M2(ii); opaque issue only",
        "filename_grammar": "[A-Za-z0-9_-]+YYYY-MM-DD.csv.gz; exact prefix required for admission",
        "restart": "no resume; existing output roots are refused before HTTP",
        "failure": "any incomplete source, response, scan or budget leaves stage1_incomplete",
        "operational_counts": "attempts and bytes may vary; no post-cut launch metric is kept",
    }
    body["request_list_sha256"] = s.contract_hash(body)
    return body


@dataclass(frozen=True)
class Source:
    root: Path
    symbols: frozenset[str]
    report: dict[str, Any]
    binding: dict[str, str]
    required_root: frozenset[str] = frozenset()


def load_source(root: Path) -> Source:
    """Validate the sealed source; never write into it or silently accept another cohort."""
    report_bytes = (root / "stage1-report.json").read_bytes()
    manifest = (root / "manifest.json").read_bytes()
    hashes = json.loads((root / "stage1-input-hashes.json").read_text())
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    if (
        s.digest(report_bytes) != SOURCE_REPORT
        or s.digest(manifest) != SOURCE_MANIFEST
        or s.digest(encoded) != SOURCE_INPUTS
    ):
        raise ValueError("source fingerprints differ")
    for name, expected in hashes.items():
        path = root / name
        if (
            not path.resolve().is_relative_to(root.resolve())
            or s.digest(path.read_bytes()) != expected
        ):
            raise ValueError("source input differs")
    run = json.loads((root / "run.json").read_text())
    report = json.loads(report_bytes)
    phase = json.loads((root / "phase0.json").read_text())
    symbols = frozenset(r["symbol"] for r in phase["catalog"]["rows"])
    if (
        run["code"]["revision"] != SOURCE_REVISION
        or len(symbols) != report["launches"]
        or report["outcome"] != "stage1_incomplete"
        or report["not_assessed"] != ["m2_archive_only_dates_absent"]
        or report["stop_codes"]
        or not (root / "go2_done.json").exists()
        or (root / "stop.json").exists()
        or any(not s.SYMBOL.fullmatch(symbol) for symbol in symbols)
    ):
        raise ValueError("source is not the frozen completed cohort")
    required: set[str] = set()
    for path in (root / "receipts").glob("*.json"):
        receipt = json.loads(path.read_text())
        if receipt["cls"] == "D1" and receipt["outcome"] == "ok" and receipt["symbol"] in symbols:
            required.add(receipt["symbol"])
    return Source(
        root.resolve(),
        symbols,
        report,
        {
            "revision": SOURCE_REVISION,
            "report_sha256": SOURCE_REPORT,
            "manifest_sha256": SOURCE_MANIFEST,
            "input_map_sha256": SOURCE_INPUTS,
        },
        frozenset(required),
    )


class Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value is not None:
                    self.links.append(value)


def links(body: bytes) -> list[str]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise s.SchemaMismatchError("not_utf8") from exc
    if "</html>" not in text.lower():
        raise s.SchemaMismatchError("incomplete_html")
    parser = Anchors()
    parser.feed(text)
    if not parser.links:
        raise s.SchemaMismatchError("no_anchors")
    return parser.links


def root_symbols(body: bytes) -> dict[str, Any]:
    names: set[str] = set()
    for href in links(body):
        if href in ("../", "/"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)/", href)
        if not match:
            raise s.SchemaMismatchError("unsupported_root_anchor")
        symbol = match[1]
        if symbol.endswith("USDT") and not s.SYMBOL.fullmatch(symbol):
            raise s.SchemaMismatchError("unsupported_usdt_directory")
        if s.SYMBOL.fullmatch(symbol):
            names.add(symbol)
    if not names:
        raise s.SchemaMismatchError("no_usdt_directories")
    return {"symbols": sorted(names)}  # RAM only; never passed to publish_once.


def listing(symbol: str, body: bytes) -> dict[str, Any]:
    before: list[str] = []
    recognized = False
    foreign_before = False
    pattern = re.compile(r"([A-Za-z0-9_-]+)(\d{4}-\d{2}-\d{2})\.csv\.gz")
    for href in links(body):
        if href == "../":
            continue
        match = pattern.fullmatch(href)
        if not match:
            raise s.SchemaMismatchError("unsupported_file_anchor")
        try:
            day = date.fromisoformat(match[2]).isoformat()
        except ValueError as exc:
            raise s.SchemaMismatchError("invalid_file_day") from exc
        recognized = True
        if day < s.CUT_DAY:
            if match[1] == symbol:
                before.append(day)
            else:
                foreign_before = True
    if not recognized:
        raise s.SchemaMismatchError("unverifiable_listing")
    if foreign_before:
        # Neither prefix is admitted as an instrument, even alongside matching files.
        return {"identity_status": "unverified_pre_cut_filename"}
    if not before:
        return {}  # Positive format evidence, no post-cut identity/date/count survives.
    days = sorted(set(before))
    return {
        "symbol": symbol,
        "first_day": days[0],
        "last_day_before_cut": days[-1],
        "file_count_before_cut": len(days),
    }


@dataclass(frozen=True, repr=False)
class OpaqueRequest(s.Request):
    token: str = field(default_factory=lambda: secrets.token_hex(32))

    def __post_init__(self) -> None:
        expected = (self.cls == "C3" and self.url == s.ARCHIVE_ROOT) or (
            self.cls == "D1"
            and re.fullmatch(re.escape(s.ARCHIVE_ROOT) + r"[A-Z0-9]+USDT/", self.url)
        )
        if (
            not expected
            or self.method != "GET"
            or self.symbol
            or self.meta
            or self.params
            or re.fullmatch(r"[a-f0-9]{64}", self.token) is None
        ):
            raise ValueError("opaque request outside the metadata contract")

    @property
    def key(self) -> str:
        return self.token

    def __repr__(self) -> str:
        return f"OpaqueRequest(cls={self.cls!r}, token={self.token!r})"


class PrivateFetcher(s.Fetcher):
    def _attempt(
        self,
        seq: int,
        request: s.Request,
        reduce: Callable[[bytes], dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int | None]:
        receipt, facts, asked = super()._attempt(seq, request, reduce)
        # Even key-only JSON schema errors can contain an unadmitted symbol as a JSON key.
        for key in ("schema", "body_sha256", "ret_msg"):
            receipt.pop(key, None)
        return receipt, facts, asked


@contextmanager
def private_logs() -> Iterator[None]:
    """HTTP libraries must not publish request URLs before admission by the reducer."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def combined_report(
    source: Source,
    accepted: list[dict[str, Any]],
    complete: bool,
    usage: dict[str, Any],
    contract: dict[str, Any],
    *,
    identity_unverified: int = 0,
) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(json.dumps(source.report))
    archive_only = sum(s.WINDOW_START_DAY <= r["first_day"] < s.CUT_DAY for r in accepted)
    n = len(source.symbols)
    share = archive_only / (n + archive_only)
    report["version"] += "+m2ii_v2"
    if complete:
        report["m2"]["counts"]["archive_only_in_window"] = archive_only
        report["m2"]["counts"]["archive_only_unlisted"] = 0
        report["m2"]["counts"]["candidates_listed"] = archive_only
        report["m2"]["shares"]["archive_only"] = share
        report["m2"].update(
            archive_only_assessed=True,
            archive_only_proven=False,
            archive_only_scope="observed archive, first file inside window, outside frozen catalog",
            archive_only_status="observed_archive_partial",
            partial=True,
            **{"pass": report["m2"]["pass"] and share <= 0.05},
        )
        report["not_assessed"].remove("m2_archive_only_dates_absent")
        report["partial_checks"] = [
            x for x in report["partial_checks"] if not x.startswith("m2(ii)")
        ]
        report["partial_checks"].append(
            "m2(ii): observed first archive day only; not proof of instrument launch, "
            "ticker identity, or completeness beyond the observed archive index"
        )
        if share > 0.05:
            report["outcome"] = "incomplete_coverage"
            report["stop_codes"].append("incomplete_coverage")
        elif not report["not_assessed"] and not report["stop_codes"] and report["m2"]["pass"]:
            report["outcome"] = "stage1_pass_partial"
    if complete and identity_unverified:
        # A completed HTTP scan is not a completed identity check. Never assign a
        # mismatched prefix to the directory or erase it from the denominator.
        report["m2"]["counts"]["archive_filename_identity_unverified"] = identity_unverified
        report["m2"]["counts"]["archive_only_unlisted"] = None
        report["m2"]["counts"]["candidates_listed"] = None
        report["m2"]["shares"]["archive_only"] = None
        report["m2"].update(
            archive_only_assessed=False,
            archive_only_status="filename_identity_unverified",
            **{"pass": False},
        )
        report["not_assessed"].append("m2_archive_filename_identity_unverified")
        report["partial_checks"].append(
            "m2(ii): pre-cut foreign-prefix filenames have unresolved identity; "
            "no prefix is assigned to an instrument and no positive verdict is allowed"
        )
        # A known threshold breach remains a failure; uncertainty cannot mask it.
        if share <= 0.05:
            report["outcome"] = "stage1_incomplete"
    report["supplement"] = {
        "rules": VERSION,
        "complete": complete,
        "identity_check_complete": complete and not identity_unverified,
        "identity_unverified_directories": identity_unverified,
        "source": source.binding,
        "request_list_sha256": contract["request_list_sha256"],
        "requests": usage,
    }
    # Preserve base accounting explicitly; this companion includes the separately charged run.
    report["base_requests"] = report["requests"]
    report["requests"] = {
        "attempts": report["base_requests"]["attempts"] + usage["attempts"],
        "bytes": report["base_requests"]["bytes"] + usage["bytes"],
    }
    return report


def run(
    source: Source,
    root: Path,
    client: httpx.Client,
    *,
    contract: dict[str, Any],
    code: dict[str, Any],
    specs: dict[str, s.ClassSpec] | None = None,
    sleep: Callable[[float], None] | None = None,
    validate_source: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if root.resolve().is_relative_to(source.root):
        raise ValueError("output must be outside the sealed source")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)  # No resume or root-reset loophole.
    options: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    fetcher = PrivateFetcher(
        root,
        contract,
        client,
        go=1,
        code=code,
        specs=class_specs() if specs is None else specs,
        **options,
    )
    accepted: list[dict[str, Any]] = []
    identity_unverified = 0
    complete = False
    source_intact = True
    with private_logs():
        try:
            result = fetcher.run_request(
                OpaqueRequest("C3", "GET", s.ARCHIVE_ROOT), root_symbols, keep_facts=False
            )
            if result["outcome"] != "ok":
                raise s.FetchStoppedError("root_unavailable")
            observed = set(result["facts"]["symbols"])
            if not source.required_root <= observed:
                raise s.FetchStoppedError("known_archive_directory_missing")
            names = sorted(observed - source.symbols)
            if len(names) > contract["max_directories"]:
                raise s.FetchStoppedError("plan_exceeds_cap")
            for symbol in names:
                request = OpaqueRequest("D1", "GET", s.ARCHIVE_ROOT + symbol + "/")

                result = fetcher.run_request(request, partial(listing, symbol), keep_facts=False)
                if result["outcome"] != "ok":
                    raise s.FetchStoppedError("listing_unavailable")
                facts = result["facts"]
                if facts.get("identity_status") == "unverified_pre_cut_filename":
                    identity_unverified += 1
                    publish_once(root / "identity-unverified" / f"{request.key}.json", facts)
                elif facts:
                    accepted.append(facts)
                    publish_once(root / "admitted" / f"{request.key}.json", facts)
            complete = True
        except (s.FetchStoppedError, s.BudgetExceededError):
            fetcher.record_stop("supplement_incomplete")
        except KeyboardInterrupt:
            fetcher.record_stop("interrupted")
        except Exception:
            # Neither exception messages nor traceback locals may expose unadmitted URLs.
            fetcher.record_stop("unexpected_error")
        if validate_source is not None:
            try:
                validate_source()
            except Exception:
                source_intact = False
                complete = False
                fetcher.record_stop("source_integrity_error")
    usage = fetcher.summary()
    report = combined_report(
        source, accepted, complete, usage, contract, identity_unverified=identity_unverified
    )
    if not source_intact:
        report["outcome"] = "source_integrity_error"
    evidence = {
        str(path.relative_to(root)): s.digest(path.read_bytes())
        for path in sorted(root.rglob("*.json"))
    }
    evidence_sha = publish_once(root / "evidence-hashes.json", evidence)
    report["supplement"].update(
        code=code,
        binding_valid=source_intact,
        evidence_sha256=evidence_sha,
        finished_at_utc=datetime.now(UTC).isoformat(),
    )
    publish_once(root / "combined-stage1-report.json", report)
    if complete:
        publish_once(
            root / "supplement_done.json",
            {"complete": True, "identity_check_complete": not identity_unverified},
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--source", type=Path, required=True)
    fetch.add_argument("--root", type=Path, required=True)
    fetch.add_argument("--approved-sha256", required=True)
    args = parser.parse_args(argv)
    contract = build_contract()
    if args.command == "plan":
        print(json.dumps(contract, indent=2))  # noqa: T201
        return 0
    if args.approved_sha256 != contract["request_list_sha256"]:
        raise ValueError("approval differs from the supplement contract")
    if json.loads(Path(CONTRACT_PATH).read_text()) != contract:
        raise ValueError("checked-in supplement contract differs")
    source = load_source(args.source)
    code = {
        "revision": transport.clean_revision(),
        "git_clean_before": True,
        "publication": publication.publication(list(PUBLISHED)),
        "source": source.binding,
    }

    def validate_inputs() -> None:
        load_source(args.source)
        if (
            transport.clean_revision() != code["revision"]
            or publication.publication(list(PUBLISHED)) != code["publication"]
        ):
            raise ValueError("code binding changed")

    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        report = run(
            source,
            args.root,
            client,
            contract=contract,
            code=code,
            validate_source=validate_inputs,
        )
    print(json.dumps({"outcome": report["outcome"], "complete": report["supplement"]["complete"]}))  # noqa: T201
    return 0 if report["supplement"]["complete"] and not report["not_assessed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
