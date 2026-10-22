"""Planning screen of spot/perpetual funding carry on Bybit public history.

Discovery for the carry line (docs/research/bybit-carry-economics-screen-v1.md):
whether stage B is worth designing and what must be true for it. Not a cohort,
verdict, order gate or expected return. ``fetch`` stores every native response
once; ``report`` reads only that verified snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import gzip
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .bybit_carry_feasibility import fetch_catalogs, publish_once, select_pairs
from .exchange_registry import EXCHANGE_FACTORIES
from .reporting import markdown_table, normalize_code_revision, profit_factor

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

VERSION: Final = "bybit_carry_economics_screen_v1"
HOUR_MS: Final = 3_600_000
BAR_MS: Final = 4 * HOUR_MS
DAY_MS: Final = 24 * HOUR_MS
DECISIONS_FROM_MS: Final = int(datetime(2024, 10, 1, tzinfo=UTC).timestamp() * 1000)
FETCH_FROM_MS: Final = DECISIONS_FROM_MS - 2 * DAY_MS
DATA_UNTIL_MS: Final = int(datetime(2026, 9, 29, tzinfo=UTC).timestamp() * 1000) - 1
LAST_FILL_MS: Final = DATA_UNTIL_MS + 1
MAX_SETTLEMENT_GAP_MS: Final = 8 * HOUR_MS
THRESHOLDS_BPS_PER_DAY: Final = (5, 10, 20, 40)
FEE_BPS_PER_FILL: Final = 10.0  # the canary's scenario fee, not an account rate
NOTIONAL_USD: Final = 50.0
ALLOCATED_CAPITAL_USD: Final = 210.0  # spot leg plus +300% isolated collateral, design v1
BANK_USD: Final = 300.0
FUNDING_PAGE_LIMIT: Final = 200
KLINE_PAGE_LIMIT: Final = 1000
MAX_PAGES: Final = 400
CONCURRENCY: Final = 4
MIN_BREAK_EVEN_CROSSING_BPS: Final = 20.0
MIN_POSITIVE_MONTH_SHARE: Final = 0.6
MAX_TOP5_SHARE: Final = 0.5


def require_boundary(until_ms: int) -> None:
    if until_ms > DATA_UNTIL_MS:
        raise SystemExit(
            f"refusing a request ending after {DATA_UNTIL_MS} ms (2026-09-28T23:59:59.999Z)"
        )


def _ok_list(response: Any, what: str) -> list[Any]:
    if not isinstance(response, dict) or str(response.get("retCode")) != "0":
        message = response.get("retMsg") if isinstance(response, dict) else None
        raise RuntimeError(f"{what}: bybit error {message!r}")
    result = response.get("result")
    items = result.get("list") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise RuntimeError(f"{what}: no result.list")
    return items


async def _pages(
    call: Any,
    params: dict[str, Any],
    *,
    what: str,
    limit: int,
    oldest: Any,
    record: Any,
) -> None:
    """Page backwards from ``params['end']``/``['endTime']`` until a short page."""
    end_key = "endTime" if "endTime" in params else "end"
    start_key = "startTime" if "startTime" in params else "start"
    require_boundary(int(params[end_key]))
    for _ in range(MAX_PAGES):
        response = await call(dict(params))
        items = _ok_list(response, what)
        record(dict(params), response)
        if len(items) < limit:
            return
        params[end_key] = min(oldest(item) for item in items) - 1
        if params[end_key] < params[start_key]:
            return
    raise RuntimeError(f"{what}: more than {MAX_PAGES} pages")


async def fetch_snapshot(client: Any, path: Path) -> dict[str, Any]:
    """Write every native response once; return the coverage summary."""
    spot, linear = await fetch_catalogs(client)
    pairs, excluded = select_pairs(spot, linear)
    errors: dict[str, str] = {}
    written = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = gzip.open(path, "xb", compresslevel=6)  # noqa: SIM115 closed below

    def record(kind: str, symbol: str | None, params: Any, response: Any) -> None:
        nonlocal written
        line = {"kind": kind, "symbol": symbol, "params": params, "response": response}
        stream.write(json.dumps(line, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        written += 1

    record("catalog", None, {"category": "spot"}, {"list": spot})
    record("catalog", None, {"category": "linear"}, {"list": linear})
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(symbol: str) -> None:
        async with semaphore:
            try:
                await _pages(
                    client.public_get_v5_market_funding_history,
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "startTime": FETCH_FROM_MS,
                        "endTime": DATA_UNTIL_MS,
                        "limit": FUNDING_PAGE_LIMIT,
                    },
                    what=f"funding {symbol}",
                    limit=FUNDING_PAGE_LIMIT,
                    oldest=lambda item: int(item["fundingRateTimestamp"]),
                    record=lambda p, r: record("funding", symbol, p, r),
                )
                for category in ("spot", "linear"):
                    await _pages(
                        client.public_get_v5_market_kline,
                        {
                            "category": category,
                            "symbol": symbol,
                            "interval": "240",
                            "start": FETCH_FROM_MS,
                            "end": DATA_UNTIL_MS,
                            "limit": KLINE_PAGE_LIMIT,
                        },
                        what=f"kline {category} {symbol}",
                        limit=KLINE_PAGE_LIMIT,
                        oldest=lambda item: int(item[0]),
                        record=lambda p, r, c=category: record(f"kline_{c}", symbol, p, r),
                    )
            except Exception as exc:  # one pair's failure is an exclusion, not a stop
                errors[symbol] = str(exc)[:300]
                record("fetch_error", symbol, {}, {"error": errors[symbol]})

    try:
        await asyncio.gather(*(one(pair.spot_id) for pair in pairs))
    finally:
        stream.close()
    return {
        "catalog_pairs": len(pairs),
        "catalog_excluded": dict(Counter(excluded.values())),
        "fetch_errors": errors,
        "responses": written,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class Series:
    funding: list[tuple[int, float]]
    spot: dict[int, float]
    perp: dict[int, float]

    @property
    def funding_ts(self) -> list[int]:
        return [ts for ts, _ in self.funding]


def load_snapshot(path: Path) -> tuple[dict[str, Series], list[str]]:
    """Per symbol: settlements (ts ms, rate) and 4 h closes keyed by bar start.

    The second value lists symbols whose fetch failed or returned another symbol's
    rows; the report drops them whole."""
    data: dict[str, Series] = {}
    failed: set[str] = set()
    with gzip.open(path, "rb") as stream:
        for raw in stream:
            line = json.loads(raw)
            symbol = line["symbol"]
            if symbol is None:
                continue
            if line["kind"] == "fetch_error":
                failed.add(symbol)
                continue
            series = data.setdefault(symbol, Series([], {}, {}))
            items = line["response"]["result"]["list"]
            if line["kind"] == "funding":
                for item in items:
                    if item.get("symbol") != symbol:
                        failed.add(symbol)
                        continue
                    series.funding.append(
                        (int(item["fundingRateTimestamp"]), float(item["fundingRate"]))
                    )
            else:
                target = series.spot if line["kind"] == "kline_spot" else series.perp
                for item in items:
                    target[int(item[0])] = float(item[4])
    for series in data.values():
        series.funding = sorted(set(series.funding))
    return data, sorted(failed)


def f24_path(series: Series) -> list[float | None]:
    """F24 at every decision of the grid, computed once per pair."""
    signal = Signal(series.funding)
    return [signal.f24(t) for t in decision_grid()]


class Signal:
    """F24(t): settled funding in (t - 24 h, t] in bps/day, or None when unavailable."""

    def __init__(self, funding: list[tuple[int, float]]) -> None:
        self.ts = [ts for ts, _ in funding]
        self.prefix = [0.0]
        for _, rate in funding:
            self.prefix.append(self.prefix[-1] + rate)
        self.gap_prefix = [0]
        for index in range(1, len(self.ts)):
            gap = self.ts[index] - self.ts[index - 1] > MAX_SETTLEMENT_GAP_MS
            self.gap_prefix.append(self.gap_prefix[-1] + int(gap))

    def f24(self, t: int) -> float | None:
        hi = bisect.bisect_right(self.ts, t)  # settlements <= t
        lo = bisect.bisect_right(self.ts, t - DAY_MS)  # settlements <= t - 24h
        if lo == 0 or hi == 0 or t - self.ts[hi - 1] > MAX_SETTLEMENT_GAP_MS:
            return None
        if self.gap_prefix[hi - 1] - self.gap_prefix[lo - 1] > 0:
            return None
        return (self.prefix[hi] - self.prefix[lo]) * 10_000


def price_at(closes: dict[int, float], t: int) -> float | None:
    return closes.get(t - BAR_MS)


@dataclass(frozen=True)
class Trade:
    symbol: str
    threshold: int
    entry_fill_ms: int
    exit_fill_ms: int
    funding_bps: float
    basis_bps: float
    fee_bps: float
    closed_at_end: bool

    @property
    def net_bps(self) -> float:
        return self.funding_bps + self.basis_bps - self.fee_bps

    @property
    def hold_days(self) -> float:
        return (self.exit_fill_ms - self.entry_fill_ms) / DAY_MS


def money(
    series: Series,
    symbol: str,
    threshold: int,
    entry: int,
    exit_: int,
    *,
    at_end: bool,
    funding_ts: list[int] | None = None,
) -> Trade | str:
    """One pair held from fill ``entry`` to fill ``exit_``, or the reason it is not formed."""
    s0, p0 = price_at(series.spot, entry), price_at(series.perp, entry)
    s1, p1 = price_at(series.spot, exit_), price_at(series.perp, exit_)
    if None in (s0, p0, s1, p1):
        return "fill_price_missing"
    assert s0 is not None and p0 is not None and s1 is not None and p1 is not None
    quantity = NOTIONAL_USD / max(s0, p0)
    funding_usd = 0.0
    ts_list = funding_ts if funding_ts is not None else series.funding_ts
    lo = bisect.bisect_right(ts_list, entry)
    hi = bisect.bisect_left(ts_list, exit_)
    for ts, rate in series.funding[lo:hi]:
        if entry < ts < exit_:
            boundary = -(-ts // BAR_MS) * BAR_MS
            mark = price_at(series.perp, boundary)
            if mark is None:
                return "funding_price_missing"
            funding_usd += quantity * mark * rate
    basis_usd = quantity * ((s1 - s0) - (p1 - p0))
    fee_usd = FEE_BPS_PER_FILL / 10_000 * quantity * (s0 + p0 + s1 + p1)
    scale = 10_000 / NOTIONAL_USD
    return Trade(
        symbol,
        threshold,
        entry,
        exit_,
        funding_usd * scale,
        basis_usd * scale,
        fee_usd * scale,
        at_end,
    )


def decision_grid() -> range:
    return range(DECISIONS_FROM_MS, LAST_FILL_MS - BAR_MS + 1, BAR_MS)


def last_common_fill(series: Series) -> int | None:
    both = sorted(set(series.spot) & set(series.perp))
    return both[-1] + BAR_MS if both else None


def simulate_pair(
    series: Series,
    symbol: str,
    threshold: int,
    path: list[float | None],
    missing: Counter[str],
) -> list[Trade]:
    trades: list[Trade] = []
    entry: int | None = None
    funding_ts = series.funding_ts
    for t, value in zip(decision_grid(), path, strict=True):
        if entry is None:
            if value is not None and value >= threshold:
                fill = t + BAR_MS
                if price_at(series.spot, fill) is None or price_at(series.perp, fill) is None:
                    missing["entry_price_missing"] += 1
                    continue
                entry = fill
        elif value is None or value < threshold / 2:
            result = money(
                series, symbol, threshold, entry, t + BAR_MS, at_end=False, funding_ts=funding_ts
            )
            _keep(result, trades, missing)
            entry = None
    if entry is not None:
        _close_at_end(series, symbol, threshold, entry, trades, missing)
    return trades


def _close_at_end(
    series: Series,
    symbol: str,
    threshold: int,
    entry: int,
    trades: list[Trade],
    missing: Counter[str],
) -> None:
    end = last_common_fill(series)
    if end is not None and end > entry:
        _keep(money(series, symbol, threshold, entry, end, at_end=True), trades, missing)
    else:
        missing["no_exit_bar"] += 1


def _keep(result: Trade | str, trades: list[Trade], missing: Counter[str]) -> None:
    if isinstance(result, str):
        missing[result] += 1
    else:
        trades.append(result)


def simulate_one_slot(
    data: dict[str, Series],
    paths: dict[str, list[float | None]],
    threshold: int,
    missing: Counter[str],
) -> list[Trade]:
    """One position at a time: the eligible pair with the highest F24 (ties by symbol)."""
    trades: list[Trade] = []
    held: tuple[str, int] | None = None
    symbols = sorted(data)
    for index, t in enumerate(decision_grid()):
        if held is not None:
            symbol, entry = held
            value = paths[symbol][index]
            if value is None or value < threshold / 2:
                result = money(data[symbol], symbol, threshold, entry, t + BAR_MS, at_end=False)
                _keep(result, trades, missing)
                held = None
            continue
        best: tuple[float, str] | None = None
        for symbol in symbols:
            value = paths[symbol][index]
            if value is None or value < threshold or (best is not None and value <= best[0]):
                continue
            series = data[symbol]
            fill = t + BAR_MS
            if price_at(series.spot, fill) is None or price_at(series.perp, fill) is None:
                continue
            best = (value, symbol)
        if best is not None:
            held = (best[1], t + BAR_MS)
    if held is not None:
        _close_at_end(data[held[0]], held[0], threshold, held[1], trades, missing)
    return trades


def _month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m")


def _week(ms: int) -> str:
    year, week, _ = datetime.fromtimestamp(ms / 1000, tz=UTC).isocalendar()
    return f"{year}-W{week:02d}"


def months_in_window() -> list[str]:
    months: list[str] = []
    for t in decision_grid():
        month = _month(t)
        if not months or months[-1] != month:
            months.append(month)
    return months


def max_concurrent(trades: Iterable[Trade]) -> int:
    events = sorted(
        [(trade.entry_fill_ms, 1) for trade in trades]
        + [(trade.exit_fill_ms, -1) for trade in trades]
    )
    current = peak = 0
    for _, step in events:  # exits sort before entries at the same time
        current += step
        peak = max(peak, current)
    return peak


def top5_share(trades: Sequence[Trade]) -> float | None:
    by_pair: dict[str, float] = defaultdict(float)
    for trade in trades:
        by_pair[trade.symbol] += trade.net_bps
    total = sum(by_pair.values())
    if total <= 0:
        return None
    return sum(sorted(by_pair.values(), reverse=True)[:5]) / total


def summarize(trades: Sequence[Trade]) -> dict[str, Any]:
    nets = [trade.net_bps for trade in trades]
    if not trades:
        return {"trades": 0}
    return {
        "trades": len(trades),
        "distinct_pairs": len({trade.symbol for trade in trades}),
        "distinct_weeks": len({_week(trade.entry_fill_ms) for trade in trades}),
        "closed_at_end": sum(trade.closed_at_end for trade in trades),
        "mean_net_bps": statistics.fmean(nets),
        "median_net_bps": statistics.median(nets),
        "profit_factor": profit_factor(nets),
        "mean_funding_bps": statistics.fmean(trade.funding_bps for trade in trades),
        "mean_basis_bps": statistics.fmean(trade.basis_bps for trade in trades),
        "mean_fee_bps": statistics.fmean(trade.fee_bps for trade in trades),
        "mean_hold_days": statistics.fmean(trade.hold_days for trade in trades),
        "break_even_crossing_bps": statistics.fmean(nets),
        "top5_pair_share_of_net": top5_share(trades),
        "max_concurrent": max_concurrent(trades),
    }


def portfolio(trades: Sequence[Trade], months: list[str]) -> dict[str, Any]:
    by_month = dict.fromkeys(months, 0.0)
    for trade in trades:
        month = _month(trade.exit_fill_ms)
        by_month[month] = by_month.get(month, 0.0) + trade.net_bps * NOTIONAL_USD / 10_000
    total = sum(by_month.values())
    years = len(months) / 12
    summary = summarize(trades)
    return summary | {
        "net_usd": total,
        "net_usd_at_20bps_crossing": total - len(trades) * 20 / 10_000 * NOTIONAL_USD,
        "usd_per_month": total / len(months),
        "positive_month_share": sum(value > 0 for value in by_month.values()) / len(months),
        "annual_return_on_allocated": total / years / ALLOCATED_CAPITAL_USD,
        "annual_return_on_bank": total / years / BANK_USD,
        "by_month_usd": by_month,
    }


def landscape(data: dict[str, Series], paths: dict[str, list[float | None]]) -> dict[str, Any]:
    rates = [
        rate
        for series in data.values()
        for ts, rate in series.funding
        if DECISIONS_FROM_MS <= ts <= DATA_UNTIL_MS
    ]
    values: list[float] = []
    by_month: dict[str, list[float]] = defaultdict(list)
    months = [_month(t) for t in decision_grid()]
    for symbol in sorted(data):
        for month, value in zip(months, paths[symbol], strict=True):
            if value is not None:
                values.append(value)
                by_month[month].append(value)
    if not values:
        return {"settlements": len(rates), "f24_points": 0}
    quantiles = statistics.quantiles(values, n=100)
    return {
        "settlements": len(rates),
        "positive_settlement_share": sum(rate > 0 for rate in rates) / len(rates)
        if rates
        else None,
        "f24_points": len(values),
        "f24_bps_per_day": {
            "p10": quantiles[9],
            "p50": quantiles[49],
            "p90": quantiles[89],
            "p99": quantiles[98],
        },
        "share_f24_at_least": {
            str(h): sum(value >= h for value in values) / len(values)
            for h in THRESHOLDS_BPS_PER_DAY
        },
        "by_month": {
            month: {
                "points": len(items),
                "median_f24_bps_per_day": statistics.median(items),
                **{
                    f"share_at_least_{h}": sum(v >= h for v in items) / len(items)
                    for h in THRESHOLDS_BPS_PER_DAY
                },
            }
            for month, items in sorted(by_month.items())
        },
    }


def reading(by_threshold: dict[str, dict[str, Any]]) -> dict[str, Any]:
    passing = []
    for key, view in by_threshold.items():
        slot, every = view["one_slot"], view["all_pairs"]
        if (
            slot.get("trades", 0) > 0
            and slot["net_usd"] > 0
            and slot["break_even_crossing_bps"] >= MIN_BREAK_EVEN_CROSSING_BPS
            and slot["positive_month_share"] >= MIN_POSITIVE_MONTH_SHARE
            and every.get("top5_pair_share_of_net") is not None
            and every["top5_pair_share_of_net"] < MAX_TOP5_SHARE
        ):
            passing.append(key)
    return {
        "passing_thresholds": passing,
        "decision": "stage_b_worth_designing" if passing else "carry_not_worth_stage_b_at_usd_50",
    }


def build_report(
    snapshot: Path, expected_sha256: str, *, code_revision: str, working_tree_dirty: bool
) -> dict[str, Any]:
    digest = file_sha256(snapshot)
    if digest != expected_sha256:
        raise SystemExit(f"snapshot sha256 {digest} does not match {expected_sha256}")
    data, mismatched = load_snapshot(snapshot)
    for symbol in mismatched:
        data.pop(symbol, None)
    paths = {symbol: f24_path(series) for symbol, series in data.items()}
    months = months_in_window()
    by_threshold: dict[str, dict[str, Any]] = {}
    missing: dict[str, dict[str, int]] = {}
    for threshold in THRESHOLDS_BPS_PER_DAY:
        pair_missing: Counter[str] = Counter()
        slot_missing: Counter[str] = Counter()
        every = [
            trade
            for symbol in sorted(data)
            for trade in simulate_pair(data[symbol], symbol, threshold, paths[symbol], pair_missing)
        ]
        slot = simulate_one_slot(data, paths, threshold, slot_missing)
        by_threshold[str(threshold)] = {
            "all_pairs": summarize(every),
            "one_slot": portfolio(slot, months),
        }
        missing[str(threshold)] = {
            **{f"all_pairs_{k}": v for k, v in pair_missing.items()},
            **{f"one_slot_{k}": v for k, v in slot_missing.items()},
        }
    first = {
        symbol: series.funding[0][0] if series.funding else None for symbol, series in data.items()
    }
    return {
        "version": VERSION,
        "run": {
            "code_revision": normalize_code_revision(code_revision),
            "working_tree_dirty": working_tree_dirty,
        },
        "snapshot": {"file": snapshot.name, "sha256": digest},
        "boundaries": {
            "decisions_from_ms": DECISIONS_FROM_MS,
            "data_until_ms": DATA_UNTIL_MS,
            "months": len(months),
        },
        "fixed": {
            "thresholds_bps_per_day": list(THRESHOLDS_BPS_PER_DAY),
            "fee_bps_per_fill": FEE_BPS_PER_FILL,
            "notional_usd": NOTIONAL_USD,
            "allocated_capital_usd": ALLOCATED_CAPITAL_USD,
            "bank_usd": BANK_USD,
        },
        "coverage": {
            "pairs": len(data),
            "fetch_failed_or_mismatched_dropped": mismatched,
            "pairs_listed_after_window_start": sum(
                1 for ts in first.values() if ts is None or ts > DECISIONS_FROM_MS
            ),
            "missing": missing,
        },
        "landscape": landscape(data, paths),
        "by_threshold": by_threshold,
        "reading": reading(by_threshold),
    }


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['version']}",
        "",
        f"Code `{report['run']['code_revision']}`, snapshot `{report['snapshot']['sha256']}`, "
        f"{report['coverage']['pairs']} pairs, {report['boundaries']['months']} months.",
        "",
        "## All pairs, unlimited slots (bps of USD 50 per trade, zero crossing cost)",
        "",
    ]
    rows: list[tuple[Any, ...]] = []
    for key, view in report["by_threshold"].items():
        s = view["all_pairs"]
        rows.append(
            (
                key,
                s.get("trades", 0),
                s.get("distinct_pairs"),
                s.get("distinct_weeks"),
                _fmt(s.get("mean_net_bps")),
                _fmt(s.get("median_net_bps")),
                _fmt(s.get("mean_funding_bps")),
                _fmt(s.get("mean_basis_bps")),
                _fmt(s.get("mean_fee_bps")),
                _fmt(s.get("mean_hold_days")),
                _fmt(s.get("top5_pair_share_of_net"), 2),
                s.get("max_concurrent"),
            )
        )
    lines += markdown_table(
        (
            "H bps/day",
            "trades",
            "pairs",
            "weeks",
            "mean net",
            "median net",
            "funding",
            "basis",
            "fees",
            "hold days",
            "top-5 share",
            "max open",
        ),
        rows,
    )
    lines += ["", "## One slot, the owner's bank (USD)", ""]
    rows = []
    for key, view in report["by_threshold"].items():
        s = view["one_slot"]
        rows.append(
            (
                key,
                s.get("trades", 0),
                _fmt(s.get("net_usd"), 2),
                _fmt(s.get("net_usd_at_20bps_crossing"), 2),
                _fmt(s.get("usd_per_month"), 2),
                _fmt(s.get("positive_month_share"), 2),
                _fmt(s.get("break_even_crossing_bps")),
                _fmt(
                    100 * s["annual_return_on_allocated"]
                    if "annual_return_on_allocated" in s
                    else None
                ),
                _fmt(100 * s["annual_return_on_bank"] if "annual_return_on_bank" in s else None),
            )
        )
    lines += markdown_table(
        (
            "H bps/day",
            "trades",
            "net USD",
            "net USD at 20 bps crossing",
            "USD/month",
            "months positive",
            "break-even crossing bps",
            "% a year on 210",
            "% a year on 300",
        ),
        rows,
    )
    lines += [
        "",
        f"Reading: `{report['reading']['decision']}` "
        f"(passing H: {report['reading']['passing_thresholds'] or 'none'}).",
        "",
    ]
    return "\n".join(lines)


def _json_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, sort_keys=True, indent=2, default=str) + "\n").encode()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="store the native public history once")
    fetch.add_argument("--snapshot", type=Path, required=True)
    report = sub.add_parser("report", help="compute the screen from a verified snapshot")
    report.add_argument("--snapshot", type=Path, required=True)
    report.add_argument("--snapshot-sha256", required=True)
    report.add_argument("--output-dir", type=Path, required=True)
    report.add_argument("--code-revision", required=True)
    dirty = report.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "fetch":
        if args.snapshot.exists():
            raise SystemExit(f"{args.snapshot} exists; a snapshot is written once")

        async def collect() -> dict[str, Any]:
            client = EXCHANGE_FACTORIES["bybit"]()
            try:
                return await fetch_snapshot(client, args.snapshot)
            finally:
                await client.close()

        coverage = asyncio.run(collect())
        sys.stdout.write(json.dumps(coverage | {"sha256": file_sha256(args.snapshot)}) + "\n")
        return
    report = build_report(
        args.snapshot,
        args.snapshot_sha256,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    digest = publish_once(args.output_dir / "result.json", report)
    (args.output_dir / "tables.md").write_text(render_markdown(report), encoding="utf-8")
    sys.stdout.write(render_markdown(report) + f"\nresult sha256 {digest}\n")
