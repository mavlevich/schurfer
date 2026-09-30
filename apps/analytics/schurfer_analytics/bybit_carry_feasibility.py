"""Read-only spot/perpetual execution feasibility on Bybit public V5 data.

This measures a simultaneous four-sided book crossing and published order
limits. It does not establish margin safety, funding income, liquidation price
or a trading edge. Catalog matching is not approved asset identity.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, cast

from .exchange_registry import EXCHANGE_FACTORIES
from .research_code_state import run_code_state

VERSION: Final = "bybit_spot_perp_feasibility_v1"
RUN_AFTER: Final = datetime(2026, 10, 31, tzinfo=UTC)
DEFAULT_OUTPUT: Final = Path("/runtime/research/bybit-spot-perp-feasibility-v1/result.json")
TARGET_USD: Final = Decimal("50")
BANK_USD: Final = Decimal("300")
TAKER_FEE_BPS: Final = Decimal("10")  # conservative scenario, not an account fee
STRESS_RISE: Final = Decimal("3")  # illustrative +300% short loss, not a margin gate
BOOK_AGE_MIN_MS: Final = -1000
BOOK_AGE_MAX_MS: Final = 2000
MAX_BOOK_SKEW_MS: Final = 1000
BOOK_DEPTH: Final = 50
MAX_PAIRS: Final = 50
ROUNDS: Final = 3
ROUND_INTERVAL_SECONDS: Final = 60
CONCURRENCY: Final = 4


@dataclass(frozen=True)
class Pair:
    base: str
    spot_id: str
    perp_id: str
    spot_step: Decimal
    spot_min_amount: Decimal
    spot_min_qty_deprecated: Decimal | None
    spot_max_market_qty: Decimal | None
    perp_step: Decimal
    perp_min_qty: Decimal
    perp_min_notional: Decimal
    perp_max_market_qty: Decimal | None


def _positive(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _catalog_rows(response: dict[str, Any], category: str) -> list[dict[str, Any]]:
    if str(response.get("retCode")) != "0":
        raise RuntimeError(f"{category} catalog retCode={response.get('retCode')}")
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("list"), list):
        raise RuntimeError(f"{category} catalog has no list")
    if not all(isinstance(item, dict) for item in result["list"]):
        raise RuntimeError(f"{category} catalog contains a malformed row")
    return cast("list[dict[str, Any]]", result["list"])


async def fetch_catalogs(client: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Spot is unpaginated; linear requires complete, non-repeating cursor traversal."""
    spot_response = await client.public_get_v5_market_instruments_info({"category": "spot"})
    spot = _catalog_rows(spot_response, "spot")
    linear: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = ""
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = await client.public_get_v5_market_instruments_info(params)
        linear.extend(_catalog_rows(page, "linear"))
        result = page["result"]
        next_cursor = result.get("nextPageCursor") or ""
        if not next_cursor:
            return spot, linear
        if next_cursor in seen:
            raise RuntimeError("linear catalog cursor repeated")
        seen.add(next_cursor)
        cursor = next_cursor


def _group(items: list[dict[str, Any]], *, category: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "Trading":
            continue
        if item.get("quoteCoin") != "USDT":
            continue
        if category == "linear" and (
            item.get("contractType") != "LinearPerpetual"
            or item.get("settleCoin") != "USDT"
            or item.get("isPreListing") is True
        ):
            continue
        base, symbol = item.get("baseCoin"), item.get("symbol")
        if isinstance(base, str) and base and isinstance(symbol, str) and symbol:
            grouped.setdefault(base, []).append(item)
    return grouped


def select_pairs(
    spot_items: list[dict[str, Any]], linear_items: list[dict[str, Any]]
) -> tuple[list[Pair], dict[str, str]]:
    """All exact native base matches; ambiguous or unsupported markets fail closed."""
    spot, linear = _group(spot_items, category="spot"), _group(linear_items, category="linear")
    pairs: list[Pair] = []
    excluded: dict[str, str] = {}
    for base in sorted(linear):
        spots, perps = spot.get(base, []), linear[base]
        if len(spots) != 1 or len(perps) != 1:
            excluded[base] = f"catalog_count:spot={len(spots)},linear={len(perps)}"
            continue
        s, p = spots[0], perps[0]
        if s["symbol"] != p["symbol"]:
            excluded[base] = "native_symbol_mismatch"
            continue
        if (
            _positive(s.get("xstockMultiplier", "1")) != Decimal(1)
            or str(s.get("stTag", "0")) != "0"
        ):
            excluded[base] = "spot_special_product"
            continue
        spot_lot, perp_lot = s.get("lotSizeFilter") or {}, p.get("lotSizeFilter") or {}
        leverage = p.get("leverageFilter") or {}
        mandatory = {
            "spot_step": _positive(spot_lot.get("basePrecision")),
            "spot_min_amount": _positive(spot_lot.get("minOrderAmt")),
            "perp_step": _positive(perp_lot.get("qtyStep")),
            "perp_min_qty": _positive(perp_lot.get("minOrderQty")),
            "perp_min_notional": _positive(perp_lot.get("minNotionalValue")),
        }
        minimum_leverage = _positive(leverage.get("minLeverage"))
        maximum_leverage = _positive(leverage.get("maxLeverage"))
        if any(value is None for value in mandatory.values()):
            excluded[base] = "order_limit_unknown"
            continue
        if minimum_leverage is None or maximum_leverage is None:
            excluded[base] = "leverage_limit_unknown"
            continue
        if minimum_leverage > 1 or maximum_leverage < 1:
            excluded[base] = "one_x_unavailable"
            continue
        pairs.append(
            Pair(
                base=base,
                spot_id=s["symbol"],
                perp_id=p["symbol"],
                spot_min_qty_deprecated=_positive(spot_lot.get("minOrderQty")),
                spot_max_market_qty=_positive(spot_lot.get("maxMarketOrderQty")),
                perp_max_market_qty=_positive(perp_lot.get("maxMktOrderQty")),
                **mandatory,  # type: ignore[arg-type]
            )
        )
    return pairs, excluded


def sample_pairs(pairs: list[Pair]) -> list[Pair]:
    """Request cap independent of prices, funding and pump outcomes."""
    return sorted(
        pairs,
        key=lambda pair: (hashlib.sha256(pair.spot_id.encode()).hexdigest(), pair.spot_id),
    )[:MAX_PAIRS]


def feasibility_gate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """The only predeclared decision supported by this public-book canary."""
    passed_rounds: dict[str, set[int]] = {}
    for row in samples:
        if row.get("status") == "book_and_limits_pass":
            passed_rounds.setdefault(str(row["base"]), set()).add(int(row["round"]))
    repeatable = sorted(base for base, rounds in passed_rounds.items() if len(rounds) >= 2)
    return {
        "repeatably_book_feasible_bases": repeatable,
        "n_repeatably_book_feasible": len(repeatable),
        "decision": (
            "funding_capture_design_permitted"
            if len(repeatable) >= 10
            else "stop_bybit_50_usd_carry_feasibility"
        ),
    }


def _book(response: dict[str, Any], symbol: str, received_ms: int) -> dict[str, Any]:
    if str(response.get("retCode")) != "0":
        raise ValueError("book_ret_code")
    raw = response.get("result")
    if not isinstance(raw, dict) or raw.get("s") != symbol:
        raise ValueError("book_symbol_mismatch")
    try:
        ts = int(raw["ts"])
        bids = [(Decimal(str(p)), Decimal(str(q))) for p, q in raw["b"]]
        asks = [(Decimal(str(p)), Decimal(str(q))) for p, q in raw["a"]]
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError("book_malformed") from exc
    if (
        not bids
        or not asks
        or any(not p.is_finite() or not q.is_finite() or p <= 0 or q <= 0 for p, q in bids + asks)
    ):
        raise ValueError("book_empty_or_nonpositive")
    if any(bids[i][0] < bids[i + 1][0] for i in range(len(bids) - 1)) or any(
        asks[i][0] > asks[i + 1][0] for i in range(len(asks) - 1)
    ):
        raise ValueError("book_unsorted")
    if bids[0][0] >= asks[0][0]:
        raise ValueError("book_crossed")
    age = received_ms - ts
    if not BOOK_AGE_MIN_MS <= age <= BOOK_AGE_MAX_MS:
        raise ValueError("book_stale")
    return {"bids": bids, "asks": asks, "ts": ts, "age_ms": age}


def _common_step(a: Decimal, b: Decimal) -> Decimal:
    scale = Decimal(10) ** max(0, -int(a.as_tuple().exponent), -int(b.as_tuple().exponent))
    return Decimal(math.lcm(int(a * scale), int(b * scale))) / scale


def _vwap_notional(levels: list[tuple[Decimal, Decimal]], qty: Decimal) -> Decimal | None:
    remaining, total = qty, Decimal(0)
    for price, depth in levels:
        fill = min(depth, remaining)
        total += fill * price
        remaining -= fill
        if remaining <= 0:
            return total
    return None


def evaluate_pair(
    pair: Pair,
    spot_response: dict[str, Any],
    perp_response: dict[str, Any],
    *,
    spot_received_ms: int,
    perp_received_ms: int,
) -> dict[str, Any]:
    """Four executable sides at one observed instant; no future funding or return."""
    row: dict[str, Any] = {"base": pair.base, "spot_id": pair.spot_id, "perp_id": pair.perp_id}
    try:
        spot = _book(spot_response, pair.spot_id, spot_received_ms)
        perp = _book(perp_response, pair.perp_id, perp_received_ms)
        if abs(spot["ts"] - perp["ts"]) > MAX_BOOK_SKEW_MS:
            raise ValueError("book_time_skew")
        step = _common_step(pair.spot_step, pair.perp_step)
        qty = (TARGET_USD / max(spot["asks"][0][0], perp["asks"][0][0]) / step).to_integral_value(
            rounding=ROUND_DOWN
        ) * step
        if qty <= 0:
            raise ValueError("quantity_rounds_to_zero")
        totals: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
        for _ in range(12):
            parts = (
                _vwap_notional(spot["asks"], qty),
                _vwap_notional(spot["bids"], qty),
                _vwap_notional(perp["bids"], qty),
                _vwap_notional(perp["asks"], qty),
            )
            if any(part is None for part in parts):
                raise ValueError("insufficient_four_side_depth")
            totals = cast("tuple[Decimal, Decimal, Decimal, Decimal]", parts)
            if max(totals) <= TARGET_USD:
                break
            smaller = (qty * TARGET_USD / max(totals) / step).to_integral_value(
                rounding=ROUND_DOWN
            ) * step
            qty = min(qty - step, smaller)
            if qty <= 0:
                raise ValueError("quantity_rounds_to_zero")
        else:
            raise ValueError("quantity_did_not_converge")
        assert totals is not None
        spot_buy, spot_sell, perp_sell, perp_buy = totals
        if (
            qty < pair.perp_min_qty
            or spot_buy < pair.spot_min_amount
            or perp_sell < pair.perp_min_notional
        ):
            raise ValueError("minimum_order_failed")
        if (pair.spot_max_market_qty is not None and qty > pair.spot_max_market_qty) or (
            pair.perp_max_market_qty is not None and qty > pair.perp_max_market_qty
        ):
            raise ValueError("maximum_market_qty_failed")
        fees = sum(totals) * TAKER_FEE_BPS / Decimal(10_000)
        friction = spot_buy - spot_sell + perp_buy - perp_sell + fees
        # A free USDT balance cannot prevent isolated-margin liquidation without
        # verified position-margin behavior. This reference is intentionally not
        # a pass/fail condition, and the $300 bank cannot bind a $50-per-side run.
        capital_reference = spot_buy + (Decimal(1) + STRESS_RISE) * perp_buy + fees
        row.update(
            status="book_and_limits_pass",
            quantity=str(qty),
            spot_buy_usd=str(spot_buy),
            spot_sell_usd=str(spot_sell),
            perp_short_usd=str(perp_sell),
            perp_cover_usd=str(perp_buy),
            four_trade_friction_usd=str(friction),
            four_trade_friction_bps=str(friction / perp_sell * 10_000),
            illustrative_stress_cash_usd=str(capital_reference),
            margin_safety="unverified",
            spot_book_age_ms=spot["age_ms"],
            perp_book_age_ms=perp["age_ms"],
            book_skew_ms=abs(spot["ts"] - perp["ts"]),
        )
    except ValueError as exc:
        row["status"] = str(exc)
    return row


async def _fetch_book(client: Any, category: str, symbol: str) -> tuple[dict[str, Any], int, int]:
    requested_ms = round(time.time() * 1000)
    response = await client.public_get_v5_market_orderbook(
        {"category": category, "symbol": symbol, "limit": BOOK_DEPTH}
    )
    return response, requested_ms, round(time.time() * 1000)


async def run_canary(
    client: Any, *, rounds: int = ROUNDS, interval_seconds: int = ROUND_INTERVAL_SECONDS
) -> dict[str, Any]:
    if rounds <= 0 or interval_seconds < 0:
        raise ValueError("invalid bounded run")
    spot_items, linear_items = await fetch_catalogs(client)
    pairs, excluded = select_pairs(spot_items, linear_items)
    selected = sample_pairs(pairs)
    started_at = datetime.now(UTC)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    samples: list[dict[str, Any]] = []
    for index in range(rounds):
        started = time.monotonic()

        async def one(pair: Pair, round_index: int) -> dict[str, Any]:
            async with semaphore:
                try:
                    (
                        (spot_book, spot_requested, spot_ms),
                        (
                            perp_book,
                            perp_requested,
                            perp_ms,
                        ),
                    ) = await asyncio.gather(
                        _fetch_book(client, "spot", pair.spot_id),
                        _fetch_book(client, "linear", pair.perp_id),
                    )
                    result = evaluate_pair(
                        pair,
                        spot_book,
                        perp_book,
                        spot_received_ms=spot_ms,
                        perp_received_ms=perp_ms,
                    )
                    result["native_books"] = {
                        "spot": {
                            "requested_ms": spot_requested,
                            "received_ms": spot_ms,
                            "response": spot_book,
                        },
                        "linear": {
                            "requested_ms": perp_requested,
                            "received_ms": perp_ms,
                            "response": perp_book,
                        },
                    }
                except Exception as exc:
                    result = {
                        "base": pair.base,
                        "status": "fetch_failed",
                        "error_type": type(exc).__name__,
                    }
                result["round"] = round_index
                return result

        samples.extend(await asyncio.gather(*(one(pair, index) for pair in selected)))
        remaining = interval_seconds - (time.monotonic() - started)
        if index + 1 < rounds and remaining > 0:
            await asyncio.sleep(remaining)
    statuses: dict[str, int] = {}
    for sample in samples:
        status = str(sample["status"])
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "version": VERSION,
        "classification": "read_only_feasibility_not_strategy_evidence",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "parameters": {
            "target_usd_per_leg": str(TARGET_USD),
            "bank_usd": str(BANK_USD),
            "taker_fee_bps_per_trade": str(TAKER_FEE_BPS),
            "illustrative_short_stress_price_rise": str(STRESS_RISE),
            "margin_safety": "unverified",
            "book_depth": BOOK_DEPTH,
            "book_age_range_ms": [BOOK_AGE_MIN_MS, BOOK_AGE_MAX_MS],
            "max_book_skew_ms": MAX_BOOK_SKEW_MS,
            "max_pairs": MAX_PAIRS,
            "rounds": rounds,
            "interval_seconds": interval_seconds,
        },
        "coverage": {
            "trading_spot_rows": len(spot_items),
            "linear_rows": len(linear_items),
            "catalog_pairs": len(pairs),
            "sampled_pairs": len(selected),
            "excluded_by_base": excluded,
            "sample_statuses": statuses,
        },
        "feasibility_gate": feasibility_gate(samples),
        "selection": [asdict(pair) for pair in selected],
        "native_catalogs": {"spot": spot_items, "linear": linear_items},
        "samples": samples,
    }


def _json_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, sort_keys=True, indent=2, default=str) + "\n").encode()


def publish_once(path: Path, report: dict[str, Any]) -> str:
    """No reader can see partial output, and a second run cannot replace it."""
    payload = _json_bytes(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink()
    return hashlib.sha256(payload).hexdigest()


def require_run_window(now: datetime) -> None:
    if now.tzinfo is None or now.astimezone(UTC) < RUN_AFTER:
        raise SystemExit(f"canary cannot run before {RUN_AFTER.isoformat()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()
    require_run_window(datetime.now(UTC))
    if args.output.exists():
        raise SystemExit("output already exists")

    async def collect() -> dict[str, Any]:
        client = EXCHANGE_FACTORIES["bybit"]()
        try:
            return await run_canary(client)
        finally:
            await client.close()

    report = asyncio.run(collect())
    report["run"] = run_code_state()
    try:
        digest = publish_once(args.output, report)
    except FileExistsError as exc:
        raise SystemExit("output already exists") from exc
    sys.stdout.write(
        json.dumps({"sha256": digest, "coverage": report["coverage"]}, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
