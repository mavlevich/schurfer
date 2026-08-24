"""Immutable, file-based cache for exact-venue CCXT candle fetches.

Root cause fixed here (found 2026-08-24 diffing an archived 2026-08-08 run of
virtual-entry-challenger-report against a same-day re-run): the locked
100-episode formal sample lost resolution from 99/100 to 94/100 between the
two runs even though the frozen episode ID set was byte-identical. All five
newly-broken episodes flipped `complete -> fetch_failed` with the exact same
error shape: `"<exchange> does not have market symbol <BASE>/USDT:USDT"` --
the token was delisted from the exchange's live market catalog between the
two runs. `fetch_symbol_candles` (ohlcv.py) re-fetches every candle from the
live exchange API on every report invocation, with no persistence at all; a
"formal", frozen-cohort report's own reproducibility is therefore at the
mercy of every venue's current listing status, not just its own frozen
inputs, in direct contradiction to the "same manifest -> same result"
invariant every other formal report in this codebase relies on.

**This module cannot heal the 5 already-broken episodes.** They were never
cached before this fix existed, and the underlying candle history is gone
from the live exchange catalog now -- there is no remaining source to
re-derive it from. This module only stops the SAME class of loss from
happening to any episode from this point forward: once a fetch succeeds
through this cache, it can never again regress to fetch_failed on a later
run, no matter what happens to the venue's live listing afterward. The
94/100 entry-challenger baseline is the honest starting point going
forward, not something this PR restores to 99/100.

**"Forever" here means "for as long as the host's `/runtime` volume
survives"**, not literally forever: this cache has no backup, replication,
or rotation policy of its own -- it is a plain directory on the host bind-
mounted into the container. Losing that host directory (disk failure, a
manual `rm`, a host rebuild without restoring `/runtime`) loses the cache
exactly as completely as it would have been lost before this fix existed.
Backup/retention for `/runtime/market-path-cache` is deliberately left as a
separate, later operational concern, not solved here.

**Not automatic** -- this cache is opt-in per call (`fetch_symbol_candles`'s
own `use_cache` parameter), not a silent behavior change to the shared
low-level OHLCV client. A coverage/latency-diagnostic report that passes
`on_page` specifically wants to observe real API calls on every run; if
caching were unconditional, a re-run would silently report "0 API calls"
and empty diagnostics instead of the fetch behavior it exists to measure.
Only the formal replay-report path (virtual_market.py's market-path
fetchers, which back entry-challenger/exit-discovery/wider-stop-shadow/
maker-entry) opts in.

**Correctness invariants a formal, reproducibility-focused cache actually
needs, not just a speed optimization:**

* First-writer-wins, not last-writer-wins. Two concurrent report runs
  racing to fetch and cache the same window must not let the second one
  silently overwrite the first's already-persisted, already-referenced
  result. Enforced with `os.link`: creating a hard link at the final path
  is atomic and fails with `FileExistsError` if the path is already taken,
  which is exactly the "create exactly once" semantics needed here (unlike
  `os.replace`, which always succeeds and always overwrites).
* Only a PROVEN-complete fetch is cached -- see `fetch_symbol_candles`'s own
  docstring on why an empty-page-after-retries or a stalled cursor is
  fundamentally ambiguous between "real retention limit / delisted
  instrument" and "one transient API hiccup" from inside that function.
  Caching the ambiguous case would let a single bad response freeze a wrong
  `no_data` or truncated path in place forever. Only the loop's genuine
  `cursor >= end_ms` completion is trusted enough to persist.
* A stored SHA-256 of the candle payload is verified on every read, on top
  of the existing "the file's own recorded key matches the recomputed key"
  check -- defense in depth against on-disk corruption or a hand-edited
  file, independent of whichever check would catch it first.
* A cache entry that exists at the expected path but fails any integrity
  check is a hard error (`MarketPathCacheCorruptError`), not a silent
  cache-miss-and-refetch. Silently falling back to a live re-fetch on a
  formal report's own cache would reintroduce exactly the "same manifest,
  different result depending on venue state" hazard this module exists to
  close -- corruption must surface, not be quietly masked as normal. Every
  formal call site in virtual_market.py re-raises this specifically,
  ahead of its own broad `except Exception -> fetch_failed`, so a corrupt
  cache degrades the report loudly (the whole run fails) rather than
  quietly (one more episode silently drops out of the cohort -- the exact
  99->94 failure mode this module exists to close, just with a different
  cause).
* A write's outcome (`CacheWriteOutcome`) is not discarded by the caller.
  `ALREADY_EXISTS` (lost a race against a concurrent writer) makes the
  caller read back and use the winner's candles instead of its own, so two
  reports racing on the same window can never end up with two different
  "reproducible" answers. `WRITE_FAILED` (disk/permission/I/O error) makes
  a `use_cache=True` caller fail the current fetch outright rather than
  silently completing without persisting -- a formal report that believes
  it is now protected against future delisting, but silently isn't, is
  worse than one that fails loudly today.
* Only a window with no leading gap, no internal gap, and an exact expected
  bar count is cached -- reaching `cursor >= end_ms` alone does not prove
  this: an exchange can return a window's first and last bar while quietly
  skipping one in the middle, or start its first page later than
  `start_ms`, and the simple cursor check cannot see either. See
  `fetch_symbol_candles`'s own gap-validation helper.

Deliberately NOT a database table: `fetch_symbol_candles` and
`fetch_candles` currently take no `db_url`/connection parameter at all, and
both are called directly from 5 report modules and indirectly (via
virtual_market.py) from roughly 15 more. Threading a mandatory DB dependency
through every one of those call sites to cache what is, per call, at most a
few hundred rows would be a large, invasive change for what this fix
actually needs. A local file cache under `runtime/` (the same convention
`liquidation_cascade_cohort_split.py`'s own cohort-state file uses) gets the
identical reproducibility guarantee with a change scoped to one function in
one file. In production/dev compose, `SCHURFER_MARKET_PATH_CACHE_DIR` must
be set to a path under the mounted `/runtime` volume (see the analytics
service's own environment block) -- the module's own relative default
(`runtime/market-path-cache`) resolves against the container's `/app`
WORKDIR, not the mount, and would silently vanish every time a disposable
`docker compose run --rm` report container exits otherwise.

Cache identity uses `exchange.id` (ccxt's own canonical exchange-class
name, e.g. "binance", "bybit" -- present on every ccxt exchange instance,
see https://docs.ccxt.com), not a name threaded in by the caller: every
existing call site already constructs `exchange` from `EXCHANGE_FACTORIES`
without also carrying its own name string around, so requiring one would be
its own small invasive change across the same 15+ call sites this fix is
trying to avoid touching.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ohlcv import Candle

# Bump this if fetch_symbol_candles's own processing (normalize_candles,
# closed_candles, the since-lookback/dedup behavior) ever changes in a way
# that could make an old cached result stop matching what a fresh fetch
# would now produce for the same inputs. A version bump makes every
# previously-cached entry a silent cache miss (re-fetched, then re-cached
# under the new version) rather than a wrong cache hit.
CACHE_CONTRACT_VERSION = "v1"

_CACHE_DIR_ENV_VAR = "SCHURFER_MARKET_PATH_CACHE_DIR"
DEFAULT_CACHE_DIR = "runtime/market-path-cache"


class MarketPathCacheCorruptError(Exception):
    """A cache entry exists at its expected path but failed an integrity
    check (bad JSON, key mismatch, or content-hash mismatch). Deliberately
    NOT treated as a cache miss: silently falling back to a live re-fetch
    here would mean two runs of the same formal, frozen-cohort report could
    silently disagree depending on whether the on-disk cache happened to be
    intact, which is exactly the reproducibility hazard this module exists
    to close. The caller (or the operator) must resolve this explicitly --
    typically by deleting the one corrupt file and re-running, which
    re-fetches and re-caches cleanly."""


class MarketPathCacheWriteError(Exception):
    """Raised by `fetch_symbol_candles` (not by this module) when
    `use_cache=True` and `write_cached_candles` reports `WRITE_FAILED`. A
    formal report's whole point of opting into caching is to become
    protected against a future delisting; completing the current run
    without actually persisting anything would leave it believing it is
    protected when it is not. Failing loudly now, while the operator is
    looking at this run, is better than a silent no-op that only becomes
    visible the next time the exact same venue-side loss this cache exists
    to prevent happens again."""


def cache_dir(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get(_CACHE_DIR_ENV_VAR) or DEFAULT_CACHE_DIR)


def _cache_key(
    *,
    exchange_id: str,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
) -> str:
    payload = json.dumps(
        {
            "contract_version": CACHE_CONTRACT_VERSION,
            "exchange_id": exchange_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "start_ms": start_ms,
            "end_ms": end_ms,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cache_path(directory: Path, key: str) -> Path:
    # Two-level fan-out so a large cache never puts hundreds of thousands of
    # files in one directory (the same convention Git itself uses for loose
    # objects).
    return directory / key[:2] / f"{key}.json"


def _candles_content_hash(rows: list[dict[str, float | int | None]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def read_cached_candles(
    *,
    exchange_id: str,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    directory: str | Path | None = None,
) -> list[Candle] | None:
    """Returns the cached candle list, or None on a genuine cache miss (no
    file at the expected path). Raises `MarketPathCacheCorruptError` if a
    file exists there but fails any integrity check -- see this module's
    own docstring on why that must never be silently treated as a miss."""
    key = _cache_key(
        exchange_id=exchange_id,
        symbol=symbol,
        timeframe=timeframe,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    path = _cache_path(cache_dir(directory), key)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MarketPathCacheCorruptError(f"cannot read cache file at {path}: {exc}") from exc

    try:
        payload = json.loads(raw)
        rows = payload["candles"]
        cached_key = payload["cache_key"]
        cached_content_hash = payload["content_sha256"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise MarketPathCacheCorruptError(f"cache file at {path} is not valid: {exc}") from exc

    if cached_key != key:
        raise MarketPathCacheCorruptError(
            f"cache file at {path} has cache_key {cached_key!r}, expected {key!r} "
            "(hash collision, or the file was hand-edited/replaced)"
        )
    if _candles_content_hash(rows) != cached_content_hash:
        raise MarketPathCacheCorruptError(
            f"cache file at {path} failed its content_sha256 check -- the candle "
            "payload was modified after it was written"
        )

    from .ohlcv import Candle

    try:
        return [
            Candle(
                ts_ms=row["ts_ms"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
            )
            for row in rows
        ]
    except (KeyError, TypeError) as exc:
        raise MarketPathCacheCorruptError(
            f"cache file at {path} has a malformed candle row: {exc}"
        ) from exc


class CacheWriteOutcome(Enum):
    """Distinguishes the three ways a write call can end, because the
    caller must react differently to each: `CREATED` means these exact
    candles are now the durable record; `ALREADY_EXISTS` means a concurrent
    writer's candles are the durable record instead (the caller must read
    those back and use them, not its own -- otherwise two reports racing on
    the same window could each return different candles even though only
    one version is ever actually persisted, which is its own reproducibility
    hazard: two "reproducible" runs disagreeing with each other); and
    `WRITE_FAILED` means nothing is durable yet at all."""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    WRITE_FAILED = "write_failed"


def write_cached_candles(
    *,
    exchange_id: str,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    candles: list[Candle],
    directory: str | Path | None = None,
) -> CacheWriteOutcome:
    """First-writer-wins, atomic write. Never raises on an I/O failure or a
    lost race -- both are reported through the return value instead (see
    `CacheWriteOutcome`), so the caller can decide how strict to be. The one
    case that must NOT be silent -- a cache entry that exists but is corrupt
    -- is handled by read_cached_candles raising, not by this function; a
    different failure mode with a different correct response."""
    key = _cache_key(
        exchange_id=exchange_id,
        symbol=symbol,
        timeframe=timeframe,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    path = _cache_path(cache_dir(directory), key)
    rows = [
        {
            "ts_ms": c.ts_ms,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in candles
    ]
    payload = {
        "cache_key": key,
        "content_sha256": _candles_content_hash(rows),
        "contract_version": CACHE_CONTRACT_VERSION,
        "exchange_id": exchange_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "candles": rows,
    }
    tmp_path_obj: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        tmp_path_obj = Path(tmp_name)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, sort_keys=True, separators=(",", ":"))
        try:
            # os.link (a hard link) is atomic and fails with FileExistsError
            # if `path` already exists -- unlike os.replace, which always
            # succeeds and always overwrites. This is what actually gives
            # first-writer-wins instead of last-writer-wins.
            os.link(tmp_path_obj, path)
        except FileExistsError:
            return CacheWriteOutcome.ALREADY_EXISTS
        return CacheWriteOutcome.CREATED
    except OSError:
        return CacheWriteOutcome.WRITE_FAILED
    finally:
        if tmp_path_obj is not None:
            tmp_path_obj.unlink(missing_ok=True)
