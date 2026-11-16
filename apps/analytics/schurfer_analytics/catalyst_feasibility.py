"""Catalyst feasibility v1: announcement timestamps and counts, never market values.

Contract: docs/research/catalyst-feasibility-v1.md. The fetcher reads public
announcement lists and the Bybit linear catalog. Every item whose canonical machine
time is outside the two windows is dropped before any other field is read, and no
response body is ever persisted (blind-window guards, contract section 6).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economics as b3
from .bybit_carry_feasibility import publish_once

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

VERSION = "catalyst_feasibility_v1"
STUDY_ID = "catalyst-feasibility-v1"
CONTRACT = "docs/research/catalyst-feasibility-v1.md"
REGISTRY = "docs/research/seen-windows-registry.md"
PUBLISHED = (
    CONTRACT,
    REGISTRY,
    "apps/analytics/schurfer_analytics/catalyst_feasibility.py",
    "apps/analytics/schurfer_analytics/bybit_carry_feasibility.py",
    "apps/analytics/schurfer_analytics/bybit_presettlement_economics.py",
    "apps/analytics/schurfer_analytics/bybit_presettlement_bounded_fetch.py",
)


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).astimezone(UTC).timestamp() * 1000)


SECOND_MS = 1_000
MINUTE_MS = 60_000
DAY_MS = 86_400_000
WINDOWS = {
    "discovery": (_ms("2024-10-01T00:00:00+00:00"), _ms("2025-10-01T00:00:00+00:00")),
    "holdout": (_ms("2023-02-01T00:00:00+00:00"), _ms("2024-09-16T00:00:00+00:00")),
}
OLDEST_MS = WINDOWS["holdout"][0]
CUT_MS = WINDOWS["discovery"][1]  # instruments: nothing launched or delivered from here

MAX_REQUESTS = 3_000
MAX_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SPACING_S = 1.0
ATTEMPTS_PER_PAGE = 3
MAX_PAGES_PER_SERIES = 600
COMPLETE_STOPS = ("empty", "time_boundary", "cursor_end")

MIN_CLUSTERS = {"discovery": 40, "holdout": 30}
MIN_ANNOUNCEMENTS = {"discovery": 40, "holdout": 30}
MIN_BASES = {"discovery": 20, "holdout": 15}
MIN_WEEKS = {"discovery": 8, "holdout": 8}
MIN_TIMED_SHARE = 0.90
MAX_AMBIGUOUS_SHARE = 0.10
MAX_EDIT_SHARE = 0.10
MAX_DISCORDANT_SHARE = 0.05
MAX_WHOLE_MINUTE_SHARE = 0.20
EDIT_TOLERANCE_MS = 60 * SECOND_MS
LIVE_LEAD_MS = DAY_MS

BUCKETS = ("A1", "A2", "A3", "A4d", "A4f", "A4r")
BUCKET_SOURCES = {
    "A1": ("binance", "upbit", "coinbase"),
    "A2": ("binance",),
    "A3": ("binance", "upbit"),
    "A4d": ("bybit",),
    "A4f": ("bybit",),
    "A4r": ("bybit",),
}
# No public history with a second-precision listing time is known (contract section 6).
UNREQUESTED_SOURCES = {"coinbase": "not_reconstructible"}


@dataclass(frozen=True)
class Series:
    name: str
    source: str
    url: str
    params: tuple[tuple[str, str], ...]
    paging: str  # "page" or "cursor"
    page_param: str = "page"


BYBIT_ANN = "https://api.bybit.com/v5/announcements/index"
BYBIT_INST = "https://api.bybit.com/v5/market/instruments-info"
BINANCE_CMS = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
UPBIT_NOTICES = "https://api-manager.upbit.com/api/v1/announcements"

SERIES: tuple[Series, ...] = (
    *(
        Series(
            f"bybit_ann:{kind}",
            "bybit",
            BYBIT_ANN,
            (("locale", "en-US"), ("type", kind), ("limit", "20")),
            "page",
        )
        for kind in ("delistings", "product_updates")
    ),
    *(
        Series(
            f"bybit_inst:{status}",
            "bybit_instruments",
            BYBIT_INST,
            (("category", "linear"), ("status", status), ("limit", "1000")),
            "cursor",
        )
        for status in ("Trading", "Delivering", "Closed")
    ),
    *(
        Series(
            f"binance:{catalog}",
            "binance",
            BINANCE_CMS,
            (("type", "1"), ("catalogId", catalog), ("pageSize", "20")),
            "page",
            "pageNo",
        )
        for catalog in ("48", "161", "49")
    ),
    Series("upbit:notices", "upbit", UPBIT_NOTICES, (("os", "web"), ("per_page", "20")), "page"),
)


# ---------------------------------------------------------------- source schemas


def _get(payload: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(payload, dict) or key not in payload:
            raise KeyError(".".join(path))
        payload = payload[key]
    return payload


def raw_items(source: str, payload: Any) -> tuple[list[dict[str, Any]], str]:
    """Return the list of items and the next cursor; raise on any schema mismatch."""
    if source in ("bybit", "bybit_instruments"):
        if _get(payload, "retCode") != 0:
            raise ValueError("retCode")
        items = _get(payload, "result", "list")
        cursor = payload["result"].get("nextPageCursor") or ""
        if not isinstance(cursor, str):
            raise TypeError("cursor")
    elif source == "binance":
        if _get(payload, "code") != "000000":
            raise ValueError("code")
        catalogs = _get(payload, "data", "catalogs")
        if not isinstance(catalogs, list) or len(catalogs) != 1:
            raise ValueError("catalogs")
        items, cursor = _get(catalogs[0], "articles"), ""
    elif source == "upbit":
        if _get(payload, "success") is not True:
            raise ValueError("success")
        items, cursor = _get(payload, "data", "notices"), ""
    else:
        raise ValueError(source)
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise TypeError("items")
    return items, cursor


def _int_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("bool time")
    number = int(value)  # Bybit sends strings in the catalog, ints in announcements
    if number <= 0:
        raise ValueError("time")
    return number * 1000 if number < 100_000_000_000 else number


def _iso_ms(value: Any) -> int:
    if not isinstance(value, str):
        raise TypeError("iso time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive time")
    return int(parsed.timestamp() * 1000)


def canonical_ms(source: str, item: dict[str, Any]) -> int:
    """Earliest machine-set publication time; the only field read before the window cut."""
    if source == "bybit":
        return _int_ms(item["publishTime"])
    if source == "bybit_instruments":
        return _int_ms(item["launchTime"])
    if source == "binance":
        return _int_ms(item["releaseDate"])
    if source == "upbit":
        return min(_iso_ms(item["first_listed_at"]), _iso_ms(item["listed_at"]))
    raise ValueError(source)


TIME_FIELDS = {
    "bybit": ("publishTime",),
    "bybit_instruments": ("launchTime",),
    "binance": ("releaseDate",),
    "upbit": ("first_listed_at", "listed_at"),
}


class SchemaMismatchError(ValueError):
    """One item broke the expected shape. Carries a fixed-vocabulary reason, never a value."""

    def __init__(self, index: int, reason: str) -> None:
        super().__init__(f"item {index}: {reason}")
        self.index, self.reason = index, reason


def explain_time_failure(source: str, item: dict[str, Any]) -> str:
    """Name why an item's machine time is unusable, from key presence and types only.

    The vocabulary is fixed here: field names come from the code and Python type
    names from the value, so no value, title or symbol can reach a receipt.
    """
    for field in TIME_FIELDS[source]:
        if field not in item:
            return f"missing:{field}"
        value = item[field]
        if isinstance(value, bool):
            return f"bool:{field}"
        if source == "upbit":
            if not isinstance(value, str):
                return f"type_{type(value).__name__}:{field}"
            continue
        if isinstance(value, int | float):
            return f"non_positive_or_unusable_number:{field}"
        if isinstance(value, str):
            return f"str_not_positive_integer:{field}"
        return f"type_{type(value).__name__}:{field}"
    return "unusable_time"


ENVELOPE_REASONS = frozenset(
    {
        "retCode",
        "code",
        "catalogs",
        "success",
        "cursor",
        "items",
        "result",
        "result.list",
        "data",
        "data.catalogs",
        "data.notices",
        "articles",
        "list",
    }
)


def failure_record(exc: BaseException) -> dict[str, Any]:
    """Key-only account of a schema failure: exception class and a fixed reason."""
    record: dict[str, Any] = {"class": type(exc).__name__}
    if isinstance(exc, SchemaMismatchError):
        record.update(stage="item", item_index=exc.index, reason=exc.reason)
        return record
    first = exc.args[0] if exc.args else None
    record["stage"] = "envelope"
    record["reason"] = first if isinstance(first, str) and first in ENVELOPE_REASONS else "other"
    return record


def window_of(ms: int) -> str | None:
    for name, (first, end) in WINDOWS.items():
        if first <= ms < end:
            return name
    return None


def key_tree(payload: Any, prefix: str = "") -> list[str]:
    """Key paths and value types only, never values (schema probe)."""
    out: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.add(f"{path}:{type(value).__name__}")
            out.update(key_tree(value, path))
    elif isinstance(payload, list):
        for value in payload:
            out.update(key_tree(value, f"{prefix}[]"))
    return sorted(out)


def instrument_record(item: dict[str, Any], launch: int) -> dict[str, Any] | None:
    """Keep symbol, launch and a delivery before the cut; never status (section 6)."""
    if item.get("contractType") != "LinearPerpetual" or item.get("settleCoin") != "USDT":
        return None
    symbol = item["symbol"]
    if not isinstance(symbol, str) or not symbol.endswith("USDT"):
        return None
    raw = item.get("deliveryTime")
    delivery = _int_ms(raw) if raw not in (None, "", "0", 0) else None
    return {
        "symbol": symbol,
        "launch_ms": launch,
        "delivery_ms": delivery if delivery is not None and delivery < CUT_MS else None,
    }


def cut_page(source: str, payload: Any) -> dict[str, Any]:
    """Apply the window cut to one parsed page. Raises on any schema mismatch."""
    items, cursor = raw_items(source, payload)
    kept: list[dict[str, Any]] = []
    dropped, oldest_reached = 0, False
    for index, item in enumerate(items):
        try:
            ms = canonical_ms(source, item)
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise SchemaMismatchError(index, explain_time_failure(source, item)) from exc
        # The time boundary belongs to announcement feeds only, which run from the
        # newest. It is read from the page's last item: an old notice pinned above
        # newer ones must not end the series. A catalog of instruments has no time
        # order at all and is followed by cursor to its end.
        if source != "bybit_instruments" and index == len(items) - 1 and ms < OLDEST_MS:
            oldest_reached = True
        if source == "bybit_instruments":
            if ms >= CUT_MS:
                dropped += 1
                continue
            record = instrument_record(item, ms)
            if record is not None:
                kept.append(record)
            continue
        if window_of(ms) is None:
            dropped += 1
            continue
        kept.append(item)
    return {
        "items": len(items),
        "kept": kept,
        "dropped": dropped,
        "oldest_reached": oldest_reached,
        "next_cursor": cursor,
    }


# ---------------------------------------------------------------- bounded fetch


class BudgetExceededError(RuntimeError):
    pass


class FetchStoppedError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def limits() -> dict[str, Any]:
    return {
        "max_requests": MAX_REQUESTS,
        "max_bytes": MAX_BYTES,
        "max_response_bytes": MAX_RESPONSE_BYTES,
        "spacing_s": SPACING_S,
        "attempts_per_page": ATTEMPTS_PER_PAGE,
        "max_pages_per_series": MAX_PAGES_PER_SERIES,
    }


def _ledger_usage(root: Path) -> tuple[str, int, int, list[dict[str, Any]]]:
    run_file = root / "run.json"
    if not run_file.exists():
        raise ValueError(f"prior root has no run.json: {root}")
    run_bytes = run_file.read_bytes()
    run_data = json.loads(run_bytes)
    attempts = [json.loads(p.read_text()) for p in sorted((root / "attempts").glob("*.json"))]
    receipts = [json.loads(p.read_text()) for p in sorted((root / "receipts").glob("*.json"))]
    if [a["seq"] for a in attempts] != list(range(1, len(attempts) + 1)):
        raise ValueError("attempt reservations are not a contiguous sequence")
    if not {r["seq"] for r in receipts} <= {a["seq"] for a in attempts}:
        raise ValueError("receipt without a charged attempt")
    done = {r["seq"]: int(r["body_bytes"]) for r in receipts}
    used = sum(done.get(a["seq"], MAX_RESPONSE_BYTES) for a in attempts)
    inherited = list(run_data.get("prior", {}).get("runs", []))
    return digest(run_bytes), len(attempts), used, inherited


def prior_usage(roots: Iterable[Path] = ()) -> dict[str, Any]:
    seen: dict[str, dict[str, Any]] = {}
    for root in roots:
        sha, count, used, inherited = _ledger_usage(root)
        for entry in inherited:
            key = str(entry["run_binding_sha256"])
            seen[key] = {
                "run_binding_sha256": key,
                "attempts": int(entry["attempts"]),
                "bytes": int(entry["bytes"]),
            }
        seen[sha] = {
            "run_binding_sha256": sha,
            "attempts": count,
            "bytes": used,
        }
    runs = [seen[k] for k in sorted(seen)]
    return {
        "attempts": sum(int(r["attempts"]) for r in runs),
        "bytes": sum(int(r["bytes"]) for r in runs),
        "runs": runs,
    }


def binding(prior_roots: Iterable[Path] = ()) -> dict[str, Any]:
    published = b3.publication(list(PUBLISHED))
    if f"`{STUDY_ID}`" not in Path(REGISTRY).read_text():
        raise ValueError("no seen-windows registry row names this study")
    out: dict[str, Any] = {
        "rules": VERSION,
        "revision": transport.clean_revision(),
        "publication": published,
        "limits": limits(),
        "series": [asdict(s) for s in SERIES],
    }
    prior = prior_usage(prior_roots)
    if prior["runs"]:
        out["prior"] = prior
    return out


class Fetcher:
    """Sequential, resumable, never persists a response body."""

    def __init__(
        self,
        root: Path,
        contract: dict[str, Any],
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root, self.contract, self.now, self.sleep = root, contract, now, sleep
        run = root / "run.json"
        if run.exists():
            if json.loads(run.read_text()) != contract:
                raise ValueError(
                    "run binding differs; resume cannot change code or limits, "
                    "a corrected revision starts a new run in a new root"
                )
        else:
            publish_once(run, contract)
        self.last = -SPACING_S
        self._attempts = self._files("attempts")
        self._receipts = self._files("receipts")
        if [a["seq"] for a in self._attempts] != list(range(1, len(self._attempts) + 1)):
            raise ValueError("attempt reservations are not a contiguous sequence")
        if not {r["seq"] for r in self._receipts} <= {a["seq"] for a in self._attempts}:
            raise ValueError("receipt without a charged attempt")

    def _files(self, kind: str) -> list[dict[str, Any]]:
        folder = self.root / kind
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]

    def receipts(self) -> list[dict[str, Any]]:
        return list(self._receipts)

    def _totals(self) -> tuple[int, int]:
        prior = self.contract.get("prior", {})
        prior_attempts = int(prior.get("attempts", 0))
        prior_bytes = int(prior.get("bytes", 0))
        done = {r["seq"]: r["body_bytes"] for r in self._receipts}
        used = prior_bytes + sum(done.get(a["seq"], MAX_RESPONSE_BYTES) for a in self._attempts)
        return prior_attempts + len(self._attempts), used

    def _state(self, series: Series) -> tuple[bool, int | str, int]:
        """(terminal, next page or cursor, failed attempts on that position)."""
        own = [r for r in self._receipts if r["series"] == series.name]
        ok = [r for r in own if r["outcome"] == "ok"]
        if any(r["outcome"] in ("schema_mismatch", "too_large") for r in own):
            raise FetchStoppedError(f"{series.name}: stopped on a schema or size failure")
        if ok and ok[-1]["terminal"]:
            return True, 0, 0
        if series.paging == "page":
            position: int | str = (ok[-1]["position"] + 1) if ok else 1
        else:
            position = ok[-1]["next_cursor"] if ok else ""
        failed = sum(1 for r in own if r["position"] == position and r["outcome"] != "ok")
        return False, position, failed

    def _params(self, series: Series, position: int | str) -> dict[str, str]:
        params = dict(series.params)
        if series.paging == "page":
            params[series.page_param] = str(position)
        elif position:
            params["cursor"] = str(position)
        return params

    def _one(self, client: httpx.Client, series: Series, position: int | str) -> dict[str, Any]:
        count, used = self._totals()
        if count >= MAX_REQUESTS or used + MAX_RESPONSE_BYTES > MAX_BYTES:
            raise BudgetExceededError("request or byte cap reached")
        delay = self.last + SPACING_S - self.now()
        if delay > 0:
            self.sleep(delay)
        seq = len(self._attempts) + 1
        base = {"seq": seq, "series": series.name, "position": position}
        publish_once(self.root / "attempts" / f"{seq:05d}.json", base)  # charge before I/O
        self._attempts.append(base)
        self.last = self.now()
        body, status, error = bytearray(), 0, None
        try:
            with client.stream("GET", series.url, params=self._params(series, position)) as resp:
                status = resp.status_code
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        error = "too_large"
                        break
        except httpx.HTTPError:
            error = "transport_error"  # never the exception text, URL or body
        receipt: dict[str, Any] = {
            **base,
            "http_status": status,
            "body_sha256": digest(bytes(body)),
            "body_bytes": len(body),
        }
        if error is None and status != 200:
            error = "http_error"
        if error is None:
            try:
                payload = json.loads(bytes(body))
                page = cut_page(series.source, payload)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                error = "schema_mismatch"
                receipt["failure"] = failure_record(exc)
                try:
                    receipt["schema"] = key_tree(json.loads(bytes(body)))
                except ValueError:
                    receipt["schema"] = ["<not json>"]
            else:
                if position in (1, ""):
                    receipt["schema"] = key_tree(payload)
                # Why paging ends. Only the first three mean the series was read to
                # its end; a page budget is a limit, not evidence of no more events.
                stop: str | None = None
                if page["items"] == 0:
                    stop = "empty"
                elif page["oldest_reached"]:
                    stop = "time_boundary"
                elif series.paging == "cursor" and not page["next_cursor"]:
                    stop = "cursor_end"
                elif series.paging == "page" and int(position) >= MAX_PAGES_PER_SERIES:
                    stop = "page_budget"
                terminal = stop is not None
                receipt.update(
                    outcome="ok",
                    stop=stop,
                    items=page["items"],
                    dropped=page["dropped"],
                    kept=page["kept"],
                    next_cursor=page["next_cursor"] if series.paging == "cursor" else "",
                    terminal=terminal,
                )
        if error is not None:
            receipt["outcome"] = error
        publish_once(self.root / "receipts" / f"{seq:05d}.json", receipt)
        self._receipts.append(receipt)
        return receipt

    def fetch(self, client: httpx.Client) -> dict[str, Any]:
        for series in SERIES:
            while True:
                terminal, position, failed = self._state(series)
                if terminal:
                    break
                if failed >= ATTEMPTS_PER_PAGE:
                    raise FetchStoppedError(f"{series.name}: page attempts exhausted")
                receipt = self._one(client, series, position)
                if receipt["outcome"] in ("schema_mismatch", "too_large"):
                    raise FetchStoppedError(f"{series.name}: {receipt['outcome']}")
        return self.summary()

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"series": {}}
        receipts = self.receipts()
        for series in SERIES:
            own = [r for r in receipts if r["series"] == series.name]
            ok = [r for r in own if r["outcome"] == "ok"]
            out["series"][series.name] = {
                "attempts": len(own),
                "pages_ok": len(ok),
                "kept": sum(len(r["kept"]) for r in ok),
                "dropped": sum(r["dropped"] for r in ok),
                "terminal": bool(ok and ok[-1]["terminal"]),
                "stop": ok[-1]["stop"] if ok else None,
            }
        count, used = self._totals()
        out["attempts_and_reserved_bytes"] = [count, used]
        out["budget_exhausted"] = sorted(
            name for name, v in out["series"].items() if v["stop"] == "page_budget"
        )
        out["complete"] = all(
            v["terminal"] and v["stop"] in COMPLETE_STOPS for v in out["series"].values()
        )
        return out


# ---------------------------------------------------------------- classification

TITLE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T,]+(\d{2}:\d{2})\s*\(?UTC\)?)?")
PAREN = re.compile(r"\(([A-Z0-9]{1,15})\)")
PAIR = re.compile(r"\b([A-Z0-9]{1,20})USDT\b")
CAPS = re.compile(r"(?<![A-Za-z0-9])([A-Z0-9]{2,15})(?![A-Za-z0-9])")
STOP = frozenset(
    {
        "USDT",
        "USDC",
        "USD",
        "UTC",
        "FDUSD",
        "TRY",
        "EUR",
        "BRL",
        "KRW",
        "JPY",
        "BNB",
        "BTC",
        "ETH",
        "NFT",
        "VIP",
        "API",
        "AM",
        "PM",
        "TUSD",
        "BUSD",
    }
)
# All-caps tokens of exchange titles that are never an asset. Applied only to the
# free-text parser below; a ticker in parentheses or in a ...USDT pair is explicit.
CAPS_STOP = STOP | frozenset(
    {
        "AMA",
        "APR",
        "APY",
        "CEX",
        "DEX",
        "EEA",
        "ETF",
        "EU",
        "FAQ",
        "KYC",
        "OTC",
        "P2P",
        "UAE",
        "UID",
        "UK",
        "US",
    }
)
MULTIPLIER_PREFIX = re.compile(r"(?:10{2,}|1M)([A-Z0-9]+)")
MULTIPLIER_SUFFIX = re.compile(r"([A-Z0-9]+?)10{2,}")


def asset(ticker: str) -> str:
    """Strip a contract-size multiplier: 1000PEPE, 1000000MOG, SHIB1000 -> base."""
    for pattern in (MULTIPLIER_PREFIX, MULTIPLIER_SUFFIX):
        match = pattern.fullmatch(ticker)
        if match:
            return match.group(1)
    return ticker


def _pairs(title: str) -> list[str]:
    return sorted({asset(m) for m in PAIR.findall(title) if m not in STOP})


def _caps(title: str) -> list[str]:
    tokens = {t[:-4] if t.endswith("USDT") and len(t) > 4 else t for t in CAPS.findall(title)}
    return sorted({asset(t) for t in tokens if t not in CAPS_STOP and not t.isdigit()})


def _parens(title: str) -> list[str]:
    return sorted({asset(t) for t in PAREN.findall(title) if t not in STOP})


def classify(source: str, series: str, item: dict[str, Any]) -> tuple[str, list[str]]:
    """Bucket (or 'other') and the base assets. Patterns are fixed before any count."""
    bucket, bases, _ = classify_parsed(source, series, item)
    return bucket, bases


def classify_parsed(source: str, series: str, item: dict[str, Any]) -> tuple[str, list[str], str]:
    """As classify, plus how the bases were read: 'explicit' or 'caps'.

    'explicit' is a ticker in parentheses or a ...USDT pair. 'caps' is the free-text
    parser, which reads every all-caps token after the title's key phrase and can
    therefore take an ordinary word for a ticker; the report counts its events.
    """
    title = str(item.get("title", ""))
    if source == "binance":
        catalog = series.split(":", 1)[1]
        if catalog == "48":
            if re.search(r"\bWill List\b", title):
                explicit = _parens(title)
                if explicit:
                    return "A1", explicit, "explicit"
                return "A1", _caps(title.split("Will List", 1)[1]), "caps"
            if re.search(r"Futures Will Launch", title) and "Perpetual" in title:
                return "A2", _pairs(title), "explicit"
        elif catalog == "161":
            if re.search(r"\bWill Delist\b", title):
                explicit = _pairs(title)
                if explicit:
                    return "A3", explicit, "explicit"
                return "A3", _caps(title.split("Will Delist", 1)[1]), "caps"
        elif catalog == "49" and "Monitoring Tag" in title and not re.search(r"Remov", title):
            # Only the text after the phrase: the words before it are never the list.
            return "A3", _caps(title.split("Monitoring Tag", 1)[1]), "caps"
        return "other", [], "explicit"
    if source == "upbit":
        listing = "디지털 자산 추가" in title or "신규 거래지원" in title
        market = "원화" in title or "KRW" in title or "USDT" in title
        if listing and market:
            return "A1", _parens(title), "explicit"
        warning = ("유의 종목 지정" in title or "유의종목 지정" in title) and "해제" not in title
        if "거래지원 종료" in title or warning:
            return "A3", _parens(title), "explicit"
        return "other", [], "explicit"
    if source == "bybit":
        kind = series.split(":", 1)[1]
        if kind == "delistings" and re.search(r"perpetual|contract", title, re.I):
            return "A4d", _pairs(title), "explicit"
        if kind == "product_updates":
            if re.search(r"funding (rate |fee )?(settlement )?(interval|frequency)", title, re.I):
                return "A4f", _pairs(title), "explicit"
            if re.search(r"risk limit|leverage", title, re.I):
                return "A4r", _pairs(title), "explicit"
        return "other", [], "explicit"
    raise ValueError(source)


def effective_ms(title: str) -> int | None:
    match = TITLE_DATE.search(title)
    if not match:
        return None
    day = _ms(f"{match.group(1)}T00:00:00+00:00")
    if match.group(2):
        hours, minutes = (int(x) for x in match.group(2).split(":"))
        return day + hours * 3_600_000 + minutes * MINUTE_MS
    return day + DAY_MS - 1  # date only: latest moment of that day


def article_id(source: str, item: dict[str, Any]) -> str:
    return str(item["url"] if source == "bybit" else item["id"])


def other_machine_ms(source: str, item: dict[str, Any]) -> list[int]:
    if source == "upbit":
        return [_iso_ms(item["first_listed_at"]), _iso_ms(item["listed_at"])]
    return []


def author_ms(source: str, item: dict[str, Any]) -> int | None:
    if source == "bybit" and item.get("dateTimestamp") not in (None, "", 0):
        return _int_ms(item["dateTimestamp"])
    return None


# ---------------------------------------------------------------- report


def instruments(receipts: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, dict[tuple[str, int], dict[str, Any]]] = defaultdict(dict)
    for r in receipts:
        if r["outcome"] == "ok" and r["series"].startswith("bybit_inst:"):
            for row in r["kept"]:
                out[asset(row["symbol"][:-4])][(row["symbol"], row["launch_ms"])] = row
    return {k: list(v.values()) for k, v in out.items()}


def launch_counts(perps: dict[str, list[dict[str, Any]]], window: str) -> dict[str, int]:
    """Count distinct linear USDT perpetual launches, bases and ISO weeks in a window."""
    by_symbol: dict[str, tuple[str, int]] = {}
    for base, records in perps.items():
        for row in records:
            symbol = str(row["symbol"])
            ms = int(row["launch_ms"])
            if symbol not in by_symbol or ms < by_symbol[symbol][1]:
                by_symbol[symbol] = (base, ms)
    launched = [(base, ms) for base, ms in by_symbol.values() if window_of(ms) == window]
    return {
        "launches": len(launched),
        "bases": len({base for base, _ in launched}),
        "weeks": len({iso_week(ms) for _, ms in launched}),
    }


def articles(receipts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    sources = {s.name: s.source for s in SERIES}
    for r in receipts:
        source = sources[r["series"]]
        if r["outcome"] != "ok" or source == "bybit_instruments":
            continue
        for item in r["kept"]:
            key = (source, article_id(source, item))
            if key in seen:
                continue
            ms = canonical_ms(source, item)
            bucket, bases, parser = classify_parsed(source, r["series"], item)
            seen[key] = {
                "source": source,
                "series": r["series"],
                "id": key[1],
                "announced_ms": ms,
                "window": window_of(ms),
                "bucket": bucket,
                "bases": bases,
                "parser": parser,
                "effective_ms": effective_ms(str(item.get("title", ""))),
                "machine_ms": other_machine_ms(source, item),
                "author_ms": author_ms(source, item),
            }
    return sorted(seen.values(), key=lambda a: (a["source"], a["announced_ms"], a["id"]))


def live_status(perps: list[dict[str, Any]], at_ms: int) -> tuple[str, str | None]:
    if not perps:
        return "no_candidate", None
    live = {
        p["symbol"]
        for p in perps
        if p["launch_ms"] <= at_ms - LIVE_LEAD_MS
        and (p["delivery_ms"] is None or p["delivery_ms"] > at_ms)
    }
    if not live:
        return "no_live_perp", None
    if len(live) > 1:
        return "identity_ambiguous", None
    return "matched", next(iter(live))


def discordant_share(rows: list[dict[str, Any]]) -> float | None:
    numeric = [(int(a["id"]), a["announced_ms"]) for a in rows if str(a["id"]).isdigit()]
    if len(numeric) < 2 or len(numeric) != len(rows):
        return None
    pairs = bad = 0
    for i, (id_a, t_a) in enumerate(numeric):
        for id_b, t_b in numeric[i + 1 :]:
            if id_a == id_b or t_a == t_b:
                continue
            pairs += 1
            bad += (id_a < id_b) != (t_a < t_b)
    return bad / pairs if pairs else None


def source_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    whole = sum(1 for a in rows if a["announced_ms"] % MINUTE_MS == 0)
    with_other = [a for a in rows if a["machine_ms"]]
    edits = sum(
        1
        for a in with_other
        if max(abs(t - a["announced_ms"]) for t in a["machine_ms"]) > EDIT_TOLERANCE_MS
    )
    authored = [a for a in rows if a["author_ms"] is not None]
    author_diff = sum(
        1 for a in authored if abs(a["author_ms"] - a["announced_ms"]) > EDIT_TOLERANCE_MS
    )
    effective = [a for a in rows if a["effective_ms"] is not None]
    whole_share = whole / n if n else None
    edit_share = edits / len(with_other) if with_other else None
    # Ids are compared only within one catalog: a draft of one catalog can be created
    # long before another catalog's article that is published first.
    by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in rows:
        by_series[a["series"]].append(a)
    discord_by_series = {k: discordant_share(v) for k, v in sorted(by_series.items())}
    observed = [d for d in discord_by_series.values() if d is not None]
    order_check_failed = any(d > MAX_DISCORDANT_SHARE for d in observed)
    timed = bool(n) and whole_share is not None and whole_share <= MAX_WHOLE_MINUTE_SHARE
    edit_check_failed = edit_share is not None and edit_share > MAX_EDIT_SHARE
    provenance_ok = not edit_check_failed and not order_check_failed
    return {
        "articles": n,
        "whole_minute_share": whole_share,
        "timed": timed,
        "machine_edit_share": edit_share,
        "machine_edit_observable": bool(with_other),
        "author_time_differs_share": author_diff / len(authored) if authored else None,
        "id_order_discordant_share": max(observed) if observed else None,
        "id_order_discordant_by_series": discord_by_series,
        "id_order_discordant_pooled": discordant_share(rows),
        "edit_check_failed": edit_check_failed,
        "order_check_failed": order_check_failed,
        "effective_before_announced_share": (
            sum(1 for a in effective if a["effective_ms"] < a["announced_ms"]) / len(effective)
            if effective
            else None
        ),
        "effective_extracted": len(effective),
        "provenance_ok": provenance_ok,
    }


def iso_week(ms: int) -> str:
    year, week, _ = datetime.fromtimestamp(ms / 1000, UTC).isocalendar()
    return f"{year}-W{week:02d}"


def bucket_window(
    bucket: str,
    window: str,
    rows: list[dict[str, Any]],
    quality: dict[tuple[str, str], dict[str, Any]],
    perps: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sources = BUCKET_SOURCES[bucket]
    mine = [a for a in rows if a["bucket"] == bucket and a["window"] == window]
    usable = {
        s for s in sources if (q := quality.get((s, window))) and q["timed"] and q["provenance_ok"]
    }
    timed_sources = {s for s in sources if (q := quality.get((s, window))) and q["timed"]}
    status: Counter[str] = Counter()
    events: list[dict[str, Any]] = []
    for a in mine:
        if a["source"] not in timed_sources:
            status["not_timed"] += len(a["bases"]) or 1
            continue
        if a["source"] not in usable:
            status["provenance_failed"] += len(a["bases"]) or 1
            continue
        if not a["bases"]:
            status["no_base"] += 1
        for base in a["bases"]:
            outcome, symbol = live_status(perps.get(base, []), a["announced_ms"])
            status[outcome] += 1
            if outcome == "matched":
                events.append(
                    {
                        "source": a["source"],
                        "id": a["id"],
                        "announced_ms": a["announced_ms"],
                        "base": base,
                        "symbol": symbol,
                        "bucket": bucket,
                        "parser": a["parser"],
                    }
                )
    clusters = {(e["base"], e["announced_ms"] // DAY_MS) for e in events}
    timed_articles = sum(1 for a in mine if a["source"] in timed_sources)
    denominator = status["matched"] + status["identity_ambiguous"]
    stats = {
        "articles": len(mine),
        "articles_by_source": dict(Counter(a["source"] for a in mine)),
        "timed_share": timed_articles / len(mine) if mine else None,
        "status": dict(status),
        "events": len(events),
        "events_by_source": dict(Counter(e["source"] for e in events)),
        "clusters": len(clusters),
        "announcements": len({(e["source"], e["id"]) for e in events}),
        "caps_parsed_events": sum(1 for e in events if e["parser"] == "caps"),
        "bases": len({e["base"] for e in events}),
        "weeks": len({iso_week(e["announced_ms"]) for e in events}),
        "ambiguous_share": status["identity_ambiguous"] / denominator if denominator else None,
        "timed_sources": sorted(timed_sources),
        "usable_sources": sorted(usable),
    }
    return stats, events


def passes(stats: dict[str, Any], window: str) -> bool:
    return (
        stats["clusters"] >= MIN_CLUSTERS[window]
        and stats["announcements"] >= MIN_ANNOUNCEMENTS[window]
        and stats["bases"] >= MIN_BASES[window]
        and stats["weeks"] >= MIN_WEEKS[window]
        and (stats["timed_share"] or 0.0) >= MIN_TIMED_SHARE
        and (stats["ambiguous_share"] or 0.0) <= MAX_AMBIGUOUS_SHARE
    )


def verdict(discovery: dict[str, Any], holdout: dict[str, Any]) -> str:
    if not discovery["timed_sources"]:
        return "not_reconstructible"
    if not discovery["usable_sources"]:
        return "provenance_unproven"
    if not passes(discovery, "discovery"):
        return "insufficient_events"
    if not passes(holdout, "holdout"):
        return "lab_only"
    return "feasible"


HOLDOUT_FIELDS = (
    "articles",
    "timed_share",
    "status",
    "events",
    "clusters",
    "announcements",
    "caps_parsed_events",
    "bases",
    "weeks",
    "ambiguous_share",
    "timed_sources",
    "usable_sources",
)


def report(receipts: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the publishable report and the holdout events to seal outside Git."""
    rows = articles(receipts)
    perps = instruments(receipts)
    by_source: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for a in rows:
        if a["window"]:
            by_source[(a["source"], a["window"])].append(a)
    quality = {key: source_quality(value) for key, value in by_source.items()}
    sources = sorted({s.source for s in SERIES if s.source != "bybit_instruments"})
    unclassified = [
        s
        for s in sources
        if len(by_source.get((s, "discovery"), [])) >= 50
        and all(a["bucket"] == "other" for a in by_source[(s, "discovery")])
    ]
    if unclassified:
        raise ValueError(f"no discovery-year article classified for {unclassified}")
    buckets: dict[str, Any] = {}
    discovery_events: list[dict[str, Any]] = []
    holdout_events: list[dict[str, Any]] = []
    for bucket in BUCKETS:
        disc, d_events = bucket_window(bucket, "discovery", rows, quality, perps)
        hold, h_events = bucket_window(bucket, "holdout", rows, quality, perps)
        discovery_events += d_events
        holdout_events += h_events
        buckets[bucket] = {
            "verdict": verdict(disc, hold),
            "discovery": disc,
            "holdout": {k: hold[k] for k in HOLDOUT_FIELDS},
        }
    outcome = (
        "continue_to_lab_protocol"
        if any(b["verdict"] == "feasible" for b in buckets.values())
        else "stop_direction_a"
    )
    public_quality = {f"{s}|{w}": q for (s, w), q in sorted(quality.items())}
    return (
        {
            "rules": VERSION,
            "outcome": outcome,
            "buckets": buckets,
            "source_quality": public_quality,
            "unrequested_sources": UNREQUESTED_SOURCES,
            "instrument_assets": len(perps),
            "instrument_launches": {w: launch_counts(perps, w) for w in ("discovery", "holdout")},
            "discovery_events": sorted(
                discovery_events, key=lambda e: (e["bucket"], e["announced_ms"], e["base"])
            ),
        },
        sorted(holdout_events, key=lambda e: (e["bucket"], e["announced_ms"], e["base"])),
    )


def _canonical(data: Any) -> bytes:
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def _emit(text: str) -> None:
    sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--root", type=Path, required=True)
    fetch.add_argument("--prior-root", action="append", type=Path, default=[])
    read = sub.add_parser("report")
    read.add_argument("--root", type=Path, required=True)
    read.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        _emit(
            json.dumps(
                {
                    "rules": VERSION,
                    "limits": limits(),
                    "series": [s.name for s in SERIES],
                    "windows": WINDOWS,
                },
                indent=2,
            )
        )
        return 0
    if args.command == "fetch":
        contract = binding(args.prior_root)
        fetcher = Fetcher(args.root, contract)
        with httpx.Client(timeout=30.0) as client:
            _emit(json.dumps(fetcher.fetch(client), indent=2, sort_keys=True))
        return 0
    contract = binding()
    saved = json.loads((args.root / "run.json").read_text())
    if saved["revision"] != contract["revision"]:
        raise ValueError("report must run from the fetch revision")
    fetcher = Fetcher(args.root, saved)
    done = fetcher.summary()
    if done["budget_exhausted"]:
        raise ValueError(f"page budget reached before the end of {done['budget_exhausted']}")
    if not done["complete"]:
        raise ValueError("fetch is not complete")
    public, sealed = report(fetcher.receipts())
    sealed_bytes = _canonical(sealed)
    sealed_path = args.root / "holdout-events.json"
    if sealed_path.exists() and sealed_path.read_bytes() != sealed_bytes:
        raise ValueError("sealed holdout events differ from a previous report")
    if not sealed_path.exists():
        sealed_path.write_bytes(sealed_bytes)
    public["holdout_events_sha256"] = digest(sealed_bytes)
    provenance: dict[str, Any] = {
        "revision": contract["revision"],
        "run_binding_sha256": digest((args.root / "run.json").read_bytes()),
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if saved.get("prior", {}).get("runs"):
        provenance["prior_runs"] = saved["prior"]["runs"]
    public["provenance"] = provenance
    args.out.write_bytes(_canonical(public))
    _emit(
        json.dumps(
            {
                "outcome": public["outcome"],
                "verdicts": {k: v["verdict"] for k, v in public["buckets"].items()},
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
