"""Read-only, DB-only preflight for the token-behavior-history feasibility
line (see ROADMAP.md). Answers exactly one question, with zero exchange
calls and zero new dependencies: how many replay-eligible baseline decisions
even have a usable, already-recorded instrument identity to fetch history
for, and how many distinct exact instruments that actually is.

This is step 1 of 3 in the agreed rollout:
1. This report: DB-only identity preflight (no CCXT, no new dependencies).
2. A bounded live sample against a handful of instruments per exchange, to
   measure real page sizes, latency, retention limits, and gaps before
   committing to fetching every instrument.
3. A full feasibility fetch across unique exact instruments, only if step 2
   comes back clean.

Identity reuse, not new capture: `app.pump_event_sources` already records,
per (pump_event_id, exchange), the exact-market identity fields the Gate
identity work established. This report joins replay-eligible baseline
decisions to that table on (pump_event_id, exchange) and classifies each
into a fail-closed readiness bucket, reusing the exact same identity
discipline `source_lead.py`'s `_identity_reason` already established
(identity_conflict, identity_key/unified_symbol presence, market_type must
be "swap", base/quote/settle asset match, naive timestamps fail closed
rather than being guessed as UTC). It never queries an exchange, never
computes a feature, and never touches production score settings.

Important caveat, recorded honestly in the manifest as `identity_
completeness_basis`: this table is the STABILIZED identity as of when this
report runs, not necessarily what was already confirmed at decision time.
`identity_key`/`market_id`/`onboarded_at` are filled in incrementally via
COALESCE on repeated observations (see persistence.py's UPSERT), so a
decision made before an instrument's identity fully stabilized may show as
"ready" here even though that information was not yet settled back then.
That is acceptable for this feasibility read (it is not a forward-looking
trading claim), but must never be reported as point-in-time-confirmed. The
same mutability means the fingerprint must cover the fetched identity rows,
not just the decision dataset: see `_identity_fingerprint`.

`available_history_days` (decision.ts minus onboarded_at, floored at 0) is
reported only as an upper bound on what a later live fetch could possibly
retrieve. It is not itself evidence that the exchange's OHLCV endpoint
actually retains that much history, or that the instrument's price data is
otherwise usable. That question is exactly what step 2 exists to answer.

Ready decisions are not the same count as ready instruments: several
decisions can share one exact instrument (repeat pumps on the same token).
The instrument-level summary below is what step 2 picks buckets and
candidates from; step 2 then resolves each chosen instrument back to one of
this report's own `IdentityRecord` rows (by exchange and identity_key) to
read its `onboarded_at`, rather than re-querying `pump_event_sources`
separately, since that table is mutable and a second, later query could
legitimately disagree with the snapshot this report's fingerprint already
covers.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import median
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEventSource
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from .decision_quality import SCORE_THRESHOLD_BASELINE_POLICY, select_score_policy
from .episode_replay import CONFIRMATION_COHORT_START, PROTOCOL_VERSION
from .outcome_repository import async_database_url
from .outcomes import RESOLVER_VERSION
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    FOUNDATION_VERSION,
    QUERY_VERSION,
    ReplayDataset,
    ReplayFilters,
    build_replay_dataset,
)
from .reporting import (
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    resolve_report_until,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.sql import Select

    from .replay import ReplayDecision

TOKEN_HISTORY_PREFLIGHT_REPORT_VERSION = "token_history_identity_preflight_report_v1"  # noqa: S105
TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE = CONFIRMATION_COHORT_START
TOKEN_HISTORY_PREFLIGHT_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
# Reporting thresholds only, not eligibility gates: a decision below these is
# still counted "ready", just bucketed separately by how much of the
# eventually-desired window it could possibly cover.
HISTORY_WINDOW_DAYS = (90, 365)


@dataclass(frozen=True)
class IdentityRecord:
    pump_event_id: int
    base: str
    exchange: str
    decision_ts: datetime
    readiness: str
    identity_key: str | None
    unified_symbol: str | None
    available_history_days: int | None
    # Populated only when readiness == "identity_ready", same discipline as
    # available_history_days: this is the exact input that number was derived
    # from, and step 2 needs it (not just the derived day count) to build its
    # own fetch window from the SAME DB snapshot this report already read,
    # instead of re-querying pump_event_sources later against what could by
    # then be a different (mutable) row.
    onboarded_at: datetime | None


def identity_readiness(
    source: PumpEventSource | None,
    base: str,
    decision_ts: datetime,
) -> tuple[str, int | None]:
    """Pure, fail-closed classification (no I/O), reusing the exact identity
    discipline `source_lead.py`'s `_identity_reason` already established
    (identity_conflict, identity_key/unified_symbol presence, market_type
    must be "swap", base/quote/settle asset match, naive timestamps fail
    closed rather than being guessed as UTC). `market_id` alone is never
    sufficient here: `identity_key` is the canonical identity string this
    project already treats as authoritative, and `unified_symbol` is what
    a later `fetch_symbol_candles` call would actually need. Every branch
    that is not `identity_ready` deliberately returns no
    `available_history_days`: a number attached to an unusable identity
    would invite exactly the kind of silent misuse this report exists to
    prevent."""
    if source is None:
        return "no_source_row", None
    if source.identity_conflict:
        return "identity_conflict", None
    if not source.identity_key or not source.unified_symbol:
        return "missing_identity", None
    if source.market_type != "swap":
        return "not_swap", None
    if (source.base_asset or "").casefold() != base.casefold():
        return "base_mismatch", None
    if (source.quote_asset or "").upper() != "USDT":
        return "quote_not_usdt", None
    if (source.settle_asset or "").upper() != "USDT":
        return "settle_not_usdt", None
    if source.onboarded_at is None:
        return "missing_onboarded_at", None
    if source.onboarded_at.utcoffset() is None:
        # A naive timestamp from a loosely-typed driver must not be guessed
        # as UTC: fail closed and let a human confirm it, same discipline
        # as source_lead.py's "naive_timestamp" reason.
        return "invalid_onboarded_at_timezone", None
    if source.onboarded_at >= decision_ts:
        # Recorded onboarding is not before the decision itself: the
        # instrument cannot have any usable pre-decision history at all.
        return "onboarded_at_after_decision", None
    available_days = (decision_ts - source.onboarded_at).days
    return "identity_ready", available_days


def _record_onboarded_at(readiness: str, source: PumpEventSource | None) -> datetime | None:
    """Same discipline as `available_history_days`: only a record whose
    identity resolved all the way to `identity_ready` carries the raw
    `onboarded_at` it was computed from. Step 2 needs this exact value (not
    just the derived day count) to build its fetch window from this report's
    own DB snapshot instead of re-reading the mutable `pump_event_sources`
    row later, when it could legitimately have changed."""
    if readiness != "identity_ready" or source is None:
        return None
    return source.onboarded_at


def _history_window_bucket(available_days: int) -> str:
    floor = 0
    for window in sorted(HISTORY_WINDOW_DAYS):
        if available_days >= window:
            floor = window
    if floor == 0:
        return "under_90d"
    return f"at_least_{floor}d"


@dataclass(frozen=True)
class ReadinessRow:
    readiness: str
    count: int


@dataclass(frozen=True)
class HistoryWindowRow:
    bucket: str
    count: int


@dataclass(frozen=True)
class ExchangeReadinessRow:
    exchange: str
    ready_decisions: int
    unique_instruments: int


@dataclass(frozen=True)
class InstrumentSummary:
    exchange: str
    identity_key: str
    unified_symbol: str
    base: str
    decisions: int
    min_available_history_days: int
    max_available_history_days: int


@dataclass(frozen=True)
class TokenHistoryPreflightManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    decision_fingerprint: str
    identity_fingerprint: str
    input_fingerprint: str
    strategy_versions: tuple[str, ...]
    resolver_version: str
    required_horizons: tuple[int, ...]
    fallback_allowed: bool
    history_window_days: tuple[int, ...]
    identity_completeness_basis: str = "stabilized_as_of_report_generation_not_decision_time"
    report_scope: str = "db_only_identity_preflight_no_exchange_calls_no_score_change"


@dataclass(frozen=True)
class TokenHistoryPreflightReport:
    manifest: TokenHistoryPreflightManifest
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[ReadinessRow, ...]
    replay_eligible_baseline_decisions: int
    readiness_distribution: tuple[ReadinessRow, ...]
    readiness_by_exchange: tuple[ExchangeReadinessRow, ...]
    unique_ready_instruments: int
    history_window_distribution: tuple[HistoryWindowRow, ...]
    median_available_history_days: float | None
    instruments: tuple[InstrumentSummary, ...]
    records: tuple[IdentityRecord, ...]


def pump_event_sources_statement(event_ids: list[int]) -> Select[Any]:
    """Pure query builder (no I/O), testable against compiled SQL without a
    live database. Deliberately not scoped by exchange here: the caller
    matches each decision to its own exchange's row in Python, since one
    pump_event_id can have a source row per exchange. Selects every field
    `identity_readiness` needs to reuse source_lead.py's full identity
    discipline, not just the minimal subset the earlier draft checked."""
    return select(
        PumpEventSource.event_id,
        PumpEventSource.exchange,
        PumpEventSource.market_id,
        PumpEventSource.identity_key,
        PumpEventSource.unified_symbol,
        PumpEventSource.market_type,
        PumpEventSource.base_asset,
        PumpEventSource.quote_asset,
        PumpEventSource.settle_asset,
        PumpEventSource.onboarded_at,
        PumpEventSource.identity_conflict,
    ).where(PumpEventSource.event_id.in_(event_ids))


def _select_baseline_decisions(
    dataset: ReplayDataset,
) -> list[tuple[int, str, ReplayDecision]]:
    """Every replay-eligible baseline-selected (score_6) decision in
    `dataset.eligible_episodes`, as (pump_event_id, base, decision) triples.
    This is already filtered by whatever `ReplayFilters` the caller used
    (resolver_version, required_horizons, allow_fallback), which is why the
    manifest records those explicitly: without them, the exact count here
    is not reproducible from the report alone. Pure, no I/O."""
    selected = []
    for episode in dataset.eligible_episodes:
        selection = select_score_policy(episode, SCORE_THRESHOLD_BASELINE_POLICY)
        if selection.status != "selected" or selection.decision is None:
            continue
        selected.append((episode.pump_event_id, episode.base, selection.decision))
    return selected


async def _load_sources(
    engine: AsyncEngine,
    event_ids: list[int],
) -> dict[tuple[int, str], PumpEventSource]:
    if not event_ids:
        return {}
    async with engine.connect() as connection:
        result = await connection.execute(pump_event_sources_statement(event_ids))
        rows = result.all()
    sources: dict[tuple[int, str], PumpEventSource] = {}
    for row in rows:
        source = PumpEventSource(
            event_id=row.event_id,
            exchange=row.exchange,
            market_id=row.market_id,
            identity_key=row.identity_key,
            unified_symbol=row.unified_symbol,
            market_type=row.market_type,
            base_asset=row.base_asset,
            quote_asset=row.quote_asset,
            settle_asset=row.settle_asset,
            onboarded_at=row.onboarded_at,
            identity_conflict=row.identity_conflict,
        )
        sources[(row.event_id, row.exchange)] = source
    return sources


def _identity_fingerprint(sources: dict[tuple[int, str], PumpEventSource]) -> str:
    """Hashes the canonicalized identity rows actually used, not just the
    decision dataset (see module docstring): these rows are mutable, so a
    report re-run against the same decisions can legitimately produce a
    different classification later without this fingerprint changing,
    unless it covers the identity rows too."""
    payload = []
    for (event_id, exchange), source in sorted(sources.items()):
        payload.append(
            {
                "event_id": event_id,
                "exchange": exchange,
                "identity_key": source.identity_key,
                "market_id": source.market_id,
                "unified_symbol": source.unified_symbol,
                "market_type": source.market_type,
                "base_asset": source.base_asset,
                "quote_asset": source.quote_asset,
                "settle_asset": source.settle_asset,
                "onboarded_at": (
                    source.onboarded_at.isoformat() if source.onboarded_at is not None else None
                ),
                "identity_conflict": source.identity_conflict,
            }
        )
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _instrument_summaries(records: list[IdentityRecord]) -> tuple[InstrumentSummary, ...]:
    by_instrument: dict[tuple[str, str], list[IdentityRecord]] = defaultdict(list)
    for record in records:
        if record.readiness != "identity_ready" or record.identity_key is None:
            continue
        by_instrument[(record.exchange, record.identity_key)].append(record)
    summaries = []
    for (exchange, identity_key), instrument_records in sorted(by_instrument.items()):
        days = [
            record.available_history_days
            for record in instrument_records
            if record.available_history_days is not None
        ]
        unified_symbol = instrument_records[0].unified_symbol or ""
        summaries.append(
            InstrumentSummary(
                exchange=exchange,
                identity_key=identity_key,
                unified_symbol=unified_symbol,
                base=instrument_records[0].base,
                decisions=len(instrument_records),
                min_available_history_days=min(days) if days else 0,
                max_available_history_days=max(days) if days else 0,
            )
        )
    return tuple(summaries)


async def build_token_history_preflight_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    engine: AsyncEngine,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> TokenHistoryPreflightReport:
    if filters.since is None:
        raise ValueError("token-history preflight report requires an explicit since")
    revision = normalize_code_revision(code_revision)

    baseline_decisions = _select_baseline_decisions(dataset)
    event_ids = [pump_event_id for pump_event_id, _, _ in baseline_decisions]
    sources = await _load_sources(engine, event_ids)

    records = []
    for pump_event_id, base, decision in baseline_decisions:
        source = sources.get((pump_event_id, decision.exchange))
        readiness, available_days = identity_readiness(source, base, decision.ts)
        records.append(
            IdentityRecord(
                pump_event_id=pump_event_id,
                base=base,
                exchange=decision.exchange,
                decision_ts=decision.ts,
                readiness=readiness,
                identity_key=source.identity_key if source is not None else None,
                unified_symbol=source.unified_symbol if source is not None else None,
                available_history_days=available_days,
                onboarded_at=_record_onboarded_at(readiness, source),
            )
        )

    readiness_counts = Counter(record.readiness for record in records)
    window_counts = Counter(
        _history_window_bucket(record.available_history_days)
        for record in records
        if record.available_history_days is not None
    )
    available_days_values = [
        record.available_history_days
        for record in records
        if record.available_history_days is not None
    ]
    median_days = median(available_days_values) if available_days_values else None

    exchanges = sorted({record.exchange for record in records})
    ready_records = [record for record in records if record.readiness == "identity_ready"]
    readiness_by_exchange = tuple(
        ExchangeReadinessRow(
            exchange=exchange,
            ready_decisions=sum(1 for record in ready_records if record.exchange == exchange),
            unique_instruments=len(
                {
                    record.identity_key
                    for record in ready_records
                    if record.exchange == exchange and record.identity_key is not None
                }
            ),
        )
        for exchange in exchanges
    )
    unique_ready_instruments = len(
        {(record.exchange, record.identity_key) for record in ready_records}
    )

    input_exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    identity_fingerprint = _identity_fingerprint(sources)
    combined_fingerprint = hashlib.sha256(
        f"{dataset.input_fingerprint}:{identity_fingerprint}".encode()
    ).hexdigest()

    return TokenHistoryPreflightReport(
        manifest=TokenHistoryPreflightManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=TOKEN_HISTORY_PREFLIGHT_REPORT_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            decision_fingerprint=dataset.input_fingerprint,
            identity_fingerprint=identity_fingerprint,
            input_fingerprint=combined_fingerprint,
            strategy_versions=filters.strategy_versions,
            resolver_version=filters.resolver_version,
            required_horizons=filters.required_horizons,
            fallback_allowed=filters.allow_fallback,
            history_window_days=HISTORY_WINDOW_DAYS,
        ),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=tuple(
            ReadinessRow(reason, count)
            for reason, count in sorted(
                input_exclusions.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        replay_eligible_baseline_decisions=len(records),
        readiness_distribution=tuple(
            ReadinessRow(readiness, count)
            for readiness, count in sorted(
                readiness_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        readiness_by_exchange=readiness_by_exchange,
        unique_ready_instruments=unique_ready_instruments,
        history_window_distribution=tuple(
            HistoryWindowRow(bucket, count)
            for bucket, count in sorted(window_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        median_available_history_days=(float(median_days) if median_days is not None else None),
        instruments=_instrument_summaries(records),
        records=tuple(records),
    )


def render_json(report: TokenHistoryPreflightReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: TokenHistoryPreflightReport) -> str:
    manifest = report.manifest
    lines = [
        "# Token-Behavior-History Identity Preflight (Step 1 of 3)",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{manifest.decision_fingerprint}`",
        f"Identity fingerprint: `{manifest.identity_fingerprint}`",
        f"Combined input fingerprint: `{manifest.input_fingerprint}`",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= decision "
            f"< {manifest.dataset_until_exclusive.isoformat()}"
        ),
        f"Strategy versions: {', '.join(manifest.strategy_versions)}",
        f"Resolver version: `{manifest.resolver_version}`",
        f"Required horizons: {manifest.required_horizons}",
        f"Fallback allowed: {'yes' if manifest.fallback_allowed else 'no'}",
        "",
        (
            "> DB-only preflight. No exchange calls, no new dependencies, no "
            "feature computation, no score change. Identity fields reflect "
            f"`{manifest.identity_completeness_basis}`."
        ),
        "",
        "## Funnel",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Eligible episodes", report.eligible_episodes),
                ("Excluded episodes", report.excluded_episodes),
                (
                    "Replay-eligible baseline decisions",
                    report.replay_eligible_baseline_decisions,
                ),
            ],
        )
    )
    lines.extend(["", "## Input exclusion reasons", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Count"),
            [(row.readiness, row.count) for row in report.input_exclusion_reasons],
        )
    )
    lines.extend(["", "## Identity readiness distribution", ""])
    lines.extend(
        markdown_table(
            ("Readiness", "Count"),
            [(row.readiness, row.count) for row in report.readiness_distribution],
        )
    )
    lines.extend(
        [
            "",
            (
                "_`identity_ready` is the only bucket step 2 (bounded live sample) "
                "will draw from. Every other bucket is excluded, fail-closed, from "
                "any later fetch._"
            ),
            "",
            "## Readiness by exchange (decisions vs. unique instruments)",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Exchange", "Ready decisions", "Unique instruments"),
            [
                (row.exchange, row.ready_decisions, row.unique_instruments)
                for row in report.readiness_by_exchange
            ],
        )
    )
    total_ready_decisions = sum(row.ready_decisions for row in report.readiness_by_exchange)
    lines.extend(
        [
            "",
            (
                f"Total unique ready instruments: {report.unique_ready_instruments} "
                f"(from {total_ready_decisions} ready decisions; several decisions can "
                "share one instrument)."
            ),
            "",
            "## Available-history-days distribution (identity_ready only, upper bound)",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Bucket", "Count"),
            [(row.bucket, row.count) for row in report.history_window_distribution],
        )
    )
    median = report.median_available_history_days
    lines.extend(
        [
            "",
            (
                f"Median available history: {median:.1f} days"
                if median is not None
                else "Median available history: n/a"
            ),
            "",
            (
                "_This counts days between recorded onboarding and decision time. "
                "It is an upper bound only: it says nothing about whether the "
                "exchange's OHLCV endpoint actually retains that much history, or "
                "whether the daily series has gaps. That is exactly what step 2 "
                "(a bounded live sample) exists to measure._"
            ),
            "",
            "## Sampling frame for step 2 (one row per unique ready instrument)",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "Exchange",
                "Base",
                "Identity key",
                "Unified symbol",
                "Decisions",
                "Min history (d)",
                "Max history (d)",
            ),
            [
                (
                    instrument.exchange,
                    instrument.base,
                    instrument.identity_key,
                    instrument.unified_symbol,
                    instrument.decisions,
                    instrument.min_available_history_days,
                    instrument.max_available_history_days,
                )
                for instrument in report.instruments
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "_Pick a handful per exchange spanning short/medium/long history from "
                "this table for step 2's bounded live sample._"
            ),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DB-only identity preflight for the token-behavior-history line"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE,
        help="inclusive UTC cutoff; defaults to the confirmation cohort start",
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
    from .replay_repository import ReplayRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for token-history-identity-preflight-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    until = resolve_report_until(
        args.until,
        generated_at,
        cohort_start=TOKEN_HISTORY_PREFLIGHT_DEFAULT_SINCE,
        report_label="token-history-identity-preflight",
    )
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or TOKEN_HISTORY_PREFLIGHT_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    sys.stderr.write("token-history-identity-preflight: loading decisions\n")
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)

    engine = create_async_engine(
        async_database_url(db_url),
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    try:
        sys.stderr.write("token-history-identity-preflight: building report\n")
        report = await build_token_history_preflight_report(
            dataset,
            filters,
            engine,
            generated_at=generated_at,
            code_revision=args.code_revision,
            working_tree_dirty=args.working_tree_dirty,
        )
    finally:
        await engine.dispose()
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
