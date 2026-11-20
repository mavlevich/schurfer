"""A synthetic Bybit for the I25 stage 1 tests. No network: an httpx mock transport.

Every price, rate and tick size in a body is a SENTINEL, so a test can prove that no
receipt, plan or phase file of a run holds one. A launch after the cut (POSTCUT) is in
the catalog and the archive root to prove it is never requested or recorded.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx
from schurfer_analytics import new_perp_stage1 as s1
from schurfer_analytics import new_perp_stage1_verdict as v1

if TYPE_CHECKING:
    from pathlib import Path

PRICE = "98765.4321"
RATE = "0.0123456789"
TICK = "0.00012345"
POST_CUT_SYMBOL = "POSTCUTUSDT"
POST_CUT_DAY = "2025-10-05"
SENTINELS = (PRICE, RATE, TICK, POST_CUT_SYMBOL, POST_CUT_DAY)
DAY = s1.DAY_MS


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


@dataclass
class Launch:
    symbol: str
    base: str
    launch_ms: int
    first_trade_delay_ms: int = 10 * s1.MINUTE_MS
    status: str = "Trading"
    delivery_ms: int | None = None
    min_notional: str | None = "5"
    max_leverage: str | None = "50"
    funding_rows: bool = True  # False: the funding history is empty
    entry_bar: bool = True
    hourly_end_days: float | None = None  # series ends this many days after T0
    hourly_gap_hours: tuple[int, ...] = ()  # positions of hourly bars missing inside the series
    funding_mode: str = "full"  # "full", "empty", "sparse" (one row), "hole", "half4h"
    funding_interval_min: int | None = 480  # the catalog snapshot (None: field absent)
    listing_days: int = 40
    first_file_missing: bool = False
    listing_gap: tuple[int, int] | None = None  # list positions removed from the file list
    first_file_offset_days: int = 0  # the archive starts this many days after the launch day
    base_coin: str | None = "same"


@dataclass
class World:
    launches: list[Launch]
    prior: list[tuple[str, str, int | None]] = field(default_factory=list)
    archive_only: list[str] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    fail: dict[str, list[int]] = field(default_factory=dict)  # url substring -> statuses
    sizes_scale: int = 1
    retry_after: int = 7
    # (url substrings that must all match, retCode, retMsg): answered once with a 200 error envelope
    api_errors: list[tuple[tuple[str, ...], int, str]] = field(default_factory=list)
    extra_dirs: list[tuple[str, str | None]] = field(default_factory=list)  # root listing only
    crash: list[str] = field(default_factory=list)  # a url substring: die once on it
    raw: dict[str, bytes] = field(default_factory=dict)  # a url substring: answer with these bytes

    # -- builders

    @staticmethod
    def evenly(count: int, *, start: str = "2024-11-01T10:00:00", step_days: int = 2) -> World:
        first = ms(start)
        launches = [
            Launch(f"T{i:03d}USDT", f"T{i:03d}", first + i * step_days * DAY) for i in range(count)
        ]
        return World(launches)

    def by_symbol(self) -> dict[str, Launch]:
        return {launch.symbol: launch for launch in self.launches}

    # -- handler

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer as a network stream would: a body that is read once, in chunks."""
        built = self._respond(request)
        return httpx.Response(
            built.status_code, headers=built.headers, stream=httpx.ByteStream(built.content)
        )

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for entry in self.api_errors:
            needles, code, message = entry
            if all(needle in url for needle in needles):
                self.api_errors.remove(entry)
                envelope = {
                    "retCode": code,
                    "retMsg": message,
                    "result": {},
                    "retExtInfo": {},
                    "time": 1,
                }
                return httpx.Response(200, json=envelope)
        for part in self.crash:
            if part in url:
                self.crash.remove(part)
                raise RuntimeError("the process died")
        for part, body in self.raw.items():
            if part in url:
                del self.raw[part]
                return httpx.Response(200, content=body)
        for part, statuses in self.fail.items():
            if part in url and statuses:
                code = statuses.pop(0)
                if code:
                    headers = {"retry-after": str(self.retry_after)} if code == 429 else {}
                    return httpx.Response(code, headers=headers, text="x")
        path, query = request.url.path, parse_qs(request.url.query.decode())
        host = request.url.host
        if host == "api.bybit.com":
            return self.api(path, {k: v[0] for k, v in query.items()})
        if host == "public.bybit.com":
            return self.archive(path, request.method)
        return httpx.Response(404)

    # -- api

    def catalog_row(self, launch: Launch) -> dict[str, Any]:
        base_coin = launch.base if launch.base_coin == "same" else launch.base_coin
        return {
            "symbol": launch.symbol,
            "contractType": "LinearPerpetual",
            "settleCoin": "USDT",
            "baseCoin": base_coin,
            "status": launch.status,
            "launchTime": str(launch.launch_ms),
            "deliveryTime": str(launch.delivery_ms or 0),
            **(
                {"fundingInterval": launch.funding_interval_min}
                if launch.funding_interval_min
                else {}
            ),
            "isPreListing": False,
            "priceFilter": {"tickSize": TICK, "minPrice": TICK},
            "lotSizeFilter": {
                "minNotionalValue": launch.min_notional,
                "qtyStep": "1",
                "minOrderQty": "1",
            },
            "leverageFilter": {"maxLeverage": launch.max_leverage},
            "upperFundingRate": RATE,
        }

    def api(self, path: str, q: dict[str, str]) -> httpx.Response:
        if path.endswith("/instruments-info"):
            return self.instruments(q)
        symbol = q.get("symbol", "")
        launch = self.by_symbol().get(symbol)
        if launch is None:
            return httpx.Response(
                200, json={"retCode": 0, "result": {"list": [], "symbol": symbol}}
            )
        t0 = launch.launch_ms + launch.first_trade_delay_ms
        if path.endswith("/funding/history"):
            return self.funding(launch, q)
        start, end = int(q["start"]), int(q["end"])
        step = s1.MINUTE_MS if q["interval"] == "1" else s1.HOUR_MS
        if step == s1.MINUTE_MS:
            present = launch.entry_bar
            rows = [[str(start), PRICE, PRICE, PRICE, PRICE, "1", PRICE]] if present else []
            return httpx.Response(200, json=self.kline(symbol, rows, path))
        first = start - start % step
        last = end - end % step
        if launch.hourly_end_days is not None:
            last = min(
                last,
                (t0 + int(launch.hourly_end_days * DAY))
                - (t0 + int(launch.hourly_end_days * DAY)) % step,
            )
        count = max(0, (last - first) // step + 1)
        width = 5 if "mark-price" in path else 7
        rows = [
            [str(first + i * step), *([PRICE] * (width - 1))]
            for i in reversed(range(count))
            if i not in launch.hourly_gap_hours
        ][: int(q["limit"])]
        return httpx.Response(200, json=self.kline(symbol, rows, path))

    @staticmethod
    def kline(symbol: str, rows: list[list[str]], path: str) -> dict[str, Any]:
        return {"retCode": 0, "result": {"symbol": symbol, "category": "linear", "list": rows}}

    def instruments(self, q: dict[str, str]) -> httpx.Response:
        status = q["status"]
        rows = [self.catalog_row(launch) for launch in self.launches if launch.status == status]
        if status == "Trading":
            old = [
                {
                    "symbol": symbol,
                    "contractType": "LinearPerpetual",
                    "settleCoin": "USDT",
                    "baseCoin": base,
                    "status": "Closed" if delivered else "Trading",
                    "launchTime": str(ms("2024-05-01T00:00:00")),
                    "deliveryTime": str(delivered or 0),
                    "priceFilter": {"tickSize": TICK},
                }
                for symbol, base, delivered in self.prior
            ]
            post = {
                "symbol": POST_CUT_SYMBOL,
                "contractType": "LinearPerpetual",
                "settleCoin": "USDT",
                "baseCoin": "POSTCUT",
                "status": "Trading",
                "launchTime": str(ms(f"{POST_CUT_DAY}T10:00:00")),
                "priceFilter": {"tickSize": TICK},
            }
            rows = [*rows, *old, post]
        return httpx.Response(
            200, json={"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}}
        )

    def funding(self, launch: Launch, q: dict[str, str]) -> httpx.Response:
        if not launch.funding_rows or launch.funding_mode == "empty":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
        start, end, limit = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
        eight = 8 * s1.HOUR_MS
        first = start + (-start) % eight
        stamps = list(range(first, end + 1, eight))
        if launch.funding_mode == "sparse":
            stamps = stamps[len(stamps) // 2 : len(stamps) // 2 + 1]
        elif launch.funding_mode == "half4h":
            # a 4 h schedule with every second settlement missing: steps of 8 h, never above it
            stamps = list(range(first + (-first) % (4 * s1.HOUR_MS), end + 1, 8 * s1.HOUR_MS))
        elif launch.funding_mode == "hole":
            mid = stamps[len(stamps) // 2]
            stamps = [t for t in stamps if not mid <= t < mid + 3 * eight]
        rows = [
            {"symbol": launch.symbol, "fundingRate": RATE, "fundingRateTimestamp": str(t)}
            for t in reversed(stamps)
        ][:limit]
        return httpx.Response(200, json={"retCode": 0, "result": {"list": rows}})

    # -- archive

    def archive(self, path: str, method: str) -> httpx.Response:
        parts = [p for p in path.split("/") if p]
        if parts == ["trading"]:
            names = [x.symbol for x in self.launches] + [s for s, _, _ in self.prior]
            names += [*self.archive_only, POST_CUT_SYMBOL]
            lines = []
            for n in names:
                modified = POST_CUT_DAY if n == POST_CUT_SYMBOL else "2025-06-01"
                lines.append(f'<a href="{n}/">{n}/</a>   {modified} 02:00    -')
            for extra, dated in self.extra_dirs:
                lines.append(f'<a href="{extra}/">{extra}/</a>   {dated or ""}')
            return httpx.Response(200, text="<html>" + "\n".join(lines) + "</html>")
        symbol = parts[1]
        launch = self.by_symbol().get(symbol)
        if len(parts) == 2:
            return self.listing(symbol, launch)
        match = re.fullmatch(rf"{symbol}(\d{{4}}-\d{{2}}-\d{{2}})\.csv\.gz", parts[2])
        if launch is None or match is None or launch.first_file_missing:
            return httpx.Response(404)
        body = self.trade_file(launch)
        headers = {"content-length": str(len(body) * self.sizes_scale)}
        if method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, content=body, headers={"content-length": str(len(body))})

    def listing(self, symbol: str, launch: Launch | None) -> httpx.Response:
        if symbol == POST_CUT_SYMBOL:
            raise AssertionError("a post-cut symbol directory was requested")
        if symbol in self.archive_only:
            days = [f'<a href="{symbol}2024-12-0{d}.csv.gz">x</a>' for d in range(1, 4)]
            return httpx.Response(200, text="".join(days))
        if launch is None:
            return httpx.Response(404)
        day = launch.launch_ms - launch.launch_ms % DAY + launch.first_file_offset_days * DAY
        files = []
        for i in range(launch.listing_days):
            if launch.listing_gap and launch.listing_gap[0] <= i < launch.listing_gap[1]:
                continue
            stamp = datetime.fromtimestamp((day + i * DAY) / 1000, UTC).date().isoformat()
            files.append(f'<a href="{symbol}{stamp}.csv.gz">{symbol}{stamp}.csv.gz</a>')
        # a file after the cut, which must be dropped uncounted
        files.append(f'<a href="{symbol}{POST_CUT_DAY}.csv.gz">x</a>')
        return httpx.Response(200, text="<html>" + "\n".join(files) + "</html>")

    def trade_file(self, launch: Launch) -> bytes:
        t0 = launch.launch_ms + launch.first_trade_delay_ms
        rows = ["timestamp,symbol,side,size,price,tickDirection,trdMatchID"]
        for i in range(5):
            stamp = f"{(t0 + i * 1000) // 1000}.{(t0 + i * 1000) % 1000:03d}"
            rows.append(f"{stamp},{launch.symbol},Buy,1,{PRICE},PlusTick,abc{i}")
        return gzip.compress(("\n".join(rows) + "\n").encode())


def urls(world: World) -> list[str]:
    return [str(r.url) for r in world.requests]


def query(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


def dump(payload: Any) -> str:
    return json.dumps(payload)


def rehash(contract: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(contract))
    out["request_list_sha256"] = s1.contract_hash(out)
    return out


class Clock:
    def __init__(self, wall_offset: float = 0.0, die_over: float | None = None) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.wall_offset = wall_offset
        self.die_over = die_over

    def now(self) -> float:
        self.t += 0.01
        return self.t

    def wall(self) -> float:
        return 1_800_000_000.0 + self.wall_offset + self.t

    def sleep(self, seconds: float) -> None:
        if self.die_over is not None and seconds > self.die_over:
            raise RuntimeError("the process died before the wait")
        self.sleeps.append(seconds)
        self.t += seconds


def fetcher(
    world: World,
    root: Path,
    contract: dict[str, Any] | None = None,
    *,
    go: int = 1,
    code: dict[str, Any] | None = None,
    clock: Clock | None = None,
    prior: dict[str, dict[str, int]] | None = None,
) -> tuple[s1.Fetcher, Clock]:
    clock = clock or Clock()
    client = httpx.Client(transport=world.transport())
    f = s1.Fetcher(
        root,
        contract or s1.build_contract(),
        client,
        go=go,
        code=code,
        prior=prior,
        now=clock.now,
        sleep=clock.sleep,
        wall=clock.wall,
    )
    return f, clock


def run_go(
    world: World, root: Path, go: int, contract: dict[str, Any] | None = None
) -> dict[str, Any]:
    f, _ = fetcher(world, root, contract, go=go)
    if go == 1:
        return s1.run_go1(f)
    return s1.run_go2(f, s1.manifest_sha256(root))


def run_world(
    world: World, root: Path, contract: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both gos against the synthetic world, then the verdict from the receipts on disk."""
    run_go(world, root, 1, contract)
    summary = run_go(world, root, 2, contract)
    summary["go1"] = json.loads((root / "manifest.json").read_text())
    return summary, v1.evaluate(v1.load_run(root))


def streamed(
    status: int = 200, *, content: bytes = b"", headers: dict[str, str] | None = None
) -> httpx.Response:
    """A response whose body is read once from a stream, as a network response is."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))
