"""Read-only canary of order-book quality for the HYP-012 v4 venue choice (Bybit, Binance).

Before Bybit can compete with Binance as an execution venue in a registered cohort, this
measures whether each venue's executable quote is usable for the frozen $50 notional. For
each base it resolves, per venue, the exact native USDT linear perpetual from that venue's
own instrument catalog (no symbol is built from a ticker; a ``1000``-prefixed or ambiguous
contract is skipped with its reason). Every venue that has exactly one such instrument is
sampled, so a Bybit-only or Binance-only asset is still checked; where both exist the two
books are fetched at the same moment and the difference of their own timestamps recorded.

Each sample separates three questions, all recorded, none folded into another:

* ``book_ok``: the book has both sides, is not crossed, and fills $50 on each side;
* ``fresh``: the venue gave a book timestamp and its age at receipt is within the
  DESCRIPTIVE ``max_book_age_ms`` (``None`` when there is no timestamp);
* ``min_order_ok``: $50, rounded DOWN to the venue's quantity step, still meets the minimum
  quantity and minimum notional (``None`` when any of those limits is unknown).

``executable`` is true only when all three are true. Nothing is written to the database
and no order is placed. The output is one JSON artifact; the limits a v4 qualification
uses are registered separately, not tuned from this run. ``book_age_ms`` depends on the
local clock; the cross-venue timestamp difference does not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from typing import Any, Final

from .source_lead_capture import summarize_order_book

CANARY_VERSION: Final = "source_lead_venue_canary_v2"
DEFAULT_TARGET_USD: Final = 50.0
BOOK_DEPTH: Final = 50
VENUES: Final = ("bybit", "binance")


@dataclass(frozen=True)
class InstrumentSpec:
    venue: str
    base: str
    native_id: str
    contract_size: float | None
    min_order_qty: float | None
    qty_step: float | None
    min_notional_usd: float | None


@dataclass(frozen=True)
class BookSample:
    venue: str
    base: str
    native_id: str
    round_index: int
    requested_at: str
    received_at: str
    latency_ms: int
    book_timestamp_ms: int | None
    book_age_ms: int | None
    sequence: str | None
    book_ok: bool
    book_failure: str | None
    fresh: bool | None
    min_order_ok: bool | None
    executable: bool
    spread_bps: float | None
    ask_impact_bps: float | None
    bid_impact_bps: float | None
    ask_filled_usd: float | None
    bid_filled_usd: float | None
    order_qty_at_target: float | None


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _levels(raw: Any) -> list[list[float]]:
    """Venue string levels as floats, dropping malformed ones."""
    levels: list[list[float]] = []
    if not isinstance(raw, list):
        return levels
    for level in raw:
        if isinstance(level, list | tuple) and len(level) >= 2:
            price, amount = _float(level[0]), _float(level[1])
            if price is not None and amount is not None:
                levels.append([price, amount])
    return levels


def bybit_instruments(items: list[dict[str, Any]]) -> dict[str, list[InstrumentSpec]]:
    """Trading USDT linear perpetuals by base coin. Bybit linear quantity is in base coin,
    so the contract size is 1 by the venue's own definition."""
    specs: dict[str, list[InstrumentSpec]] = {}
    for item in items:
        if (
            item.get("status") != "Trading"
            or item.get("quoteCoin") != "USDT"
            or item.get("settleCoin") != "USDT"
            or item.get("contractType") != "LinearPerpetual"
        ):
            continue
        lot = item.get("lotSizeFilter") or {}
        spec = InstrumentSpec(
            venue="bybit",
            base=str(item.get("baseCoin")),
            native_id=str(item.get("symbol")),
            contract_size=1.0,
            min_order_qty=_float(lot.get("minOrderQty")),
            qty_step=_float(lot.get("qtyStep")),
            min_notional_usd=_float(lot.get("minNotionalValue")),
        )
        specs.setdefault(spec.base, []).append(spec)
    return specs


def binance_instruments(symbols: list[dict[str, Any]]) -> dict[str, list[InstrumentSpec]]:
    """Trading USDT perpetuals by base asset, with LOT_SIZE and MIN_NOTIONAL filters."""
    specs: dict[str, list[InstrumentSpec]] = {}
    for item in symbols:
        if (
            item.get("status") != "TRADING"
            or item.get("contractType") != "PERPETUAL"
            or item.get("quoteAsset") != "USDT"
            or item.get("marginAsset") != "USDT"
        ):
            continue
        filters = {f.get("filterType"): f for f in item.get("filters") or []}
        lot = filters.get("LOT_SIZE") or {}
        notional = filters.get("MIN_NOTIONAL") or {}
        spec = InstrumentSpec(
            venue="binance",
            base=str(item.get("baseAsset")),
            native_id=str(item.get("symbol")),
            contract_size=1.0,
            min_order_qty=_float(lot.get("minQty")),
            qty_step=_float(lot.get("stepSize")),
            min_notional_usd=_float(notional.get("notional")),
        )
        specs.setdefault(spec.base, []).append(spec)
    return specs


def resolve_instruments(
    bases: list[str], catalogs: dict[str, dict[str, list[InstrumentSpec]]]
) -> tuple[dict[str, dict[str, InstrumentSpec]], dict[str, dict[str, str]]]:
    """Per base, every venue with exactly one instrument for the exact base. A venue with
    none or several is recorded as skipped with its reason; a base is sampled on whatever
    venues resolve. A ``1000``-prefixed contract is never matched to a plain base."""
    resolved: dict[str, dict[str, InstrumentSpec]] = {}
    skipped: dict[str, dict[str, str]] = {}
    for base in bases:
        for venue, catalog in catalogs.items():
            found = catalog.get(base, [])
            if len(found) == 1:
                resolved.setdefault(base, {})[venue] = found[0]
            else:
                skipped.setdefault(base, {})[venue] = f"{len(found)} exact instruments"
    return resolved, skipped


def min_order_check(
    spec: InstrumentSpec, *, ask_vwap: float, target_usd: float
) -> tuple[bool | None, float | None]:
    """Whether $target, rounded DOWN to the quantity step, meets the minimum quantity and
    notional. ``None`` (unknown) when the step, minimum quantity or minimum notional is not
    published, never an assumed pass."""
    if (
        spec.contract_size is None
        or spec.qty_step is None
        or spec.qty_step <= 0
        or spec.min_order_qty is None
        or spec.min_notional_usd is None
    ):
        return None, None
    raw_qty = target_usd / ask_vwap / spec.contract_size
    steps = math.floor(raw_qty / spec.qty_step + 1e-9)
    qty = steps * spec.qty_step
    notional = qty * ask_vwap * spec.contract_size
    ok = qty > 0 and qty + 1e-12 >= spec.min_order_qty and notional + 1e-9 >= spec.min_notional_usd
    return ok, qty


def evaluate_book(
    spec: InstrumentSpec,
    *,
    bids: Any,
    asks: Any,
    book_timestamp_ms: int | None,
    sequence: str | None,
    round_index: int,
    requested_at: datetime,
    received_at: datetime,
    target_usd: float,
    max_book_age_ms: int,
) -> BookSample:
    """One venue sample; every failure keeps its reason."""
    latency_ms = max(0, round((received_at - requested_at).total_seconds() * 1000))
    age = (
        round(received_at.timestamp() * 1000) - book_timestamp_ms
        if book_timestamp_ms is not None
        else None
    )
    fresh = None if age is None else age <= max_book_age_ms
    common: dict[str, Any] = {
        "venue": spec.venue,
        "base": spec.base,
        "native_id": spec.native_id,
        "round_index": round_index,
        "requested_at": requested_at.isoformat(),
        "received_at": received_at.isoformat(),
        "latency_ms": latency_ms,
        "book_timestamp_ms": book_timestamp_ms,
        "book_age_ms": age,
        "sequence": sequence,
        "fresh": fresh,
    }
    no_book: dict[str, Any] = {
        "book_ok": False,
        "min_order_ok": None,
        "executable": False,
        "spread_bps": None,
        "ask_impact_bps": None,
        "bid_impact_bps": None,
        "ask_filled_usd": None,
        "bid_filled_usd": None,
        "order_qty_at_target": None,
    }
    if spec.contract_size is None:
        return BookSample(**common, book_failure="contract_size_unknown", **no_book)
    try:
        summary = summarize_order_book(
            {"bids": _levels(bids), "asks": _levels(asks)},
            target_usd=target_usd,
            contract_size=spec.contract_size,
        )
    except ValueError as exc:
        reason = "crossed_book" if "crossed" in str(exc) else "empty_side"
        return BookSample(**common, book_failure=reason, **no_book)
    filled = summary["ask_vwap"] is not None and summary["bid_vwap"] is not None
    min_order_ok, qty = (
        min_order_check(spec, ask_vwap=summary["ask_vwap"], target_usd=target_usd)
        if summary["ask_vwap"]
        else (None, None)
    )
    return BookSample(
        **common,
        book_ok=filled,
        book_failure=None if filled else "insufficient_depth",
        min_order_ok=min_order_ok,
        executable=filled and fresh is True and min_order_ok is True,
        spread_bps=summary["spread_bps"],
        ask_impact_bps=summary["ask_impact_bps"],
        bid_impact_bps=summary["bid_impact_bps"],
        ask_filled_usd=summary["ask_filled_notional_usd"],
        bid_filled_usd=summary["bid_filled_notional_usd"],
        order_qty_at_target=qty,
    )


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[round(q * 100) - 1]


def summarize_samples(samples: list[BookSample], cross_venue_ms: list[int]) -> dict[str, Any]:
    """Per venue: book failures, freshness and minimum-order outcomes (true/false/unknown),
    executable count, and latency/age/spread/impact percentiles; plus the cross-venue
    book-timestamp difference over paired samples."""
    summary: dict[str, Any] = {}
    for venue in VENUES:
        rows = [s for s in samples if s.venue == venue]
        failures: dict[str, int] = {}
        for row in rows:
            if row.book_failure:
                failures[row.book_failure] = failures.get(row.book_failure, 0) + 1
        ages = [float(r.book_age_ms) for r in rows if r.book_age_ms is not None]
        impacts = [r.ask_impact_bps for r in rows if r.ask_impact_bps is not None]
        summary[venue] = {
            "samples": len(rows),
            "instruments": len({r.native_id for r in rows}),
            "book_ok": sum(r.book_ok for r in rows),
            "book_failures": failures,
            "fresh": {
                "true": sum(r.fresh is True for r in rows),
                "false": sum(r.fresh is False for r in rows),
                "no_timestamp": sum(r.fresh is None for r in rows),
            },
            "min_order": {
                "ok": sum(r.min_order_ok is True for r in rows),
                "blocked": sum(r.min_order_ok is False for r in rows),
                "unknown": sum(r.min_order_ok is None for r in rows),
            },
            "executable": sum(r.executable for r in rows),
            "latency_ms_p50": _pct([float(r.latency_ms) for r in rows], 0.5),
            "latency_ms_p90": _pct([float(r.latency_ms) for r in rows], 0.9),
            "book_age_ms_p50": _pct(ages, 0.5),
            "book_age_ms_p90": _pct(ages, 0.9),
            "spread_bps_p50": _pct([r.spread_bps for r in rows if r.spread_bps is not None], 0.5),
            "ask_impact_bps_p50": _pct(impacts, 0.5),
            "ask_impact_bps_p90": _pct(impacts, 0.9),
        }
    diffs = [float(abs(d)) for d in cross_venue_ms]
    summary["cross_venue_book_ts_diff_ms"] = {
        "pairs": len(diffs),
        "p50": _pct(diffs, 0.5),
        "p90": _pct(diffs, 0.9),
        "max": max(diffs) if diffs else None,
    }
    return summary


async def _bybit_catalog(exchange: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        response = await exchange.public_get_v5_market_instruments_info(params)
        if str(response.get("retCode")) != "0":
            raise RuntimeError(f"bybit instruments-info error: {response.get('retMsg')}")
        result = response["result"]
        items.extend(result.get("list") or [])
        cursor = result.get("nextPageCursor") or ""
        if not cursor:
            return items


def _failed(spec: InstrumentSpec, round_index: int, requested: datetime, reason: str) -> BookSample:
    now = datetime.now(UTC)
    return BookSample(
        venue=spec.venue,
        base=spec.base,
        native_id=spec.native_id,
        round_index=round_index,
        requested_at=requested.isoformat(),
        received_at=now.isoformat(),
        latency_ms=max(0, round((now - requested).total_seconds() * 1000)),
        book_timestamp_ms=None,
        book_age_ms=None,
        sequence=None,
        book_ok=False,
        book_failure=reason[:200],
        fresh=None,
        min_order_ok=None,
        executable=False,
        spread_bps=None,
        ask_impact_bps=None,
        bid_impact_bps=None,
        ask_filled_usd=None,
        bid_filled_usd=None,
        order_qty_at_target=None,
    )


async def _fetch_venue(
    client: Any,
    spec: InstrumentSpec,
    *,
    round_index: int,
    target_usd: float,
    max_book_age_ms: int,
) -> BookSample:
    requested = datetime.now(UTC)
    try:
        if spec.venue == "bybit":
            response = await client.public_get_v5_market_orderbook(
                {"category": "linear", "symbol": spec.native_id, "limit": BOOK_DEPTH}
            )
            received = datetime.now(UTC)
            result = response.get("result") or {}
            if str(response.get("retCode")) != "0" or result.get("s") != spec.native_id:
                raise RuntimeError(f"bybit orderbook error: {response.get('retMsg')}")
            ts = result.get("ts")
            bids, asks = result.get("b"), result.get("a")
            sequence = f"u={result.get('u')} seq={result.get('seq')} cts={result.get('cts')}"
        else:
            response = await client.fapipublic_get_depth(
                {"symbol": spec.native_id, "limit": BOOK_DEPTH}
            )
            received = datetime.now(UTC)
            ts = response.get("T") or response.get("E")
            bids, asks = response.get("bids"), response.get("asks")
            sequence = f"lastUpdateId={response.get('lastUpdateId')}"
        return evaluate_book(
            spec,
            bids=bids,
            asks=asks,
            book_timestamp_ms=int(ts) if ts is not None else None,
            sequence=sequence,
            round_index=round_index,
            requested_at=requested,
            received_at=received,
            target_usd=target_usd,
            max_book_age_ms=max_book_age_ms,
        )
    except Exception as exc:
        return _failed(spec, round_index, requested, f"fetch_failed: {exc}")


async def run_canary(
    bases: list[str],
    *,
    rounds: int,
    interval_seconds: float,
    target_usd: float,
    max_book_age_ms: int,
    concurrency: int = 4,
) -> dict[str, Any]:
    from .exchange_registry import EXCHANGE_FACTORIES

    if rounds <= 0 or interval_seconds < 0 or target_usd <= 0 or concurrency <= 0:
        raise ValueError("rounds, target and concurrency must be positive")
    clients = {venue: EXCHANGE_FACTORIES[venue]() for venue in VENUES}
    started_at = datetime.now(UTC)
    try:
        binance_info = await clients["binance"].fapipublic_get_exchangeinfo()
        catalogs = {
            "bybit": bybit_instruments(await _bybit_catalog(clients["bybit"])),
            "binance": binance_instruments(binance_info.get("symbols") or []),
        }
        resolved, skipped = resolve_instruments(bases, catalogs)
        samples: list[BookSample] = []
        cross: list[int] = []
        gate = asyncio.Semaphore(concurrency)

        async def one_base(venues: dict[str, InstrumentSpec], index: int) -> None:
            async with gate:
                rows = await asyncio.gather(
                    *(
                        _fetch_venue(
                            clients[venue],
                            spec,
                            round_index=index,
                            target_usd=target_usd,
                            max_book_age_ms=max_book_age_ms,
                        )
                        for venue, spec in venues.items()
                    )
                )
            samples.extend(rows)
            by_venue = {row.venue: row for row in rows}
            if len(by_venue) == 2:
                a, b = by_venue["bybit"].book_timestamp_ms, by_venue["binance"].book_timestamp_ms
                if a is not None and b is not None:
                    cross.append(a - b)

        for index in range(rounds):
            round_started = time.monotonic()
            await asyncio.gather(*(one_base(venues, index) for venues in resolved.values()))
            sys.stderr.write(f"[canary] round {index + 1}/{rounds}: {len(resolved)} bases\n")
            sys.stderr.flush()
            remaining = interval_seconds - (time.monotonic() - round_started)
            if index + 1 < rounds and remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        for client in clients.values():
            await client.close()
    return {
        "canary_version": CANARY_VERSION,
        "classification": "read_only_descriptive_canary_not_a_registered_limit",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "parameters": {
            "bases_requested": len(bases),
            "rounds": rounds,
            "interval_seconds": interval_seconds,
            "target_usd": target_usd,
            "descriptive_max_book_age_ms": max_book_age_ms,
            "book_depth": BOOK_DEPTH,
        },
        "coverage": {
            "bases_resolved": len(resolved),
            "both_venues": sum(len(v) == 2 for v in resolved.values()),
            "bybit_only": sum(set(v) == {"bybit"} for v in resolved.values()),
            "binance_only": sum(set(v) == {"binance"} for v in resolved.values()),
        },
        "instruments": {
            base: {venue: asdict(spec) for venue, spec in venues.items()}
            for base, venues in resolved.items()
        },
        "skipped": skipped,
        "summary": summarize_samples(samples, cross),
        "samples": [asdict(sample) for sample in samples],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bases", required=True, help="comma-separated exact base assets")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--target-usd", type=float, default=DEFAULT_TARGET_USD)
    parser.add_argument("--max-book-age-ms", type=int, default=2000)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    bases = sorted({part.strip().upper() for part in args.bases.split(",") if part.strip()})
    report = asyncio.run(
        run_canary(
            bases,
            rounds=args.rounds,
            interval_seconds=args.interval_seconds,
            target_usd=args.target_usd,
            max_book_age_ms=args.max_book_age_ms,
        )
    )
    from .abnormal_flow_portfolio_diagnostic import _run_code_state

    report["run"] = _run_code_state()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(
        json.dumps({"coverage": report["coverage"], "summary": report["summary"]}, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
