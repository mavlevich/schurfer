"""Bounded, outcome-blind probes for docs/research/pre-move-source-selection-v1.md.

Runs once from a workstation against public endpoints. It enforces the registered
protocol rather than trusting the caller:

- a request is either an allowed price-free metadata request (catalogues, archive
  listings, HEAD) or a value-bearing historical request whose data ends no later than
  BOUNDARY (2026-08-01T00:00:00Z); forbidden current-data endpoints are refused;
- hard limits on requests (total and per source), pages, bytes per file and in total,
  retries and wall time; a 429 or 418 stops that source;
- raw downloads are kept under the gitignored runtime directory with SHA-256, and the
  artifact holds only structure, counts, sizes and hashes, never a price or a return.
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
from collections import Counter
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
BOUNDARY = datetime(2026, 8, 1, tzinfo=UTC)
UNIVERSE_LAUNCHED_BEFORE = datetime(2026, 7, 1, tzinfo=UTC)
PROBE_DAY = datetime(2026, 7, 15, tzinfo=UTC)
PROBE_HOUR = (datetime(2026, 7, 15, 12, tzinfo=UTC), datetime(2026, 7, 15, 13, tzinfo=UTC))
SAMPLE_SALT = "pre-move-source-selection-v1:"
SAMPLE_SIZE = 12
ANCHORS = ("BTC", "ETH")
SOURCES = ("gate", "binance", "blofin", "mexc", "bybit")

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

# Price-free metadata (documented responses carry no price, volume, OI, funding or
# trade field) and archive hosts, where listings and HEAD are metadata.
METADATA_PREFIXES: tuple[str, ...] = (
    f"{BYBIT}/v5/market/instruments-info",
    f"{BINANCE_FAPI}/fapi/v1/exchangeInfo",
    f"{BLOFIN}/api/v1/market/instruments",
    *(f"{host}/api/v1/contract/detail" for host in MEXC_HOSTS),
    BINANCE_LISTING,
)
ARCHIVE_PREFIXES: tuple[str, ...] = (BINANCE_ARCHIVE, GATE_ARCHIVE, BYBIT_ARCHIVE)
# Historical value endpoints, allowed only with an explicit data end before BOUNDARY.
HISTORICAL_PREFIXES: tuple[str, ...] = (
    f"{GATE_API}/futures/usdt/trades",
    f"{GATE_API}/futures/usdt/contract_stats",
    f"{BYBIT}/v5/market/open-interest",
)
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


class ProtocolViolationError(RuntimeError):
    """The request is outside the registered protocol; it is never sent."""


class SourceStoppedError(RuntimeError):
    """The source hit a rate-limit stop or its request budget."""


def check_request(method: str, url: str, *, data_end: datetime | None) -> str:
    """Classify a request as `metadata`, `archive` or `historical`, or refuse it."""
    base = url.split("?", 1)[0]
    if any(fragment in base for fragment in FORBIDDEN_FRAGMENTS) and not base.startswith(
        ARCHIVE_PREFIXES
    ):
        raise ProtocolViolationError(f"forbidden current-data endpoint: {base}")
    if base.startswith(METADATA_PREFIXES):
        if method != "GET" or data_end is not None:
            raise ProtocolViolationError(f"metadata request must be a plain GET: {base}")
        return "metadata"
    if base.startswith(ARCHIVE_PREFIXES):
        if method == "HEAD":
            return "archive"
        if data_end is None or data_end > BOUNDARY:
            raise ProtocolViolationError(
                f"archive download without a data end before boundary: {base}"
            )
        return "archive"
    if base.startswith(HISTORICAL_PREFIXES):
        if method != "GET" or data_end is None or data_end > BOUNDARY:
            raise ProtocolViolationError(f"historical request must end before the boundary: {base}")
        return "historical"
    raise ProtocolViolationError(f"endpoint not in the registered protocol: {base}")


@dataclass
class Budget:
    started: float = field(default_factory=time.monotonic)
    requests: Counter[str] = field(default_factory=Counter)
    bytes_downloaded: int = 0
    stopped: dict[str, str] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)

    def admit(self, source: str) -> None:
        if source in self.stopped:
            raise SourceStoppedError(f"{source} stopped: {self.stopped[source]}")
        if time.monotonic() - self.started > MAX_WALL_SECONDS:
            raise SourceStoppedError("wall-time limit reached")
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

    def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data_end: datetime | None = None,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> Response:
        kind = check_request(method, url, data_end=data_end)
        attempt = 0
        while True:
            self.budget.admit(source)
            entry: dict[str, Any] = {"source": source, "method": method, "kind": kind}
            entry["url"] = url
            try:
                if method == "HEAD":
                    raw = self.client.head(url, params=params, timeout=TIMEOUT_SECONDS)
                    body = b""
                else:
                    with self.client.stream(
                        "GET", url, params=params, timeout=TIMEOUT_SECONDS
                    ) as streamed:
                        raw = streamed
                        chunks: list[bytes] = []
                        size = 0
                        for chunk in streamed.iter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise ProtocolViolationError(f"response over {max_bytes} bytes")
                            if self.budget.bytes_downloaded + size > MAX_TOTAL_BYTES:
                                raise SourceStoppedError("total byte limit reached")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                self.budget.bytes_downloaded += len(body)
                entry.update(status=raw.status_code, bytes=len(body))
                self.budget.log.append(entry)
                if raw.status_code in STOP_STATUSES:
                    self.budget.stopped[source] = f"HTTP {raw.status_code}"
                    raise SourceStoppedError(f"{source} rate-limited: HTTP {raw.status_code}")
                if raw.status_code >= 500 and attempt < len(RETRY_BACKOFF_SECONDS):
                    self.sleep(RETRY_BACKOFF_SECONDS[attempt])
                    attempt += 1
                    continue
                return Response(
                    raw.status_code, {k.lower(): v for k, v in raw.headers.items()}, body
                )
            except httpx.TransportError as error:
                entry.update(status=None, error=type(error).__name__)
                self.budget.log.append(entry)
                if attempt < len(RETRY_BACKOFF_SECONDS):
                    self.sleep(RETRY_BACKOFF_SECONDS[attempt])
                    attempt += 1
                    continue
                raise

    def keep(self, name: str, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / name).write_bytes(body)
        return digest


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
    month_end = BOUNDARY
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
        r = p.request("gate", "GET", url, data_end=month_end)
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
                data_end=PROBE_HOUR[1],
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
    out["g4"] = {}
    for base in chosen[:2]:
        r = p.request(
            "gate",
            "GET",
            f"{GATE_API}/futures/usdt/contract_stats",
            params={"contract": f"{base}_USDT", "interval": "5m", "from": lo, "limit": 12},
            data_end=PROBE_HOUR[1],
        )
        if r.status == 200:
            rows = [x for x in _json(r) if x.get("time", 0) < hi]
            fields = sorted(rows[0]) if rows else []
            out["g4"][base] = {"status": 200, **series_structure([x["time"] for x in rows], fields)}
        else:
            out["g4"][base] = {"status": r.status, "error": r.body[:200].decode(errors="replace")}
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
                g = p.request("gate", "GET", f"{GATE_ARCHIVE}/{pattern}", data_end=BOUNDARY)
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
        keys = [
            k
            for k in _listing(p, f"data/futures/um/daily/aggTrades/{symbol}/")
            if k.endswith(".zip")
        ]
        days = sorted(k.rsplit("-aggTrades-", 1)[-1].removesuffix(".zip") for k in keys)
        out["b1"][base] = {
            "days": len(days),
            "first": days[0] if days else None,
            "last": days[-1] if days else None,
        }
    chosen = _first_present(sample_bases, set(present), 3)
    day = PROBE_DAY.date().isoformat()
    day_end = PROBE_DAY + timedelta(days=1)

    def archive(kind: str, base: str) -> tuple[bytes, str, bool]:
        symbol = symbols[base]["symbol"]
        url = f"{BINANCE_ARCHIVE}/data/futures/um/daily/{kind}/{symbol}/{symbol}-{kind}-{day}.zip"
        body = p.request("binance", "GET", url, data_end=day_end).body
        checksum = (
            p.request("binance", "GET", url + ".CHECKSUM", data_end=day_end).body.decode().split()
        )
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
            g = p.request("bybit", "GET", url, data_end=PROBE_DAY + timedelta(days=1))
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
            data_end=PROBE_DAY + timedelta(days=1),
        )
        rows = (_json(r).get("result") or {}).get("list") or [] if r.status == 200 else []
        fields = sorted(rows[0]) if rows else []
        out["y2"][base] = {
            "status": r.status,
            **series_structure([int(x["timestamp"]) / 1000 for x in rows], fields),
        }
    return out


def run(
    client: httpx.Client, raw_dir: Path, *, code_revision: str, working_tree_dirty: bool
) -> dict[str, Any]:
    p = Prober(client, raw_dir)
    result: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "boundary": BOUNDARY.isoformat(),
        "code_revision": normalize_code_revision(code_revision),
        "working_tree_dirty": working_tree_dirty,
        "started_at": datetime.now(UTC).isoformat(),
    }
    cat = catalogues(p)
    result["catalogue"] = {
        "bybit_instruments": cat["bybit_instruments"],
        "universe_size": cat["universe_size"],
        "sample": cat["sample"],
        "mexc_host": cat["mexc"].get("host"),
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
        "wall_seconds": round(time.monotonic() - p.budget.started, 1),
    }
    result["request_log"] = p.budget.log
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--clean-tree", action="store_true")
    parser.add_argument(
        "--raw-dir", type=Path, default=Path("runtime/research/pre-move-source-probe-v1")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    with httpx.Client(
        follow_redirects=False, headers={"User-Agent": "schurfer-research-probe/1"}
    ) as client:
        result = run(
            client,
            args.raw_dir,
            code_revision=args.code_revision,
            working_tree_dirty=not args.clean_tree,
        )
    body = json.dumps(result, indent=2, sort_keys=True).encode() + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(body)
    args.output.with_name(args.output.name + ".sha256").write_text(
        hashlib.sha256(body).hexdigest() + "\n"
    )
    sys.stdout.write(
        json.dumps(
            {
                "requests": result["budget"]["requests_total"],
                "bytes": result["budget"]["bytes_downloaded"],
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
