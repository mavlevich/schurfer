"""Bounded, outcome-blind probes for docs/research/pre-move-source-selection-v1.md.

Runs from a workstation against public endpoints and enforces the registered protocol
(with amendment A1) rather than trusting the caller:

- every request is classified from its actual URL and parameters before it is sent:
  an allowed price-free metadata request, or an archive or historical request whose
  data window, derived from the file name or the query bounds, ends no later than
  BOUNDARY (2026-08-01T00:00:00Z). A request without a derivable upper bound, with a
  bound after BOUNDARY, or to a current-data endpoint is refused and never sent. This
  covers HEAD requests and archive listings, whose sizes are themselves data;
- hard limits on requests (total and per source), pages, bytes (counted per received
  chunk, failed attempts included) and wall time (checked during transfers too);
  a 429 or 418 stops that source;
- every response body is recorded under the gitignored runtime directory by its
  SHA-256, and the request log keeps method, URL, parameters, window end, status and
  hash, so `--replay` recomputes the whole artifact offline from those inputs. The
  artifact holds structure, counts, sizes and hashes only, never a price or a return.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .reporting import normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

PROTOCOL_VERSION = "pre_move_source_selection_v1"
PROTOCOL_AMENDMENT = "A2"
BOUNDARY = datetime(2026, 8, 1, tzinfo=UTC)
UNIVERSE_LAUNCHED_BEFORE = datetime(2026, 7, 1, tzinfo=UTC)
PROBE_DAY = datetime(2026, 7, 15, tzinfo=UTC)
PROBE_HOUR = (datetime(2026, 7, 15, 12, tzinfo=UTC), datetime(2026, 7, 15, 13, tzinfo=UTC))
SAMPLE_SALT = "pre-move-source-selection-v1:"
SAMPLE_SIZE = 12
ANCHORS = ("BTC", "ETH")
SOURCES = ("gate", "binance", "blofin", "mexc", "bybit")
# Binance archive listings are asked per year, then per month in 2026, so no key
# (and no size) dated after the boundary month is ever returned.
LISTING_YEARS: tuple[int, ...] = tuple(range(2019, 2026))
LISTING_MONTHS_2026: tuple[int, ...] = tuple(range(1, 8))

MAX_REQUESTS = 600
MAX_REQUESTS_PER_SOURCE = 200
MAX_PAGES = 50
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 300 * 1024 * 1024
TIMEOUT_SECONDS = 30.0
RETRY_BACKOFF_SECONDS = (1.0, 4.0)
MAX_WALL_SECONDS = 30 * 60
STOP_STATUSES = (418, 429)

BYBIT = "https://api.bybit.com"
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_ARCHIVE = "https://data.binance.vision"
BINANCE_LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BLOFIN = "https://openapi.blofin.com"
MEXC_HOSTS = ("https://contract.mexc.com", "https://api.mexc.com")
GATE_API = "https://api.gateio.ws/api/v4"
GATE_ARCHIVE = "https://download.gatedata.org"
BYBIT_ARCHIVE = "https://public.bybit.com"

# Price-free metadata: the documented responses carry no price, volume, OI, funding
# or trade field. Exact endpoints only.
METADATA_ENDPOINTS: frozenset[str] = frozenset(
    {
        f"{BYBIT}/v5/market/instruments-info",
        f"{BINANCE_FAPI}/fapi/v1/exchangeInfo",
        f"{BLOFIN}/api/v1/market/instruments",
        *(f"{host}/api/v1/contract/detail" for host in MEXC_HOSTS),
        BINANCE_LISTING,
    }
)
GATE_TRADES = f"{GATE_API}/futures/usdt/trades"
GATE_CONTRACT_STATS = f"{GATE_API}/futures/usdt/contract_stats"
BYBIT_OPEN_INTEREST = f"{BYBIT}/v5/market/open-interest"
FORBIDDEN_FRAGMENTS: tuple[str, ...] = (
    "/futures/usdt/contracts",
    "/ticker",
    "/tickers",
    "/depth",
    "/orderbook",
    "/deals",
    "/funding",
    "/premiumIndex",
    "/openInterest",
    "/market/trades",
    "/market/recent-trade",
)
_GATE_FILE = re.compile(
    r"^/futures_usdt/(trades|orderbooks)/(\d{6})/[A-Z0-9]+_USDT-(\d{6}|\d{10})\.csv\.gz$"
)
_BINANCE_FILE = re.compile(
    r"^/data/futures/um/daily/(aggTrades|metrics|bookDepth)/([A-Z0-9]+)/"
    r"\2-\1-(\d{4}-\d{2}-\d{2})\.zip(?:\.CHECKSUM)?$"
)
_BYBIT_FILE = re.compile(r"^/trading/([A-Z0-9]+)/\1(\d{4}-\d{2}-\d{2})\.csv\.gz$")
_BINANCE_PREFIX = re.compile(
    r"^data/futures/um/daily/aggTrades/([A-Z0-9]+)/\1-aggTrades-(\d{4})(?:-(\d{2}))?$"
)


class ProtocolViolationError(RuntimeError):
    """The request is outside the registered protocol; it is never sent."""


class SourceStoppedError(RuntimeError):
    """The source hit a rate-limit stop, a request or byte budget, or the wall time."""


@dataclass(frozen=True)
class RequestClass:
    kind: str  # metadata, archive or historical
    window_end: datetime | None


def _month_end(yyyymm: str) -> datetime:
    year, month = int(yyyymm[:4]), int(yyyymm[4:])
    return datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)


def _int_param(params: dict[str, Any], key: str, url: str) -> int:
    try:
        return int(params[key])
    except (KeyError, TypeError, ValueError):
        raise ProtocolViolationError(f"{url} needs an integer `{key}` bound") from None


def _archive_window_end(url: str, path: str) -> datetime:
    if url.startswith(GATE_ARCHIVE) and (match := _GATE_FILE.match(path)):
        directory, stamp = match.group(2), match.group(3)
        if not stamp.startswith(directory):
            raise ProtocolViolationError(f"archive file and directory disagree: {url}")
        if len(stamp) == 6:
            return _month_end(stamp)
        hour = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        return hour + timedelta(hours=1)
    if url.startswith(BINANCE_ARCHIVE) and (match := _BINANCE_FILE.match(path)):
        return datetime.fromisoformat(match.group(3)).replace(tzinfo=UTC) + timedelta(days=1)
    if url.startswith(BYBIT_ARCHIVE) and (match := _BYBIT_FILE.match(path)):
        return datetime.fromisoformat(match.group(2)).replace(tzinfo=UTC) + timedelta(days=1)
    raise ProtocolViolationError(f"archive path without a registered date: {url}")


def _listing_window_end(params: dict[str, Any]) -> datetime:
    match = _BINANCE_PREFIX.match(str(params.get("prefix", "")))
    if match is None:
        raise ProtocolViolationError("archive listing needs a dated year or month prefix")
    year, month = int(match.group(2)), match.group(3)
    if month is None:
        return datetime(year + 1, 1, 1, tzinfo=UTC)
    return _month_end(f"{year}{month}")


def classify_request(method: str, url: str, params: dict[str, Any] | None) -> RequestClass:
    """Derive the request's data window from its actual URL and parameters and refuse
    anything outside the protocol. Nothing is trusted from the caller."""
    params = params or {}
    if "?" in url:
        raise ProtocolViolationError("query parameters must be passed separately")
    parsed = httpx.URL(url)
    if any(fragment in parsed.path for fragment in FORBIDDEN_FRAGMENTS) and not url.startswith(
        (BINANCE_ARCHIVE, GATE_ARCHIVE, BYBIT_ARCHIVE)
    ):
        raise ProtocolViolationError(f"forbidden current-data endpoint: {url}")
    if url in METADATA_ENDPOINTS:
        if method != "GET":
            raise ProtocolViolationError(f"metadata request must be a GET: {url}")
        listing_end = _listing_window_end(params) if url == BINANCE_LISTING else None
        result = RequestClass("metadata", listing_end)
    elif url.startswith((BINANCE_ARCHIVE, GATE_ARCHIVE, BYBIT_ARCHIVE)):
        if method not in ("GET", "HEAD") or params:
            raise ProtocolViolationError(f"archive request must be a plain GET or HEAD: {url}")
        result = RequestClass("archive", _archive_window_end(url, parsed.path))
    elif url == GATE_TRADES:
        start_s, end_s = _int_param(params, "from", url), _int_param(params, "to", url)
        if start_s >= end_s:
            raise ProtocolViolationError("empty or inverted trade window")
        result = RequestClass("historical", datetime.fromtimestamp(end_s, UTC))
    elif url == GATE_CONTRACT_STATS:
        # Amendment A2: `from + interval x limit` does not bound the response (run 2
        # asked for 12:00 to 13:00 and received rows to 13:05), and Gate documents no
        # upper time bound. Without a provable end, the request is never sent.
        raise ProtocolViolationError(
            "contract_stats has no enforceable upper time bound; deferred under A2"
        )
    elif url == BYBIT_OPEN_INTEREST:
        start_ms = _int_param(params, "startTime", url)
        end_ms = _int_param(params, "endTime", url)
        if start_ms >= end_ms:
            raise ProtocolViolationError("empty or inverted open-interest window")
        result = RequestClass("historical", datetime.fromtimestamp(end_ms / 1000, UTC))
    else:
        raise ProtocolViolationError(f"endpoint not in the registered protocol: {url}")
    if method != "GET" and result.kind != "archive":
        raise ProtocolViolationError(f"only GET is registered for {url}")
    if result.window_end is not None and result.window_end > BOUNDARY:
        raise ProtocolViolationError(f"data window ends {result.window_end.isoformat()}: {url}")
    if result.kind != "metadata" and result.window_end is None:
        raise ProtocolViolationError(f"no derivable data window: {url}")
    return result


@dataclass
class Budget:
    started: float = field(default_factory=lambda: time.monotonic())
    requests: Counter[str] = field(default_factory=Counter)
    bytes_downloaded: int = 0
    stopped: dict[str, str] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    def remaining(self) -> float:
        return MAX_WALL_SECONDS - (time.monotonic() - self.started)

    def check_time(self) -> None:
        if self.remaining() <= 0:
            raise SourceStoppedError("wall-time limit reached")

    def take_bytes(self, count: int) -> None:
        """Count every received chunk, failed attempts included, then stop past the cap."""
        self.bytes_downloaded += count
        if self.bytes_downloaded > MAX_TOTAL_BYTES:
            raise SourceStoppedError("total byte limit reached")

    def admit(self, source: str) -> None:
        if source in self.stopped:
            raise SourceStoppedError(f"{source} stopped: {self.stopped[source]}")
        self.check_time()
        if sum(self.requests.values()) >= MAX_REQUESTS:
            raise SourceStoppedError("total request limit reached")
        if self.requests[source] >= MAX_REQUESTS_PER_SOURCE:
            self.stopped[source] = "per-source request limit"
            raise SourceStoppedError(f"{source} request limit reached")
        self.requests[source] += 1


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def _canonical_params(params: dict[str, Any] | None) -> dict[str, str]:
    return {k: str(v) for k, v in sorted((params or {}).items())}


class Prober:
    def __init__(
        self,
        client: httpx.Client,
        raw_dir: Path,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.raw_dir = raw_dir
        self.budget = Budget()
        self.sleep = sleep

    def record(self, body: bytes) -> str:
        """Content-addressed store of a response body, for offline replay."""
        digest = hashlib.sha256(body).hexdigest()
        target = self.raw_dir / "responses" / digest
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return digest

    def keep(self, _name: str, body: bytes) -> str:
        return self.record(body)

    def _retry(self, attempt: int) -> bool:
        if attempt >= len(RETRY_BACKOFF_SECONDS):
            return False
        delay = RETRY_BACKOFF_SECONDS[attempt]
        if self.budget.remaining() <= delay:
            raise SourceStoppedError("wall-time limit reached")
        self.sleep(delay)
        return True

    def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> Response:
        cls = classify_request(method, url, params)
        attempt = 0
        while True:
            self.budget.admit(source)
            entry: dict[str, Any] = {
                "source": source,
                "method": method,
                "url": url,
                "params": _canonical_params(params),
                "kind": cls.kind,
                "window_end": cls.window_end.isoformat() if cls.window_end else None,
            }
            received = 0
            try:
                timeout = min(TIMEOUT_SECONDS, self.budget.remaining())
                if method == "HEAD":
                    raw = self.client.head(url, timeout=timeout)
                    body = b""
                else:
                    with self.client.stream("GET", url, params=params, timeout=timeout) as streamed:
                        raw = streamed
                        chunks: list[bytes] = []
                        for chunk in streamed.iter_bytes():
                            received += len(chunk)
                            self.budget.take_bytes(len(chunk))
                            self.budget.check_time()
                            if received > max_bytes:
                                raise ProtocolViolationError(f"response over {max_bytes} bytes")
                            chunks.append(chunk)
                        body = b"".join(chunks)
            except httpx.TransportError as error:
                entry.update(status=None, error=type(error).__name__, bytes=received)
                self.budget.log.append(entry)
                if self._retry(attempt):
                    attempt += 1
                    continue
                raise
            except (SourceStoppedError, ProtocolViolationError) as error:
                entry.update(status=None, error=str(error), bytes=received)
                self.budget.log.append(entry)
                raise
            headers = {k.lower(): v for k, v in raw.headers.items()}
            entry.update(
                status=raw.status_code,
                bytes=received,
                sha256=self.record(body),
                content_length=headers.get("content-length"),
            )
            self.budget.log.append(entry)
            if raw.status_code in STOP_STATUSES:
                self.budget.stopped[source] = f"HTTP {raw.status_code}"
                raise SourceStoppedError(f"{source} rate-limited: HTTP {raw.status_code}")
            if raw.status_code >= 500 and self._retry(attempt):
                attempt += 1
                continue
            return Response(raw.status_code, headers, body)


def replay_transport(log: Sequence[dict[str, Any]], raw_dir: Path) -> httpx.MockTransport:
    """Serve the recorded responses in their original order, offline. A request that
    was not recorded fails, so a replay cannot silently differ from the run."""
    queues: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in log:
        key = (entry["method"], entry["url"], json.dumps(entry["params"], sort_keys=True))
        queues[key].append(entry)

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        params = {k: v for k, v in sorted(request.url.params.multi_items())}
        key = (request.method, url, json.dumps(params, sort_keys=True))
        if not queues.get(key):
            raise AssertionError(f"request not in the recorded log: {request.method} {request.url}")
        entry = queues[key].pop(0)
        if entry.get("status") is None:
            raise httpx.ReadError(str(entry.get("error")), request=request)
        body = (raw_dir / "responses" / entry["sha256"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise ValueError(f"recorded response {entry['sha256']} is corrupt")
        if entry.get("content_length") is None:
            # Recorded without a Content-Length (a chunked transfer): stream it so no
            # length header is synthesized and the replayed log matches the run.
            return httpx.Response(entry["status"], stream=httpx.ByteStream(body))
        headers = {"content-length": entry["content_length"]}
        return httpx.Response(entry["status"], headers=headers, content=body)

    return httpx.MockTransport(handle)


# --- catalogue and sample --------------------------------------------------------------


def _ms(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def bybit_universe(instruments: Sequence[dict[str, Any]]) -> list[str]:
    """Bases of USDT linear perpetuals launched before July and not delivered before
    August, from the price-free catalogue."""
    launched_before = int(UNIVERSE_LAUNCHED_BEFORE.timestamp() * 1000)
    boundary = int(BOUNDARY.timestamp() * 1000)
    bases = set()
    for item in instruments:
        if item.get("quoteCoin") != "USDT" or item.get("contractType") != "LinearPerpetual":
            continue
        launch = _ms(item.get("launchTime"))
        delivery = _ms(item.get("deliveryTime"))
        if 0 < launch < launched_before and (delivery == 0 or delivery >= boundary):
            bases.add(str(item["baseCoin"]))
    return sorted(bases)


def sample(universe: Sequence[str]) -> list[str]:
    ordered = sorted(
        (b for b in universe if b not in ANCHORS),
        key=lambda b: hashlib.sha256((SAMPLE_SALT + b).encode()).hexdigest(),
    )
    return [*ordered[:SAMPLE_SIZE], *(a for a in ANCHORS if a in universe)]


def binance_symbols(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(s["baseAsset"]): {
            "symbol": s["symbol"],
            "status": s.get("status"),
            "onboard_date": s.get("onboardDate"),
            "delivery_date": s.get("deliveryDate"),
        }
        for s in info.get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL"
    }


def blofin_symbols(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(i["baseCurrency"]): {
            "symbol": i["instId"],
            "state": i.get("state"),
            "list_time": i.get("listTime"),
            "off_time": i.get("offTime"),
        }
        for i in payload.get("data", [])
        if i.get("quoteCurrency") == "USDT" and i.get("contractType") in (None, "linear")
    }


def mexc_symbols(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(i["baseCoin"]): {"symbol": i["symbol"], "state": i.get("state")}
        for i in payload.get("data", [])
        if i.get("quoteCoin") == "USDT" and i.get("settleCoin") == "USDT"
    }


# --- parsers (structure and counts only) -------------------------------------------------


def _timestamp_unit(value: float) -> str:
    if value > 1e17:
        return "nanoseconds"
    if value > 1e14:
        return "microseconds"
    if value > 1e11:
        return "milliseconds"
    return "seconds"


def _id_continuity(ids: Sequence[int]) -> dict[str, Any]:
    gaps = sum(1 for a, b in pairwise(ids) if b - a > 1)
    backwards = sum(1 for a, b in pairwise(ids) if b <= a)
    return {"gaps": gaps, "non_increasing": backwards}


def gate_trades_structure(
    body: bytes, hour: tuple[datetime, datetime]
) -> tuple[dict[str, Any], set[int]]:
    """Gate monthly trade archive: `timestamp, dealid, price, size` (size sign = side)."""
    rows = list(csv.reader(io.StringIO(gzip.decompress(body).decode())))
    header = rows[0] if rows and not _is_number(rows[0][0]) else None
    data = rows[1:] if header else rows
    stamps = [float(r[0]) for r in data]
    ids = [int(r[1]) for r in data]
    sizes = [float(r[3]) for r in data]
    lo, hi = hour[0].timestamp(), hour[1].timestamp()
    hour_ids = {i for t, i in zip(stamps, ids, strict=True) if lo <= t < hi}
    return (
        {
            "header": header,
            "columns": len(data[0]) if data else 0,
            "rows": len(data),
            "timestamp_unit": _timestamp_unit(stamps[0]) if stamps else None,
            "first": stamps[0] if stamps else None,
            "last": stamps[-1] if stamps else None,
            "timestamps_non_decreasing": all(a <= b for a, b in pairwise(stamps)),
            "ids": _id_continuity(ids),
            "size_sign": {
                "positive": sum(s > 0 for s in sizes),
                "negative": sum(s < 0 for s in sizes),
                "zero": sum(s == 0 for s in sizes),
            },
            "probe_hour_rows": len(hour_ids),
        },
        hour_ids,
    )


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def zip_csv_rows(body: bytes) -> tuple[list[str] | None, list[list[str]]]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        (name,) = archive.namelist()
        text = archive.read(name).decode()
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0] if rows and not _is_number(rows[0][0]) else None
    return header, rows[1:] if header else rows


def binance_agg_trades_structure(body: bytes) -> dict[str, Any]:
    """agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time,
    is_buyer_maker."""
    header, rows = zip_csv_rows(body)
    agg = [int(r[0]) for r in rows]
    first = [int(r[3]) for r in rows]
    last = [int(r[4]) for r in rows]
    stamps = [int(r[5]) for r in rows]
    maker = Counter(r[6].strip().lower() for r in rows)
    trade_gaps = sum(1 for a, b in zip(last, first[1:], strict=False) if b != a + 1)
    return {
        "header": header,
        "rows": len(rows),
        "timestamp_unit": _timestamp_unit(stamps[0]) if stamps else None,
        "timestamps_non_decreasing": all(a <= b for a, b in pairwise(stamps)),
        "agg_ids": _id_continuity(agg),
        "underlying_trade_id_gaps": trade_gaps,
        "is_buyer_maker_values": dict(maker),
    }


def binance_table_structure(body: bytes, time_column: int = 0) -> dict[str, Any]:
    header, rows = zip_csv_rows(body)
    stamps = [r[time_column] for r in rows]
    parsed: list[float] = []
    for s in stamps:
        try:
            parsed.append(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())
        except ValueError:
            parsed.append(float(s) / (1000 if float(s) > 1e11 else 1))
    steps = Counter(round(b - a) for a, b in pairwise(parsed))
    empty = sum(1 for r in rows for v in r if v.strip() == "")
    return {
        "header": header,
        "rows": len(rows),
        "time_format": "iso" if stamps and not _is_number(stamps[0]) else "epoch",
        "step_seconds_top": steps.most_common(3),
        "empty_cells": empty,
    }


def bybit_trades_structure(body: bytes) -> dict[str, Any]:
    text = gzip.decompress(body).decode()
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    stamps = [float(r["timestamp"]) for r in rows if r.get("timestamp")]
    return {
        "header": reader.fieldnames,
        "rows": len(rows),
        "timestamp_unit": _timestamp_unit(stamps[0]) if stamps else None,
        "timestamps_non_decreasing": all(a <= b for a, b in pairwise(stamps)),
        "side_values": dict(Counter(r.get("side") for r in rows)),
    }


def series_structure(times: Sequence[float], fields: Sequence[str]) -> dict[str, Any]:
    ordered = sorted(times)
    steps = Counter(round(b - a) for a, b in pairwise(ordered))
    return {
        "rows": len(times),
        "fields": sorted(fields),
        "first": ordered[0] if ordered else None,
        "last": ordered[-1] if ordered else None,
        "step_seconds_top": steps.most_common(3),
    }


# --- the probes --------------------------------------------------------------------------


def _json(response: Response) -> Any:
    return json.loads(response.body)


def _day_ms(day: datetime) -> tuple[int, int]:
    return int(day.timestamp() * 1000), int((day + timedelta(days=1)).timestamp() * 1000)


def catalogues(p: Prober) -> dict[str, Any]:
    bybit: list[dict[str, Any]] = []
    for status in ("Trading", "PreLaunch", "Delivering", "Closed"):
        cursor = ""
        for _ in range(MAX_PAGES):
            params = {"category": "linear", "status": status, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = _json(
                p.request("bybit", "GET", f"{BYBIT}/v5/market/instruments-info", params=params)
            )["result"]
            bybit.extend(result.get("list", []))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
    out: dict[str, Any] = {"bybit_instruments": len(bybit)}
    universe = bybit_universe(bybit)
    out["universe_size"] = len(universe)
    out["sample"] = sample(universe)
    out["binance"] = binance_symbols(
        _json(p.request("binance", "GET", f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"))
    )
    out["blofin"] = blofin_symbols(
        _json(p.request("blofin", "GET", f"{BLOFIN}/api/v1/market/instruments"))
    )
    mexc: dict[str, Any] = {}
    for host in MEXC_HOSTS:
        response = p.request("mexc", "GET", f"{host}/api/v1/contract/detail")
        if response.status == 200:
            mexc = {"host": host, "symbols": mexc_symbols(_json(response))}
            break
    out["mexc"] = mexc
    out["universe"] = universe
    return out


def coverage(cat: dict[str, Any], gate_present: set[str]) -> dict[str, Any]:
    universe = cat["universe"]
    sample_bases = cat["sample"]
    tables = {
        "binance": set(cat["binance"]),
        "blofin": set(cat["blofin"]),
        "mexc": set(cat["mexc"].get("symbols", {})),
    }
    out = {
        name: {
            "universe_share": len(tables[name] & set(universe)) / len(universe)
            if universe
            else None,
            "sample_present": sorted(set(sample_bases) & tables[name]),
        }
        for name in tables
    }
    out["gate"] = {"universe_share": None, "sample_present": sorted(gate_present)}
    out["bybit"] = {"universe_share": 1.0, "sample_present": list(sample_bases)}
    return out


def _first_present(order: Sequence[str], present: set[str], k: int) -> list[str]:
    return [b for b in order if b in present and b not in ANCHORS][:k]


def gate_probes(p: Prober, sample_bases: Sequence[str]) -> dict[str, Any]:
    sizes: dict[str, int | None] = {}
    for base in sample_bases:
        url = f"{GATE_ARCHIVE}/futures_usdt/trades/202607/{base}_USDT-202607.csv.gz"
        r = p.request("gate", "HEAD", url)
        sizes[base] = int(r.headers.get("content-length", 0)) if r.status == 200 else None
    present = {b for b, s in sizes.items() if s is not None}
    out: dict[str, Any] = {"g1_july_trade_files": sizes}
    chosen = [
        b
        for b in _first_present(sample_bases, present, len(sample_bases))
        if (sizes[b] or 0) <= MAX_FILE_BYTES
    ][:3]
    out["g2"] = {}
    hour_ids: dict[str, set[int]] = {}
    for base in chosen:
        url = f"{GATE_ARCHIVE}/futures_usdt/trades/202607/{base}_USDT-202607.csv.gz"
        r = p.request("gate", "GET", url)
        digest = p.keep(f"gate-trades-{base}-202607.csv.gz", r.body)
        structure, ids = gate_trades_structure(r.body, PROBE_HOUR)
        out["g2"][base] = {"sha256": digest, "bytes": len(r.body), **structure}
        hour_ids[base] = ids
    out["g3"] = {}
    lo, hi = (int(t.timestamp()) for t in PROBE_HOUR)
    for base in chosen:
        seen: set[int] = set()
        pages = 0
        status = None
        for page in range(MAX_PAGES):
            r = p.request(
                "gate",
                "GET",
                f"{GATE_API}/futures/usdt/trades",
                params={
                    "contract": f"{base}_USDT",
                    "from": lo,
                    "to": hi,
                    "limit": 1000,
                    "offset": page * 1000,
                },
            )
            status = r.status
            pages += 1
            if r.status != 200:
                break
            batch = _json(r)
            seen.update(int(t["id"]) for t in batch)
            if len(batch) < 1000:
                break
        archive = hour_ids.get(base, set())
        out["g3"][base] = {
            "status": status,
            "pages": pages,
            "rest_rows": len(seen),
            "archive_rows": len(archive),
            "only_rest": len(seen - archive),
            "only_archive": len(archive - seen),
        }
    # G4 is deferred under amendment A2: contract_stats has no enforceable upper bound.
    out["g4"] = {"status": "deferred", "reason": "no enforceable upper time bound (A2)"}
    out["g5"] = {}
    if chosen:
        base = chosen[0]
        for pattern in (
            f"futures_usdt/orderbooks/202607/{base}_USDT-202607.csv.gz",
            f"futures_usdt/orderbooks/202607/{base}_USDT-2026071512.csv.gz",
        ):
            r = p.request("gate", "HEAD", f"{GATE_ARCHIVE}/{pattern}")
            size = int(r.headers.get("content-length", 0)) if r.status == 200 else None
            out["g5"][pattern] = {"status": r.status, "bytes": size}
            if r.status == 200 and size is not None and size <= MAX_FILE_BYTES:
                g = p.request("gate", "GET", f"{GATE_ARCHIVE}/{pattern}")
                digest = p.keep(pattern.replace("/", "-"), g.body)
                head = gzip.decompress(g.body).decode().splitlines()[:50]
                rows = list(csv.reader(head))
                out["g5"][pattern].update(
                    sha256=digest,
                    columns=len(rows[0]) if rows else 0,
                    actions=sorted({r[1] for r in rows if len(r) > 1}),
                    timestamp_unit=_timestamp_unit(float(rows[0][0]))
                    if rows and _is_number(rows[0][0])
                    else None,
                )
                break
    out["chosen"] = chosen
    return out


def _listing(p: Prober, prefix: str) -> list[str]:
    keys: list[str] = []
    marker = ""
    for _ in range(MAX_PAGES):
        params = {"prefix": prefix, "max-keys": 1000}
        if marker:
            params["marker"] = marker
        r = p.request("binance", "GET", BINANCE_LISTING, params=params)
        # A plain S3 ListBucket document from a fixed public host: the keys and the
        # truncation flag are read with patterns, without an XML parser.
        text = r.body.decode()
        found = re.findall(r"<Key>([^<]+)</Key>", text)
        keys.extend(found)
        if "<IsTruncated>true</IsTruncated>" not in text or not found:
            break
        marker = found[-1]
    return keys


def binance_probes(p: Prober, cat: dict[str, Any], sample_bases: Sequence[str]) -> dict[str, Any]:
    symbols = cat["binance"]
    present = [b for b in sample_bases if b in symbols]
    out: dict[str, Any] = {"b1": {}}
    for base in present:
        symbol = symbols[base]["symbol"]
        prefix = f"data/futures/um/daily/aggTrades/{symbol}/{symbol}-aggTrades-"
        periods = [str(y) for y in LISTING_YEARS] + [f"2026-{m:02d}" for m in LISTING_MONTHS_2026]
        keys = [k for period in periods for k in _listing(p, prefix + period) if k.endswith(".zip")]
        days = sorted(k.rsplit("-aggTrades-", 1)[-1].removesuffix(".zip") for k in keys)
        out["b1"][base] = {
            "days": len(days),
            "first": days[0] if days else None,
            "last": days[-1] if days else None,
        }
    chosen = _first_present(sample_bases, set(present), 3)
    day = PROBE_DAY.date().isoformat()

    def archive(kind: str, base: str) -> tuple[bytes, str, bool]:
        symbol = symbols[base]["symbol"]
        url = f"{BINANCE_ARCHIVE}/data/futures/um/daily/{kind}/{symbol}/{symbol}-{kind}-{day}.zip"
        body = p.request("binance", "GET", url).body
        checksum = p.request("binance", "GET", url + ".CHECKSUM").body.decode().split()
        digest = p.keep(f"binance-{kind}-{symbol}-{day}.zip", body)
        return body, digest, bool(checksum) and checksum[0] == digest

    out["b2"], out["b3"], out["b4"] = {}, {}, {}
    for base in chosen:
        body, digest, ok = archive("aggTrades", base)
        out["b2"][base] = {
            "sha256": digest,
            "checksum_ok": ok,
            "bytes": len(body),
            **binance_agg_trades_structure(body),
        }
        body, digest, ok = archive("metrics", base)
        out["b3"][base] = {"sha256": digest, "checksum_ok": ok, **binance_table_structure(body)}
    if chosen:
        body, digest, ok = archive("bookDepth", chosen[0])
        out["b4"][chosen[0]] = {
            "sha256": digest,
            "checksum_ok": ok,
            **binance_table_structure(body),
        }
    out["chosen"] = chosen
    return out


def bybit_probes(p: Prober, sample_bases: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"y1": {}, "y2": {}}
    day = PROBE_DAY.date().isoformat()
    candidates = [b for b in sample_bases if b not in ANCHORS][:3]
    downloaded = False
    for base in candidates:
        symbol = f"{base}USDT"
        url = f"{BYBIT_ARCHIVE}/trading/{symbol}/{symbol}{day}.csv.gz"
        r = p.request("bybit", "HEAD", url)
        size = int(r.headers.get("content-length", 0)) if r.status == 200 else None
        out["y1"][base] = {"status": r.status, "bytes": size}
        if not downloaded and size is not None and size <= MAX_FILE_BYTES:
            g = p.request("bybit", "GET", url)
            out["y1"][base].update(
                sha256=p.keep(f"bybit-trades-{symbol}-{day}.csv.gz", g.body),
                **bybit_trades_structure(g.body),
            )
            downloaded = True
    start, end = _day_ms(PROBE_DAY)
    for base in candidates[:2]:
        r = p.request(
            "bybit",
            "GET",
            f"{BYBIT}/v5/market/open-interest",
            params={
                "category": "linear",
                "symbol": f"{base}USDT",
                "intervalTime": "5min",
                "startTime": start,
                "endTime": end - 1,
                "limit": 200,
            },
        )
        rows = (_json(r).get("result") or {}).get("list") or [] if r.status == 200 else []
        fields = sorted(rows[0]) if rows else []
        out["y2"][base] = {
            "status": r.status,
            **series_structure([int(x["timestamp"]) / 1000 for x in rows], fields),
        }
    return out


RUN_FIELDS = ("code_revision", "working_tree_dirty", "started_at", "wall_seconds")


def run(
    client: httpx.Client,
    raw_dir: Path,
    *,
    code_revision: str,
    working_tree_dirty: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    p = Prober(client, raw_dir, sleep=sleep)
    result: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "protocol_amendment": PROTOCOL_AMENDMENT,
        "boundary": BOUNDARY.isoformat(),
        "run_info": {
            "code_revision": normalize_code_revision(code_revision),
            "working_tree_dirty": working_tree_dirty,
            "started_at": datetime.now(UTC).isoformat(),
        },
    }
    cat = catalogues(p)
    universe = set(cat["universe"])
    result["catalogue"] = {
        "bybit_instruments": cat["bybit_instruments"],
        "universe_size": cat["universe_size"],
        "universe": cat["universe"],
        "sample": cat["sample"],
        "mexc_host": cat["mexc"].get("host"),
        "universe_present": {
            "binance": sorted(universe & set(cat["binance"])),
            "blofin": sorted(universe & set(cat["blofin"])),
            "mexc": sorted(universe & set(cat["mexc"].get("symbols", {}))),
        },
        "binance_fields": {b: v for b, v in cat["binance"].items() if b in cat["sample"]},
        "blofin_fields": {b: v for b, v in cat["blofin"].items() if b in cat["sample"]},
        "mexc_fields": {
            b: v for b, v in cat["mexc"].get("symbols", {}).items() if b in cat["sample"]
        },
    }
    sample_bases: list[str] = cat["sample"]
    probes: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("gate", lambda: gate_probes(p, sample_bases)),
        ("binance", lambda: binance_probes(p, cat, sample_bases)),
        ("bybit", lambda: bybit_probes(p, sample_bases)),
    ]
    for name, probe in probes:
        try:
            result[name] = probe()
        except (SourceStoppedError, httpx.HTTPError, ValueError, KeyError) as error:
            result[name] = {"aborted": f"{type(error).__name__}: {error}"}
    gate_present = {
        b for b, s in result.get("gate", {}).get("g1_july_trade_files", {}).items() if s is not None
    }
    result["coverage"] = coverage(cat, gate_present)
    result["budget"] = {
        "requests": dict(p.budget.requests),
        "requests_total": sum(p.budget.requests.values()),
        "bytes_downloaded": p.budget.bytes_downloaded,
        "stopped": p.budget.stopped,
        "limits": {
            "requests": MAX_REQUESTS,
            "per_source": MAX_REQUESTS_PER_SOURCE,
            "pages": MAX_PAGES,
            "file_bytes": MAX_FILE_BYTES,
            "total_bytes": MAX_TOTAL_BYTES,
            "wall_seconds": MAX_WALL_SECONDS,
        },
    }
    result["run_info"]["wall_seconds"] = round(time.monotonic() - p.budget.started, 1)
    result["request_log"] = p.budget.log
    return result


def replayable(result: dict[str, Any]) -> dict[str, Any]:
    """The artifact without its run-specific fields."""
    return {k: v for k, v in result.items() if k != "run_info"}


def replay(artifact: dict[str, Any], raw_dir: Path) -> dict[str, Any]:
    """Recompute the artifact offline from its recorded responses; refuse a mismatch."""
    transport = replay_transport(artifact["request_log"], raw_dir)
    with httpx.Client(transport=transport) as client:
        again = run(
            client,
            raw_dir,
            code_revision=artifact["run_info"]["code_revision"],
            working_tree_dirty=artifact["run_info"]["working_tree_dirty"],
            sleep=lambda _seconds: None,
        )
    # Compared in JSON form: the artifact on disk has lists where a run has tuples.
    if json.loads(json.dumps(replayable(again))) != json.loads(json.dumps(replayable(artifact))):
        raise ValueError("offline replay differs from the recorded artifact")
    return again


def _write(path: Path, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    path.with_name(path.name + ".sha256").write_text(digest + "\n")
    return digest


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--code-revision")
    parser.add_argument("--clean-tree", action="store_true")
    parser.add_argument(
        "--raw-dir", type=Path, default=Path("runtime/research/pre-move-source-probe-v1")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--replay", type=Path, help="recompute this artifact offline")
    args = parser.parse_args(argv)
    if args.replay is not None:
        artifact = json.loads(args.replay.read_bytes())
        replay(artifact, args.raw_dir)
        sys.stdout.write(json.dumps({"replay": "identical", "artifact": str(args.replay)}) + "\n")
        return
    if not args.code_revision or args.output is None:
        parser.error("--code-revision and --output are required for a live run")
    with httpx.Client(
        follow_redirects=False, headers={"User-Agent": "schurfer-research-probe/1"}
    ) as client:
        result = run(
            client,
            args.raw_dir,
            code_revision=args.code_revision,
            working_tree_dirty=not args.clean_tree,
        )
    digest = _write(args.output, result)
    sys.stdout.write(
        json.dumps(
            {
                "requests": result["budget"]["requests_total"],
                "bytes": result["budget"]["bytes_downloaded"],
                "sha256": digest,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
