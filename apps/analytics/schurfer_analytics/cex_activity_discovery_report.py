"""CEXTrack-like activity -> exact +25% move discovery report.

The primary family is frozen in code before this report's first real run:

* buy burst -> long-side +25% favorable excursion within 24h;
* sell burst -> short-side +25% favorable excursion within 24h.

Both use a 10% five-minute directional-notional share of the instrument's
own strictly complete trailing 24h volume, a $50k 24h liquidity floor, a
60-minute refractory period, entry at the next exact native one-minute bar
open, and a same-instrument/same-UTC-time quiet-day control.  Holm correction
covers the two registered directions.  The viewed discovery window can
nominate at most one direction for a later untouched forward shadow cohort.

Bar-open/high/low paths are market-outcome proxies, not executable quotes.
This report cannot authorize paper or live trading.

## Freeze/evaluate split (colleague review, 2026-09-03,
## research/cex-activity-discovery-completion-v1)

Two mutually exclusive CLI modes:

- `--freeze-artifact`: the ONLY mode that touches PostgreSQL. Runs the
  full DB-fetching pipeline (candidate detection, episode declustering,
  signal/control request building, bounded exact-path fetches), then
  writes an immutable `cex_activity_discovery_dataset_artifact` and claims
  its cohort's own authoritative-fingerprint lock (see that module's own
  docstring for why content-fingerprint addressing alone is not enough).
  Requires `--no-working-tree-dirty` -- a formal freeze that becomes a
  permanent record should not be produced from an uncommitted tree.
  Prints the resulting fingerprint; does not render a report.
- `--from-artifact FINGERPRINT`: reads the already-frozen artifact and
  renders a report from it, `build_report` (pure -- no PostgreSQL, no
  filesystem beyond the one artifact read) computing the funnel/direction
  statistics. Two `--from-artifact` calls against the SAME fingerprint
  produce byte-identical output: `manifest.generated_at`/
  `database_snapshot_at` are the FREEZE's own recorded timestamp, not
  wall-clock "now" at render time.

`if artifact_not_found: query_live_database()` is explicitly NOT a
fallback this module implements anywhere -- a missing or corrupted
artifact is a loud error (via `cex_activity_discovery_dataset_artifact
.read`'s own `research_dataset_artifact` integrity checks), never a
silent trigger to fall back to a live query.

## What `--freeze-artifact` does and does not guarantee about consistency
## (colleague review, 2026-09-04, two rounds)

`freeze_dataset` reads PostgreSQL in three phases: (1) `database_now()`,
(2) the offline candidate-detection extract (`extract_bars_to_parquet`,
chunked into many connections of its own), (3) one `fetch_exact_paths`
call over the combined signal + control request set. This is NOT a
single PostgreSQL snapshot spanning all three phases -- a late backfill/
correction to `timeseries.bybit_momentum_bars_1m` landing between phase 2
and phase 3 could, in principle, mean the candidate universe was detected
under one version of the data while the exact entry/outcome paths for
that same universe were read under a different, corrected version.

What phase 3 DOES now guarantee on its own (fixed in the second review
round, after the first round's own fix -- merely combining signal and
control requests into one Python-level call -- turned out to be
insufficient): `fetch_exact_paths` runs every one of its internal batches
inside ONE `REPEATABLE READ`, read-only Postgres transaction, not a new
connection/transaction per batch. An episode's own signal path and its
matched control path -- the exact pair the paired estimand compares --
are therefore guaranteed to come from the SAME consistent snapshot
regardless of which batch either one lands in, proven directly by
`test_fetch_exact_paths_holds_one_snapshot_across_batches` (a real
Postgres test that commits a write between two batches of one call and
asserts the later batch still cannot see it).

What remains open: phase 2 (the extract) and phase 3 (the path fetch)
still run as separate transactions against separate connections/engines.
A genuine fix -- importing one `pg_export_snapshot()`-exported snapshot
into both phases via `SET TRANSACTION SNAPSHOT` -- was considered and
rejected: the exporting transaction must stay open for both phases
combined (up to ~30 minutes worst-case, per the resource bounds below),
which means holding a live `REPEATABLE READ` transaction against
production's `timeseries.bybit_momentum_bars_1m` -- a large, high-
throughput table -- for that whole duration, holding back autovacuum on
it the entire time. That is exactly the class of new production resource
risk `research/cex-activity-offline-denominator-v1` (PR #327) was built
to eliminate in the first place. The realistic exposure this leaves is
narrow but not negligible: a HYP-016 freeze is a one-time, manually
triggered run (not a recurring job), so the risk is not about how old the
underlying bars are -- it is whether an operator happens to run a
backfill/correction during the specific ~15-30 minute window one freeze
attempt is in flight. `extra["database_snapshot_at"]`/
`extra["extract_completed_at"]`/`extra["path_fetch_completed_at"]` (all
from Postgres's own `now()`) record the real wall-clock bounds of each
phase, so a reviewer auditing a specific freeze can check those bounds
against any known backfill/correction event rather than the run being
silently unauditable either way. This is a documented, accepted residual
risk, not a solved one -- see
docs/research/cex-activity-discovery-completion-v1.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import cex_activity_discovery_dataset_artifact as dataset_artifact
from .cex_activity_discovery import (
    CONTROL_BOUNDARY_POLICY_VERSION,
    CONTROL_QUIET_HOURS,
    CONTROL_SEARCH_DAYS,
    DISCOVERY_SINCE,
    DISCOVERY_UNTIL,
    HYPOTHESIS_ID,
    MATCHING_POLICY_VERSION,
    MISSING_PATH_RESULT_REASON,
    OUTCOME_HORIZON_MINUTES,
    PRIMARY_MOVE_PCT,
    DirectionMoveResult,
    ExactPricePath,
    IncompatibleResearchContractError,
    OutcomeSignalEpisode,
    PathRequest,
    build_control_requests,
    build_direction_results,
    contract_fingerprint,
    input_fingerprint,
    select_forward_candidate,
    select_matched_pairs,
    signal_request,
)
from .cex_activity_discovery_repository import (
    PATH_QUERY_VERSION,
    CexActivityDiscoveryRepository,
    report_maturity_at,
)
from .momentum_flow_bidirectional_burst_offline_repository import (
    OfflineBarsExtractRepository,
    fetch_candidate_extreme_minutes_offline,
)
from .momentum_flow_bidirectional_burst_report import (
    DEFAULT_EXTREME_THRESHOLD_PCT,
    DEFAULT_MAX_CANDIDATE_MINUTES,
    DEFAULT_MIN_VOLUME_24H_USD,
    DEFAULT_REFRACTORY_MINUTES,
    check_candidate_count,
)
from .momentum_flow_bidirectional_burst_study import (
    DIRECTIONS,
    BurstEpisode,
    BurstMinute,
    decluster_episodes,
)
from .momentum_flow_capture_contract import (
    BYBIT_MOMENTUM_CAPTURE_VERSION,
    BYBIT_MOMENTUM_EXCHANGE,
    BYBIT_MOMENTUM_MARKET_TYPE,
)
from .reporting import json_ready, markdown_table, normalize_code_revision, parse_utc_datetime

if TYPE_CHECKING:
    from .research_dataset_artifact import DatasetArtifactManifest

REPORT_VERSION = "cex_activity_discovery_report_v2"
CANDIDATE_QUERY_VERSION = "strict_5_of_5_and_1440_of_1440_range_windows_v1"
INTERPRETATION = "discovery_only_max_one_forward_candidate_no_trading_authorization"

# Generous headroom guard, this codebase's usual fail-loud-not-silently-
# large convention -- colleague review, 2026-09-03 (research/cex-activity-
# discovery-completion-v1 planning): the total signal+control path-request
# count was previously unbounded (implicitly capped only by however many
# candidate minutes decluster_episodes happened to produce), checked here
# BEFORE either fetch_exact_paths call, not after the fact.
DEFAULT_MAX_PATH_REQUESTS = 200_000


def check_path_request_count(count: int, max_path_requests: int) -> None:
    if count > max_path_requests:
        raise ValueError(
            f"report would issue {count} signal+control path requests, over "
            f"--max-path-requests={max_path_requests}; investigate before raising the "
            "bound and silently evaluating an unexpectedly large result"
        )


@dataclass(frozen=True)
class CexActivityDataset:
    """Everything `build_report` needs, in one uniform shape regardless of
    whether it came from a just-completed `freeze_dataset` or a
    `load_dataset_from_artifact` read -- `build_report` itself never knows
    or cares which."""

    artifact_fingerprint: str
    artifact_code_revision: str
    artifact_working_tree_dirty: bool
    manifest_generated_at: datetime
    database_snapshot_at: datetime
    since: datetime
    until_exclusive: datetime
    exchange: str
    market_type: str
    capture_version: str
    extreme_threshold_pct: float
    refractory_minutes: int
    min_volume_24h_usd: float
    max_candidate_minutes: int
    max_path_requests: int
    candidate_extreme_minutes: int
    candidate_query_version: str
    path_query_version: str
    contract_fingerprint: str
    episodes: tuple[OutcomeSignalEpisode, ...]
    signal_paths: dict[str, ExactPricePath]
    controls_by_episode: dict[int, tuple[PathRequest, ...]]
    control_paths: dict[str, ExactPricePath]


@dataclass(frozen=True)
class CexActivityManifest:
    hypothesis_id: str
    report_version: str
    candidate_query_version: str
    path_query_version: str
    interpretation: str
    # code_revision/working_tree_dirty describe THIS RENDER -- the
    # evaluator, i.e. whatever code state ran `build_report` right now.
    # artifact_code_revision/artifact_working_tree_dirty describe the
    # FREEZE instead -- the code state `freeze_dataset` ran under when it
    # produced the underlying raw rows. These are two different points in
    # time and are kept as two separate field pairs (colleague review,
    # 2026-09-04) rather than one, so a reader can always tell whether a
    # `--from-artifact` render used the same code that froze the data.
    code_revision: str
    working_tree_dirty: bool
    artifact_code_revision: str
    artifact_working_tree_dirty: bool
    generated_at: datetime
    database_snapshot_at: datetime
    since: datetime
    until_exclusive: datetime
    exchange: str
    market_type: str
    capture_version: str
    extreme_threshold_pct: float
    refractory_minutes: int
    min_volume_24h_usd: float
    primary_move_pct: float
    outcome_horizon_minutes: int
    control_search_days: int
    control_quiet_hours: int
    control_boundary_policy_version: str
    matching_policy_version: str
    contract_fingerprint: str
    artifact_fingerprint: str
    input_fingerprint: str


@dataclass(frozen=True)
class CexActivityFunnel:
    """`unmatched_resolved_signal_episodes` (colleague review, 2026-09-03,
    research/cex-activity-discovery-completion-v1 planning): an episode
    whose own signal path resolved but never made it into a matched pair
    (no resolved control candidate existed, or it lost the maximum-
    cardinality assignment to another episode) previously only showed up
    as an implicit gap between resolved_signal_paths and matched_pairs --
    easy to miss, and impossible to distinguish from an episode whose
    signal itself never resolved. Counted explicitly here instead.

    `signal_unresolved_by_reason`/`control_unresolved_by_reason`
    (colleague review, 2026-09-04): the artifact already classifies every
    unresolved path via `ExactPricePath.unresolved_reason`, but the
    formal report previously only ever surfaced the aggregate
    resolved/unresolved counts -- never WHY or on WHICH SIDE observations
    were missing (missing entry, incomplete 24h, invalid extrema, or no
    row at all). A systematic selection bias hiding behind one of those
    reasons (e.g. every `sell`-direction control consistently missing its
    entry bar) would be invisible in the aggregate count alone. Per
    `build_report`'s own reconciliation check: `resolved_signal_paths +
    sum(signal_unresolved_by_reason.values())` always equals the total
    number of signal requests (one per episode), and the equivalent holds
    for control -- every request is accounted for exactly once, resolved
    or under exactly one unresolved reason, never both and never neither."""

    candidate_extreme_minutes: int
    independent_episodes: int
    resolved_signal_paths: int
    generated_control_candidates: int
    resolved_control_paths: int
    matched_pairs: int
    unmatched_resolved_signal_episodes: int
    signal_unresolved_by_reason: dict[str, int]
    control_unresolved_by_reason: dict[str, int]


@dataclass(frozen=True)
class CexActivityDiscoveryReport:
    manifest: CexActivityManifest
    funnel: CexActivityFunnel
    directions: tuple[DirectionMoveResult, ...]
    selected_forward_candidate: str | None
    caveats: tuple[str, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CEX activity -> exact +25% discovery with quiet-day matched controls"
    )
    # Defaulted to (and, in freeze_dataset, validated against) the frozen
    # DISCOVERY_SINCE/DISCOVERY_UNTIL -- colleague review, 2026-09-03
    # (research/cex-activity-discovery-completion-v1 planning): this window
    # is already viewed and registered (docs/research/discovery-ledger.md's
    # own HYP-016 row), so accepting an arbitrary --since/--until here
    # would let a caller silently widen or shift the estimand after the
    # window was already chosen -- kept as real CLI arguments (not removed
    # outright) only so an invocation's own command line stays self-
    # documenting, mirroring source_lead_forward_cohort_report.py's own
    # established --since pattern.
    parser.add_argument("--since", type=parse_utc_datetime, default=DISCOVERY_SINCE)
    parser.add_argument("--until", type=parse_utc_datetime, default=DISCOVERY_UNTIL)
    parser.add_argument("--max-candidate-minutes", type=int, default=DEFAULT_MAX_CANDIDATE_MINUTES)
    parser.add_argument("--max-path-requests", type=int, default=DEFAULT_MAX_PATH_REQUESTS)
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--artifact-directory",
        default=None,
        help="override the default research-artifact store location",
    )
    parser.add_argument(
        "--extract-directory",
        default=None,
        help=(
            "keep the offline Parquet extract this run's own candidate-minute detection "
            "produces under this directory instead of a temp dir deleted at exit -- for "
            "inspecting or reusing a real production extract, --freeze-artifact only"
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--freeze-artifact",
        action="store_true",
        help="the only mode that touches PostgreSQL -- writes an immutable artifact, "
        "prints its fingerprint",
    )
    mode.add_argument(
        "--from-artifact",
        metavar="FINGERPRINT",
        default=None,
        help="render a report from an already-frozen artifact -- makes zero DB calls",
    )
    parser.set_defaults(
        exchange=BYBIT_MOMENTUM_EXCHANGE,
        market_type=BYBIT_MOMENTUM_MARKET_TYPE,
        capture_version=BYBIT_MOMENTUM_CAPTURE_VERSION,
        extreme_threshold_pct=DEFAULT_EXTREME_THRESHOLD_PCT,
        refractory_minutes=DEFAULT_REFRACTORY_MINUTES,
        min_volume_24h_usd=DEFAULT_MIN_VOLUME_24H_USD,
    )
    return parser


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def render_markdown(report: CexActivityDiscoveryReport) -> str:
    manifest = report.manifest
    lines = [
        "# CEX Activity → Exact 25% Move Discovery",
        "",
        "Discovery only. This report may nominate at most one direction for a new",
        "untouched forward shadow cohort; it cannot authorize paper/live trading.",
        "",
        f"Window: `{manifest.since.isoformat()}` → `{manifest.until_exclusive.isoformat()}`",
        f"Exact route: `{manifest.exchange}` / `{manifest.market_type}` / "
        f"`{manifest.capture_version}`",
        f"Signal: ≥{manifest.extreme_threshold_pct:.1f}% of strict trailing 24h volume "
        "inside five complete one-minute bars",
        f"Primary outcome: {manifest.primary_move_pct:.1f}% favorable excursion within "
        f"{manifest.outcome_horizon_minutes // 60}h from next-bar open",
        f"Artifact fingerprint: `{manifest.artifact_fingerprint}`",
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        f"Research contract fingerprint: `{manifest.contract_fingerprint}`",
        f"Freeze code revision: `{manifest.artifact_code_revision}`"
        + (" (dirty)" if manifest.artifact_working_tree_dirty else ""),
        f"Render code revision: `{manifest.code_revision}`"
        + (" (dirty)" if manifest.working_tree_dirty else ""),
        "",
        "## Funnel",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Stage", "Count"),
            [
                ("Candidate extreme minutes", report.funnel.candidate_extreme_minutes),
                ("Independent episodes", report.funnel.independent_episodes),
                ("Resolved signal paths", report.funnel.resolved_signal_paths),
                ("Generated control candidates", report.funnel.generated_control_candidates),
                ("Resolved control paths", report.funnel.resolved_control_paths),
                ("Matched pairs", report.funnel.matched_pairs),
                (
                    "Resolved signal, unmatched",
                    report.funnel.unmatched_resolved_signal_episodes,
                ),
            ],
        )
    )
    if report.funnel.signal_unresolved_by_reason or report.funnel.control_unresolved_by_reason:
        lines.extend(["", "### Unresolved, by reason", ""])
        reasons = sorted(
            {
                *report.funnel.signal_unresolved_by_reason,
                *report.funnel.control_unresolved_by_reason,
            }
        )
        lines.extend(
            markdown_table(
                ("Reason", "Signal", "Control"),
                [
                    (
                        reason,
                        report.funnel.signal_unresolved_by_reason.get(reason, 0),
                        report.funnel.control_unresolved_by_reason.get(reason, 0),
                    )
                    for reason in reasons
                ],
            )
        )
    lines.extend(["", "## Registered directional family", ""])
    rows = []
    for row in report.directions:
        estimate = row.estimate
        interval = (
            "n/a"
            if estimate is None
            else f"[{estimate.lower_bound * 100:.2f}%, {estimate.upper_bound * 100:.2f}%]"
        )
        rows.append(
            (
                row.direction,
                row.eligible_episodes,
                row.paired_episodes,
                row.clusters,
                row.utc_weeks,
                _fmt_pct(row.signal_hit_rate_pct),
                _fmt_pct(row.control_hit_rate_pct),
                _fmt_pct(row.paired_hit_rate_delta_pct),
                interval,
                "n/a" if row.holm_adjusted_p_value is None else f"{row.holm_adjusted_p_value:.4f}",
                row.readiness,
                row.verdict,
            )
        )
    lines.extend(
        markdown_table(
            (
                "Direction",
                "Episodes",
                "Pairs",
                "Assets",
                "Weeks",
                "Signal hit",
                "Control hit",
                "Paired delta",
                "95% cluster CI",
                "Holm p",
                "Readiness",
                "Verdict",
            ),
            rows,
        )
    )
    lines.extend(
        [
            "",
            "Selected forward candidate: "
            + (
                f"`{report.selected_forward_candidate}`"
                if report.selected_forward_candidate
                else "none"
            ),
            "",
            "## Caveats",
            "",
            *(f"- {item}" for item in report.caveats),
        ]
    )
    return "\n".join(lines) + "\n"


def render_json(report: CexActivityDiscoveryReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def _build_episodes(
    candidate_minutes: tuple[BurstMinute, ...],
    *,
    extreme_threshold_pct: float,
    refractory_minutes: int,
) -> tuple[OutcomeSignalEpisode, ...]:
    """Pure -- shared by freeze_dataset so the exact same episode-building
    logic is exercised regardless of where candidate_minutes came from."""
    all_episodes: list[BurstEpisode] = []
    next_id = 1
    for direction in DIRECTIONS:
        direction_episodes = decluster_episodes(
            candidate_minutes,
            direction=direction,
            threshold_pct=extreme_threshold_pct,
            refractory_minutes=refractory_minutes,
            start_id=next_id,
        )
        all_episodes.extend(direction_episodes)
        next_id += len(direction_episodes)
    burst_episodes = sorted(all_episodes, key=lambda item: (item.trigger_at, item.episode_id))
    return tuple(
        OutcomeSignalEpisode(
            episode_id=episode.episode_id,
            signal_id=f"{episode.exchange}:{episode.symbol}:{episode.direction}:"
            f"{episode.trigger_at.isoformat()}",
            source=f"cex_{episode.direction}_burst_v1",
            exchange=episode.exchange,
            symbol=episode.symbol,
            direction=episode.direction,
            trigger_at=episode.trigger_at,
            entry_at=episode.trigger_at + timedelta(minutes=1),
            signal_value=episode.peak_burst_pct,
        )
        for episode in burst_episodes
    )


async def freeze_dataset(args: argparse.Namespace) -> DatasetArtifactManifest:
    """The ONLY function in this module allowed to touch PostgreSQL.
    Writes an immutable artifact and claims its cohort's own authoritative-
    fingerprint lock (see cex_activity_discovery_dataset_artifact.py's own
    docstring); raises CohortDriftDetectedError rather than silently
    treating a changed result as current if this cohort was already
    frozen once before with different content."""
    if args.working_tree_dirty:
        raise ValueError(
            "--freeze-artifact requires --no-working-tree-dirty -- a formal freeze that "
            "becomes a permanent record must not be produced from an uncommitted tree"
        )
    if args.since != DISCOVERY_SINCE or args.until != DISCOVERY_UNTIL:
        raise ValueError(
            f"--since/--until must equal the frozen {HYPOTHESIS_ID} window "
            f"[{DISCOVERY_SINCE.isoformat()}, {DISCOVERY_UNTIL.isoformat()}); this contract "
            "registers exactly one already-viewed discovery window, not an arbitrary one"
        )
    code_revision = normalize_code_revision(args.code_revision)
    path_repository = CexActivityDiscoveryRepository.from_url(os.environ["DATABASE_URL"])
    extract_repository = OfflineBarsExtractRepository.from_url(os.environ["DATABASE_URL"])
    try:
        database_now = await path_repository.database_now()
        # Candidate-minute detection runs OFFLINE, against a Parquet
        # extract, not the live 5m/24h RANGE-window query -- see
        # momentum_flow_bidirectional_burst_offline_repository.py's own
        # docstring: the first production attempt at the live query was
        # manually stopped after 12 minutes for degrading production I/O
        # (ROADMAP.md's "Near-term interleaving from 2026-08-31", item 3).
        # extract_bars_to_parquet is the only piece of THIS function that
        # ever touches production -- a plain indexed range SELECT, not a
        # per-symbol window aggregation. Touches only the already-fully-
        # past discovery window itself (DISCOVERY_UNTIL is not gated by
        # any forward-looking maturity requirement -- the candidates found
        # here are burst-trigger minutes, not outcome paths), so it is
        # safe to run before the real maturity check below, which needs
        # the actual request set this run will build to compute correctly.
        with contextlib.ExitStack() as scratch_stack:
            # Only opened (and only ever deleted at exit) when the caller
            # did not pin --extract-directory -- a real production freeze
            # that wants to keep/inspect the extract must never have this
            # function silently create and then immediately discard an
            # unused scratch directory alongside it.
            extract_directory = (
                Path(args.extract_directory)
                if args.extract_directory
                else Path(
                    scratch_stack.enter_context(
                        tempfile.TemporaryDirectory(prefix="cex_activity_hyp016_extract_")
                    )
                )
            )
            extract_directory.mkdir(parents=True, exist_ok=True)
            extract_output_path = extract_directory / (
                f"{args.exchange}_{args.market_type}_{args.capture_version}_"
                f"{args.since.date().isoformat()}_{args.until.date().isoformat()}.parquet"
            )
            extract_manifest = await extract_repository.extract_bars_to_parquet(
                exchange=args.exchange,
                capture_version=args.capture_version,
                market_type=args.market_type,
                since=args.since,
                until=args.until,
                output_path=extract_output_path,
            )
            candidate_minutes = fetch_candidate_extreme_minutes_offline(
                extract_manifest,
                since=args.since,
                until=args.until,
                min_volume_24h_usd=args.min_volume_24h_usd,
                extreme_threshold_pct=args.extreme_threshold_pct,
            )
        check_candidate_count(len(candidate_minutes), args.max_candidate_minutes)

        outcome_episodes = _build_episodes(
            candidate_minutes,
            extreme_threshold_pct=args.extreme_threshold_pct,
            refractory_minutes=args.refractory_minutes,
        )

        # Both request sets are built (pure, no I/O) BEFORE either is
        # fetched -- colleague review, 2026-09-03 (research/cex-activity-
        # discovery-completion-v1 planning): the real maturity check below
        # needs the actual entry_at of every request this run depends on,
        # not just --until, since a control can be offset up to
        # CONTROL_SEARCH_DAYS forward of its own episode.
        signal_requests = tuple(signal_request(episode) for episode in outcome_episodes)
        controls_by_episode = build_control_requests(
            outcome_episodes,
            since=args.since,
            until=args.until,
        )
        control_request_rows = tuple(
            request for rows in controls_by_episode.values() for request in rows
        )

        total_path_requests = len(signal_requests) + len(control_request_rows)
        check_path_request_count(total_path_requests, args.max_path_requests)

        all_entry_ats = tuple(
            request.entry_at for request in (*signal_requests, *control_request_rows)
        )
        if all_entry_ats:
            maturity_at = report_maturity_at(max(all_entry_ats))
            if database_now < maturity_at:
                raise ValueError(
                    f"report window is immature: database now {database_now.isoformat()}, "
                    f"requires at least {maturity_at.isoformat()} (the latest of "
                    f"{total_path_requests} signal+control requests' own entry_at, plus "
                    f"{OUTCOME_HORIZON_MINUTES} minutes and one bar close)"
                )

        # ONE fetch_exact_paths call over the union of signal and control
        # requests, not two separate ones -- colleague review, 2026-09-04
        # (both the original round and a follow-up round that found the
        # first fix here was incomplete). The paired estimand directly
        # compares each episode's own signal path against its matched
        # control path, so if a late backfill/correction landed on the
        # underlying bars table mid-fetch, a signal path and its own
        # matched control could be read from two different data vintages
        # -- the exact pair this report's whole statistic depends on being
        # comparable. Merely combining them into one Python-level call was
        # NOT enough on its own (the follow-up review's own finding):
        # fetch_exact_paths still batched internally, and the previous
        # version opened a brand-new connection/transaction PER BATCH, so
        # two requests landing in different batches -- guaranteed once the
        # combined request count exceeds PATH_BATCH_SIZE, which any real
        # run does -- could still observe different data. fetch_exact_paths
        # itself now runs every batch inside ONE REPEATABLE READ, read-only
        # transaction (see that method's own docstring), so this one call
        # genuinely reads one consistent snapshot regardless of batch
        # boundaries or request order. See the freeze-consistency note in
        # this module's own top-of-file docstring for what this does and
        # does not guarantee across the extract phase too.
        extract_completed_at = await path_repository.database_now()
        all_requests = (*signal_requests, *control_request_rows)
        all_paths = await path_repository.fetch_exact_paths(
            exchange=args.exchange,
            market_type=args.market_type,
            capture_version=args.capture_version,
            requests=all_requests,
        )
        path_fetch_completed_at = await path_repository.database_now()
        signal_request_ids = {request.request_id for request in signal_requests}
        signal_paths = {
            request_id: path
            for request_id, path in all_paths.items()
            if request_id in signal_request_ids
        }
        control_paths = {
            request_id: path
            for request_id, path in all_paths.items()
            if request_id not in signal_request_ids
        }
    finally:
        await asyncio.gather(path_repository.close(), extract_repository.close())

    rows = dataset_artifact.build_rows(
        episodes=outcome_episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    # Computed BEFORE build_cohort, not just stored alongside it in `extra`
    # -- colleague review, 2026-09-04 follow-up: `extra` is deliberately
    # excluded from research_dataset_artifact's own generic fingerprint,
    # so a value that lived only there was never actually bound to the
    # artifact/cohort identity a caller addresses the data by. Folding it
    # (and the three raw threshold/floor parameters that directly define
    # WHICH candidates get selected) into `cohort` instead makes a
    # different research contract, or a different threshold, genuinely a
    # different cohort -- never silently absorbed into an existing one via
    # ALREADY_EXISTS. See contract_fingerprint's and build_cohort's own
    # docstrings for the full reasoning.
    current_contract_fingerprint = contract_fingerprint(
        candidate_query_version=CANDIDATE_QUERY_VERSION,
        path_query_version=PATH_QUERY_VERSION,
        extreme_threshold_pct=args.extreme_threshold_pct,
        refractory_minutes=args.refractory_minutes,
        min_volume_24h_usd=args.min_volume_24h_usd,
    )
    cohort = dataset_artifact.build_cohort(
        hypothesis_id=HYPOTHESIS_ID,
        since=args.since,
        until_exclusive=args.until,
        exchange=args.exchange,
        market_type=args.market_type,
        capture_version=args.capture_version,
        directions=DIRECTIONS,
        control_boundary_policy_version=CONTROL_BOUNDARY_POLICY_VERSION,
        extreme_threshold_pct=args.extreme_threshold_pct,
        refractory_minutes=args.refractory_minutes,
        min_volume_24h_usd=args.min_volume_24h_usd,
        contract_fingerprint=current_contract_fingerprint,
    )
    extra = {
        "candidate_extreme_minutes": len(candidate_minutes),
        "candidate_query_version": CANDIDATE_QUERY_VERSION,
        "path_query_version": PATH_QUERY_VERSION,
        "matching_policy_version": MATCHING_POLICY_VERSION,
        "max_candidate_minutes": args.max_candidate_minutes,
        "max_path_requests": args.max_path_requests,
        "database_snapshot_at": database_now.isoformat(),
        # Provenance for the offline candidate-detection extract itself
        # (colleague review, 2026-09-03 planning) -- so a reviewer of the
        # frozen artifact alone can see exactly how the candidate universe
        # was computed (row/symbol counts, the Parquet's own content hash,
        # wall time) without needing the original run's own logs.
        "extract_query_version": extract_manifest.extract_query_version,
        "extract_row_count": extract_manifest.row_count,
        "extract_symbol_count": extract_manifest.symbol_count,
        "extract_parquet_sha256": extract_manifest.parquet_sha256,
        "extract_wall_seconds": extract_manifest.wall_seconds,
        # Phase-timing provenance (colleague review, 2026-09-04, "freeze is
        # not one snapshot"): database_snapshot_at/extract_completed_at/
        # path_fetch_completed_at bound the real wall-clock window during
        # which this run's own reads happened, all from Postgres's own
        # `now()` (not this process's local clock) so they are directly
        # comparable to each other and to any known backfill/correction
        # timestamp during a later audit. This does NOT provide snapshot
        # isolation -- see this module's own top-of-file docstring for why
        # holding a live snapshot transaction open against production for
        # the whole freeze was evaluated and rejected.
        "extract_completed_at": extract_completed_at.isoformat(),
        "path_fetch_completed_at": path_fetch_completed_at.isoformat(),
    }
    return dataset_artifact.freeze(
        cohort=cohort,
        rows=rows,
        code_revision=code_revision,
        working_tree_dirty=args.working_tree_dirty,
        extra=extra,
        directory=args.artifact_directory,
    )


def load_dataset_from_artifact(
    fingerprint: str, *, directory: str | None = None
) -> CexActivityDataset:
    """Zero PostgreSQL calls -- everything comes from the already-frozen
    artifact via dataset_artifact.read(), which raises loudly (never falls
    back to a live query) on any integrity failure or a fingerprint
    belonging to the wrong dataset."""
    manifest, episodes, signal_paths, controls_by_episode, control_paths = dataset_artifact.read(
        fingerprint, directory=directory
    )
    # `cohort` (not `extra`) is this dataset's own identity-bearing source
    # for the research-contract fields -- colleague review, 2026-09-04
    # follow-up: extreme_threshold_pct/refractory_minutes/
    # min_volume_24h_usd/contract_fingerprint live in `cohort`, which
    # participates in both the generic artifact fingerprint and the
    # cohort-lock key, never only in `extra` (deliberately excluded from
    # the generic fingerprint -- see build_cohort's own docstring).
    cohort = manifest.cohort
    since = datetime.fromisoformat(cohort["since"])
    until_exclusive = datetime.fromisoformat(cohort["until_exclusive"])
    database_snapshot_at = datetime.fromisoformat(manifest.extra["database_snapshot_at"])
    return CexActivityDataset(
        artifact_fingerprint=manifest.fingerprint,
        # The FREEZE's own recorded code state -- colleague review,
        # 2026-09-04: distinct from the `code_revision`/`working_tree_
        # dirty` build_report's own caller passes in, which describe the
        # RENDER instead. manifest.code_revision/working_tree_dirty come
        # straight from write_dataset_artifact's own top-level fields
        # (never part of the content fingerprint), not from `extra`.
        artifact_code_revision=manifest.code_revision,
        artifact_working_tree_dirty=manifest.working_tree_dirty,
        manifest_generated_at=database_snapshot_at,
        database_snapshot_at=database_snapshot_at,
        since=since,
        until_exclusive=until_exclusive,
        exchange=cohort["exchange"],
        market_type=cohort["market_type"],
        capture_version=cohort["capture_version"],
        extreme_threshold_pct=cohort["extreme_threshold_pct"],
        refractory_minutes=cohort["refractory_minutes"],
        min_volume_24h_usd=cohort["min_volume_24h_usd"],
        max_candidate_minutes=manifest.extra["max_candidate_minutes"],
        max_path_requests=manifest.extra["max_path_requests"],
        candidate_extreme_minutes=manifest.extra["candidate_extreme_minutes"],
        candidate_query_version=manifest.extra["candidate_query_version"],
        path_query_version=manifest.extra["path_query_version"],
        contract_fingerprint=cohort["contract_fingerprint"],
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )


def _unresolved_by_reason(
    requests: tuple[PathRequest, ...], paths: dict[str, ExactPricePath]
) -> dict[str, int]:
    """Colleague review, 2026-09-04: iterates over every REQUEST, not just
    the paths dict, so a request with no returned row at all (absent from
    `paths` entirely, `MISSING_PATH_RESULT_REASON`) is correctly counted
    rather than silently vanishing -- the same distinction `ExactPricePath
    .unresolved_reason` already makes for a present-but-unresolved path,
    applied consistently at the funnel level."""
    counts: dict[str, int] = defaultdict(int)
    for request in requests:
        path = paths.get(request.request_id)
        reason = MISSING_PATH_RESULT_REASON if path is None else path.unresolved_reason
        if reason is not None:
            counts[reason] += 1
    return dict(counts)


def build_report(
    dataset: CexActivityDataset, *, code_revision: str, working_tree_dirty: bool
) -> CexActivityDiscoveryReport:
    """Pure -- no PostgreSQL, no filesystem. Takes an already-loaded
    CexActivityDataset (from either freeze_dataset's own in-memory result
    or load_dataset_from_artifact) and computes the funnel/direction
    statistics/verdict, exactly the same way regardless of which.

    Fails closed with `IncompatibleResearchContractError` if the CURRENT
    code's own `contract_fingerprint()` does not match the one this
    dataset was frozen with (colleague review, 2026-09-04) -- never
    silently apply a changed matching/bootstrap/floor/query contract to
    old frozen raw data while labeling the rendered result with the new
    code's own version strings."""
    current_contract_fingerprint = contract_fingerprint(
        candidate_query_version=CANDIDATE_QUERY_VERSION,
        path_query_version=PATH_QUERY_VERSION,
        # These three are operational parameters, not code constants --
        # this module has no independent "live" opinion about them, so
        # recomputing with the dataset's own recorded values checks that
        # `extra`/`cohort` are internally self-consistent (e.g. catches a
        # manually-tampered threshold that was not also re-hashed) rather
        # than a live-vs-frozen code drift, which is what the query/
        # matching/floor/bootstrap constants above this check for.
        extreme_threshold_pct=dataset.extreme_threshold_pct,
        refractory_minutes=dataset.refractory_minutes,
        min_volume_24h_usd=dataset.min_volume_24h_usd,
    )
    if current_contract_fingerprint != dataset.contract_fingerprint:
        raise IncompatibleResearchContractError(
            f"dataset (artifact fingerprint {dataset.artifact_fingerprint}) was frozen "
            f"under research contract {dataset.contract_fingerprint}, but the running "
            f"code's own contract is {current_contract_fingerprint} -- refusing to "
            "evaluate old frozen raw data against a changed matching/bootstrap/floor/"
            "query contract; see contract_fingerprint's own docstring"
        )

    signal_requests = tuple(signal_request(episode) for episode in dataset.episodes)
    control_request_rows = tuple(
        request for rows in dataset.controls_by_episode.values() for request in rows
    )
    pairs = select_matched_pairs(
        dataset.episodes,
        signal_paths=dataset.signal_paths,
        control_requests=dataset.controls_by_episode,
        control_paths=dataset.control_paths,
    )
    direction_results = build_direction_results(dataset.episodes, pairs, dataset.signal_paths)
    fingerprint = input_fingerprint(dataset.episodes, dataset.signal_paths, dataset.control_paths)
    matched_episode_ids = {pair.episode.episode_id for pair in pairs}
    unmatched_resolved_signal_episodes = sum(
        1
        for episode in dataset.episodes
        if episode.episode_id not in matched_episode_ids
        and (path := dataset.signal_paths.get(signal_request(episode).request_id)) is not None
        and path.resolved
    )
    resolved_signal_paths = sum(path.resolved for path in dataset.signal_paths.values())
    resolved_control_paths = sum(path.resolved for path in dataset.control_paths.values())
    signal_unresolved_by_reason = _unresolved_by_reason(signal_requests, dataset.signal_paths)
    control_unresolved_by_reason = _unresolved_by_reason(
        control_request_rows, dataset.control_paths
    )
    # Reconciliation (colleague review, 2026-09-04): every signal/control
    # request is either resolved or accounted for under exactly one
    # unresolved reason -- never both, never neither. This must hold by
    # construction (both counts are computed from the SAME per-request
    # loop, just filtered differently); asserted here as a cheap, direct
    # sanity check rather than trusting that invariant silently.
    assert resolved_signal_paths + sum(signal_unresolved_by_reason.values()) == len(
        signal_requests
    ), "signal resolved + unresolved-by-reason must reconcile to the total request count"
    assert resolved_control_paths + sum(control_unresolved_by_reason.values()) == len(
        control_request_rows
    ), "control resolved + unresolved-by-reason must reconcile to the total request count"
    return CexActivityDiscoveryReport(
        manifest=CexActivityManifest(
            hypothesis_id=HYPOTHESIS_ID,
            report_version=REPORT_VERSION,
            candidate_query_version=dataset.candidate_query_version,
            path_query_version=dataset.path_query_version,
            interpretation=INTERPRETATION,
            code_revision=code_revision,
            working_tree_dirty=working_tree_dirty,
            artifact_code_revision=dataset.artifact_code_revision,
            artifact_working_tree_dirty=dataset.artifact_working_tree_dirty,
            generated_at=dataset.manifest_generated_at,
            database_snapshot_at=dataset.database_snapshot_at,
            since=dataset.since,
            until_exclusive=dataset.until_exclusive,
            exchange=dataset.exchange,
            market_type=dataset.market_type,
            capture_version=dataset.capture_version,
            extreme_threshold_pct=dataset.extreme_threshold_pct,
            refractory_minutes=dataset.refractory_minutes,
            min_volume_24h_usd=dataset.min_volume_24h_usd,
            primary_move_pct=PRIMARY_MOVE_PCT,
            outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
            control_search_days=CONTROL_SEARCH_DAYS,
            control_quiet_hours=CONTROL_QUIET_HOURS,
            control_boundary_policy_version=CONTROL_BOUNDARY_POLICY_VERSION,
            matching_policy_version=MATCHING_POLICY_VERSION,
            contract_fingerprint=dataset.contract_fingerprint,
            artifact_fingerprint=dataset.artifact_fingerprint,
            input_fingerprint=fingerprint,
        ),
        funnel=CexActivityFunnel(
            candidate_extreme_minutes=dataset.candidate_extreme_minutes,
            independent_episodes=len(dataset.episodes),
            resolved_signal_paths=resolved_signal_paths,
            generated_control_candidates=len(control_request_rows),
            resolved_control_paths=resolved_control_paths,
            matched_pairs=len(pairs),
            unmatched_resolved_signal_episodes=unmatched_resolved_signal_episodes,
            signal_unresolved_by_reason=signal_unresolved_by_reason,
            control_unresolved_by_reason=control_unresolved_by_reason,
        ),
        directions=direction_results,
        selected_forward_candidate=select_forward_candidate(direction_results),
        caveats=(
            "Next-bar OHLCV open/high/low is exact-native market data "
            "but not an executable bid/ask fill.",
            "Quiet controls match exact instrument and UTC time, but not "
            "order-book depth or BTC regime.",
            "The viewed discovery window cannot be reused as forward confirmation evidence.",
            "A selected direction requires a new prospective quote-capture "
            "shadow before paper trading.",
        ),
    )


def main() -> None:
    args = build_parser().parse_args()
    if args.freeze_artifact:
        if not os.getenv("DATABASE_URL"):
            raise ValueError("DATABASE_URL is required for --freeze-artifact")
        manifest = asyncio.run(freeze_dataset(args))
        sys.stdout.write(
            json.dumps(
                {"fingerprint": manifest.fingerprint, "row_count": manifest.row_count},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return

    assert args.from_artifact is not None  # guaranteed by the required mutex group
    code_revision = normalize_code_revision(args.code_revision)
    dataset = load_dataset_from_artifact(args.from_artifact, directory=args.artifact_directory)
    report = build_report(
        dataset, code_revision=code_revision, working_tree_dirty=args.working_tree_dirty
    )
    sys.stdout.write(render_json(report) if args.format == "json" else render_markdown(report))
