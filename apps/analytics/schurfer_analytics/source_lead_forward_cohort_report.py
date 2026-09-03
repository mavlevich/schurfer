"""DB-fetch, CLI, and Markdown/JSON rendering for source_lead_forward_cohort_v1
(research/source-lead-forward-cohort-plumbing-v1).

`source_lead_forward_cohort.py`'s own `resolve_episode`/`formal_verdict` are
pure functions, frozen and synthetic-fixture-tested well before any real
qualified capture exists (see that module's own docstring, "What is frozen
now vs. what waits"). This file is exactly the DB-fetching/CLI/rendering
plumbing that docstring defers to later -- built now, before the cohort's
own earliest possible checkpoint (~2026-10-01), against
`test_source_lead_forward_cohort_report.py`'s synthetic fixtures, so no one
has to design resolution mechanics after real outcomes are already visible.

## Honest scope note

The frozen contract's own "Required output" section also calls for a
secondary diagnostic (`SECONDARY_DIAGNOSTIC_VERSION`, the paired early-
minus-confirmed-entry delta HYP-012 originally used, kept here only as
non-gating context). No reusable Python computation for that delta exists
anywhere in this codebase yet -- `confirmed_within_hour` currently only
exists as a Go dashboard signal (`apps/api-gateway/internal/research/
handler.go`'s `SourceLeadProgress`), reading `app.pump_event_sources`
through a query this module would otherwise have to reimplement from
scratch, unreviewed, under this same PR. Since the secondary diagnostic
never gates `formal_verdict` (frozen contract, explicit), shipping the
PRIMARY verdict pipeline correctly now and the secondary diagnostic as a
clearly-labeled follow-up is a better trade than rushing a first, unreviewed
implementation of it into a report whose PRIMARY number is what any future
promotion decision actually depends on. `CohortResult.secondary_diagnostic`
is `None` throughout this version; `render_markdown` says so explicitly
rather than omitting the section silently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from .clustered_inference import ClusterObservation, cluster_bootstrap_mean
from .exchange_registry import EXCHANGE_FACTORIES
from .momentum_flow_bidirectional_burst_study import utc_week_key
from .ohlcv import fetch_symbol_candles
from .reporting import (
    canonical_json_array_fingerprint,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)
from .source_lead_forward_cohort import (
    BOOTSTRAP_ITERATIONS,
    BOOTSTRAP_SEED,
    CONFIDENCE_LEVEL,
    CONTRACT_VERSION,
    ESTIMAND_VERSION,
    EVIDENCE_FLOOR,
    HYPOTHESIS_ORIGIN,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    OUTCOME_HORIZON_MINUTES,
    QUALIFICATION_VERSION,
    SECONDARY_DIAGNOSTIC_VERSION,
    SMALL_UNIVERSE_PROMOTION_NOTE,
    SOURCE_LEAD_FORWARD_COHORT_START,
    STOPPING_RULE,
    EpisodeInputs,
    EpisodeResult,
    expected_exit_boundary_ms,
    formal_verdict,
    resolve_episode,
)
from .source_lead_forward_cohort_repository import (
    QUALIFIED_EPISODE_QUERY_VERSION,
    RawQualifiedEpisode,
    SourceLeadForwardCohortRepository,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from .ohlcv import Candle

REPORT_VERSION = "source_lead_forward_cohort_report_v1"
INTERPRETATION = "prospective_small_universe_no_trading_authorization"

# Generous headroom guard, this codebase's usual fail-loud-not-silently-
# large convention (see e.g. cex_activity_discovery_report.check_candidate_
# count) -- the entire identity- and route-verified universe is 14
# canonical assets (EVIDENCE_FLOOR's own "unreachable 30-cluster floor"
# rationale), so a cohort anywhere near this size would itself be the
# anomaly worth investigating, not a number to silently evaluate.
DEFAULT_MAX_QUALIFIED_EPISODES = 20_000

# Exit-bar fetch window padding around the expected exact boundary: wide
# enough to always contain MAX_EXIT_BAR_GAP_MINUTES worth of slack on
# either side, small enough to stay a cheap, single-page CCXT fetch per
# episode.
_FETCH_WINDOW_PAD_MS = 10 * 60_000


def check_qualified_episode_count(count: int, max_qualified_episodes: int) -> None:
    if count > max_qualified_episodes:
        raise ValueError(
            f"cohort produced {count} qualified episodes, over "
            f"--max-qualified-episodes={max_qualified_episodes}; investigate before "
            "raising the bound and silently evaluating an unexpectedly large result"
        )


@dataclass(frozen=True)
class CohortManifest:
    report_version: str
    interpretation: str
    contract_version: str
    estimand_version: str
    hypothesis_origin: str
    qualification_version: str
    qualified_episode_query_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    since: datetime
    input_fingerprint: str


@dataclass(frozen=True)
class CohortFunnel:
    qualified_episodes_total: int
    qualified_episodes_matured: int
    resolved_episodes: int
    unresolved_by_reason: dict[str, int]


@dataclass(frozen=True)
class CohortResult:
    resolved_episodes: int
    distinct_asset_clusters: int
    distinct_utc_weeks: int
    max_single_asset_share: float
    max_single_week_share: float
    mean_net_return_pct: float | None
    ci_lower_bound_pct: float | None
    ci_upper_bound_pct: float | None
    verdict: str
    secondary_diagnostic: str | None


@dataclass(frozen=True)
class SourceLeadForwardCohortReport:
    manifest: CohortManifest
    funnel: CohortFunnel
    result: CohortResult
    caveats: tuple[str, ...]


def _resolve_one(episode: RawQualifiedEpisode, exit_bar: Candle | None) -> EpisodeResult:
    liquidity = episode.liquidity if isinstance(episode.liquidity, dict) else {}
    ask_vwap = liquidity.get("ask_vwap")
    if not isinstance(ask_vwap, int | float) or not (ask_vwap > 0):
        return EpisodeResult(episode.base, False, "invalid_market_data", None)
    return resolve_episode(
        EpisodeInputs(
            base=episode.base,
            entry_at=episode.observed_at,
            entry_price=float(ask_vwap),
            entry_notional_usd=episode.requested_notional_usd,
            exit_bar=exit_bar,
        )
    )


async def _fetch_exit_bar(
    clients: dict[str, object],
    episode: RawQualifiedEpisode,
) -> Candle | None:
    client = clients.get(episode.target_exchange)
    unified_symbol = episode.instrument.get("unified_symbol")
    if client is None or not isinstance(unified_symbol, str) or not unified_symbol:
        return None
    boundary_ms = expected_exit_boundary_ms(episode.observed_at)
    candles = await fetch_symbol_candles(
        client,
        unified_symbol,
        boundary_ms - _FETCH_WINDOW_PAD_MS,
        boundary_ms + _FETCH_WINDOW_PAD_MS,
        use_cache=True,
    )
    candidates = [candle for candle in candles if candle.ts_ms >= boundary_ms]
    if not candidates:
        return None
    return min(candidates, key=lambda candle: candle.ts_ms)


@dataclass(frozen=True)
class CohortAggregate:
    funnel: CohortFunnel
    result: CohortResult
    input_fingerprint: str


def aggregate_cohort(
    *,
    raw_episodes_count: int,
    matured: Sequence[RawQualifiedEpisode],
    results: Sequence[EpisodeResult],
) -> CohortAggregate:
    """Pure aggregation from already-resolved episodes to the report's own
    funnel/result/fingerprint -- no I/O. Kept separate from `generate_report`
    specifically so it can be exercised directly against synthetic
    `RawQualifiedEpisode`/`EpisodeResult` fixtures before any real qualified
    capture exists (see this module's own docstring and
    `test_source_lead_forward_cohort_report.py`), the same discipline
    `source_lead_forward_cohort.py`'s own pure `resolve_episode`/
    `formal_verdict` were built under."""
    if len(matured) != len(results):
        raise ValueError("matured and results must be the same length and order")

    resolved = [result for result in results if result.resolved]
    unresolved_reasons: list[str] = []
    for result in results:
        if result.resolved:
            continue
        # resolve_episode's own contract: resolved=False always pairs with a
        # non-None unresolved_reason (see EpisodeResult's construction
        # sites) -- narrows the type for mypy and doubles as a check on
        # that invariant actually holding.
        assert result.unresolved_reason is not None
        unresolved_reasons.append(result.unresolved_reason)
    unresolved_by_reason = dict(Counter(unresolved_reasons))

    # Same invariant, the other direction: resolved=True always pairs with
    # a non-None net_return_pct (resolve_episode's own final `assert`).
    resolved_returns: list[float] = []
    for result in resolved:
        assert result.net_return_pct is not None
        resolved_returns.append(result.net_return_pct)

    weeks_by_episode = [
        utc_week_key(episode.observed_at)
        for episode, result in zip(matured, results, strict=True)
        if result.resolved
    ]
    distinct_asset_clusters = len({result.base for result in resolved})
    distinct_utc_weeks = len(set(weeks_by_episode))
    asset_counts = Counter(result.base for result in resolved)
    week_counts = Counter(weeks_by_episode)
    max_single_asset_share = max(asset_counts.values()) / len(resolved) if resolved else 0.0
    max_single_week_share = max(week_counts.values()) / len(resolved) if resolved else 0.0
    mean_net_return_pct = (
        sum(resolved_returns) / len(resolved_returns) if resolved_returns else None
    )

    ci_lower: float | None = None
    ci_upper: float | None = None
    if resolved:
        observations = tuple(
            ClusterObservation(cluster_key=result.base, value=value)
            for result, value in zip(resolved, resolved_returns, strict=True)
        )
        computation = cluster_bootstrap_mean(
            observations,
            iterations=BOOTSTRAP_ITERATIONS,
            seed=BOOTSTRAP_SEED,
            confidence_level=CONFIDENCE_LEVEL,
        )
        ci_lower = computation.estimate.lower_bound
        ci_upper = computation.estimate.upper_bound

    verdict = formal_verdict(
        resolved_episodes=len(resolved),
        distinct_asset_clusters=distinct_asset_clusters,
        distinct_utc_weeks=distinct_utc_weeks,
        max_single_asset_share=max_single_asset_share,
        max_single_week_share=max_single_week_share,
        ci_lower_bound_pct=ci_lower,
    )

    fingerprint_source = [
        {
            "capture_id": episode.capture_id,
            "base": episode.base,
            "target_exchange": episode.target_exchange,
            "observed_at": episode.observed_at.isoformat(),
            "resolved": result.resolved,
            "unresolved_reason": result.unresolved_reason,
            "net_return_pct": result.net_return_pct,
        }
        for episode, result in zip(matured, results, strict=True)
    ]
    fingerprint = canonical_json_array_fingerprint(fingerprint_source)

    return CohortAggregate(
        funnel=CohortFunnel(
            qualified_episodes_total=raw_episodes_count,
            qualified_episodes_matured=len(matured),
            resolved_episodes=len(resolved),
            unresolved_by_reason=unresolved_by_reason,
        ),
        result=CohortResult(
            resolved_episodes=len(resolved),
            distinct_asset_clusters=distinct_asset_clusters,
            distinct_utc_weeks=distinct_utc_weeks,
            max_single_asset_share=round(max_single_asset_share, 4),
            max_single_week_share=round(max_single_week_share, 4),
            mean_net_return_pct=(
                round(mean_net_return_pct, 6) if mean_net_return_pct is not None else None
            ),
            ci_lower_bound_pct=(round(ci_lower, 6) if ci_lower is not None else None),
            ci_upper_bound_pct=(round(ci_upper, 6) if ci_upper is not None else None),
            verdict=verdict,
            secondary_diagnostic=None,
        ),
        input_fingerprint=fingerprint,
    )


async def generate_report(args: argparse.Namespace) -> SourceLeadForwardCohortReport:
    if args.since != SOURCE_LEAD_FORWARD_COHORT_START:
        raise ValueError(
            f"--since must equal the frozen SOURCE_LEAD_FORWARD_COHORT_START "
            f"({SOURCE_LEAD_FORWARD_COHORT_START.isoformat()}); this contract registers "
            "exactly one cohort boundary, not an arbitrary window"
        )
    code_revision = normalize_code_revision(args.code_revision)
    repository = SourceLeadForwardCohortRepository.from_url(os.environ["DATABASE_URL"])
    database_now = await repository.database_now()
    raw_episodes = await repository.fetch_qualified_episodes(
        qualification_version=QUALIFICATION_VERSION,
        since=args.since,
        limit=args.max_qualified_episodes + 1,
    )
    check_qualified_episode_count(len(raw_episodes), args.max_qualified_episodes)

    maturity_cutoff = database_now - timedelta(minutes=OUTCOME_HORIZON_MINUTES)
    matured = [episode for episode in raw_episodes if episode.observed_at <= maturity_cutoff]

    clients = {exchange: factory() for exchange, factory in EXCHANGE_FACTORIES.items()}
    try:
        exit_bars = await asyncio.gather(
            *(_fetch_exit_bar(clients, episode) for episode in matured)
        )
    finally:
        await asyncio.gather(*(client.close() for client in clients.values()))

    results = [
        _resolve_one(episode, exit_bar)
        for episode, exit_bar in zip(matured, exit_bars, strict=True)
    ]
    aggregate = aggregate_cohort(
        raw_episodes_count=len(raw_episodes), matured=matured, results=results
    )

    return SourceLeadForwardCohortReport(
        manifest=CohortManifest(
            report_version=REPORT_VERSION,
            interpretation=INTERPRETATION,
            contract_version=CONTRACT_VERSION,
            estimand_version=ESTIMAND_VERSION,
            hypothesis_origin=HYPOTHESIS_ORIGIN,
            qualification_version=QUALIFICATION_VERSION,
            qualified_episode_query_version=QUALIFIED_EPISODE_QUERY_VERSION,
            code_revision=code_revision,
            working_tree_dirty=args.working_tree_dirty,
            generated_at=database_now,
            since=args.since,
            input_fingerprint=aggregate.input_fingerprint,
        ),
        funnel=aggregate.funnel,
        result=aggregate.result,
        caveats=(
            "Prospective, small-universe estimand (gate -> binance only, 14 canonical "
            "assets) -- not a same-methodology confirmation of HYP-012's original "
            "4-route paired family; see the module docstring.",
            f"Secondary diagnostic ({SECONDARY_DIAGNOSTIC_VERSION}) is not yet computed "
            "by this report -- see this file's own 'Honest scope note'. It never gates "
            "the verdict above even once implemented.",
            "Exit price is an OHLCV-close proxy with a pre-registered 15bps slippage "
            "haircut, not a guarantee a real fill could never be worse.",
            SMALL_UNIVERSE_PROMOTION_NOTE,
            STOPPING_RULE,
        ),
    )


def render_json(report: SourceLeadForwardCohortReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}%"


def render_markdown(report: SourceLeadForwardCohortReport) -> str:
    manifest = report.manifest
    funnel = report.funnel
    result = report.result
    interval = (
        "n/a"
        if result.ci_lower_bound_pct is None
        else f"[{result.ci_lower_bound_pct:.4f}%, {result.ci_upper_bound_pct:.4f}%]"
    )
    lines = [
        "# Source-Lead Forward Cohort",
        "",
        "Prospective, small-universe measurement. A `candidate` verdict does not by "
        "itself authorize paper or live execution -- see the small-universe promotion "
        "note below.",
        "",
        f"Cohort start: `{manifest.since.isoformat()}`",
        f"Contract: `{manifest.contract_version}` / estimand `{manifest.estimand_version}` "
        f"(motivated by `{manifest.hypothesis_origin}`, not a claimed replication of it)",
        f"Generated at: `{manifest.generated_at.isoformat()}`",
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        "",
        "## Funnel",
        "",
        *markdown_table(
            ("Stage", "Count"),
            [
                ("Qualified episodes (total)", funnel.qualified_episodes_total),
                ("Matured (horizon elapsed)", funnel.qualified_episodes_matured),
                ("Resolved", funnel.resolved_episodes),
                *[
                    (f"Unresolved: {reason}", count)
                    for reason, count in sorted(funnel.unresolved_by_reason.items())
                ],
            ],
        ),
        "",
        "## Frozen primary result",
        "",
        *markdown_table(
            (
                "Resolved",
                "Asset clusters",
                "UTC weeks",
                "Max asset share",
                "Max week share",
                "Mean net return",
                "95% cluster CI",
                "Verdict",
            ),
            [
                (
                    result.resolved_episodes,
                    result.distinct_asset_clusters,
                    result.distinct_utc_weeks,
                    f"{result.max_single_asset_share:.2%}",
                    f"{result.max_single_week_share:.2%}",
                    _fmt_pct(result.mean_net_return_pct),
                    interval,
                    result.verdict,
                )
            ],
        ),
        "",
        f"Evidence floor: {EVIDENCE_FLOOR['min_resolved_episodes']} resolved episodes, "
        f"{EVIDENCE_FLOOR['min_distinct_asset_clusters']} distinct asset clusters, "
        f"{EVIDENCE_FLOOR['min_distinct_utc_weeks']} distinct UTC weeks, concentration caps "
        f"{MAX_SINGLE_ASSET_EPISODE_SHARE:.0%} asset / {MAX_SINGLE_WEEK_EPISODE_SHARE:.0%} week.",
        "",
        "## Caveats",
        "",
        *(f"- {item}" for item in report.caveats),
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="source_lead_forward_cohort_v1 -- prospective forward cohort report"
    )
    parser.add_argument(
        "--since", type=parse_utc_datetime, default=SOURCE_LEAD_FORWARD_COHORT_START
    )
    parser.add_argument(
        "--max-qualified-episodes",
        type=int,
        default=DEFAULT_MAX_QUALIFIED_EPISODES,
        help=(
            f"default {DEFAULT_MAX_QUALIFIED_EPISODES}; fails loudly rather than silently "
            "evaluating an unexpectedly large result"
        ),
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not os.getenv("DATABASE_URL"):
        raise ValueError("DATABASE_URL is required")
    if not args.code_revision:
        raise ValueError("--code-revision (or SCHURFER_GIT_SHA) is required")
    report = asyncio.run(generate_report(args))
    sys.stdout.write(render_json(report) if args.format == "json" else render_markdown(report))
