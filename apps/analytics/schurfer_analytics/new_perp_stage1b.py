"""Sealed I25 Stage 1b metadata only. No body/coverage fetch is implemented here."""

from __future__ import annotations

import argparse
import json
import re
import secrets
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economics as publication
from . import new_perp_m2ii as m
from . import new_perp_m2ii_identity_probe as p
from . import new_perp_m2ii_v3 as v
from . import new_perp_stage1 as s
from .bybit_carry_feasibility import publish_once
from .catalyst_feasibility import asset
from .new_perp_window import HOLDOUT, CoverageWindow

if TYPE_CHECKING:
    from collections.abc import Callable

VERSION = "new_perp_stage1b_metadata_v1"
DOC = "docs/research/new-perp-stage1b-metadata-v1.md"
CONTRACT = "docs/research/evidence/new-perp-stage1b-metadata-v1/contract.json"
SOURCE_REPORT = "38b569aa5dc713179ded3db70f7cdf788cea560db7c0c99c65d9332a879070b8"
SOURCE_MAP = "532d101483c2691c422e1ddce2187423dfce5d460c4de1b719d626b66645ad03"
PUBLISHED = tuple(
    dict.fromkeys(
        (
            *s.PUBLISHED,
            *v.PUBLISHED,
            DOC,
            CONTRACT,
            "apps/analytics/schurfer_analytics/new_perp_window.py",
            "apps/analytics/schurfer_analytics/new_perp_stage1b.py",
        )
    )
)
MAX_CATALOG = 300
MAX_UNKNOWN = 1200
MAX_PREFIX_SLOTS = 16384


def class_specs() -> dict[str, s.ClassSpec]:
    limits = {
        "C1": (9, 28 * s.MIB),
        "C3": (1, 9 * s.MIB),
        "D1": (1500, 384 * s.MIB),
        "D2h": (300, 0),
    }
    return {
        name: replace(
            s.CLASS_BY_ID[name],
            max_requests=count,
            max_class_bytes=cap,
            parsed="sealed holdout metadata allowlist; RAM-only outside-window labels",
            discarded="raw response, excluded labels/dates, values, schema and body hash",
            params="bounded metadata parameters; cursor and target in RAM only",
            expected=0,
            note="no CSV GET, bars, funding, books, resume or stage pass",
        )
        for name, (count, cap) in limits.items()
    }


def build_contract() -> dict[str, Any]:
    contract: dict[str, Any] = {
        "rules": VERSION,
        "window": asdict(HOLDOUT),
        "prerequisite_report_sha256": SOURCE_REPORT,
        "prerequisite_evidence_map_sha256": SOURCE_MAP,
        "classes": [asdict(spec) for spec in class_specs().values()],
        "go_limits": {"1": {"max_attempts": 2400, "max_bytes": 448 * s.MIB}},
        "max_catalog_rows": MAX_CATALOG,
        "max_unknown_directories": MAX_UNKNOWN,
        "max_pre_cut_prefix_slots": MAX_PREFIX_SLOTS,
        "catalog_pagination": {
            "limit": 1000,
            "max_pages_per_status": 3,
            "full_page_without_cursor": "unproven stop",
        },
        "catalog_numeric_fields": ["min_notional", "qty_step", "min_order_qty", "max_leverage"],
        "clock": "E=ceil_minute(T0+7d); X=E+7d; censor X>cut, not X==cut",
        "before_window": "RAM-only filename dates and catalog labels; fixed predicates persist",
        "pre_window_first_file": "t0_pre_window_unreadable; never HEAD or GET",
        "catalog_denominator": "every in-window row remains N, even no_archive or unnamed",
        "catalog_d1": "strict shared HTML parser; foreign prefixes unverified and in frozen set",
        "listing_grammar": "full filename prefix + ISO YYYY-MM-DD.csv.gz, complete UTF-8 HTML",
        "canonical_prefix": s.SYMBOL.pattern,
        "m2_frozen": "holdout catalog labels and all canonical pre-cut catalog-folder prefixes",
        "unknown_pre_window": "boolean only; prefix requires matching in-window filename",
        "m2": "observed pre-cut file-prefix series outside catalog; a/(N+a)<=0.05; N=0 stops",
        "retention": "0700 sealed facts; in-window-evidenced names only; public aggregates",
        "transport": {
            "read_reserve_bytes": s.READ_RESERVE_BYTES,
            "max_sends": s.ATTEMPTS_PER_REQUEST,
            "spacing_s": s.SPACING_S,
            "max_retry_after_s": s.MAX_RETRY_AFTER_S,
        },
        "body_phase": "not implemented, not authorized; separate reviewed manifest/hash/go",
        "restart": "fresh root only; no resume, source modification or reused allowance",
        "success": "metadata_complete_coverage_pending, never stage1b_pass_partial",
        "known_metadata_failure": "M2(i) or M2(ii) known share >5% outranks uncertainty",
    }
    contract["request_list_sha256"] = s.contract_hash(contract)
    return contract


def numeric_field(item: dict[str, Any], outer: str, inner: str) -> str | None:
    value = s._filter(item, outer, inner)
    if value is not None and re.fullmatch(r"\d{1,20}(?:\.\d{1,20})?", value) is None:
        raise s.SchemaMismatchError("catalog_numeric_field")
    return value


@dataclass(frozen=True, repr=False)
class OpaqueRequest(s.Request):
    token: str = field(default_factory=lambda: secrets.token_hex(32))

    def __post_init__(self) -> None:
        valid = False
        if self.cls == "C1" and self.method == "GET" and self.url == s.CATALOG_URL:
            params = dict(self.params)
            valid = (
                len(params) == len(self.params)
                and set(params) <= {"category", "status", "limit", "cursor"}
                and params.get("category") == "linear"
                and params.get("limit") == "1000"
                and params.get("status") in s.CATALOG_STATUSES
                and len(params.get("cursor", "")) <= 8192
            )
        elif not self.params:
            if self.cls == "C3":
                valid = self.method == "GET" and self.url == s.ARCHIVE_ROOT
            elif self.cls == "D1":
                match = re.fullmatch(re.escape(s.ARCHIVE_ROOT) + r"([A-Z0-9]+USDT)/", self.url)
                valid = (
                    self.method == "GET"
                    and match is not None
                    and s.SYMBOL.fullmatch(match[1]) is not None
                )
            elif self.cls == "D2h":
                match = re.fullmatch(
                    re.escape(s.ARCHIVE_ROOT)
                    + r"([A-Z0-9]+USDT)/([A-Z0-9]+USDT)(\d{4}-\d{2}-\d{2})\.csv\.gz",
                    self.url,
                )
                valid = (
                    self.method == "HEAD"
                    and match is not None
                    and match[1] == match[2]
                    and s.SYMBOL.fullmatch(match[1]) is not None
                    and HOLDOUT.contains_day(match[3])
                )
        if (
            not valid
            or self.symbol
            or self.meta
            or re.fullmatch("[a-f0-9]{64}", self.token) is None
        ):
            raise ValueError("request outside sealed metadata contract")

    @property
    def key(self) -> str:
        return self.token

    def __repr__(self) -> str:
        return f"OpaqueRequest(cls={self.cls!r}, token={self.token!r})"


def catalog_page(body: bytes, window: CoverageWindow = HOLDOUT) -> dict[str, Any]:
    try:
        result = s._envelope(json.loads(body))
    except ValueError as exc:
        raise s.SchemaMismatchError("catalog_envelope") from exc
    items, cursor = result.get("list"), result.get("nextPageCursor") or ""
    if not isinstance(items, list) or not isinstance(cursor, str) or len(cursor) > 8192:
        raise s.SchemaMismatchError("catalog_list")
    if len(items) > 1000:
        raise s.SchemaMismatchError("catalog_page_size")
    rows, prior = [], []
    for item in items:
        if not isinstance(item, dict):
            raise s.SchemaMismatchError("catalog_row")
        try:
            launch = s._int_ms(item.get("launchTime"))
        except (TypeError, ValueError) as exc:
            raise s.SchemaMismatchError("launch_time") from exc
        if launch >= window.cut_ms:
            continue
        if item.get("contractType") != "LinearPerpetual" or item.get("settleCoin") != "USDT":
            continue
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not s.SYMBOL.fullmatch(symbol):
            raise s.SchemaMismatchError("catalog_symbol")
        base = asset(symbol[:-4])
        raw = item.get("deliveryTime")
        try:
            delivery = None if raw in (None, "", 0, "0") else s._int_ms(raw)
        except (TypeError, ValueError) as exc:
            raise s.SchemaMismatchError("delivery_time") from exc
        if launch < window.start_ms:
            prior.append(
                {
                    "symbol": symbol,
                    "base": base,
                    "delivered_before_window": delivery is not None and delivery < window.start_ms,
                    "delivery_ms": delivery
                    if delivery is not None and window.contains(delivery)
                    else None,
                }
            )
            continue
        interval = item.get("fundingInterval")
        rows.append(
            {
                "symbol": symbol,
                "base": base,
                "launch_ms": launch,
                "delivery_ms": delivery
                if delivery is not None and window.contains(delivery)
                else None,
                "funding_interval_min": interval if type(interval) is int else None,
                "pre_listing_field": "isPreListing" in item,
                **{
                    key: numeric_field(item, outer, inner)
                    for key, outer, inner in (
                        ("min_notional", "lotSizeFilter", "minNotionalValue"),
                        ("qty_step", "lotSizeFilter", "qtyStep"),
                        ("min_order_qty", "lotSizeFilter", "minOrderQty"),
                        ("max_leverage", "leverageFilter", "maxLeverage"),
                    )
                },
            }
        )
    return {
        "rows": rows,
        "prior": prior,
        "next_cursor": cursor,
        "unproven_full_page": len(items) == 1000 and not cursor,
    }


def listing(body: bytes, window: CoverageWindow = HOLDOUT) -> dict[str, set[str]]:
    """One strict parser for catalog and unknown folders. Returned facts are RAM only."""
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
        if day < window.cut_day:
            before.setdefault(match[1], set()).add(day)
            if len(before) > MAX_PREFIX_SLOTS:
                raise s.SchemaMismatchError("pre_cut_prefix_budget")
    if not recognized:
        raise s.SchemaMismatchError("unverifiable_listing")
    return before


def catalog_facts(
    row: dict[str, Any],
    before: dict[str, set[str]] | None,
    state: str,
    window: CoverageWindow = HOLDOUT,
) -> dict[str, Any]:
    symbol, launch = row["symbol"], row["launch_ms"]
    days = sorted((before or {}).get(symbol, set()))
    foreign = bool(before and (len(before) != 1 or symbol not in before))
    first = days[0] if days else None
    within = first is not None and abs(s.day_diff(first, s.day_of(launch))) <= 1
    reuse = bool(
        days
        and (
            s.day_diff(s.day_of(launch), days[0]) > 30
            or any(a < s.day_of(launch) and s.day_diff(b, a) > 30 for a, b in pairwise(days))
        )
    )
    safe_label = bool(not foreign and any(window.contains_day(day) for day in days))
    if state == "absent":
        clock = "no_archive"
    elif foreign:
        clock = "filename_identity_unverified"
    elif first is None:
        clock = "no_pre_cut_file"
    elif first < window.start_day:
        clock = "t0_pre_window_unreadable"
    else:
        clock = s.class_from_first_day(first, launch) or "csv_pending"
    facts: dict[str, Any] = {
        "launch_ms": launch,
        "listing_state": state,
        "has_pre_window_file": any(day < window.start_day for day in days),
        "first_file_within_1d_of_launch": within,
        "first_file_class_from_day": s.class_from_first_day(first, launch),
        "ticker_reuse_suspected": reuse,
        "filename_identity_unverified": foreign,
        "t0_status": clock,
        "reason": "t0_unavailable" if clock != "csv_pending" else "csv_pending",
    }
    if safe_label:
        facts["symbol"] = symbol
        facts.update({key: value for key, value in row.items() if key not in ("symbol", "base")})
    if safe_label and within and first is not None and window.contains_day(first):
        facts["first_file_day"] = first
    return facts


def unknown_observation(
    before: dict[str, set[str]], window: CoverageWindow = HOLDOUT
) -> dict[str, Any]:
    canonical = sorted(prefix for prefix in before if s.SYMBOL.fullmatch(prefix))
    unverified = len(before) != 1 or len(canonical) != 1
    candidate: dict[str, Any] | None = None
    if not unverified:
        prefix = canonical[0]
        days = sorted(before[prefix])
        in_window = [day for day in days if window.contains_day(day)]
        candidate = {"has_pre_window_file": days[0] < window.start_day}
        if in_window:
            candidate.update(
                file_prefix=prefix,
                first_day_in_window=in_window[0],
                last_day_in_window=in_window[-1],
                file_count_in_window=len(in_window),
            )
    return {"canonical_prefixes": canonical, "unverified": unverified, "candidate": candidate}


def load_prerequisite(base: Path, v2: Path, diagnostic: Path, discovery: Path) -> None:
    v.load_inputs(base, v2, diagnostic)
    raw = (discovery / "combined-stage1-report.json").read_bytes()
    mapping = (discovery / "evidence-hashes.json").read_bytes()
    if s.digest(raw) != SOURCE_REPORT or s.digest(mapping) != SOURCE_MAP:
        raise ValueError("discovery prerequisite differs")
    for name, expected in json.loads(mapping).items():
        path = discovery / name
        if (
            not path.resolve().is_relative_to(discovery.resolve())
            or s.digest(path.read_bytes()) != expected
        ):
            raise ValueError("discovery evidence differs")
    report = json.loads(raw)
    if (
        report["outcome"] != "stage1_pass_partial"
        or report["not_assessed"]
        or report["stop_codes"]
        or not report["supplement"]["complete"]
        or not report["supplement"]["binding_valid"]
        or not (discovery / "supplement_done.json").exists()
        or (discovery / "stop.json").exists()
    ):
        raise ValueError("discovery prerequisite incomplete")
    if json.loads((discovery / "supplement_done.json").read_text()) != {
        "complete": True,
        "file_prefix_check_complete": True,
    }:
        raise ValueError("discovery completion marker differs")


def fetch_catalog(fetcher: m.PrivateFetcher) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    rows: dict[str, dict[str, Any]] = {}
    prior: dict[str, dict[str, Any]] = {}
    for status in s.CATALOG_STATUSES:
        cursor = ""
        seen: set[str] = set()
        for _ in range(3):
            params: tuple[tuple[str, str], ...] = (
                ("category", "linear"),
                ("status", status),
                ("limit", "1000"),
            )
            if cursor:
                params += (("cursor", cursor),)
            result = fetcher.run_request(
                OpaqueRequest("C1", "GET", s.CATALOG_URL, params), catalog_page, keep_facts=False
            )
            if result["outcome"] != "ok":
                raise s.FetchStoppedError("catalog_unavailable")
            facts = result["facts"]
            if facts["unproven_full_page"]:
                raise s.FetchStoppedError("catalog_truncation_unproven")
            for kind, target in (("rows", rows), ("prior", prior)):
                for row in facts[kind]:
                    symbol = row["symbol"]
                    if symbol in target and target[symbol] != row:
                        raise s.FetchStoppedError("catalog_duplicate_conflict")
                    target[symbol] = row
            if len(rows) > MAX_CATALOG:
                raise s.FetchStoppedError("catalog_plan_exceeds_cap")
            cursor = facts["next_cursor"]
            if not cursor:
                break
            if cursor in seen:
                raise s.FetchStoppedError("catalog_cursor_cycle")
            seen.add(cursor)
        else:
            raise s.FetchStoppedError("catalog_pages_exceed_cap")
    contradictions = s.catalog_contradictions(
        {"catalog": {"rows": list(rows.values()), "prior": list(prior.values())}}
    )
    return rows, contradictions


def run(
    root: Path,
    client: httpx.Client,
    *,
    contract: dict[str, Any],
    code: dict[str, Any],
    validate: Callable[[], None],
    sources: tuple[Path, ...] = (),
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    if any(root.resolve().is_relative_to(path.resolve()) for path in sources):
        raise ValueError("output inside a sealed source")
    try:
        validate()
    except Exception:
        raise ValueError("source/code prerequisite invalid") from None
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    options: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    fetcher = m.PrivateFetcher(root, contract, client, specs=class_specs(), code=code, **options)
    catalog: dict[str, dict[str, Any]] = {}
    facts: dict[str, dict[str, Any]] = {}
    observations: dict[str, dict[str, Any]] = {}
    frozen: set[str] = set()
    complete, intact, catalog_complete = False, True, False
    accepted: dict[str, dict[str, Any]] = {}
    unverified: set[str] = set()
    collisions: set[str] = set()
    with m.private_logs():
        try:
            catalog, contradictions = fetch_catalog(fetcher)
            catalog_complete = True
            if not catalog:
                raise s.FetchStoppedError("insufficient_events")
            frozen.update(catalog)
            result = fetcher.run_request(
                OpaqueRequest("C3", "GET", s.ARCHIVE_ROOT), m.root_symbols, keep_facts=False
            )
            if result["outcome"] != "ok":
                raise s.FetchStoppedError("root_unavailable")
            unknown = set(result["facts"]["symbols"]) - set(catalog)
            if len(unknown) > MAX_UNKNOWN:
                raise s.FetchStoppedError("unknown_plan_exceeds_cap")
            slots = 0
            # Read all catalog folders first so every canonical prefix can protect M2(ii).
            for directory in [*sorted(catalog), *sorted(unknown)]:
                request = OpaqueRequest("D1", "GET", s.ARCHIVE_ROOT + directory + "/")
                response = fetcher.run_request(request, listing, keep_facts=False)
                state = response["outcome"]
                if state not in ("ok", "absent") or (state == "absent" and directory in unknown):
                    raise s.FetchStoppedError("listing_unavailable")
                before = response["facts"] if state == "ok" else None
                slots += len(before or {})
                if slots > MAX_PREFIX_SLOTS:
                    raise s.FetchStoppedError("pre_cut_prefix_budget")
                if directory in catalog:
                    frozen.update(prefix for prefix in before or {} if s.SYMBOL.fullmatch(prefix))
                    row = catalog_facts(catalog[directory], before, state)
                    row["catalog_identity"] = contradictions.get(directory, "not_contradicted")
                    facts[request.key] = row
                elif before:
                    observations[request.key] = unknown_observation(before)
            accepted, unverified, collisions = v.classify(observations, frozenset(frozen))
            for row in facts.values():
                if "first_file_day" not in row:
                    continue
                symbol, day = row["symbol"], row["first_file_day"]
                head = OpaqueRequest("D2h", "HEAD", s.first_file_url(symbol, day))
                response = fetcher.run_request(head, keep_facts=False)
                if response["outcome"] not in ("ok", "absent"):
                    raise s.FetchStoppedError("head_unavailable")
                row["head"] = {
                    "key": head.key,
                    "outcome": response["outcome"],
                    "content_length": (response["facts"] or {}).get("content_length"),
                }
            complete = True
        except (s.FetchStoppedError, s.BudgetExceededError) as exc:
            fetcher.record_stop(str(exc))
        except KeyboardInterrupt:
            fetcher.record_stop("interrupted")
        except Exception:
            fetcher.record_stop("unexpected_error")
        try:
            validate()
        except Exception:
            complete, intact = False, False
            fetcher.record_stop("source_integrity_error")
    if not complete:
        accepted = {}
    if not intact:
        facts = {}
        catalog_complete = False
    # Missing and anonymized rows never disappear from the frozen denominator.
    n = len(catalog) if catalog_complete else None
    if catalog_complete:
        while len(facts) < len(catalog):
            facts[secrets.token_hex(32)] = {
                "t0_status": "listing_unavailable",
                "reason": "t0_unavailable",
            }
    for key, row in facts.items():
        publish_once(root / "catalog" / f"{key}.json", row)
    for key, row in accepted.items():
        publish_once(root / "admitted" / f"{key}.json", row)
    for folder, keys, status in (
        ("unverified", unverified, "unverified_pre_cut_filename"),
        ("collisions", collisions, "prefix_collision_unverified"),
    ):
        for key in keys:
            publish_once(root / folder / f"{key}.json", {"status": status})
    count = sum(not row["has_pre_window_file"] for row in accepted.values())
    share = count / (n + count) if complete and n and not (unverified or collisions) else None
    missing_near = sum(
        row.get("first_file_within_1d_of_launch") is False
        and not row.get("filename_identity_unverified", True)
        for row in facts.values()
    )
    known_failures = []
    if complete and n and missing_near / n > 0.05:
        known_failures.append("m2_catalog_no_file_near_launch")
    if complete and n and count / (n + count) > 0.05:
        known_failures.append("m2_archive_only_share")
    outcome = "metadata_complete_coverage_pending" if complete else "metadata_incomplete"
    if complete and (unverified or collisions):
        outcome = "metadata_complete_unassessed"
    if n == 0:
        outcome = "insufficient_events"
    if known_failures:
        outcome = "incomplete_coverage"
    if not intact:
        outcome = "source_integrity_error"
    manifest = {
        "complete": complete,
        "catalog_slots": len(facts),
        "catalog_keys": sorted(facts),
        "admitted_keys": sorted(accepted),
        "body_phase_authorized": False,
    }
    manifest_sha = publish_once(root / "manifest.json", manifest)
    usage = fetcher.summary()
    not_assessed = ["m1_csv_pending", "m3_historical_identity_not_proven", "m4_m5_m6_body_pending"]
    if not complete:
        not_assessed.append("metadata_scan_incomplete")
    if unverified:
        not_assessed.append("m2_unverified_pre_cut_filename")
    if collisions:
        not_assessed.append("m2_prefix_collision_unverified")
    if any(row.get("filename_identity_unverified", True) for row in facts.values()):
        not_assessed.append("m2_catalog_filename_unverified")
    report: dict[str, Any] = {
        "rules": VERSION,
        "window": asdict(HOLDOUT),
        "outcome": outcome,
        "complete": complete,
        "binding_valid": intact,
        "code": code,
        "request_list_sha256": contract["request_list_sha256"],
        "prerequisite_report_sha256": SOURCE_REPORT,
        "manifest_sha256": manifest_sha,
        "catalog_count": n,
        "catalog_slots": len(facts),
        "not_assessed": not_assessed,
        "stop_reason": json.loads((root / "stop.json").read_text())["reason"]
        if (root / "stop.json").exists()
        else None,
        "m1_states": dict(Counter(row["t0_status"] for row in facts.values())),
        "reasons": dict(Counter(row["reason"] for row in facts.values())),
        "m2_i_no_file_near_launch": missing_near,
        "m2_i_unproven": sum(
            row.get("filename_identity_unverified", True) for row in facts.values()
        ),
        "known_coverage_failures": known_failures,
        "first_file_sizes": {
            "heads": sum("head" in row for row in facts.values()),
            "known_files": sum(
                type(row.get("head", {}).get("content_length")) is int
                and row["head"]["outcome"] == "ok"
                for row in facts.values()
            ),
            "known_bytes": sum(
                row["head"]["content_length"]
                for row in facts.values()
                if type(row.get("head", {}).get("content_length")) is int
                and row["head"]["outcome"] == "ok"
            ),
            "unknown_or_absent": sum(
                "head" in row
                and (row["head"]["outcome"] != "ok" or row["head"]["content_length"] is None)
                for row in facts.values()
            ),
        },
        "m3_catalog_states": dict(
            Counter(row.get("catalog_identity", "unavailable") for row in facts.values())
        ),
        "m3_ticker_reuse_suspected": sum(
            bool(row.get("ticker_reuse_suspected")) for row in facts.values()
        ),
        "m2ii": {
            "unit": v.UNIT,
            "catalog_denominator": n,
            "admitted_series": len(accepted),
            "archive_only_in_window": count,
            "archive_only_pre_window": sum(row["has_pre_window_file"] for row in accepted.values()),
            "unverified_directories": len(unverified),
            "collision_directories": len(collisions),
            "share": share,
            "archive_only_proven": False,
        },
        "requests": usage,
        "body_phase_authorized": False,
        "partial_checks": [
            "M1 timestamps pending CSV; day predicates are not actual T0",
            "M2(ii) observed file series, not verified instruments or a census of launches",
            "M3 current catalog, not proof of independent identity known at E",
            "M4 coarse funding, M5 entry/hour coverage, M6 static checks await body contract",
        ],
        "finished_at_utc": datetime.now(UTC).isoformat(),
    }
    evidence = {
        str(path.relative_to(root)): s.digest(path.read_bytes())
        for path in sorted(root.rglob("*.json"))
    }
    report["evidence_map_sha256"] = publish_once(root / "evidence-hashes.json", evidence)
    publish_once(root / "metadata-report.json", report)
    if complete:
        publish_once(
            root / "metadata_done.json", {"complete": True, "body_phase_authorized": False}
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    fetch = sub.add_parser("fetch-metadata")
    for name in ("base", "v2", "diagnostic", "discovery", "root"):
        fetch.add_argument("--" + name, type=Path, required=True)
    fetch.add_argument("--approved-sha256", required=True)
    args = parser.parse_args(argv)
    contract = build_contract()
    if args.command == "plan":
        print(json.dumps(contract, indent=2))  # noqa: T201
        return 0
    if (
        args.approved_sha256 != contract["request_list_sha256"]
        or json.loads(Path(CONTRACT).read_text()) != contract
    ):
        raise ValueError("approval or checked-in metadata contract differs")
    paths = (args.base, args.v2, args.diagnostic, args.discovery)
    load_prerequisite(*paths)
    revision = transport.clean_revision()
    p.require_merged(revision)
    code = {
        "revision": revision,
        "git_clean_before": True,
        "publication": publication.publication(list(PUBLISHED)),
    }

    def validate() -> None:
        load_prerequisite(*paths)
        if (
            transport.clean_revision() != revision
            or publication.publication(list(PUBLISHED)) != code["publication"]
        ):
            raise ValueError("code publication changed")

    with httpx.Client(timeout=60, follow_redirects=False) as client:
        report = run(
            args.root, client, contract=contract, code=code, validate=validate, sources=paths
        )
    print(json.dumps({"outcome": report["outcome"], "complete": report["complete"]}))  # noqa: T201
    return 0 if report["outcome"] == "metadata_complete_coverage_pending" else 1


if __name__ == "__main__":
    raise SystemExit(main())
