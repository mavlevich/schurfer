"""Bounded live-exchange sample for the token-behavior-history feasibility line.

This is step 2 of 3 (see `token_history_identity_preflight_report.py`'s module
docstring for the full rollout): for a small, deterministic slice of the
identity-ready instruments step 1 found, actually fetch their OHLCV history
and record how the real exchange behaved (page sizes, latency, retention
limits, gaps), before committing to a full fetch across every ready
instrument in step 3.

Discovery-only diagnostic, same status as `orderflow_endpoint_sensitivity_
report.py`: this cannot change production strategy, score, or any registered
contract, and does not by itself authorize step 3. A human reads the numbers
here first.

Reproducibility is split in two, and that split is the whole point of this
report:

* Instrument SELECTION is fully deterministic and reproducible: it is a pure
  function of the same DB snapshot step 1 already fingerprinted (this report
  re-runs step 1's own query and records both step 1's fingerprint and its
  own selection-specific fingerprint).
* The FETCH RESULTS are not reproducible: they are one live measurement of
  real exchange behavior at run time, and re-running this report against
  real exchanges tomorrow can legitimately produce different page sizes,
  latency, or gaps even for the exact same selected instruments. That is
  expected, and is the reason this report exists; it must never be treated
  as a frozen contract the way the fully DB-driven reports in this project
  are.

Sampling is deliberately non-representative: it exists to exercise pagination
edge cases (long vs. short history, several exchanges), not to estimate how
often real exchange behavior looks like this across the whole instrument
population.

Each selected instrument's onboarded_at and decision_ts come from step 1's
own already-fetched `IdentityRecord` rows, not a second independent query
against the mutable `pump_event_sources` table: that table can change
between two queries, and a second read could disagree with the snapshot step
1's fingerprint already covers.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .exchange_registry import EXCHANGE_FACTORIES
from .ohlcv import (
    IncompleteFetchError,
    PageFetchObservation,
    ceil_to_timeframe,
    fetch_symbol_candles,
)
from .outcome_repository import async_database_url
from .outcomes import RESOLVER_VERSION
from .replay import DEFAULT_REPLAY_HORIZONS, ReplayFilters, build_replay_dataset
from .reporting import (
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    resolve_report_until,
)
from .token_history_identity_preflight_report import (
    TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE,
    TOKEN_HISTORY_PREFLIGHT_STRATEGY_VERSIONS,
    IdentityRecord,
    InstrumentSummary,
    ReadinessRow,
    TokenHistoryPreflightManifest,
    TokenHistoryPreflightReport,
    _history_window_bucket,
    build_token_history_preflight_report,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

TOKEN_HISTORY_LIVE_SAMPLE_REPORT_VERSION = "token_history_ohlcv_sample_report_v1"  # noqa: S105
_DAY_MS = 24 * 60 * 60 * 1000
_SAMPLE_TIMEFRAME = "1d"
# Bounded independently of how old an instrument is: step 2 exists to
# exercise real pagination behavior across a range of window sizes, not to
# fetch a full history (that is step 3's job, only if this comes back
# clean). Without this bound, an old instrument's [onboarded_at, decision_ts)
# window could span years of daily candles for what is meant to be a quick,
# polite diagnostic probe.
MAX_SAMPLE_WINDOW_DAYS = 365
# Safety net on how many INSTRUMENTS this report will ever sample, since the
# (exchange, bucket) selection below is unbounded in principle as more
# exchanges or history-length buckets are added later. At today's data (5
# exchanges with any identity_ready instrument, 3 buckets) this never
# triggers; it exists for when that changes. This does not bound the number
# of live API calls: a single instrument can still take many pages. That
# bound comes from ohlcv.py's own per-fetch page cap (_MAX_PAGES_HARD_CAP),
# independently of this constant.
MAX_SAMPLE_INSTRUMENTS = 24
# Deterministic priority order for which (exchange, bucket) picks survive
# MAX_SAMPLE_INSTRUMENTS truncation: richest history first, across ALL
# exchanges, since a long history exercises more pages than a short one.
# Exchange name is only the tie-break within the same bucket rank, not the
# primary sort key: sorting by exchange first would let an early-alphabet
# exchange's short-history buckets survive ahead of another exchange's
# long-history bucket, which is backwards for what this priority is for.
_BUCKET_ORDER = ("at_least_365d", "at_least_90d", "under_90d")


@dataclass(frozen=True)
class SampleCandidate:
    exchange: str
    bucket: str
    identity_key: str
    unified_symbol: str
    base: str
    available_history_days: int
    pump_event_id: int
    decision_ts: datetime
    onboarded_at: datetime
    selected: bool
    exclusion_reason: str | None


@dataclass(frozen=True)
class PageSizeStats:
    count: int
    min_bars: int
    median_bars: float
    max_bars: int


@dataclass(frozen=True)
class LatencyStats:
    count: int
    min_seconds: float
    median_seconds: float
    max_seconds: float


@dataclass(frozen=True)
class GapAnalysis:
    """Gaps against a full-UTC-day grid, not the raw request window: the
    first and last calendar day of [start_ms, end_ms) are very often partial
    (onboarding or the decision itself rarely lands exactly at midnight UTC),
    and a partial edge day is not a gap, it is just not a full bar. The grid
    here matches exactly what `closed_candles` already keeps, so a bar this
    report calls "expected" is one `fetch_symbol_candles` could actually have
    returned."""

    expected_full_days: int
    returned_days: int
    leading_missing_days: int
    internal_missing_dates: tuple[str, ...]
    trailing_missing_days: int
    # Descriptive, not diagnostic: a leading gap is consistent with a
    # retention limit, but equally with the instrument's real listing date
    # being later than the recorded onboarding, imprecise metadata, or an API
    # anomaly. This flag says only that the gap exists, not why.
    possible_retention_or_listing_gap: bool


@dataclass(frozen=True)
class InstrumentSampleResult:
    exchange: str
    bucket: str
    identity_key: str
    unified_symbol: str
    base: str
    onboarded_at: datetime
    decision_ts: datetime
    start_ms: int
    end_ms: int
    # Coverage outcomes (fetch_symbol_candles returned normally):
    #   "completed"            every expected full day was returned
    #   "partial_coverage"     at least one day returned, but some are missing
    #   "no_data"              full days were expected but zero were returned
    #   "no_full_days_expected" the window is too short to contain a full 1d bar
    # Operational-failure outcomes (fetch_symbol_candles never returned):
    #   "incomplete_fetch_error" | "fetch_exception" | "unsupported_exchange"
    #   | "exchange_client_init_failed"
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


@dataclass(frozen=True)
class TokenHistoryLiveSampleManifest:
    report_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    timeframe: str
    # The whole preflight manifest, not just its input_fingerprint: this
    # report's own manifest must be self-contained enough on its own to
    # recover since/until, strategy_versions, resolver_version,
    # required_horizons, fallback_allowed, and the preflight report_version,
    # without needing to go find the step 1 run that produced it. Embedding
    # the object also means it can never drift out of sync with whatever
    # fields step 1's own manifest gains later.
    preflight_manifest: TokenHistoryPreflightManifest
    sample_fingerprint: str
    max_sample_instruments: int
    max_sample_window_days: int
    sample_representativeness: str = "non_representative_deterministic_diagnostic_sample"
    interpretation: str = "discovery_only_no_strategy_change"


@dataclass(frozen=True)
class LiveSampleRun:
    results: tuple[InstrumentSampleResult, ...]
    # Cross-cutting operational issues (currently: a client that failed to
    # close cleanly) that do not belong to any single instrument's result,
    # since the client is shared across every instrument on that exchange.
    operational_warnings: tuple[str, ...]


@dataclass(frozen=True)
class TokenHistoryLiveSampleReport:
    manifest: TokenHistoryLiveSampleManifest
    candidates_considered: int
    candidates_selected: int
    candidates_excluded: int
    exclusion_reasons: tuple[ReadinessRow, ...]
    candidates: tuple[SampleCandidate, ...]
    results: tuple[InstrumentSampleResult, ...]
    operational_warnings: tuple[str, ...]


def sample_window_ms(onboarded_at: datetime, decision_ts: datetime) -> tuple[int, int]:
    """[start_ms, end_ms) bounded to at most MAX_SAMPLE_WINDOW_DAYS, even for
    a very old instrument."""
    bounded_start = max(onboarded_at, decision_ts - timedelta(days=MAX_SAMPLE_WINDOW_DAYS))
    start_ms = int(bounded_start.timestamp() * 1000)
    end_ms = int(decision_ts.timestamp() * 1000)
    return start_ms, end_ms


def _bucket_candidates(
    instruments: tuple[InstrumentSummary, ...],
) -> dict[tuple[str, str], list[InstrumentSummary]]:
    grouped: dict[tuple[str, str], list[InstrumentSummary]] = defaultdict(list)
    for instrument in instruments:
        bucket = _history_window_bucket(instrument.max_available_history_days)
        grouped[(instrument.exchange, bucket)].append(instrument)
    return grouped


def _pick_nearest_median(instruments: list[InstrumentSummary]) -> InstrumentSummary:
    """Deterministic regardless of input order: the instrument whose
    max_available_history_days is closest to the bucket's median, tied-broken
    by identity_key. identity_key is unique within a group, so this has
    exactly one minimum."""
    bucket_median = statistics.median(
        instrument.max_available_history_days for instrument in instruments
    )

    def sort_key(instrument: InstrumentSummary) -> tuple[float, str]:
        return (abs(instrument.max_available_history_days - bucket_median), instrument.identity_key)

    return min(instruments, key=sort_key)


def _representative_record(
    records: tuple[IdentityRecord, ...],
    exchange: str,
    identity_key: str,
) -> IdentityRecord:
    """The identity_ready record for this exact instrument to source
    onboarded_at from, tie-broken by (decision_ts, pump_event_id) so the
    choice is deterministic regardless of input order. pump_event_id is
    unique per record (one baseline decision per eligible episode), so this
    tie-break never has a genuine tie."""
    matching = [
        record
        for record in records
        if record.readiness == "identity_ready"
        and record.exchange == exchange
        and record.identity_key == identity_key
    ]
    if not matching:
        raise ValueError(f"no identity_ready record found for {exchange}/{identity_key}")
    return max(matching, key=lambda record: (record.decision_ts, record.pump_event_id))


def _sample_fingerprint(candidates: tuple[SampleCandidate, ...]) -> str:
    payload = [
        {
            "exchange": candidate.exchange,
            "bucket": candidate.bucket,
            "identity_key": candidate.identity_key,
            "unified_symbol": candidate.unified_symbol,
            "base": candidate.base,
            "available_history_days": candidate.available_history_days,
            "pump_event_id": candidate.pump_event_id,
            "decision_ts": candidate.decision_ts.isoformat(),
            "onboarded_at": candidate.onboarded_at.isoformat(),
            "selected": candidate.selected,
            "exclusion_reason": candidate.exclusion_reason,
        }
        for candidate in candidates
    ]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def select_sample_candidates(
    instruments: tuple[InstrumentSummary, ...],
    records: tuple[IdentityRecord, ...],
) -> tuple[SampleCandidate, ...]:
    """One instrument per (exchange, history bucket) that has at least one
    ready instrument, deterministically picked and ordered, then truncated to
    MAX_SAMPLE_INSTRUMENTS. Truncation excludes the lowest-priority picks
    (see _BUCKET_ORDER) with an explicit `sample_budget_exhausted` reason
    rather than silently dropping them."""
    grouped = _bucket_candidates(instruments)
    picks: list[tuple[str, str, InstrumentSummary]] = [
        (exchange, bucket, _pick_nearest_median(group))
        for (exchange, bucket), group in grouped.items()
    ]
    # Deterministic global order for the sample-budget cap, independent of
    # dict iteration order: richest history bucket first across ALL
    # exchanges, exchange name only breaks ties within the same bucket rank.
    picks.sort(key=lambda item: (_BUCKET_ORDER.index(item[1]), item[0]))

    candidates = []
    for index, (exchange, bucket, instrument) in enumerate(picks):
        record = _representative_record(records, exchange, instrument.identity_key)
        if record.onboarded_at is None:
            raise ValueError(
                f"identity_ready record for {exchange}/{instrument.identity_key} is "
                "missing onboarded_at; this should be impossible for that readiness"
            )
        selected = index < MAX_SAMPLE_INSTRUMENTS
        candidates.append(
            SampleCandidate(
                exchange=exchange,
                bucket=bucket,
                identity_key=instrument.identity_key,
                unified_symbol=instrument.unified_symbol,
                base=instrument.base,
                available_history_days=instrument.max_available_history_days,
                pump_event_id=record.pump_event_id,
                decision_ts=record.decision_ts,
                onboarded_at=record.onboarded_at,
                selected=selected,
                exclusion_reason=None if selected else "sample_budget_exhausted",
            )
        )
    return tuple(candidates)


def analyze_gaps(candle_ts_ms: tuple[int, ...], start_ms: int, end_ms: int) -> GapAnalysis:
    first_full_bar_ms = ceil_to_timeframe(start_ms, _DAY_MS)
    expected = list(range(first_full_bar_ms, end_ms - _DAY_MS + 1, _DAY_MS))
    expected_set = set(expected)
    returned_set = set(candle_ts_ms) & expected_set
    first_returned = min(returned_set) if returned_set else None
    last_returned = max(returned_set) if returned_set else None
    if first_returned is None or last_returned is None:
        leading_missing = len(expected)
        internal_missing: tuple[str, ...] = ()
        trailing_missing = 0
    else:
        leading_missing = sum(1 for ts in expected if ts < first_returned)
        internal_missing = tuple(
            datetime.fromtimestamp(ts / 1000, tz=UTC).date().isoformat()
            for ts in expected
            if first_returned <= ts <= last_returned and ts not in returned_set
        )
        trailing_missing = sum(1 for ts in expected if ts > last_returned)
    return GapAnalysis(
        expected_full_days=len(expected),
        returned_days=len(returned_set),
        leading_missing_days=leading_missing,
        internal_missing_dates=internal_missing,
        trailing_missing_days=trailing_missing,
        possible_retention_or_listing_gap=leading_missing > 0,
    )


def _size_stats(values: list[int]) -> PageSizeStats | None:
    if not values:
        return None
    return PageSizeStats(
        count=len(values),
        min_bars=min(values),
        median_bars=float(statistics.median(values)),
        max_bars=max(values),
    )


def _raw_page_size_stats(observations: tuple[PageFetchObservation, ...]) -> PageSizeStats | None:
    """Only over successful calls: an empty retry's raw size is 0 by
    definition and would otherwise drag min_bars to 0 for every instrument
    that ever needed a single retry, mixing retry behavior into a number that
    is supposed to describe how big the exchange's real pages are."""
    return _size_stats(
        [
            observation.raw_bar_count
            for observation in observations
            if observation.outcome == "success" and observation.raw_bar_count is not None
        ]
    )


def _normalized_page_size_stats(
    observations: tuple[PageFetchObservation, ...],
) -> PageSizeStats | None:
    """Same success-only restriction as _raw_page_size_stats, and kept as a
    separate stat (not derived from it) so a gap between the two remains
    visible: it is exactly what filtered_or_deduplicated_rows_total also
    reports, from a distribution angle instead of a single total."""
    return _size_stats(
        [
            observation.normalized_bar_count
            for observation in observations
            if observation.outcome == "success" and observation.normalized_bar_count is not None
        ]
    )


def _call_outcome_counts(observations: tuple[PageFetchObservation, ...]) -> tuple[int, int, int]:
    empty = sum(1 for observation in observations if observation.outcome == "empty")
    timeout = sum(1 for observation in observations if observation.outcome == "timeout")
    error = sum(1 for observation in observations if observation.outcome == "error")
    return empty, timeout, error


def _filtered_or_deduplicated_rows_total(
    observations: tuple[PageFetchObservation, ...],
) -> int:
    """Count rows removed by normalization without claiming they were all
    malformed. `normalize_candles` both rejects invalid rows and deduplicates
    valid rows with the same timestamp, so raw-minus-normalized cannot
    distinguish those two causes."""
    total = 0
    for observation in observations:
        if (
            observation.outcome == "success"
            and observation.raw_bar_count is not None
            and observation.normalized_bar_count is not None
        ):
            total += max(0, observation.raw_bar_count - observation.normalized_bar_count)
    return total


def _latency_stats(observations: tuple[PageFetchObservation, ...]) -> LatencyStats | None:
    latencies = [observation.latency_seconds for observation in observations]
    if not latencies:
        return None
    return LatencyStats(
        count=len(latencies),
        min_seconds=min(latencies),
        median_seconds=float(statistics.median(latencies)),
        max_seconds=max(latencies),
    )


def _page_diagnostics_fields(observations: tuple[PageFetchObservation, ...]) -> dict[str, Any]:
    """Every InstrumentSampleResult field derivable from the raw page
    observations alone, excluding api_calls/successful_pages: those two are
    sometimes sourced from IncompleteFetchError's own counters instead (see
    _fetch_instrument_sample), which are authoritative for that case."""
    empty_calls, timeout_calls, error_calls = _call_outcome_counts(observations)
    return {
        "empty_calls": empty_calls,
        "timeout_calls": timeout_calls,
        "error_calls": error_calls,
        "filtered_or_deduplicated_rows_total": _filtered_or_deduplicated_rows_total(observations),
        "raw_page_size_stats": _raw_page_size_stats(observations),
        "normalized_page_size_stats": _normalized_page_size_stats(observations),
        "latency_stats": _latency_stats(observations),
    }


def _coverage_outcome(gap: GapAnalysis) -> str:
    """Classifies a normally-returned (non-exception) fetch. "completed" is
    reserved for genuine full coverage: a fetch_symbol_candles call that
    silently returned a partial result (empty page after retries, or a
    stalled cursor, both pre-existing possibilities the fix in ohlcv.py
    deliberately did not change) must never be reported as "completed" just
    because it did not raise."""
    if gap.expected_full_days == 0:
        return "no_full_days_expected"
    if gap.returned_days == 0:
        return "no_data"
    if gap.leading_missing_days or gap.internal_missing_dates or gap.trailing_missing_days:
        return "partial_coverage"
    return "completed"


def _base_result_fields(candidate: SampleCandidate, start_ms: int, end_ms: int) -> dict[str, Any]:
    return {
        "exchange": candidate.exchange,
        "bucket": candidate.bucket,
        "identity_key": candidate.identity_key,
        "unified_symbol": candidate.unified_symbol,
        "base": candidate.base,
        "onboarded_at": candidate.onboarded_at,
        "decision_ts": candidate.decision_ts,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


def _no_calls_result(
    candidate: SampleCandidate, *, outcome: str, error_detail: str
) -> InstrumentSampleResult:
    """Shared shape for outcomes where no API call was ever made: unsupported
    exchange, or the exchange client's own constructor failing."""
    start_ms, end_ms = sample_window_ms(candidate.onboarded_at, candidate.decision_ts)
    return InstrumentSampleResult(
        **_base_result_fields(candidate, start_ms, end_ms),
        outcome=outcome,
        error_type=None,
        error_detail=error_detail,
        api_calls=0,
        successful_pages=0,
        gap_analysis=None,
        **_page_diagnostics_fields(()),
    )


def _unsupported_exchange_result(candidate: SampleCandidate) -> InstrumentSampleResult:
    return _no_calls_result(
        candidate,
        outcome="unsupported_exchange",
        error_detail=f"no exchange client factory registered for '{candidate.exchange}'",
    )


def _exchange_client_init_failed_result(
    candidate: SampleCandidate, exc: Exception
) -> InstrumentSampleResult:
    start_ms, end_ms = sample_window_ms(candidate.onboarded_at, candidate.decision_ts)
    return InstrumentSampleResult(
        **_base_result_fields(candidate, start_ms, end_ms),
        outcome="exchange_client_init_failed",
        error_type=type(exc).__name__,
        error_detail=str(exc),
        api_calls=0,
        successful_pages=0,
        gap_analysis=None,
        **_page_diagnostics_fields(()),
    )


async def _fetch_instrument_sample(
    exchange_client: Any,
    candidate: SampleCandidate,
) -> InstrumentSampleResult:
    start_ms, end_ms = sample_window_ms(candidate.onboarded_at, candidate.decision_ts)
    observations: list[PageFetchObservation] = []
    base_fields = _base_result_fields(candidate, start_ms, end_ms)
    try:
        candles = await fetch_symbol_candles(
            exchange_client,
            candidate.unified_symbol,
            start_ms,
            end_ms,
            timeframe=_SAMPLE_TIMEFRAME,
            timeframe_ms=_DAY_MS,
            on_page=observations.append,
        )
    except IncompleteFetchError as exc:
        return InstrumentSampleResult(
            **base_fields,
            outcome="incomplete_fetch_error",
            error_type=type(exc).__name__,
            error_detail=str(exc),
            api_calls=exc.api_calls,
            successful_pages=exc.successful_pages,
            gap_analysis=None,
            **_page_diagnostics_fields(tuple(observations)),
        )
    except Exception as exc:
        # A single instrument's fetch must never abort the whole sample: the
        # next instrument on this exchange, and every other exchange, still
        # needs to run.
        return InstrumentSampleResult(
            **base_fields,
            outcome="fetch_exception",
            error_type=type(exc).__name__,
            error_detail=str(exc),
            api_calls=len(observations),
            successful_pages=sum(1 for o in observations if o.outcome == "success"),
            gap_analysis=None,
            **_page_diagnostics_fields(tuple(observations)),
        )
    gap_analysis = analyze_gaps(tuple(c.ts_ms for c in candles), start_ms, end_ms)
    return InstrumentSampleResult(
        **base_fields,
        outcome=_coverage_outcome(gap_analysis),
        error_type=None,
        error_detail=None,
        api_calls=len(observations),
        successful_pages=sum(1 for o in observations if o.outcome == "success"),
        gap_analysis=gap_analysis,
        **_page_diagnostics_fields(tuple(observations)),
    )


# Outcomes where fetch_symbol_candles never returned normally: distinct from
# the coverage outcomes (completed/partial_coverage/no_data/
# no_full_days_expected), which all come from a normal return and are fully
# explained by the Gaps column in the main results table instead.
_OPERATIONAL_FAILURE_OUTCOMES = frozenset(
    {
        "incomplete_fetch_error",
        "fetch_exception",
        "unsupported_exchange",
        "exchange_client_init_failed",
    }
)


async def run_live_sample(candidates: tuple[SampleCandidate, ...]) -> LiveSampleRun:
    """Sequential, one exchange client alive at a time: politeness over speed
    for a bounded, occasional diagnostic sample. Each exchange's client is
    built once, used for every selected instrument on that exchange in turn,
    and always closed before moving to the next exchange, including when a
    fetch fails, so one bad instrument cannot leak a live client or block any
    other exchange's turn. Both a factory() failure and a close() failure are
    contained to their own exchange: neither discards results already
    collected for a previous exchange, nor stops the run from trying the
    next one."""
    by_exchange: dict[str, list[SampleCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.selected:
            by_exchange[candidate.exchange].append(candidate)

    results: list[InstrumentSampleResult] = []
    warnings: list[str] = []
    for exchange in sorted(by_exchange):
        exchange_candidates = by_exchange[exchange]
        factory = EXCHANGE_FACTORIES.get(exchange)
        if factory is None:
            results.extend(_unsupported_exchange_result(c) for c in exchange_candidates)
            continue
        try:
            client = factory()
        except Exception as exc:
            results.extend(_exchange_client_init_failed_result(c, exc) for c in exchange_candidates)
            continue
        try:
            for candidate in exchange_candidates:
                results.append(await _fetch_instrument_sample(client, candidate))
        finally:
            try:
                await client.close(clean_instance_data=True)
            except Exception as exc:
                # A close failure must not lose the results already collected
                # for this exchange, nor block any other exchange's turn: a
                # leaked connection is a lesser problem than discarding a
                # completed live sample. Surfaced structurally, not just to
                # stderr, so it shows up in the archived report too.
                warnings.append(
                    f"failed to close the {exchange} exchange client cleanly: "
                    f"{type(exc).__name__}: {exc}"
                )
    return LiveSampleRun(results=tuple(results), operational_warnings=tuple(warnings))


async def build_token_history_live_sample_report(
    preflight: TokenHistoryPreflightReport,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> TokenHistoryLiveSampleReport:
    candidates = select_sample_candidates(preflight.instruments, preflight.records)
    excluded = tuple(candidate for candidate in candidates if not candidate.selected)
    exclusion_counts = Counter(
        candidate.exclusion_reason for candidate in excluded if candidate.exclusion_reason
    )
    run = await run_live_sample(candidates)
    return TokenHistoryLiveSampleReport(
        manifest=TokenHistoryLiveSampleManifest(
            report_version=TOKEN_HISTORY_LIVE_SAMPLE_REPORT_VERSION,
            code_revision=normalize_code_revision(code_revision),
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            timeframe=_SAMPLE_TIMEFRAME,
            preflight_manifest=preflight.manifest,
            sample_fingerprint=_sample_fingerprint(candidates),
            max_sample_instruments=MAX_SAMPLE_INSTRUMENTS,
            max_sample_window_days=MAX_SAMPLE_WINDOW_DAYS,
        ),
        candidates_considered=len(candidates),
        candidates_selected=len(candidates) - len(excluded),
        candidates_excluded=len(excluded),
        exclusion_reasons=tuple(
            ReadinessRow(reason, count)
            for reason, count in sorted(
                exclusion_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        candidates=candidates,
        results=run.results,
        operational_warnings=run.operational_warnings,
    )


def render_json(report: TokenHistoryLiveSampleReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def _gap_cell(gap: GapAnalysis | None) -> str:
    if gap is None:
        return "n/a"
    flag = " (possible retention/listing gap)" if gap.possible_retention_or_listing_gap else ""
    return (
        f"{gap.returned_days}/{gap.expected_full_days} days, "
        f"leading={gap.leading_missing_days}, "
        f"internal={len(gap.internal_missing_dates)}, "
        f"trailing={gap.trailing_missing_days}{flag}"
    )


def _page_size_cell(stats: PageSizeStats | None) -> str:
    if stats is None:
        return "n/a"
    return f"{stats.min_bars}/{stats.median_bars:.0f}/{stats.max_bars} (n={stats.count})"


def _call_outcomes_cell(result: InstrumentSampleResult) -> str:
    return (
        f"empty={result.empty_calls}, timeout={result.timeout_calls}, "
        f"error={result.error_calls}, "
        f"filtered_or_deduplicated_rows={result.filtered_or_deduplicated_rows_total}"
    )


def _latency_cell(stats: LatencyStats | None) -> str:
    if stats is None:
        return "n/a"
    return f"{stats.min_seconds:.2f}/{stats.median_seconds:.2f}/{stats.max_seconds:.2f}s"


def render_markdown(report: TokenHistoryLiveSampleReport) -> str:
    manifest = report.manifest
    preflight = manifest.preflight_manifest
    lines = [
        "# Token-Behavior-History Live OHLCV Sample (Step 2 of 3)",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Timeframe: `{manifest.timeframe}`",
        f"Sample fingerprint: `{manifest.sample_fingerprint}`",
        f"Max sample instruments: {manifest.max_sample_instruments}",
        f"Max sample window: {manifest.max_sample_window_days} days",
        "",
        f"Preflight report version: `{preflight.report_version}`",
        f"Preflight input fingerprint: `{preflight.input_fingerprint}`",
        (
            f"Preflight scope: {preflight.dataset_since.isoformat()} <= decision < "
            f"{preflight.dataset_until_exclusive.isoformat()}"
        ),
        f"Preflight strategy versions: {', '.join(preflight.strategy_versions)}",
        f"Preflight resolver version: `{preflight.resolver_version}`",
        f"Preflight required horizons: {preflight.required_horizons}",
        f"Preflight fallback allowed: {'yes' if preflight.fallback_allowed else 'no'}",
        "",
        (
            f"> {manifest.sample_representativeness}. {manifest.interpretation}. "
            "Instrument selection is deterministic and reproducible from the same "
            "DB snapshot; the fetch results below are one live measurement and are "
            "NOT expected to reproduce bit-for-bit on a re-run."
        ),
        "",
    ]
    if report.operational_warnings:
        lines.extend(["## Operational warnings", ""])
        lines.extend(f"- {warning}" for warning in report.operational_warnings)
        lines.append("")
    lines.extend(["## Candidate funnel", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Candidates considered", report.candidates_considered),
                ("Candidates selected", report.candidates_selected),
                ("Candidates excluded", report.candidates_excluded),
            ],
        )
    )
    lines.extend(["", "## Exclusion reasons", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Count"),
            [(row.readiness, row.count) for row in report.exclusion_reasons],
        )
    )
    lines.extend(["", "## Selected instruments and live-fetch results", ""])
    lines.extend(
        markdown_table(
            (
                "Exchange",
                "Bucket",
                "Symbol",
                "Outcome",
                "API calls",
                "Pages",
                "Raw page size (min/med/max)",
                "Normalized page size (min/med/max)",
                "Latency s (min/med/max)",
                "Call outcomes",
                "Gaps",
            ),
            [
                (
                    result.exchange,
                    result.bucket,
                    result.unified_symbol,
                    result.outcome,
                    result.api_calls,
                    result.successful_pages,
                    _page_size_cell(result.raw_page_size_stats),
                    _page_size_cell(result.normalized_page_size_stats),
                    _latency_cell(result.latency_stats),
                    _call_outcomes_cell(result),
                    _gap_cell(result.gap_analysis),
                )
                for result in report.results
            ],
        )
    )
    failures = [
        result for result in report.results if result.outcome in _OPERATIONAL_FAILURE_OUTCOMES
    ]
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
        description=("Bounded live-exchange OHLCV sample for the token-behavior-history line")
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE,
        help="inclusive UTC cutoff for the underlying identity preflight",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument(
        "--strategy-version",
        action="append",
        dest="strategy_version",
        help="repeatable; defaults to the registered pump-short strategy version(s)",
    )
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="allow fallback outcomes in a separately identified sensitivity run",
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from sqlalchemy.ext.asyncio import create_async_engine

    from .replay_repository import ReplayRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for token-history-ohlcv-sample-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    until = resolve_report_until(
        args.until,
        generated_at,
        cohort_start=TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE,
        report_label="token-history-ohlcv-sample",
    )
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or TOKEN_HISTORY_PREFLIGHT_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    sys.stderr.write("token-history-ohlcv-sample: loading decisions\n")
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
        sys.stderr.write("token-history-ohlcv-sample: running identity preflight\n")
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

    sys.stderr.write("token-history-ohlcv-sample: fetching live sample\n")
    report = await build_token_history_live_sample_report(
        preflight,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
