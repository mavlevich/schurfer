"""Bounded, scoped historical OHLCV backfill (step 3 of 3): a Parquet dataset
for the token-behavior-history feasibility line, read via DuckDB.

Decided 2026-08-10 (see ROADMAP.md): step 2's canonical live sample
(`token_history_ohlcv_sample_report.py`) formally failed its global gate on
p95 call latency across all 5 exchanges with a ready instrument, independently
reverified; a permitted repeat attempt also failed to complete and produced no
data, ruling out a one-off fluke. The `<5s` threshold is not relaxed after the
fact. Per the pre-registered rule that a persistent single-venue problem
narrows scope rather than weakening the global contract, this step is
authorized scoped to `FROZEN_VENUE_ALLOWLIST` only. gate and mexc are not
silently absent: every instrument on those exchanges is still recorded in
this report's `excluded_instruments`, with `VENUE_EXCLUSION_REASON`.

This is a bounded, one-shot backfill, not a continuously appending pipeline:

* One instrument's fetch failure never aborts the run; the remaining
  instruments on that exchange and every other exchange still run. But the
  run as a whole is fail-closed: `dataset_ready` is only true when every
  included instrument's own result is `publishable`. A dataset with 44 good
  files and 1 failed one is not silently usable as "the dataset" -- the
  manifest says so explicitly, and it is on the consumer to check
  `dataset_ready` before treating any of it as complete.
* Each run writes to its own immutable, timestamped `run_id` directory and
  never overwrites a previous run's output. `manifest.json` is written last,
  atomically (temp file plus rename): its absence means the run never
  finished, by construction, not by convention.
* No outcomes, no score changes, no cross-venue fallback. This produces a
  reproducible historical dataset, nothing else.
* The CLI's exit code is not just "did the process crash": after writing,
  `verify_dataset_on_disk` re-reads manifest.json from disk and checks it
  against the run directory's actual contents (every publishable result's
  file exists and hashes correctly, no unaccounted-for Parquet file is
  present); a mismatch exits `3` without printing a result, since the
  in-memory report cannot be trusted once disk and manifest disagree. The
  rendered result is always printed once that passes, but the process still
  exits `2` if `dataset_ready` is false, so Make/cron/CI can tell "a
  diagnostic manifest was saved" apart from "a publishable dataset was
  produced" without parsing output.

Window semantics (the one real subtlety here): an exact instrument can be
tied to more than one eligible decision (repeat pumps on the same token). The
window this report actually fetches merges all of them:

    fetch_start = min(max(onboarded_at, decision_ts - 365d)) over all decisions
    fetch_end   = max(decision_ts) over all decisions

This avoids re-fetching the same instrument's history once per decision. Full
point-in-time correctness is preserved separately: every decision's own
`[max(onboarded_at, decision_ts - 365d), decision_ts)` window is recorded in
`InstrumentDecisionWindow`, and any later analysis MUST filter to
`bar.ts_ms < decision.decision_ts` per decision rather than assuming the whole
file's span is valid as-of every decision it informed. The 365-day cap is a
per-decision lookback bound, not a bound on the physical span of the merged
file, which can be longer when decisions on the same instrument are spread
out over time.

Parquet writing goes through DuckDB alone (`CREATE TABLE` with an explicit
typed schema, then `COPY ... TO ... (FORMAT PARQUET, COMPRESSION ZSTD)`), not
PyArrow: DuckDB is already required for reading this dataset, at this scale
(45 instruments, roughly 365 daily rows each) it needs no help, and one
engine for both read and write removes a whole class of type-mismatch risk.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ccxt
import duckdb

from .exchange_registry import EXCHANGE_FACTORIES
from .ohlcv import (
    Candle,
    IncompleteFetchError,
    PageFetchObservation,
    fetch_symbol_candles,
)
from .outcome_repository import async_database_url
from .replay import ReplayFilters, build_replay_dataset
from .reporting import (
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)
from .token_history_identity_preflight_report import (
    IdentityRecord,
    InstrumentSummary,
    TokenHistoryPreflightManifest,
    TokenHistoryPreflightReport,
    build_token_history_preflight_report,
)
from .token_history_ohlcv_sample_report import (
    GapAnalysis,
    LatencyStats,
    PageSizeStats,
    _coverage_outcome,
    _page_diagnostics_fields,
    analyze_gaps,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

DATASET_VERSION = "token_history_ohlcv_v1"
SCHEMA_VERSION = 1
_DAY_MS = 24 * 60 * 60 * 1000
_TIMEFRAME = "1d"
_MAX_LOOKBACK_DAYS = 365

# See the module docstring's first paragraph for the full decision record.
FROZEN_VENUE_ALLOWLIST = ("binance", "bybit", "xt")
VENUE_EXCLUSION_REASON = "venue_live_sample_not_ready"

# DATASET_VERSION is a frozen contract, not just a label: every replay filter
# below is pinned to the exact decision-dataset snapshot the step 2 canonical
# live sample (and therefore its gate result and this step's scoped
# authorization) was actually computed against. Letting --since/--until/
# --strategy-version/--resolver-version/--required-horizons/--allow-fallback
# drift, even by accident (--until previously defaulted to "now", so two
# ordinary runs on different days would already disagree), would mean two
# runs claiming the same dataset_version could cover different,
# never-jointly-validated instrument populations. These are deliberately
# literal values, not aliases of the shared TOKEN_HISTORY_PREFLIGHT_*/
# RESOLVER_VERSION/DEFAULT_REPLAY_HORIZONS constants: aliasing would mean an
# unrelated future change to those shared defaults silently changes both
# sides of ensure_canonical_filters' comparison at once, defeating the check
# it exists to perform. CANONICAL_SINCE/CANONICAL_STRATEGY_VERSIONS/
# CANONICAL_RESOLVER_VERSION/CANONICAL_REQUIRED_HORIZONS are exactly the
# values those shared constants held on 2026-08-10, the day the step 2
# canonical run this dataset_version is pinned to was computed.
# CANONICAL_UNTIL is exactly TokenHistoryPreflightManifest.
# dataset_until_exclusive from the archived step 2 canonical run
# (backups/reports/token-history-ohlcv-sample-2026-08-10.json). A genuinely
# different scope needs its own, separately named dataset_version, not a
# silently-drifted copy of this one.
CANONICAL_SINCE = datetime(2026, 7, 26, tzinfo=UTC)
CANONICAL_UNTIL = datetime(2026, 8, 9, 23, 21, 14, 187586, tzinfo=UTC)
CANONICAL_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
CANONICAL_RESOLVER_VERSION = "forward_v1"
CANONICAL_REQUIRED_HORIZONS = (480,)
CANONICAL_ALLOW_FALLBACK = False


class NonCanonicalRunError(ValueError):
    """Raised when the resolved replay filters do not match the exact
    decision-dataset snapshot DATASET_VERSION was validated against. A
    sensitivity or exploratory run needs its own, separately named
    dataset_version; this module refuses to produce one silently under the
    frozen v1 tag."""


def ensure_canonical_filters(
    *,
    since: datetime,
    until: datetime,
    strategy_versions: tuple[str, ...],
    resolver_version: str,
    required_horizons: tuple[int, ...],
    allow_fallback: bool,
) -> None:
    """Pure check (no I/O): raises NonCanonicalRunError unless every filter
    exactly matches the canonical snapshot. Kept separate from _run so it is
    testable without a database or a real argparse.Namespace."""
    if (
        since != CANONICAL_SINCE
        or until != CANONICAL_UNTIL
        or strategy_versions != CANONICAL_STRATEGY_VERSIONS
        or resolver_version != CANONICAL_RESOLVER_VERSION
        or required_horizons != CANONICAL_REQUIRED_HORIZONS
        or allow_fallback != CANONICAL_ALLOW_FALLBACK
    ):
        raise NonCanonicalRunError(
            f"{DATASET_VERSION} is a frozen contract: since/until/strategy_versions/"
            "resolver_version/required_horizons/allow_fallback must exactly match the "
            "canonical values this dataset_version was validated against "
            f"(since={CANONICAL_SINCE.isoformat()}, until={CANONICAL_UNTIL.isoformat()}, "
            f"strategy_versions={CANONICAL_STRATEGY_VERSIONS}, "
            f"resolver_version={CANONICAL_RESOLVER_VERSION!r}, "
            f"required_horizons={CANONICAL_REQUIRED_HORIZONS}, "
            f"allow_fallback={CANONICAL_ALLOW_FALLBACK}). A different scope needs its own, "
            "separately named dataset_version, not a drifted copy of this one."
        )


@dataclass(frozen=True)
class InstrumentDecisionWindow:
    pump_event_id: int
    decision_ts: datetime
    onboarded_at: datetime
    window_start_ms: int
    window_end_ms: int


@dataclass(frozen=True)
class ScopedInstrument:
    exchange: str
    identity_key: str
    unified_symbol: str
    base: str
    instrument_hash: str
    decisions: tuple[InstrumentDecisionWindow, ...]
    fetch_start_ms: int
    fetch_end_ms: int


@dataclass(frozen=True)
class ExcludedVenueInstrument:
    exchange: str
    identity_key: str
    unified_symbol: str
    base: str
    reason: str


@dataclass(frozen=True)
class InstrumentFetchResult:
    exchange: str
    identity_key: str
    unified_symbol: str
    base: str
    instrument_hash: str
    fetch_start_ms: int
    fetch_end_ms: int
    decisions: tuple[InstrumentDecisionWindow, ...]
    # Coverage outcomes (fetch + write both succeeded): completed /
    # partial_coverage / no_data / no_full_days_expected, same taxonomy as
    # step 2. Operational-failure outcomes: incomplete_fetch_error /
    # fetch_exception (fetch never returned) / parquet_write_failed (fetch
    # succeeded, the local write-and-verify step did not).
    outcome: str
    error_type: str | None
    error_detail: str | None
    api_calls: int
    successful_pages: int
    empty_calls: int
    timeout_calls: int
    error_calls: int
    filtered_or_deduplicated_rows_total: int
    raw_page_size_stats: PageSizeStats | None
    normalized_page_size_stats: PageSizeStats | None
    latency_stats: LatencyStats | None
    gap_analysis: GapAnalysis | None
    bar_count: int
    parquet_relative_path: str | None
    parquet_sha256: str | None
    parquet_bytes: int | None
    canonical_row_hash: str | None
    # False for every operational-failure outcome, and for a coverage outcome
    # with an internal or trailing gap: those are not "explained" the way a
    # leading-only gap is (a plausible retention limit or later real listing
    # date). A leading-only partial_coverage is still publishable.
    publishable: bool


@dataclass(frozen=True)
class VenueCount:
    exchange: str
    count: int


@dataclass(frozen=True)
class TokenHistoryDatasetManifest:
    dataset_version: str
    schema_version: int
    run_id: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    duckdb_version: str
    ccxt_version: str
    preflight_manifest: TokenHistoryPreflightManifest
    frozen_venue_allowlist: tuple[str, ...]
    venue_exclusion_reason: str
    universe_instrument_count: int
    included_by_venue: tuple[VenueCount, ...]
    excluded_by_venue: tuple[VenueCount, ...]
    included_instrument_count: int
    excluded_instrument_count: int
    success_count: int
    failure_count: int
    dataset_ready: bool
    timeframe: str
    timeframe_ms: int
    max_lookback_days: int
    dataset_content_fingerprint: str
    interpretation: str = "bounded_scoped_historical_backfill_no_outcomes_no_score_change"


@dataclass(frozen=True)
class TokenHistoryDatasetReport:
    manifest: TokenHistoryDatasetManifest
    excluded_instruments: tuple[ExcludedVenueInstrument, ...]
    results: tuple[InstrumentFetchResult, ...]
    operational_warnings: tuple[str, ...]


def instrument_hash(exchange: str, identity_key: str) -> str:
    """Stable, filesystem-safe stand-in for identity_key: identity_key is
    built elsewhere as f"{exchange}:{market_type}:{market_id}:{identity_
    version}" (see instruments.py), so it can contain ':' and, depending on
    the exchange's raw market id format, other characters that are awkward or
    unsafe as a path component. Truncated to 16 hex characters (64 bits):
    collision risk is negligible at this project's scale (a few dozen
    instruments per exchange). The full identity_key is never lost to this
    truncation, it is recorded in full inside both the Parquet rows and the
    manifest.
    """
    digest = hashlib.sha256(f"{exchange}\0{identity_key}".encode()).hexdigest()
    return digest[:16]


def _ready_decisions_for_instrument(
    records: tuple[IdentityRecord, ...],
    exchange: str,
    identity_key: str,
) -> tuple[IdentityRecord, ...]:
    return tuple(
        record
        for record in records
        if record.readiness == "identity_ready"
        and record.exchange == exchange
        and record.identity_key == identity_key
    )


def select_scoped_instruments(
    instruments: tuple[InstrumentSummary, ...],
    records: tuple[IdentityRecord, ...],
) -> tuple[tuple[ScopedInstrument, ...], tuple[ExcludedVenueInstrument, ...]]:
    """Deterministic, DB-only (no network): every identity-ready instrument
    from step 1, split into the frozen-allowlist venues this step fetches and
    every other venue, excluded with an explicit reason rather than
    disappearing from the denominator.

    Each instrument's fetch window merges ALL of its ready decisions, not one
    representative: see the module docstring's window-semantics section.
    """
    included: list[ScopedInstrument] = []
    excluded: list[ExcludedVenueInstrument] = []
    for instrument in instruments:
        if instrument.exchange not in FROZEN_VENUE_ALLOWLIST:
            excluded.append(
                ExcludedVenueInstrument(
                    exchange=instrument.exchange,
                    identity_key=instrument.identity_key,
                    unified_symbol=instrument.unified_symbol,
                    base=instrument.base,
                    reason=VENUE_EXCLUSION_REASON,
                )
            )
            continue
        decisions = _ready_decisions_for_instrument(
            records, instrument.exchange, instrument.identity_key
        )
        if not decisions:
            raise ValueError(
                f"instrument summary for {instrument.exchange}/{instrument.identity_key} "
                "has no matching identity_ready record; this should be impossible"
            )
        windows = []
        for record in decisions:
            if record.onboarded_at is None:
                raise ValueError(
                    f"identity_ready record for {instrument.exchange}/{instrument.identity_key} "
                    "is missing onboarded_at; this should be impossible for that readiness"
                )
            bounded_start = max(
                record.onboarded_at, record.decision_ts - timedelta(days=_MAX_LOOKBACK_DAYS)
            )
            windows.append(
                InstrumentDecisionWindow(
                    pump_event_id=record.pump_event_id,
                    decision_ts=record.decision_ts,
                    onboarded_at=record.onboarded_at,
                    window_start_ms=int(bounded_start.timestamp() * 1000),
                    window_end_ms=int(record.decision_ts.timestamp() * 1000),
                )
            )
        windows.sort(key=lambda window: (window.decision_ts, window.pump_event_id))
        included.append(
            ScopedInstrument(
                exchange=instrument.exchange,
                identity_key=instrument.identity_key,
                unified_symbol=instrument.unified_symbol,
                base=instrument.base,
                instrument_hash=instrument_hash(instrument.exchange, instrument.identity_key),
                decisions=tuple(windows),
                fetch_start_ms=min(window.window_start_ms for window in windows),
                fetch_end_ms=max(window.window_end_ms for window in windows),
            )
        )
    included.sort(key=lambda item: (item.exchange, item.identity_key))
    excluded.sort(key=lambda item: (item.exchange, item.identity_key))
    return tuple(included), tuple(excluded)


def _base_result_fields(instrument: ScopedInstrument) -> dict[str, Any]:
    return {
        "exchange": instrument.exchange,
        "identity_key": instrument.identity_key,
        "unified_symbol": instrument.unified_symbol,
        "base": instrument.base,
        "instrument_hash": instrument.instrument_hash,
        "fetch_start_ms": instrument.fetch_start_ms,
        "fetch_end_ms": instrument.fetch_end_ms,
        "decisions": instrument.decisions,
    }


def _write_and_verify_parquet(
    candles: list[Candle],
    instrument: ScopedInstrument,
    output_dir: Path,
    *,
    ccxt_version: str,
    generated_at_ms: int,
) -> tuple[str, str, int, str]:
    """Writes one instrument's bars to its own Parquet file via a typed
    DuckDB table (never dict-based schema inference), reads the file back to
    verify sort order, uniqueness, and row count before the atomic rename,
    and returns (relative_path, sha256, file_bytes, canonical_row_hash).
    Raises on any verification failure; the caller turns that into a
    per-instrument `parquet_write_failed` result rather than aborting the
    run.

    Both the write and the read-back use DuckDB's own `?` parameter binding
    for the file path, not an embedded string: an output root containing a
    single quote (a real, reproduced failure mode, not a theoretical one)
    breaks a string-embedded path with a ParserException, since the quote
    prematurely closes the SQL string literal.
    """
    instrument_dir = (
        output_dir / f"exchange={instrument.exchange}" / f"instrument={instrument.instrument_hash}"
    )
    instrument_dir.mkdir(parents=True, exist_ok=True)
    final_path = instrument_dir / "bars.parquet"
    tmp_path = instrument_dir / "bars.parquet.tmp"

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE bars (
                ts_ms BIGINT NOT NULL,
                open DOUBLE NOT NULL,
                high DOUBLE NOT NULL,
                low DOUBLE NOT NULL,
                close DOUBLE NOT NULL,
                volume DOUBLE,
                exchange VARCHAR NOT NULL,
                identity_key VARCHAR NOT NULL,
                unified_symbol VARCHAR NOT NULL,
                base VARCHAR NOT NULL,
                schema_version INTEGER NOT NULL,
                ccxt_version VARCHAR NOT NULL,
                fetch_start_ms BIGINT NOT NULL,
                fetch_end_ms BIGINT NOT NULL,
                generated_at_ms BIGINT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    candle.ts_ms,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    instrument.exchange,
                    instrument.identity_key,
                    instrument.unified_symbol,
                    instrument.base,
                    SCHEMA_VERSION,
                    ccxt_version,
                    instrument.fetch_start_ms,
                    instrument.fetch_end_ms,
                    generated_at_ms,
                )
                for candle in candles
            ],
        )
        connection.execute(
            "COPY (SELECT * FROM bars ORDER BY ts_ms) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
            [tmp_path.as_posix()],
        )
    finally:
        connection.close()

    verify_connection = duckdb.connect(":memory:")
    try:
        rows = verify_connection.execute(
            "SELECT ts_ms, open, high, low, close, volume FROM read_parquet(?) ORDER BY ts_ms",
            [tmp_path.as_posix()],
        ).fetchall()
    finally:
        verify_connection.close()

    timestamps = [row[0] for row in rows]
    if timestamps != sorted(timestamps):
        raise ValueError(f"{instrument.unified_symbol}: read-back timestamps are not sorted")
    if len(set(timestamps)) != len(timestamps):
        raise ValueError(f"{instrument.unified_symbol}: read-back timestamps contain duplicates")
    if len(rows) != len(candles):
        raise ValueError(
            f"{instrument.unified_symbol}: read-back row count {len(rows)} != "
            f"written {len(candles)}"
        )

    canonical_row_hash = hashlib.sha256(
        json.dumps([list(row) for row in rows], separators=(",", ":")).encode()
    ).hexdigest()
    tmp_bytes = tmp_path.read_bytes()
    file_sha256 = hashlib.sha256(tmp_bytes).hexdigest()
    tmp_path.replace(final_path)

    relative_path = final_path.relative_to(output_dir).as_posix()
    return relative_path, file_sha256, len(tmp_bytes), canonical_row_hash


def _is_explained_partial_coverage(gap: GapAnalysis) -> bool:
    """A leading-only gap is consistent with a real retention limit or a
    later real listing date than the recorded onboarding: explained enough to
    still publish. Any internal or trailing gap is not: those are holes
    inside or after data the exchange did return, which this project has no
    ready explanation for and must not paper over."""
    return not gap.internal_missing_dates and gap.trailing_missing_days == 0


async def _fetch_and_write_instrument(
    exchange_client: Any,
    instrument: ScopedInstrument,
    output_dir: Path,
    *,
    ccxt_version: str,
    generated_at_ms: int,
) -> InstrumentFetchResult:
    observations: list[PageFetchObservation] = []
    base_fields = _base_result_fields(instrument)
    try:
        candles = await fetch_symbol_candles(
            exchange_client,
            instrument.unified_symbol,
            instrument.fetch_start_ms,
            instrument.fetch_end_ms,
            timeframe=_TIMEFRAME,
            timeframe_ms=_DAY_MS,
            on_page=observations.append,
        )
    except IncompleteFetchError as exc:
        return InstrumentFetchResult(
            **base_fields,
            outcome="incomplete_fetch_error",
            error_type=type(exc).__name__,
            error_detail=str(exc),
            api_calls=exc.api_calls,
            successful_pages=exc.successful_pages,
            gap_analysis=None,
            bar_count=0,
            parquet_relative_path=None,
            parquet_sha256=None,
            parquet_bytes=None,
            canonical_row_hash=None,
            publishable=False,
            **_page_diagnostics_fields(tuple(observations)),
        )
    except Exception as exc:
        # A single instrument's fetch must never abort the whole backfill.
        return InstrumentFetchResult(
            **base_fields,
            outcome="fetch_exception",
            error_type=type(exc).__name__,
            error_detail=str(exc),
            api_calls=len(observations),
            successful_pages=sum(1 for o in observations if o.outcome == "success"),
            gap_analysis=None,
            bar_count=0,
            parquet_relative_path=None,
            parquet_sha256=None,
            parquet_bytes=None,
            canonical_row_hash=None,
            publishable=False,
            **_page_diagnostics_fields(tuple(observations)),
        )

    gap_analysis = analyze_gaps(
        tuple(candle.ts_ms for candle in candles),
        instrument.fetch_start_ms,
        instrument.fetch_end_ms,
    )
    outcome = _coverage_outcome(gap_analysis)
    api_calls = len(observations)
    successful_pages = sum(1 for observation in observations if observation.outcome == "success")
    diagnostics = _page_diagnostics_fields(tuple(observations))

    # Classify BEFORE writing anything: a coverage outcome that will end up
    # non-publishable (no_data, no_full_days_expected, or a partial_coverage
    # with an internal/trailing gap) must never produce a normal-looking
    # bars.parquet in the standard tree. A later glob read
    # ("**/*.parquet") has no way to tell a quietly-written bad file apart
    # from a good one except by cross-referencing the manifest, which this
    # is trying to make unnecessary for the common case. completed and an
    # explained (leading-only) partial_coverage both always have at least
    # one returned bar (see _coverage_outcome: both require returned_days >
    # 0), so this ordering also means _write_and_verify_parquet is never
    # called with an empty candle list.
    publishable_by_coverage = outcome == "completed" or (
        outcome == "partial_coverage" and _is_explained_partial_coverage(gap_analysis)
    )
    if not publishable_by_coverage:
        return InstrumentFetchResult(
            **base_fields,
            outcome=outcome,
            error_type=None,
            error_detail=None,
            api_calls=api_calls,
            successful_pages=successful_pages,
            gap_analysis=gap_analysis,
            bar_count=len(candles),
            parquet_relative_path=None,
            parquet_sha256=None,
            parquet_bytes=None,
            canonical_row_hash=None,
            publishable=False,
            **diagnostics,
        )

    try:
        relative_path, file_sha256, file_bytes, row_hash = _write_and_verify_parquet(
            candles,
            instrument,
            output_dir,
            ccxt_version=ccxt_version,
            generated_at_ms=generated_at_ms,
        )
    except Exception as exc:
        return InstrumentFetchResult(
            **base_fields,
            outcome="parquet_write_failed",
            error_type=type(exc).__name__,
            error_detail=str(exc),
            api_calls=api_calls,
            successful_pages=successful_pages,
            gap_analysis=gap_analysis,
            bar_count=len(candles),
            parquet_relative_path=None,
            parquet_sha256=None,
            parquet_bytes=None,
            canonical_row_hash=None,
            publishable=False,
            **diagnostics,
        )

    return InstrumentFetchResult(
        **base_fields,
        outcome=outcome,
        error_type=None,
        error_detail=None,
        api_calls=api_calls,
        successful_pages=successful_pages,
        gap_analysis=gap_analysis,
        bar_count=len(candles),
        parquet_relative_path=relative_path,
        parquet_sha256=file_sha256,
        parquet_bytes=file_bytes,
        canonical_row_hash=row_hash,
        publishable=True,
        **diagnostics,
    )


def _client_init_failed_result(
    instrument: ScopedInstrument, exc: Exception
) -> InstrumentFetchResult:
    return InstrumentFetchResult(
        **_base_result_fields(instrument),
        outcome="exchange_client_init_failed",
        error_type=type(exc).__name__,
        error_detail=str(exc),
        api_calls=0,
        successful_pages=0,
        gap_analysis=None,
        bar_count=0,
        parquet_relative_path=None,
        parquet_sha256=None,
        parquet_bytes=None,
        canonical_row_hash=None,
        publishable=False,
        **_page_diagnostics_fields(()),
    )


async def run_dataset_backfill(
    scoped: tuple[ScopedInstrument, ...],
    output_dir: Path,
    *,
    generated_at_ms: int,
    ccxt_version: str,
) -> tuple[tuple[InstrumentFetchResult, ...], tuple[str, ...]]:
    """Sequential within an exchange and across exchanges, one client alive
    at a time, in the fixed order binance -> bybit -> xt: this is a bounded,
    occasional backfill, not a latency-sensitive job, and politeness to the
    exchanges matters more than wall-clock time here. A factory() failure or
    a close() failure is contained to its own exchange, exactly like step
    2's run_live_sample: neither discards results already collected for a
    previous exchange, nor stops the run from trying the next one."""
    by_exchange: dict[str, list[ScopedInstrument]] = defaultdict(list)
    for instrument in scoped:
        by_exchange[instrument.exchange].append(instrument)

    results: list[InstrumentFetchResult] = []
    warnings: list[str] = []
    for exchange in FROZEN_VENUE_ALLOWLIST:
        instruments = sorted(by_exchange.get(exchange, ()), key=lambda item: item.identity_key)
        if not instruments:
            continue
        factory = EXCHANGE_FACTORIES.get(exchange)
        if factory is None:
            no_factory = ValueError(f"no exchange client factory registered for '{exchange}'")
            results.extend(
                _client_init_failed_result(instrument, no_factory) for instrument in instruments
            )
            continue
        try:
            client = factory()
        except Exception as exc:
            results.extend(
                _client_init_failed_result(instrument, exc) for instrument in instruments
            )
            continue
        try:
            for instrument in instruments:
                results.append(
                    await _fetch_and_write_instrument(
                        client,
                        instrument,
                        output_dir,
                        ccxt_version=ccxt_version,
                        generated_at_ms=generated_at_ms,
                    )
                )
        finally:
            try:
                await client.close(clean_instance_data=True)
            except Exception as exc:
                warnings.append(
                    f"failed to close the {exchange} exchange client cleanly: "
                    f"{type(exc).__name__}: {exc}"
                )
    return tuple(results), tuple(warnings)


def _venue_counts(items: Any, exchanges: list[str]) -> tuple[VenueCount, ...]:
    return tuple(
        VenueCount(exchange=exchange, count=sum(1 for item in items if item.exchange == exchange))
        for exchange in exchanges
        if any(item.exchange == exchange for item in items)
    )


def _dataset_content_fingerprint(results: tuple[InstrumentFetchResult, ...]) -> str:
    """Content-addressed digest over every publishable result's
    (instrument_hash, canonical_row_hash) pair, sorted for determinism so
    result ordering never changes the fingerprint. Two runs of the same
    dataset_version that produced byte-identical published Parquet data get
    the same fingerprint; any difference in which instruments published or
    in their row content changes it. Non-publishable results are excluded:
    they contribute no rows to the dataset, so they must not perturb the
    fingerprint of what a consumer actually reads."""
    parts = sorted(
        f"{result.instrument_hash}:{result.canonical_row_hash}"
        for result in results
        if result.publishable
    )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _write_manifest_atomic(report: TokenHistoryDatasetReport, output_dir: Path) -> None:
    """Written last, and atomically: a temp file plus rename means a crash
    mid-write can never leave a half-written manifest.json behind. Its
    absence is exactly how a consumer detects an unfinished run, with no
    extra convention needed."""
    payload = json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)
    tmp_path = output_dir / "manifest.json.tmp"
    final_path = output_dir / "manifest.json"
    tmp_path.write_text(payload)
    tmp_path.replace(final_path)


class ManifestVerificationError(RuntimeError):
    """Raised when the just-written manifest.json does not match what is
    actually on disk. Deliberately re-reads manifest.json from disk rather
    than trusting the in-process TokenHistoryDatasetReport object: the whole
    point is to catch a gap between "we believe we wrote X" and "X is truly
    what a later consumer will find", e.g. a write that silently truncated,
    a stray leftover file from a previous bug, or a manifest.json edited or
    replaced out from under this process. A run must never be treated as
    successful when this check fails."""


def verify_dataset_on_disk(report: TokenHistoryDatasetReport, output_dir: Path) -> None:
    """Re-reads manifest.json from output_dir and checks it against the run
    directory's actual contents: every publishable result's Parquet file
    exists and hashes to exactly what the manifest claims, and no Parquet
    file exists in the run directory that no publishable result accounts
    for. Must run after _write_manifest_atomic and before the run is
    reported as done; raises ManifestVerificationError on any mismatch."""
    manifest_path = output_dir / "manifest.json"
    try:
        on_disk_manifest = json.loads(manifest_path.read_text())["manifest"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        raise ManifestVerificationError(
            f"could not read back manifest.json at {manifest_path}: {exc}"
        ) from exc
    if on_disk_manifest.get("run_id") != report.manifest.run_id:
        raise ManifestVerificationError(
            f"manifest.json at {output_dir} has run_id "
            f"{on_disk_manifest.get('run_id')!r}, expected {report.manifest.run_id!r}: "
            "it does not describe this run directory"
        )

    expected_paths: set[Path] = set()
    for result in report.results:
        if not result.publishable:
            continue
        if result.parquet_relative_path is None or result.parquet_sha256 is None:
            raise ManifestVerificationError(
                f"{result.exchange}:{result.identity_key} is publishable but the "
                "manifest has no recorded Parquet path or sha256 for it"
            )
        parquet_path = output_dir / result.parquet_relative_path
        expected_paths.add(parquet_path.resolve())
        if not parquet_path.is_file():
            raise ManifestVerificationError(
                f"{result.exchange}:{result.identity_key}: manifest references "
                f"{parquet_path}, but that file does not exist"
            )
        actual_sha256 = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
        if actual_sha256 != result.parquet_sha256:
            raise ManifestVerificationError(
                f"{result.exchange}:{result.identity_key}: sha256 mismatch for "
                f"{parquet_path} (manifest says {result.parquet_sha256}, file hashes "
                f"to {actual_sha256})"
            )

    unexpected = {path.resolve() for path in output_dir.rglob("*.parquet")} - expected_paths
    if unexpected:
        raise ManifestVerificationError(
            "unexpected Parquet file(s) in the run directory not referenced by any "
            f"publishable result: {sorted(str(path) for path in unexpected)}"
        )


async def build_token_history_dataset_report(
    preflight: TokenHistoryPreflightReport,
    output_root: Path,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> tuple[TokenHistoryDatasetReport, Path]:
    scoped, excluded = select_scoped_instruments(preflight.instruments, preflight.records)
    run_id = f"{generated_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_dir = output_root / DATASET_VERSION / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    results, warnings = await run_dataset_backfill(
        scoped,
        output_dir,
        generated_at_ms=int(generated_at.timestamp() * 1000),
        ccxt_version=ccxt.__version__,
    )

    success_count = sum(1 for result in results if result.publishable)
    failure_count = len(results) - success_count
    excluded_exchanges = sorted({item.exchange for item in excluded})

    report = TokenHistoryDatasetReport(
        manifest=TokenHistoryDatasetManifest(
            dataset_version=DATASET_VERSION,
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            code_revision=normalize_code_revision(code_revision),
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            duckdb_version=duckdb.__version__,
            ccxt_version=ccxt.__version__,
            preflight_manifest=preflight.manifest,
            frozen_venue_allowlist=FROZEN_VENUE_ALLOWLIST,
            venue_exclusion_reason=VENUE_EXCLUSION_REASON,
            universe_instrument_count=len(scoped) + len(excluded),
            included_by_venue=_venue_counts(scoped, list(FROZEN_VENUE_ALLOWLIST)),
            excluded_by_venue=_venue_counts(excluded, excluded_exchanges),
            included_instrument_count=len(scoped),
            excluded_instrument_count=len(excluded),
            success_count=success_count,
            failure_count=failure_count,
            dataset_ready=len(results) > 0 and failure_count == 0,
            timeframe=_TIMEFRAME,
            timeframe_ms=_DAY_MS,
            max_lookback_days=_MAX_LOOKBACK_DAYS,
            dataset_content_fingerprint=_dataset_content_fingerprint(results),
        ),
        excluded_instruments=excluded,
        results=results,
        operational_warnings=warnings,
    )
    _write_manifest_atomic(report, output_dir)
    return report, output_dir


def render_json(report: TokenHistoryDatasetReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: TokenHistoryDatasetReport, output_dir: Path) -> str:
    manifest = report.manifest
    lines = [
        "# Token-Behavior-History Parquet Dataset (Step 3 of 3)",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Run id: `{manifest.run_id}`",
        f"Output directory: `{output_dir}`",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"DuckDB version: `{manifest.duckdb_version}`; CCXT version: `{manifest.ccxt_version}`",
        f"Frozen venue allowlist: {', '.join(manifest.frozen_venue_allowlist)}",
        (
            f"Timeframe: `{manifest.timeframe}` ({manifest.timeframe_ms} ms); "
            f"max lookback per decision: {manifest.max_lookback_days} days"
        ),
        f"Dataset content fingerprint: `{manifest.dataset_content_fingerprint}`",
        "",
        (
            f"> {manifest.interpretation}. `dataset_ready = {manifest.dataset_ready}`: "
            "true only when every included instrument's own result is publishable. "
            "Check this before treating any part of the dataset as complete."
        ),
        "",
        "## Universe",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Total exact instruments (universe)", manifest.universe_instrument_count),
                ("Included", manifest.included_instrument_count),
                ("Excluded", manifest.excluded_instrument_count),
                ("Successful / publishable", manifest.success_count),
                ("Failed / not publishable", manifest.failure_count),
            ],
        )
    )
    lines.extend(["", "## Included by venue", ""])
    lines.extend(
        markdown_table(
            ("Exchange", "Count"),
            [(row.exchange, row.count) for row in manifest.included_by_venue],
        )
    )
    lines.extend(["", "## Excluded by venue", ""])
    lines.extend(
        markdown_table(
            ("Exchange", "Count", "Reason"),
            [
                (row.exchange, row.count, manifest.venue_exclusion_reason)
                for row in manifest.excluded_by_venue
            ],
        )
    )
    if report.operational_warnings:
        lines.extend(["", "## Operational warnings", ""])
        lines.extend(f"- {warning}" for warning in report.operational_warnings)
    lines.extend(["", "## Per-instrument results", ""])
    lines.extend(
        markdown_table(
            ("Exchange", "Symbol", "Outcome", "Publishable", "Bars", "API calls", "Path"),
            [
                (
                    result.exchange,
                    result.unified_symbol,
                    result.outcome,
                    "yes" if result.publishable else "no",
                    result.bar_count,
                    result.api_calls,
                    result.parquet_relative_path or "n/a",
                )
                for result in report.results
            ],
        )
    )
    failures = [result for result in report.results if not result.publishable]
    if failures:
        lines.extend(["", "## Failure detail", ""])
        lines.extend(
            markdown_table(
                ("Exchange", "Symbol", "Outcome", "Error type", "Detail"),
                [
                    (
                        result.exchange,
                        result.unified_symbol,
                        result.outcome,
                        result.error_type or "",
                        result.error_detail or "",
                    )
                    for result in failures
                ],
            )
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded, scoped historical OHLCV Parquet backfill for the "
            "token-behavior-history line (step 3 of 3)"
        )
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=CANONICAL_SINCE,
        help=(
            "inclusive UTC cutoff for the underlying identity preflight. "
            f"{DATASET_VERSION} is a frozen contract: this must equal the canonical "
            "value or the run is refused, not silently accepted under this version."
        ),
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        default=CANONICAL_UNTIL,
        help=(
            "exclusive UTC cutoff. Frozen: must equal the canonical value (the exact "
            "cutoff the step 2 gate result was computed against) or the run is refused."
        ),
    )
    parser.add_argument(
        "--strategy-version",
        action="append",
        dest="strategy_version",
        help="frozen: must resolve to the canonical strategy version(s) or the run is refused",
    )
    parser.add_argument(
        "--resolver-version",
        default=CANONICAL_RESOLVER_VERSION,
        help="frozen: must equal the canonical value or the run is refused",
    )
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="frozen: must stay false (the canonical value) or the run is refused",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(os.getenv("TOKEN_HISTORY_DATASET_ROOT", "backups/token-history")),
        help="dataset root; each run writes its own immutable run_id subdirectory",
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> tuple[str, bool]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from .replay_repository import ReplayRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for token-history-parquet-dataset")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    strategy_versions = tuple(args.strategy_version or CANONICAL_STRATEGY_VERSIONS)
    ensure_canonical_filters(
        since=args.since,
        until=args.until,
        strategy_versions=strategy_versions,
        resolver_version=args.resolver_version,
        required_horizons=CANONICAL_REQUIRED_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    filters = ReplayFilters(
        since=args.since,
        until=args.until,
        strategy_versions=strategy_versions,
        resolver_version=args.resolver_version,
        required_horizons=CANONICAL_REQUIRED_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    sys.stderr.write("token-history-parquet-dataset: loading decisions\n")
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)

    engine: AsyncEngine = create_async_engine(
        async_database_url(db_url),
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    try:
        sys.stderr.write("token-history-parquet-dataset: running identity preflight\n")
        preflight = await build_token_history_preflight_report(
            dataset,
            filters,
            engine,
            generated_at=generated_at,
            code_revision=args.code_revision,
            working_tree_dirty=args.working_tree_dirty,
        )
    finally:
        await engine.dispose()

    sys.stderr.write("token-history-parquet-dataset: backfilling scoped instruments\n")
    report, output_dir = await build_token_history_dataset_report(
        preflight,
        args.output_root,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    sys.stderr.write(f"token-history-parquet-dataset: wrote {output_dir}\n")
    verify_dataset_on_disk(report, output_dir)
    sys.stderr.write(
        f"token-history-parquet-dataset: verified manifest at {output_dir / 'manifest.json'}\n"
    )
    rendered = render_json(report) if args.format == "json" else render_markdown(report, output_dir)
    return rendered, report.manifest.dataset_ready


def main() -> None:
    args = build_parser().parse_args()
    try:
        rendered, dataset_ready = asyncio.run(_run(args))
    except ManifestVerificationError as exc:
        # The disk does not match what this run believes it wrote: never
        # print a result in this case, since the report object driving it
        # may not reflect reality. This is a distinct, more severe failure
        # than an ordinary dataset_ready=False (a self-reported, expected
        # partial result), so it gets its own exit code.
        sys.stderr.write(f"token-history-parquet-dataset: manifest verification failed: {exc}\n")
        sys.exit(3)
    sys.stdout.write(rendered)
    if not dataset_ready:
        # The manifest and result are written and printed either way, so
        # Make/cron/CI can inspect what happened; but automation must be
        # able to tell "a diagnostic manifest was saved" apart from "a
        # publishable dataset was produced" without parsing output.
        sys.exit(2)


if __name__ == "__main__":
    main()
