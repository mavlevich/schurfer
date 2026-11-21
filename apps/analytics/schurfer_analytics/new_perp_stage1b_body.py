"""Sealed I25 Stage 1b BODY phase (coverage level) on the backward holdout.

Contract: docs/research/new-perp-stage1b-body-v1.md and the frozen JSON next to it. Protocol:
docs/research/new-perp-short-lab-protocol-v1.md, sections 2, 4, 5, 8 to 11 and Amendments 1 to 6.

Two gos in one fresh root. Go A reads the first daily file of every launch whose first-file
HEAD size is sealed (column 1 only). Go B reads the entry bar, the hourly mark and trade series
and the funding timestamps of the launches that Go A leaves as entry candidates. Every response
physically holds prices, volumes or rates: the reducers keep only timestamps and counts, and
nothing dated from the cut (2024-09-16) is requested, recorded or counted.

Nothing here runs against a network in tests: the fetcher takes an injected ``httpx.Client``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
import zlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economics as publication
from . import new_perp_m2ii as m
from . import new_perp_m2ii_identity_probe as p
from . import new_perp_stage1 as s
from . import new_perp_stage1b as b
from .bybit_carry_feasibility import publish_once
from .new_perp_window import HOLDOUT

if TYPE_CHECKING:
    from collections.abc import Callable

VERSION: Final = "new_perp_stage1b_body_v1"
DOC: Final = "docs/research/new-perp-stage1b-body-v1.md"
CONTRACT_PATH: Final = "docs/research/evidence/new-perp-stage1b-body-v1/contract.json"
MODULE_PATH: Final = "apps/analytics/schurfer_analytics/new_perp_stage1b_body.py"
VERDICT_PATH: Final = "apps/analytics/schurfer_analytics/new_perp_stage1b_body_verdict.py"
PUBLISHED: Final = tuple(
    dict.fromkeys((*b.PUBLISHED, DOC, CONTRACT_PATH, MODULE_PATH, VERDICT_PATH))
)

NS_PER_MS: Final = 1_000_000
MINUTE_NS: Final = s.MINUTE_MS * NS_PER_MS
DAY_NS: Final = s.DAY_MS * NS_PER_MS
MIB: Final = s.MIB


@dataclass(frozen=True)
class Pins:
    """Public facts of the finished metadata phase that this contract binds to.

    All of them are in the Stage 1b metadata result record, none comes from a sealed file.
    """

    metadata_contract_sha256: str
    metadata_report_sha256: str
    manifest_sha256: str
    catalog_slots: int
    first_files: int
    first_bytes: int


PINS: Final = Pins(
    metadata_contract_sha256="31ae1a57970e7af98d2e52fd75534cf2604cd4a79bb889f25cd2f6a90fbada96",
    metadata_report_sha256="173347ae6785dd2fe5a5bbbce7e6fdfc10b82db37d98908e7ba0fc6f64458352",
    manifest_sha256="f3098e0840be489df640fbdab66c464257bac3d6aa5315d58e70d86d594d1341",
    catalog_slots=254,
    first_files=247,
    first_bytes=2_323_690_559,
)

# The frozen limits of the body phase. They are computed, never typed twice.

PER_FILE_CAP: Final = s.PER_FILE_CAP_BYTES
RESERVE: Final = s.READ_RESERVE_BYTES
RETRY_HEADROOM_PERCENT: Final = 125  # +25% sends over the worst-case first attempts (API)
FILE_PASSES: Final = 2  # Go A: every file may be downloaded in full twice, plus the last reserve
MAX_CANDIDATES: Final = PINS.first_files  # at most one candidate per sealed first file
D6_PAGES: Final = s.FUNDING_MAX_PAGES


def ceil_mib(value: int) -> int:
    return -(-value // MIB) * MIB


def api_sends(logical: int) -> int:
    """Sends allowed for a class of API requests: worst-case first attempts plus 25%."""
    return -(-logical * RETRY_HEADROOM_PERCENT // 100)


UNREADABLE: Final = (
    "no_archive",
    "no_pre_cut_file",
    "t0_pre_window_unreadable",
    "filename_identity_unverified",
    "listing_unavailable",
    "t0_unreadable",
)


def class_specs(pins: Pins = PINS) -> dict[str, s.ClassSpec]:
    files, cap = pins.first_files, ceil_mib(FILE_PASSES * pins.first_bytes + PER_FILE_CAP + RESERVE)
    specs: dict[str, s.ClassSpec] = {
        "D2": replace(
            s.CLASS_BY_ID["D2"],
            go=1,
            expected=files,
            max_requests=files,
            max_response_bytes=PER_FILE_CAP,
            max_class_bytes=cap,
            params="none; first in-window daily file of each sealed launch, by opaque slot",
            parsed="column 1 only (trade timestamp): row count, minimum, maximum, nanoseconds",
            discarded="columns 2 and later of every row, unparsed; the file is never stored",
            note="sorted by opaque slot; preflight binds count and bytes to the public record",
        )
    }
    for name, count in (("D3", 1), ("D4", 1), ("D5", 1), ("D6", D6_PAGES)):
        base = s.CLASS_BY_ID[name]
        logical = MAX_CANDIDATES * count
        sends = api_sends(logical)
        specs[name] = replace(
            base,
            go=2,
            expected=0,
            max_requests=logical,
            max_class_bytes=ceil_mib(sends * base.max_response_bytes + RESERVE),
            params="derived from the Go A first-trade time of each candidate, by opaque slot",
            note="clipped to the window; counts are derived after Go A, ceilings are not",
        )
    return specs


def go_limits(pins: Pins = PINS) -> dict[str, dict[str, int]]:
    specs = class_specs(pins)
    a_bytes = specs["D2"].max_class_bytes
    b_classes = [c for c in specs.values() if c.go == 2]
    b_sends = sum(api_sends(c.max_requests) for c in b_classes)
    return {
        "1": {"max_attempts": FILE_PASSES * pins.first_files, "max_bytes": a_bytes},
        "2": {
            "max_attempts": b_sends,
            "max_bytes": sum(c.max_class_bytes for c in b_classes),
        },
    }


# T0 in nanoseconds: the archive gives up to nine fractional digits. A sub-millisecond fraction
# above a minute boundary would move E by a whole minute if truncated first.


def timestamp_ns(field: bytes) -> int:
    if not re.fullmatch(rb"\d{9,11}(?:\.\d{1,9})?", field):
        raise s.SchemaMismatchError("bad:timestamp")
    whole, _, frac = field.partition(b".")
    return int(whole) * 1_000_000_000 + int(frac.ljust(9, b"0"))


def entry_clock_ns(t0_ns: int) -> tuple[int, int]:
    """E = T0 + 7 d up to the next whole minute (unchanged on a minute); X = E + 7 d, in ms."""
    entry = -(-(t0_ns + s.AGE_DAYS * DAY_NS) // MINUTE_NS) * s.MINUTE_MS
    return entry, entry + s.HOLD_DAYS * s.DAY_MS


def cuts_ns(t0_ns: int) -> dict[str, Any]:
    entry, exit_ = entry_clock_ns(t0_ns)
    return {
        "entry_ms": entry,
        "exit_ms": exit_,
        # X is the end of the exit bar [X - 1 minute, X): only X > cut censors the path.
        "window_censored": exit_ > HOLDOUT.cut_ms,
        "lookback_censored": entry - s.LOOKBACK_DAYS * s.DAY_MS < HOLDOUT.start_ms,
    }


def t0_class_ns(first_ns: int, launch_ms: int) -> str:
    d = first_ns - launch_ms * NS_PER_MS
    t = s.THRESHOLDS
    if t["m1_aligned_from_ms"] * NS_PER_MS <= d <= t["m1_aligned_to_ms"] * NS_PER_MS:
        return "aligned"
    if t["m1_aligned_to_ms"] * NS_PER_MS < d <= t["m1_delayed_to_ms"] * NS_PER_MS:
        return "delayed_open"
    return "premarket_or_ambiguous"


class FirstFileWindowReducer(s.FirstFileReducer):
    """Column 1 of a gzipped trade file, in nanoseconds, with the holdout window enforced.

    A row at or after the cut, or before the window start, stops the run. Columns 2 and later
    are never parsed. Only the row count, the minimum and the maximum survive.
    """

    def __init__(self) -> None:
        super().__init__()
        self.min_ns: int | None = None
        self.max_ns: int | None = None

    def _line(self, line: bytes) -> None:
        if not line:
            return
        head = line.split(b",", 1)[0]
        if not self._header_seen:
            self._header_seen = True
            if head == b"timestamp":
                return
        ns = timestamp_ns(head)
        if ns // NS_PER_MS >= HOLDOUT.cut_ms:
            raise s.SchemaMismatchError("timestamp_after_cut")
        if ns // NS_PER_MS < HOLDOUT.start_ms:
            raise s.SchemaMismatchError("timestamp_before_window")
        self.rows += 1
        self.min_ns = ns if self.min_ns is None else min(self.min_ns, ns)
        self.max_ns = ns if self.max_ns is None else max(self.max_ns, ns)

    def feed(self, chunk: bytes) -> None:
        """Exactly one gzip member. A second member or any trailing byte is a fixed stop: the
        minimum over the whole file would otherwise be unproven."""
        if self._zlib.eof:
            if chunk:
                raise s.SchemaMismatchError("gzip_trailing_data")
            return
        try:
            data = self._tail + self._zlib.decompress(chunk)
        except zlib.error as exc:
            raise s.SchemaMismatchError("not_gzip") from exc
        if self._zlib.unused_data:
            raise s.SchemaMismatchError("gzip_trailing_data")
        *lines, self._tail = data.split(b"\n")
        for line in lines:
            self._line(line.rstrip(b"\r"))

    def finish(self) -> dict[str, Any]:
        try:
            self._tail += self._zlib.flush()
        except zlib.error as exc:
            raise s.SchemaMismatchError("not_gzip") from exc
        if not self._zlib.eof:
            raise s.SchemaMismatchError("truncated_gzip")
        if self._zlib.unused_data:
            raise s.SchemaMismatchError("gzip_trailing_data")
        self._line(self._tail.rstrip(b"\r\n"))
        if self.rows == 0:
            raise s.SchemaMismatchError("no_rows")
        return {"rows": self.rows, "min_ns": self.min_ns, "max_ns": self.max_ns}


def _result_list(payload: Any, symbol: str | None) -> tuple[dict[str, Any], list[Any]]:
    result = s._envelope(payload)
    if symbol is not None and result.get("symbol") != symbol:
        raise s.SchemaMismatchError("symbol")
    rows = result.get("list")
    if not isinstance(rows, list):
        raise s.SchemaMismatchError("list")
    return result, rows


def reduce_kline_window(
    payload: Any, symbol: str, width: int, step_ms: int, limit: int, start: int, end: int
) -> dict[str, Any]:
    """Bar start times of one response, checked against the exact request.

    Per row, before any other field: a row from the cut is dropped uncounted (the fixed
    cut policy); every other start must lie on the step grid, inside the hour (or minute)
    that contains the requested start and the requested end, and appear once. Anything else
    is a fixed schema stop, so a stale, shifted or duplicated series can never look covered.
    """
    result, rows = _result_list(payload, symbol)
    low = start - start % step_ms
    seen: set[int] = set()
    kept = []
    for item in rows:
        if not isinstance(item, list) or len(item) < width:
            raise s.SchemaMismatchError("row")
        try:
            stamp = s._int_ms(item[0])
        except (TypeError, ValueError) as exc:
            raise s.SchemaMismatchError("bad:start") from exc
        if stamp >= HOLDOUT.cut_ms:
            continue
        if stamp % step_ms:
            raise s.SchemaMismatchError("bar_off_grid")
        if not low <= stamp <= end:
            raise s.SchemaMismatchError("bar_out_of_range")
        if stamp in seen:
            raise s.SchemaMismatchError("bar_duplicate")
        seen.add(stamp)
        kept.append(item)
    facts = s.reduce_kline(
        {**payload, "result": {**result, "list": kept}}, symbol, width, step_ms, limit
    )
    facts["full"] = len(rows) >= limit  # the raw page decides whether it may be cut by its limit
    return facts


def reduce_funding_window(payload: Any, symbol: str, start: int, end: int) -> dict[str, Any]:
    """Funding timestamps of one page, checked against the page bounds.

    A row from the cut is dropped uncounted; every other stamp must lie inside
    [startTime, endTime] of the request and appear once, else a fixed schema stop.
    """
    result, items = _result_list(payload, None)
    seen: set[int] = set()
    kept = []
    for item in items:
        if not isinstance(item, dict) or item.get("symbol") != symbol:
            raise s.SchemaMismatchError("row")
        try:
            stamp = s._int_ms(item["fundingRateTimestamp"])
        except KeyError as exc:
            raise s.SchemaMismatchError("missing:fundingRateTimestamp") from exc
        except (TypeError, ValueError) as exc:
            raise s.SchemaMismatchError("bad:fundingRateTimestamp") from exc
        if stamp >= HOLDOUT.cut_ms:
            continue
        if not start <= stamp <= end:
            raise s.SchemaMismatchError("funding_out_of_range")
        if stamp in seen:
            raise s.SchemaMismatchError("funding_duplicate")
        seen.add(stamp)
        kept.append(item)
    facts = s.reduce_funding({**payload, "result": {**result, "list": kept}}, symbol)
    facts["full"] = len(items) >= s.FUNDING_PAGE_LIMIT
    return facts


# Requests. A request holds the symbol only inside its URL or parameters. Receipts, attempts and
# plans hold an opaque slot and a page number: the key never hashes the symbol.

HEX64: Final = re.compile(r"[a-f0-9]{64}")
FILE_URL: Final = re.compile(
    re.escape(s.ARCHIVE_ROOT) + r"([A-Z0-9]+USDT)/([A-Z0-9]+USDT)(\d{4}-\d{2}-\d{2})\.csv\.gz"
)
KLINE_KEYS: Final = {"category", "symbol", "interval", "start", "end", "limit"}
FUNDING_KEYS: Final = {"category", "symbol", "startTime", "endTime", "limit"}


def _stamp(value: str | None) -> int:
    if value is None or not re.fullmatch(r"\d{1,16}", value):
        raise ValueError("request outside the body contract")
    return int(value)


def validate_body_request(r: s.Request) -> None:
    meta = r.meta_dict()
    params = dict(r.params)
    if (
        r.symbol
        or len(meta) != len(r.meta)
        or len(params) != len(r.params)
        or HEX64.fullmatch(meta.get("slot", "")) is None
    ):
        raise ValueError("request outside the body contract")
    ok = False
    if r.cls == "D2":
        match = FILE_URL.fullmatch(r.url)
        ok = (
            r.method == "GET"
            and not params
            and set(meta) == {"slot"}
            and match is not None
            and match[1] == match[2]
            and s.SYMBOL.fullmatch(match[1]) is not None
            and HOLDOUT.contains_day(match[3])
        )
    elif r.cls in ("D3", "D4", "D5"):
        interval, limit = ("1", "1") if r.cls == "D3" else ("60", "1000")
        url = s.MARK_URL if r.cls == "D4" else s.KLINE_URL
        start, end = _stamp(params.get("start")), _stamp(params.get("end"))
        shape = (
            start == end and start % s.MINUTE_MS == 0 and HOLDOUT.contains(start)
            if r.cls == "D3"
            else HOLDOUT.start_ms <= start <= end <= HOLDOUT.cut_ms - 1
        )
        ok = (
            r.method == "GET"
            and r.url == url
            and set(params) == KLINE_KEYS
            and set(meta) == {"slot"}
            and params["category"] == "linear"
            and params["interval"] == interval
            and params["limit"] == limit
            and s.SYMBOL.fullmatch(params["symbol"]) is not None
            and shape
        )
    elif r.cls == "D6":
        start, end = _stamp(params.get("startTime")), _stamp(params.get("endTime"))
        page = meta.get("page", "")
        ok = (
            r.method == "GET"
            and r.url == s.FUNDING_URL
            and set(params) == FUNDING_KEYS
            and set(meta) == {"slot", "page"}
            and page in {str(n) for n in range(1, D6_PAGES + 1)}
            and params["category"] == "linear"
            and params["limit"] == str(s.FUNDING_PAGE_LIMIT)
            and s.SYMBOL.fullmatch(params["symbol"]) is not None
            and HOLDOUT.start_ms <= start <= end <= HOLDOUT.cut_ms - 1
        )
    if not ok:
        raise ValueError("request outside the body contract")


@dataclass(frozen=True, repr=False)
class BodyRequest(s.Request):
    def __post_init__(self) -> None:
        validate_body_request(self)

    @property
    def key(self) -> str:
        params = sorted((k, v) for k, v in self.params if k != "symbol")
        return s.digest(
            json.dumps(["body_v1", self.cls, self.method, self.meta_dict(), params]).encode()
        )

    def __repr__(self) -> str:
        return f"BodyRequest(cls={self.cls!r}, key={self.key[:12]!r})"

    @property
    def slot(self) -> str:
        return self.meta_dict()["slot"]


# Sealed metadata inputs. Read only by the runner, only after every hash matches.


@dataclass
class Sealed:
    report: dict[str, Any]
    rows: dict[str, dict[str, Any]]


def load_sealed(root: Path, pins: Pins = PINS) -> Sealed:
    """Verify the finished metadata root against the public record, then load its rows."""
    report_bytes = (root / "metadata-report.json").read_bytes()
    manifest_bytes = (root / "manifest.json").read_bytes()
    report = json.loads(report_bytes)
    manifest = json.loads(manifest_bytes)
    if (
        s.digest(report_bytes) != pins.metadata_report_sha256
        or s.digest(manifest_bytes) != pins.manifest_sha256
        or report.get("manifest_sha256") != pins.manifest_sha256
        or report.get("request_list_sha256") != pins.metadata_contract_sha256
        or report.get("outcome") != "metadata_complete_coverage_pending"
        or report.get("complete") is not True
        or report.get("binding_valid") is not True
        or report.get("stop_reason") is not None
        or report.get("known_coverage_failures") != []
        or report.get("body_phase_authorized") is not False
        or manifest.get("complete") is not True
        or manifest.get("body_phase_authorized") is not False
        or not (root / "metadata_done.json").exists()
        or (root / "stop.json").exists()
    ):
        raise ValueError("sealed metadata root differs from the public record")
    mapping_bytes = (root / "evidence-hashes.json").read_bytes()
    mapping = json.loads(mapping_bytes)
    if s.digest(mapping_bytes) != report.get("evidence_map_sha256"):
        raise ValueError("sealed metadata evidence map differs")
    extra = {"evidence-hashes.json", "metadata-report.json", "metadata_done.json"}
    present = {str(path.relative_to(root)) for path in root.rglob("*.json")} - extra
    if present != set(mapping):
        raise ValueError("sealed metadata file set differs")
    for name, expected in mapping.items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or s.digest(path.read_bytes()) != (
            expected
        ):
            raise ValueError("sealed metadata evidence differs")
    keys = manifest["catalog_keys"]
    rows = {key: json.loads((root / "catalog" / f"{key}.json").read_text()) for key in keys}
    if len(rows) != pins.catalog_slots or any(HEX64.fullmatch(k) is None for k in rows):
        raise ValueError("sealed metadata catalog differs from the public record")
    return Sealed(report, rows)


def preflight(rows: dict[str, dict[str, Any]], pins: Pins = PINS) -> dict[str, Any]:
    """Bytes of Go A from the sealed HEAD sizes, before a body byte is read."""
    sizes: dict[str, int] = {}
    no_size = 0
    for slot, row in sorted(rows.items()):
        if row.get("first_file_day") is None:
            continue
        head = row.get("head") or {}
        length = head.get("content_length")
        if head.get("outcome") == "ok" and type(length) is int:
            sizes[slot] = length
        else:
            no_size += 1
    total = sum(sizes.values())
    cap = class_specs(pins)["D2"].max_class_bytes
    over = sum(1 for n in sizes.values() if n > PER_FILE_CAP)
    matches = (
        len(rows) == pins.catalog_slots
        and len(sizes) == pins.first_files
        and total == pins.first_bytes
        and no_size == 0
    )
    within = total + PER_FILE_CAP + RESERVE <= cap
    return {
        "launches": len(rows),
        "files": len(sizes),
        "bytes": total,
        "no_size": no_size,
        "over_per_file_cap": over,
        "class_cap_bytes": cap,
        "matches_public_record": matches,
        "within_cap": within,
        "ok": matches and over == 0 and within,
        "sizes": sizes,
    }


def first_file_requests(rows: dict[str, dict[str, Any]], plan: dict[str, Any]) -> list[BodyRequest]:
    out = []
    for slot in sorted(plan["sizes"]):
        row = rows[slot]
        url = s.first_file_url(row["symbol"], row["first_file_day"])
        out.append(BodyRequest("D2", "GET", url, meta=(("slot", slot),)))
    return out


# Launch states from sealed facts and Go A receipts.


def t0_state(
    row: dict[str, Any], first: dict[str, Any] | None, queued: bool
) -> tuple[int | None, str]:
    """(T0 in ns or None, M1 class or an unreadable reason or `not_run`) of one launch."""
    status = row["t0_status"]
    if status != "csv_pending":
        return None, status if status in (
            "premarket_or_ambiguous",
            *UNREADABLE,
        ) else "t0_unreadable"
    if not queued:
        return None, "t0_unreadable"
    if first is None:
        return None, "not_run"
    if first["outcome"] != "ok":
        return None, "t0_unreadable"
    min_ns = first["facts"]["min_ns"]
    if s.day_of(min_ns // NS_PER_MS) != row["first_file_day"]:
        return None, "premarket_or_ambiguous"
    return min_ns, t0_class_ns(min_ns, row["launch_ms"])


def identity_excluded(row: dict[str, Any]) -> bool:
    return (
        row.get("catalog_identity", "not_contradicted") != "not_contradicted"
        or row.get("ticker_reuse_suspected") is True
    )


@dataclass(frozen=True)
class Candidate:
    slot: str
    t0_ns: int
    entry_ms: int
    exit_ms: int

    @property
    def t0_ms(self) -> int:
        return self.t0_ns // NS_PER_MS


def entry_candidates(
    rows: dict[str, dict[str, Any]],
    firsts: dict[str, dict[str, Any]],
    queued: set[str],
) -> list[Candidate]:
    """Launches that get bar and funding requests, in slot order.

    A readable, unambiguous T0 (`aligned` or `delayed_open`), no catalog or file-list
    contradiction, and a V1 path and 30-day lookback inside the window. Nothing a price does
    enters this rule.
    """
    out = []
    for slot, row in sorted(rows.items()):
        t0, cls = t0_state(row, firsts.get(slot), slot in queued)
        if t0 is None or cls not in ("aligned", "delayed_open") or identity_excluded(row):
            continue
        cuts = cuts_ns(t0)
        if cuts["window_censored"] or cuts["lookback_censored"]:
            continue
        out.append(Candidate(slot, t0, cuts["entry_ms"], cuts["exit_ms"]))
    return out


def _kline_params(
    symbol: str, interval: str, start: int, end: int, limit: int
) -> tuple[tuple[str, str], ...]:
    return (
        ("category", "linear"),
        ("symbol", symbol),
        ("interval", interval),
        ("start", str(start)),
        ("end", str(end)),
        ("limit", str(limit)),
    )


def bar_requests(candidates: list[Candidate], rows: dict[str, dict[str, Any]]) -> list[BodyRequest]:
    out = []
    for c in candidates:
        symbol = rows[c.slot]["symbol"]
        start, end = HOLDOUT.funding_span(c.t0_ms)
        slot = (("slot", c.slot),)
        out.append(
            BodyRequest(
                "D3",
                "GET",
                s.KLINE_URL,
                _kline_params(symbol, "1", c.entry_ms, c.entry_ms, 1),
                meta=slot,
            )
        )
        for cls, url in (("D4", s.MARK_URL), ("D5", s.KLINE_URL)):
            params = _kline_params(symbol, "60", start, end, 1000)
            out.append(BodyRequest(cls, "GET", url, params, meta=slot))
    return out


def funding_request_for(
    c: Candidate, rows: dict[str, dict[str, Any]], pages: list[dict[str, Any]]
) -> BodyRequest | None:
    """The next funding page of one candidate, or None when its coverage is decided."""
    start, end = HOLDOUT.funding_span(c.t0_ms)
    state = s.funding_state(pages, start, end, rows[c.slot].get("delivery_ms"))
    if state["decided"]:
        return None
    params = (
        ("category", "linear"),
        ("symbol", rows[c.slot]["symbol"]),
        ("startTime", str(start)),
        ("endTime", str(state["next_end"])),
        ("limit", str(s.FUNDING_PAGE_LIMIT)),
    )
    meta = (("slot", c.slot), ("page", str(len(pages) + 1)))
    return BodyRequest("D6", "GET", s.FUNDING_URL, params, meta=meta)


def funding_round(
    candidates: list[Candidate],
    rows: dict[str, dict[str, Any]],
    pages: dict[str, list[dict[str, Any]]],
) -> list[BodyRequest]:
    out = []
    for c in candidates:
        request = funding_request_for(c, rows, pages.get(c.slot, []))
        if request is not None:
            out.append(request)
    return out


# The contract.

# Exclusion order of a launch, one reason each (the discovery order, protocol section 2).
REASONS: Final = (
    "t0_unavailable",
    "t0_ambiguous",
    "identity_excluded",
    "window_censored",
    "lookback_censored",
    "funding_uncovered",
    "entry_unavailable",
    "primary",
)
OUTCOMES: Final = (
    *s.STOP_CODES,
    "stage1b_stopped",
    "stage1b_incomplete",
    "stage1b_a_complete",
    "stage1b_pass_partial",
)


def go_hash(contract: dict[str, Any], go: int) -> str:
    excluded = ("classes", "go_limits", "go_sha256", "request_list_sha256")
    shared = {k: v for k, v in contract.items() if k not in excluded}
    return s.digest(
        s.canonical(
            {
                "shared": shared,
                "go": go,
                "classes": [c for c in contract["classes"] if c["go"] == go],
                "limits": contract["go_limits"][str(go)],
            }
        )
    )


def build_contract(pins: Pins = PINS) -> dict[str, Any]:
    specs = class_specs(pins)
    body: dict[str, Any] = {
        "rules": VERSION,
        "study": s.STUDY_ID,
        "protocol": s.PROTOCOL,
        "window": {"start": HOLDOUT.start_day, "cut": HOLDOUT.cut_day},
        "pins": asdict(pins),
        "thresholds": s.THRESHOLDS,
        "stop_codes": list(s.STOP_CODES),
        "limits": {
            "attempts_per_request": s.ATTEMPTS_PER_REQUEST,
            "per_file_cap_bytes": PER_FILE_CAP,
            "read_reserve_bytes": RESERVE,
            "spacing_s": s.SPACING_S,
            "max_spacing_s": s.MAX_SPACING_S,
            "backoff_s": list(s.BACKOFF_S),
            "max_retry_after_s": s.MAX_RETRY_AFTER_S,
            "funding_page_limit": s.FUNDING_PAGE_LIMIT,
            "funding_max_pages": s.FUNDING_MAX_PAGES,
            "funding_max_step_ms": s.FUNDING_MAX_STEP_MS,
            "rate_limit_ret_codes": sorted(s.RET_RATE_LIMIT),
            "transient_ret_codes": sorted(s.RET_TRANSIENT),
            "max_candidates": MAX_CANDIDATES,
            "retry_headroom_percent_api": RETRY_HEADROOM_PERCENT,
            "file_passes": FILE_PASSES,
            "ret_code_rule": "retCode must be an int and not a bool; a nonzero code keeps only "
            "retCode and a short retMsg; only 10006 and 10018 (rate limit) and 10000 and 10016 "
            "(transient) are retried; any other code stops the run naming it",
            "byte_rule": "a request starts only if used + response cap + read reserve fits the "
            "go budget and the class budget; every received byte is accounted; a declared "
            "length over the cap reads nothing; Accept-Encoding is identity",
        },
        "clock": "T0 = minimum column-1 timestamp of the first in-window daily file, in "
        "nanoseconds; E = T0 + 7 d up to the next whole minute; X = E + 7 d",
        "cut_rules": "X > cut censors the path and X == cut is allowed; lookback needs "
        "E - 30 d >= window start; every API span ends at cut - 1 ms; a row from the cut is "
        "dropped uncounted from an API page and stops the run in a first file, as does a row "
        "before the window start",
        "reducer_rules": "first file: exactly one gzip member, no trailing byte, every row "
        "inside the window; API bars: start on the step grid, inside the hour (or minute) "
        "containing the requested start and the requested end, no duplicate; funding rows "
        "inside [startTime, endTime], no duplicate; a row from the cut is dropped uncounted; "
        "any other deviation is a fixed schema stop",
        "publish_order": "the sealed source, the clean revision and the publication of every "
        "bound file are verified again before any queue, report or done marker is published; "
        "a failure writes the fixed stop source_integrity_error",
        "output_root": "outside the Git tree and disjoint from the sealed metadata root, "
        "checked before any file or HTTP client exists",
        "report_command": "prints a stored report only if its go finished, or if a stop sealed "
        "the root and the report is not positive",
        "statuses": list(REASONS),
        "outcomes": list(OUTCOMES),
        "m1_unreadable": list(UNREADABLE),
        "verdict_rules": {
            "m1": "d = first trade - launchTime; aligned -5 min to +60 min, delayed_open up to "
            "+24 h, else premarket_or_ambiguous; ambiguous at most 10%, readable at least 90%, "
            "unreadable at most 15%, all over the catalog launches",
            "m2": "(i) no file within one day of launch, at most 5% of the catalog; (ii) the "
            "metadata share a/(N+a), at most 5%; (iii) candidates missing more than 5% of "
            "their V1 path days, at most 5% of the candidates",
            "m3": "survivors are readable launches with a file near launch; contradicting at most "
            "10%, unverifiable (no listing or unverified filename identity) at most 20%, "
            "survivors less contradicting at least 100; no baseCoin check",
            "m4": "pagination finished and no gap longer than 8 h over days 3 to 21 after T0, "
            "clipped to the window: partial_pass, never proof; uncovered at most 5% of candidates; "
            "a share above the limit over the fixed candidate count is a known failure even while "
            "launches are pending",
            "m5": "entry bar at E exists, hourly trade and mark series continuous from the hour "
            "of E to the hour before X, at least 95% of the episodes after funding; a series "
            "that may be cut by its limit is unproven and never green; minute level deferred; "
            "a known failure is an optimistic share below 95% (pending, unproven and "
            "funding-pending episodes counted as successes)",
            "m6": "static catalog fields: minNotionalValue at most USD 50, maxLeverage at least "
            "2, qtyStep and minOrderQty present, at least 90% of the episodes; partial; "
            "provisional while any episode awaits funding, and a known failure is an "
            "optimistic share below 90% (a pending episode that is not feasible leaves the "
            "denominator, every other episode stays)",
            "floor": "n_primary at least 100 and at least 26 ISO weeks of primary launches",
            "go_a": "Go B is allowed only with no stop code from M1, M2, M3 or the floor upper "
            "bound, no stop state and no pending first file; M4, M5, M6 and n_primary are "
            "decided only after Go B",
            "outcome": "the first stop code of the known failures, else stage1b_stopped if the "
            "root is sealed, else stage1b_incomplete if a check is not assessed, else "
            "stage1b_a_complete after Go A, else stage1b_pass_partial",
        },
        "public_report": "aggregate counts only: no symbol, slot, URL, per-launch date or "
        "result; M4 diagnostics by launch quarter and delivery state",
        "queue": {
            "go_a": "D2 for every sealed launch with first_file_day and an ok HEAD size, sorted "
            "by opaque slot; preflight binds launches, files and bytes to the public record",
            "go_b": "D3, D4, D5 and the first D6 page per candidate, sorted by opaque slot; "
            "later D6 pages by the HYP-036 rule, at most four per candidate",
            "t0_ambiguous": "a launch whose T0 is ambiguous gets no request after Go A",
            "gate": "Go B needs Go A in the same root, the sha256 of queue_b.json, and no "
            "definitive stop code from Go A",
        },
        "retention": "sealed 0700 root; opaque slots and keys, no symbol, URL or raw body hash "
        "in any receipt; public output is aggregate counts only",
        "success": "stage1b_pass_partial is a coverage statement, never an economic result",
        "restart": "fresh root; no resume under different code; a stop seals the root",
        "classes": [asdict(spec) for spec in specs.values()],
        "go_limits": go_limits(pins),
    }
    body["go_sha256"] = {str(go): go_hash(body, go) for go in (1, 2)}
    body["request_list_sha256"] = s.contract_hash(body)
    return body


# Fetcher: discovery's, with the holdout window enforced in the first-file reducer and no
# raw-body hash in any receipt.


class BodyFetcher(s.Fetcher):
    def _attempt(
        self,
        seq: int,
        request: s.Request,
        reduce: Callable[[bytes], dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int | None]:
        if request.cls != "D2":
            receipt, facts, asked = super()._attempt(seq, request, reduce)
        else:
            receipt, facts, asked = self._first_file(seq, request)
        receipt.pop("body_sha256", None)
        return receipt, facts, asked

    def _first_file(
        self, seq: int, request: s.Request
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int | None]:
        spec = self.specs["D2"]
        receipt: dict[str, Any] = {
            "seq": seq,
            "cls": "D2",
            "key": request.key,
            "symbol": "",
            "meta": request.meta_dict(),
        }
        status, size, asked = 0, 0, None
        error: str | None = None
        reason = ""
        reducer = FirstFileWindowReducer()
        facts: dict[str, Any] | None = None
        try:
            headers = {"Accept-Encoding": "identity"}
            with self.client.stream("GET", request.url, headers=headers) as resp:
                status = resp.status_code
                header = resp.headers.get("retry-after", "")
                asked = int(header) if header.isdigit() else None
                length = resp.headers.get("content-length", "")
                if status == 200:
                    if resp.headers.get("content-encoding", "identity") not in ("", "identity"):
                        raise s.SchemaMismatchError("content_encoding")
                    if length.isdigit() and int(length) > spec.max_response_bytes:
                        error = "too_large"
                    else:
                        for piece in resp.iter_raw():
                            size += len(piece)
                            if len(piece) > s.READ_RESERVE_BYTES:
                                receipt["reserve_exceeded"] = True
                            if size > spec.max_response_bytes:
                                error = "too_large"
                                break
                            reducer.feed(piece)
        except httpx.HTTPError:
            error = "error"  # never the exception text, URL or body
        except s.SchemaMismatchError as exc:
            error, reason = "schema_mismatch", exc.reason
        receipt.update(http_status=status, body_bytes=size, at_epoch=self.wall())
        if asked is not None:
            receipt["retry_after_s"] = asked
        if error is None:
            if status == 403:
                error = "forbidden"
            elif status in s.RETRYABLE_STATUS:
                error = "error"
            elif status not in (200, 404):
                error, reason = "schema_mismatch", f"http_status_{status}"
        if error is None and status == 200:
            try:
                facts = reducer.finish()
            except s.SchemaMismatchError as exc:
                error, reason = "schema_mismatch", exc.reason
        if error is None:
            receipt["outcome"] = "absent" if status == 404 else "ok"
        else:
            receipt["outcome"] = error
            if reason:
                receipt["reason"] = reason
        return receipt, facts, asked


def results_by_slot(fetcher: s.Fetcher, cls: str) -> dict[str, dict[str, Any]]:
    return {r["meta"]["slot"]: r for r in fetcher.final.values() if r["cls"] == cls}


def funding_pages(fetcher: s.Fetcher) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    pages = (r for r in fetcher.final.values() if r["cls"] == "D6")
    for r in sorted(pages, key=lambda r: (r["meta"]["slot"], int(r["meta"]["page"]))):
        out.setdefault(r["meta"]["slot"], []).append(r)
    return out


def _guard(
    fetcher: s.Fetcher, work: Callable[[], dict[str, Any]], on_stop: Callable[[], None]
) -> dict[str, Any]:
    """Seal the root on any fatal condition. An unexpected error leaves no names in a trace.

    `on_stop` publishes the aggregate report of the sealed root; it can never mask the stop.
    """
    try:
        with m.private_logs():  # HTTP libraries must not log a URL, and so a symbol
            return work()
    except (s.FetchStoppedError, s.BudgetExceededError) as exc:
        fetcher.record_stop(str(exc))
        _publish_stop_report(on_stop)
        raise
    except KeyboardInterrupt:
        raise
    except Exception:
        fetcher.record_stop("unexpected_error")
        _publish_stop_report(on_stop)
        raise s.FetchStoppedError("unexpected_error") from None


def _publish_stop_report(on_stop: Callable[[], None]) -> None:
    with contextlib.suppress(Exception):
        on_stop()


def check_source(
    fetcher: s.Fetcher, verify: Callable[[], None] | None, on_stop: Callable[[], None]
) -> None:
    """The last check before anything positive is published: any exception seals the root."""
    if verify is None:
        return
    try:
        with m.private_logs():
            verify()
    except Exception:
        fetcher.record_stop("source_integrity_error")
        _publish_stop_report(on_stop)
        raise s.FetchStoppedError("source_integrity_error") from None


def make_verify(
    source: Path,
    sealed: Sealed,
    pins: Pins,
    bound: dict[str, Any] | None,
    *,
    revision: Callable[[], str] = transport.clean_revision,
    published: Callable[[list[str]], dict[str, str]] = publication.publication,
) -> Callable[[], None]:
    """The final integrity check: the sealed metadata root (every hash, every row), the clean
    revision and the publication of every bound file are as they were at the start."""

    def verify() -> None:
        again = load_sealed(source, pins)
        if again.report != sealed.report or again.rows != sealed.rows:
            raise ValueError("sealed metadata changed")
        if bound is not None and (
            revision() != bound["revision"] or published(list(PUBLISHED)) != bound["publication"]
        ):
            raise ValueError("code publication changed")

    return verify


def stop_report(
    fetcher: s.Fetcher, sealed: Sealed, phase: str, code: dict[str, Any], name: str
) -> Callable[[], None]:
    def publish() -> None:
        path = fetcher.root / name
        if not path.exists():
            publish_once(path, final_report(fetcher, sealed, phase, code))

    return publish


def kline_reducer(
    symbol: str, width: int, step_ms: int, limit: int, start: int, end: int
) -> Callable[[bytes], dict[str, Any]]:
    return s._json_reducer(
        lambda payload: reduce_kline_window(payload, symbol, width, step_ms, limit, start, end)
    )


def funding_reducer(symbol: str, start: int, end: int) -> Callable[[bytes], dict[str, Any]]:
    return s._json_reducer(lambda payload: reduce_funding_window(payload, symbol, start, end))


def request_reducer(request: BodyRequest) -> Callable[[bytes], dict[str, Any]]:
    """The reducer of one API request, bound to that request's own symbol and bounds."""
    params = dict(request.params)
    if request.cls == "D6":
        return funding_reducer(params["symbol"], int(params["startTime"]), int(params["endTime"]))
    width, step, limit = {
        "D3": (7, s.MINUTE_MS, 1),
        "D4": (5, s.HOUR_MS, 1000),
        "D5": (7, s.HOUR_MS, 1000),
    }[request.cls]
    return kline_reducer(
        params["symbol"], width, step, limit, int(params["start"]), int(params["end"])
    )


def _publish_plan(fetcher: s.Fetcher, name: str, requests: list[BodyRequest]) -> None:
    path = fetcher.root / "plans" / f"{name}.json"
    if not path.exists():
        keys = sorted(r.key for r in requests)
        publish_once(path, {"requests": len(requests), "keys_sha256": s.digest(s.canonical(keys))})


def final_report(
    fetcher: s.Fetcher, sealed: Sealed, phase: str, code: dict[str, Any]
) -> dict[str, Any]:
    from . import new_perp_stage1b_body_verdict as verdict

    data = verdict.from_fetcher(fetcher, sealed)
    report = verdict.evaluate(data, phase)
    report["provenance"] = {
        "request_list_sha256": fetcher.contract["request_list_sha256"],
        "metadata_report_sha256": PINS.metadata_report_sha256,
        "manifest_sha256": PINS.manifest_sha256,
        "metadata_contract_sha256": PINS.metadata_contract_sha256,
        "code": code,
    }
    return report


def run_a(
    fetcher: s.Fetcher,
    sealed: Sealed,
    pins: Pins = PINS,
    code: dict[str, Any] | None = None,
    verify: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Go A: the first daily file of every sealed launch. No later request is derived here
    except the queue manifest the owner reviews before Go B."""
    if fetcher.go != 1:
        raise ValueError("run_a needs a go 1 fetcher")
    if (fetcher.root / "go_a_done.json").exists():
        raise s.FetchStoppedError("go A is already done in this root")
    plan = preflight(sealed.rows, pins)
    if not plan["ok"]:
        fetcher.record_stop("preflight_failed")
        raise s.FetchStoppedError("preflight_failed")
    requests = first_file_requests(sealed.rows, plan)

    def work() -> dict[str, Any]:
        _publish_plan(fetcher, "go_a_first_files", requests)
        for request in requests:
            fetcher.run_request(request)
        return {}

    on_stop = stop_report(fetcher, sealed, "a", code or {}, "go_a_report.json")
    _guard(fetcher, work, on_stop)
    check_source(fetcher, verify, on_stop)  # before any queue, report or done marker
    return finish_a(fetcher, sealed, code or {})


def finish_a(fetcher: s.Fetcher, sealed: Sealed, code: dict[str, Any]) -> dict[str, Any]:
    root = fetcher.root
    queued = {slot for slot, row in sealed.rows.items() if has_size(row)}
    report = final_report(fetcher, sealed, "a", code)
    firsts = results_by_slot(fetcher, "D2")
    candidates = entry_candidates(sealed.rows, firsts, queued)
    allowed = report["go_b_allowed"]
    if allowed:
        requests = bar_requests(candidates, sealed.rows)
        requests += funding_round(candidates, sealed.rows, {})
        queue = {
            "candidates": len(candidates),
            "d3_requests": len(candidates),
            "d4_requests": len(candidates),
            "d5_requests": len(candidates),
            "d6_first_pages": len(candidates),
            "d6_worst_case_pages": s.FUNDING_MAX_PAGES * len(candidates),
            "within_ceiling": len(candidates) <= MAX_CANDIDATES,
            "keys_sha256": s.digest(s.canonical(sorted(r.key for r in requests))),
        }
        if not queue["within_ceiling"]:
            fetcher.record_stop("queue_b_over_ceiling")
            raise s.FetchStoppedError("queue_b_over_ceiling")
        publish_or_same(root / "queue_b.json", queue)
        report["queue_b"] = {k: v for k, v in queue.items() if k != "keys_sha256"}
        report["queue_b_sha256"] = s.digest((root / "queue_b.json").read_bytes())
    publish_or_same(root / "go_a_report.json", report)
    publish_or_same(root / "go_a_done.json", {"go": 1, "go_b_allowed": allowed})
    return report


def publish_or_same(path: Path, content: dict[str, Any]) -> None:
    """Publish once. After a crash between two outputs the same content may be written again;
    different content under the same name is never replaced."""
    if path.exists():
        if json.loads(path.read_text()) != json.loads(s.canonical(content)):
            raise s.FetchStoppedError("sealed output differs from the earlier one")
        return
    publish_once(path, content)


def queue_b_sha256(root: Path) -> str:
    return s.digest((root / "queue_b.json").read_bytes())


def run_b(
    fetcher: s.Fetcher,
    sealed: Sealed,
    approved_queue_sha256: str,
    code: dict[str, Any] | None = None,
    verify: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Go B: entry bar, hourly series and funding of the Go A candidates."""
    if fetcher.go != 2:
        raise ValueError("run_b needs a go 2 fetcher")
    root = fetcher.root
    if (root / "go_b_done.json").exists():
        raise s.FetchStoppedError("go B is already done in this root")
    done = root / "go_a_done.json"
    if not done.exists() or json.loads(done.read_text()).get("go_b_allowed") is not True:
        raise s.FetchStoppedError("go B needs a finished Go A that allows it")
    if not (root / "queue_b.json").exists() or queue_b_sha256(root) != approved_queue_sha256:
        raise s.FetchStoppedError("the queue is not the one that was approved")
    firsts = {r["meta"]["slot"]: r for r in fetcher.final.values() if r["cls"] == "D2"}
    queued = {slot for slot, row in sealed.rows.items() if has_size(row)}
    candidates = entry_candidates(sealed.rows, firsts, queued)
    first_round = [
        *bar_requests(candidates, sealed.rows),
        *funding_round(candidates, sealed.rows, {}),
    ]
    approved = json.loads((root / "queue_b.json").read_text())
    if s.digest(s.canonical(sorted(r.key for r in first_round))) != approved["keys_sha256"]:
        fetcher.record_stop("queue_b_differs")
        raise s.FetchStoppedError("queue_b_differs")

    def work() -> dict[str, Any]:
        bars = [r for r in first_round if r.cls != "D6"]
        _publish_plan(fetcher, "go_b_bars", bars)
        for request in bars:
            fetcher.run_request(request, request_reducer(request))
        for round_no in range(1, s.FUNDING_MAX_PAGES + 1):
            pending = funding_round(candidates, sealed.rows, funding_pages(fetcher))
            if not pending:
                break
            _publish_plan(fetcher, f"go_b_funding_round{round_no}", pending)
            for request in pending:
                fetcher.run_request(request, request_reducer(request))
        return {}

    on_stop = stop_report(fetcher, sealed, "final", code or {}, "body_report.json")
    _guard(fetcher, work, on_stop)
    check_source(fetcher, verify, on_stop)  # before the report or the done marker
    report = final_report(fetcher, sealed, "final", code or {})
    publish_or_same(root / "body_report.json", report)
    publish_or_same(root / "go_b_done.json", {"go": 2})
    return report


def has_size(row: dict[str, Any]) -> bool:
    head = row.get("head") or {}
    return (
        row.get("first_file_day") is not None
        and head.get("outcome") == "ok"
        and type(head.get("content_length")) is int
    )


# Binding and command line.


def binding(pins: Pins = PINS) -> dict[str, Any]:
    published = publication.publication(list(PUBLISHED))
    if f"`{s.STUDY_ID}`" not in Path(s.REGISTRY).read_text():
        raise ValueError("no seen-windows registry row names this study")
    saved = json.loads(Path(CONTRACT_PATH).read_text())
    if saved != build_contract(pins):
        raise ValueError("the checked-in contract differs from this code")
    return {
        "rules": VERSION,
        "revision": transport.clean_revision(),
        "publication": published,
        "request_list_sha256": saved["request_list_sha256"],
    }


def repo_root() -> Path:
    git = shutil.which("git")
    if git is None:
        raise ValueError("git is required to locate the repository")
    out = subprocess.check_output([git, "rev-parse", "--show-toplevel"], text=True)  # noqa: S603
    return Path(out.strip()).resolve()


def check_root_location(root: Path, source: Path, repo: Path) -> None:
    """The sealed output root is outside the Git tree and disjoint from the sealed source:
    per-launch timestamps and receipts must never be writable into a tracked directory."""
    r, src, tree = root.resolve(), source.resolve(), repo.resolve()
    if r.is_relative_to(tree) or tree.is_relative_to(r):
        raise ValueError("the output root must be outside the Git tree")
    if r.is_relative_to(src) or src.is_relative_to(r):
        raise ValueError("the output root and the sealed metadata root must be disjoint")


POSITIVE: Final = ("stage1b_pass_partial", "stage1b_a_complete")


def show_report(root: Path, phase: str) -> int:
    """Print the stored aggregate report only if its go finished, or if a stop sealed the root
    and the report is not positive. Nothing positive is ever shown from a sealed root."""
    name, done = (
        ("go_a_report.json", "go_a_done.json")
        if phase == "a"
        else ("body_report.json", "go_b_done.json")
    )
    stored = json.loads((root / name).read_text())
    if (root / "stop.json").exists():
        if stored.get("outcome") in POSITIVE:
            raise ValueError("the root is sealed by a stop and holds no positive report")
    elif not (root / done).exists():
        raise ValueError("the go is not finished in this root")
    _emit(json.dumps(stored, indent=2, sort_keys=True))
    return 0


def root_state(go: int, root: Path) -> str:
    """`new` (create it), or `resume` (a crashed run in the same root); anything else refuses.

    A hard crash resumes in the same root under the same binding. A finished or stopped Go A
    never does, and Go B needs the root of Go A.
    """
    if go == 1:
        if not root.exists():
            return "new"
        if not (root / "run.json").exists() or (root / "go_a_done.json").exists():
            raise ValueError("the root exists and is not a resumable go 1 root")
        return "resume"
    if not (root / "go_a_done.json").exists():
        raise ValueError("go 2 needs the root of a finished go 1")
    return "resume"


def _emit(text: str) -> None:
    sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the contract and its hashes; no network")
    write = sub.add_parser("write-contract", help="write or check the frozen contract JSON")
    write.add_argument("--check", action="store_true")
    fetch = sub.add_parser("fetch", help="run one go; needs the owner's go for that go")
    fetch.add_argument("--go", type=int, choices=(1, 2), required=True)
    fetch.add_argument("--approved-sha256", required=True, help="the go_sha256 the owner approved")
    fetch.add_argument("--queue-sha256", help="go 2 only: the reviewed queue_b.json hash")
    fetch.add_argument("--metadata-root", type=Path, required=True)
    fetch.add_argument("--root", type=Path, required=True)
    show = sub.add_parser("report", help="print the aggregate report of a finished go; no network")
    show.add_argument("--root", type=Path, required=True)
    show.add_argument("--phase", choices=("a", "final"), required=True)
    args = parser.parse_args(argv)
    contract = build_contract()
    if args.command == "plan":
        _emit(json.dumps(contract, indent=2, sort_keys=True))
        return 0
    if args.command == "report":
        return show_report(args.root, args.phase)
    if args.command == "write-contract":
        path = Path(CONTRACT_PATH)
        if args.check:
            return 0 if path.exists() and json.loads(path.read_text()) == contract else 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(s.canonical(contract))
        return 0
    if args.approved_sha256 != contract["go_sha256"][str(args.go)]:
        raise ValueError("the approved hash is not this contract's hash for that go")
    if args.go == 2 and not args.queue_sha256:
        raise ValueError("go 2 needs --queue-sha256")
    root: Path = args.root
    source: Path = args.metadata_root
    check_root_location(root, source, repo_root())
    state = root_state(args.go, root)
    bound = binding()
    p.require_merged(bound["revision"])
    code = {"revision": bound["revision"], "publication": bound["publication"]}
    sealed = load_sealed(source)
    if state == "new":
        root.mkdir(mode=0o700, parents=True)
    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        fetcher = BodyFetcher(root, contract, client, go=args.go, code=bound, specs=class_specs())
        verify = make_verify(source, sealed, PINS, code)
        if args.go == 1:
            report = run_a(fetcher, sealed, code=code, verify=verify)
        else:
            report = run_b(fetcher, sealed, args.queue_sha256, code=code, verify=verify)
    _emit(json.dumps({"outcome": report["outcome"], "stops": report["stop_codes"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
