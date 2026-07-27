"""Descriptive HYP-005 candle anomaly report over virtual baseline episodes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import fmean

from .candle_anomaly_features import (
    ATR_BARS,
    BLOW_OFF_MIN_BULL_BODY_ATR,
    BLOW_OFF_TOP2_SHARE_PCT,
    CANDLE_ANOMALY_COHORT_START,
    CANDLE_ANOMALY_FEATURE_VERSION,
    FORMATION_BARS,
    STRONG_REVERSAL_MIN_BEAR_BODY_ATR,
    STRONG_REVERSAL_MIN_RETURNED_SHARE_PCT,
    VOLUME_ZSCORE_BARS,
    WARMUP_BARS,
    CandleAnomalyFeatures,
    derive_candle_anomaly_features,
)
from .outcomes import RESOLVER_VERSION
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    ReplayDataset,
    ReplayFilters,
    build_replay_dataset,
)
from .reporting import (
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    parse_utc_datetime,
)
from .virtual_strategy import (
    DEFAULT_COSTS,
    CostParameters,
    MarketPath,
    VirtualTrade,
    select_episode_decision,
)
from .virtual_strategy_report import (
    CountRow,
    VirtualReplayReport,
    build_virtual_report,
)

CANDLE_ANOMALY_REPORT_VERSION = "candle_anomaly_report_v1"
CANDLE_ANOMALY_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
CANDLE_ANOMALY_BUCKETS = (
    "blow_off__strong_reversal",
    "blow_off__weak_reversal",
    "grind__strong_reversal",
    "grind__weak_reversal",
)


@dataclass(frozen=True)
class CandleAnomalyManifest:
    report_version: str
    feature_version: str
    cohort_start: datetime
    formation_bars: int
    warmup_bars: int
    atr_bars: int
    volume_zscore_bars: int
    blow_off_top2_share_pct: float
    blow_off_min_bull_body_atr: float
    strong_reversal_min_bear_body_atr: float
    strong_reversal_min_returned_share_pct: float
    feature_cutoff_policy: str = "latest_fully_closed_5m_bar_at_or_before_decision"
    selection_policy: str = "baseline_selected_decision"
    report_scope: str = "descriptive_feature_research_no_promotion"


@dataclass(frozen=True)
class CandleAnomalyEpisodeResult:
    pump_event_id: int
    cluster_key: str
    base: str
    exchange: str
    decision_id: str
    decision_at: datetime
    features: CandleAnomalyFeatures
    trade_status: str
    net_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    captured_move_pct: float | None
    exit_reason: str | None


@dataclass(frozen=True)
class CandleAnomalyBucketMetrics:
    bucket: str
    feature_episodes: int
    resolved_trades: int
    asset_clusters: int
    largest_cluster_share_pct: float | None
    mean_net_return_pct: float | None
    net_win_rate_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_captured_move_pct: float | None
    initial_stop_rate_pct: float | None
    mean_top_2_positive_move_share_pct: float | None
    mean_max_bull_body_atr: float | None
    mean_last_bear_body_atr: float | None
    mean_returned_pump_share_pct: float | None


@dataclass(frozen=True)
class CandleAnomalyReport:
    manifest: CandleAnomalyManifest
    baseline: VirtualReplayReport
    feature_statuses: tuple[CountRow, ...]
    volume_statuses: tuple[CountRow, ...]
    buckets: tuple[CandleAnomalyBucketMetrics, ...]
    episodes: tuple[CandleAnomalyEpisodeResult, ...]


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _mean(values: list[float | None]) -> float | None:
    complete = [value for value in values if value is not None]
    return fmean(complete) if complete else None


def _bucket_metrics(
    bucket: str,
    episodes: tuple[CandleAnomalyEpisodeResult, ...],
) -> CandleAnomalyBucketMetrics:
    selected = tuple(row for row in episodes if row.features.bucket == bucket)
    resolved = tuple(row for row in selected if row.trade_status == "complete")
    cluster_counts = Counter(row.cluster_key for row in selected)
    largest_cluster_share = max(cluster_counts.values()) / len(selected) * 100 if selected else None
    net_returns = [row.net_return_pct for row in resolved if row.net_return_pct is not None]
    return CandleAnomalyBucketMetrics(
        bucket=bucket,
        feature_episodes=len(selected),
        resolved_trades=len(resolved),
        asset_clusters=len(cluster_counts),
        largest_cluster_share_pct=largest_cluster_share,
        mean_net_return_pct=_mean([row.net_return_pct for row in resolved]),
        net_win_rate_pct=(
            sum(value > 0 for value in net_returns) / len(net_returns) * 100
            if net_returns
            else None
        ),
        mean_mfe_pct=_mean([row.mfe_pct for row in resolved]),
        mean_mae_pct=_mean([row.mae_pct for row in resolved]),
        mean_captured_move_pct=_mean([row.captured_move_pct for row in resolved]),
        initial_stop_rate_pct=(
            sum(row.exit_reason == "initial_sl" for row in resolved) / len(resolved) * 100
            if resolved
            else None
        ),
        mean_top_2_positive_move_share_pct=_mean(
            [row.features.top_2_positive_move_share_pct for row in selected]
        ),
        mean_max_bull_body_atr=_mean([row.features.max_bull_body_atr for row in selected]),
        mean_last_bear_body_atr=_mean([row.features.last_bear_body_atr for row in selected]),
        mean_returned_pump_share_pct=_mean(
            [row.features.returned_pump_share_pct for row in selected]
        ),
    )


def build_candle_anomaly_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[MarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> CandleAnomalyReport:
    if filters.since != CANDLE_ANOMALY_COHORT_START:
        raise ValueError("candle anomaly report requires the registered cohort start")
    if filters.strategy_versions != CANDLE_ANOMALY_STRATEGY_VERSIONS:
        raise ValueError("candle anomaly report requires the registered strategy cohort")
    baseline = build_virtual_report(
        dataset,
        filters,
        paths,
        generated_at=generated_at,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        costs=costs,
    )
    path_by_event = {path.pump_event_id: path for path in paths}
    trade_by_event: dict[int, VirtualTrade] = {
        trade.pump_event_id: trade for trade in baseline.trades
    }
    rows: list[CandleAnomalyEpisodeResult] = []
    for episode in dataset.eligible_episodes:
        selection = select_episode_decision(episode)
        decision = selection.decision
        path = path_by_event.get(episode.pump_event_id)
        features = derive_candle_anomaly_features(
            decision,
            path.candles if path is not None and path.status == "complete" else (),
        )
        trade = trade_by_event.get(episode.pump_event_id)
        if trade is None:
            raise ValueError(f"baseline replay omitted eligible episode {episode.pump_event_id}")
        rows.append(
            CandleAnomalyEpisodeResult(
                pump_event_id=episode.pump_event_id,
                cluster_key=episode.cluster_key,
                base=episode.base,
                exchange=decision.exchange,
                decision_id=decision.decision_id or "",
                decision_at=decision.ts,
                features=features,
                trade_status=trade.status,
                net_return_pct=trade.net_return_pct,
                mfe_pct=trade.mfe_pct,
                mae_pct=trade.mae_pct,
                captured_move_pct=trade.captured_move_pct,
                exit_reason=trade.exit_reason,
            )
        )
    episodes = tuple(rows)
    return CandleAnomalyReport(
        manifest=CandleAnomalyManifest(
            report_version=CANDLE_ANOMALY_REPORT_VERSION,
            feature_version=CANDLE_ANOMALY_FEATURE_VERSION,
            cohort_start=CANDLE_ANOMALY_COHORT_START,
            formation_bars=FORMATION_BARS,
            warmup_bars=WARMUP_BARS,
            atr_bars=ATR_BARS,
            volume_zscore_bars=VOLUME_ZSCORE_BARS,
            blow_off_top2_share_pct=BLOW_OFF_TOP2_SHARE_PCT,
            blow_off_min_bull_body_atr=BLOW_OFF_MIN_BULL_BODY_ATR,
            strong_reversal_min_bear_body_atr=STRONG_REVERSAL_MIN_BEAR_BODY_ATR,
            strong_reversal_min_returned_share_pct=(STRONG_REVERSAL_MIN_RETURNED_SHARE_PCT),
        ),
        baseline=baseline,
        feature_statuses=_count_rows(Counter(row.features.status for row in episodes)),
        volume_statuses=_count_rows(Counter(row.features.volume_status for row in episodes)),
        buckets=tuple(_bucket_metrics(bucket, episodes) for bucket in CANDLE_ANOMALY_BUCKETS),
        episodes=episodes,
    )


def render_json(report: CandleAnomalyReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: CandleAnomalyReport) -> str:
    manifest = report.manifest
    baseline_manifest = report.baseline.manifest
    health = report.baseline.health
    dataset_since = baseline_manifest.dataset_since
    if dataset_since is None:
        raise ValueError("candle anomaly report requires a dataset start")
    lines = [
        "# Pump Short Candle Anomaly Research",
        "",
        f"Generated: {baseline_manifest.generated_at.isoformat()}",
        f"Code revision: `{baseline_manifest.code_revision}`",
        f"Working tree dirty: {'yes' if baseline_manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{baseline_manifest.decision_input_fingerprint}`",
        f"Market-path fingerprint: `{baseline_manifest.market_path_fingerprint}`",
        (
            f"Scope: {dataset_since.isoformat()}"
            f" <= decision < {baseline_manifest.dataset_until_exclusive.isoformat()}"
        ),
        "",
        (
            "> Descriptive feature research only. These buckets cannot change "
            "production scoring or entry gates."
        ),
        "",
        "## Feature contract",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Component", "Version / value"),
            [
                ("Feature model", manifest.feature_version),
                ("Selection", manifest.selection_policy),
                ("Formation", f"{manifest.formation_bars} x 5m"),
                ("Warm-up", f"{manifest.warmup_bars} x 5m"),
                ("ATR", f"{manifest.atr_bars} prior bars"),
                ("Volume z-score", f"{manifest.volume_zscore_bars} prior bars"),
                (
                    "Blow-off",
                    (
                        f"top-2 >= {manifest.blow_off_top2_share_pct:g}% and "
                        f"bull body >= {manifest.blow_off_min_bull_body_atr:g} ATR"
                    ),
                ),
                (
                    "Strong reversal",
                    (
                        f"bear body >= {manifest.strong_reversal_min_bear_body_atr:g} ATR "
                        f"and returned >= "
                        f"{manifest.strong_reversal_min_returned_share_pct:g}%"
                    ),
                ),
                ("Feature cutoff", manifest.feature_cutoff_policy),
                ("Report scope", manifest.report_scope),
            ],
        )
    )
    lines.extend(["", "## Coverage", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Dataset episodes", health.dataset_episodes),
                ("Eligible episodes", health.eligible_episodes),
                ("Excluded episodes", health.excluded_episodes),
                ("Completed virtual trades", health.completed_replays),
                ("Unresolved virtual trades", health.unresolved_replays),
            ],
        )
    )
    for title, rows in (
        ("Input exclusions", report.baseline.input_exclusion_reasons),
        ("Feature status", report.feature_statuses),
        ("Volume feature status", report.volume_statuses),
    ):
        lines.extend(["", f"## {title}", ""])
        lines.extend(
            markdown_table(
                ("Name", "Episodes"),
                [(row.name, row.count) for row in rows],
            )
        )
    lines.extend(["", "## Pre-registered 2x2 buckets", ""])
    lines.extend(
        markdown_table(
            (
                "Bucket",
                "Feature N",
                "Resolved N",
                "Clusters",
                "Largest cluster",
                "Mean net",
                "Win rate",
                "MFE",
                "MAE",
                "Captured",
                "Initial SL",
                "Top-2 share",
                "Bull body ATR",
                "Bear body ATR",
                "Returned",
            ),
            [
                (
                    row.bucket,
                    row.feature_episodes,
                    row.resolved_trades,
                    row.asset_clusters,
                    format_percentage(
                        row.largest_cluster_share_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.mean_net_return_pct, missing="n/a"),
                    format_percentage(row.net_win_rate_pct, missing="n/a"),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                    format_percentage(
                        row.mean_captured_move_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                    format_percentage(
                        row.mean_top_2_positive_move_share_pct,
                        missing="n/a",
                    ),
                    format_number(row.mean_max_bull_body_atr, missing="n/a"),
                    format_number(row.mean_last_bear_body_atr, missing="n/a"),
                    format_percentage(
                        row.mean_returned_pump_share_pct,
                        missing="n/a",
                    ),
                )
                for row in report.buckets
            ],
        )
    )
    lines.extend(["", "## Episode features", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Exchange",
                "Feature status",
                "Bucket",
                "24h return",
                "24h peak",
                "Top-2 share",
                "Bull body ATR",
                "Volume z",
                "Bear body ATR",
                "Returned",
                "Net",
                "Exit",
                "Error",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.exchange,
                    row.features.status,
                    row.features.bucket or "unclassified",
                    format_percentage(
                        row.features.formation_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.features.formation_peak_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.features.top_2_positive_move_share_pct,
                        missing="n/a",
                    ),
                    format_number(row.features.max_bull_body_atr, missing="n/a"),
                    format_number(row.features.max_volume_zscore, missing="n/a"),
                    format_number(row.features.last_bear_body_atr, missing="n/a"),
                    format_percentage(
                        row.features.returned_pump_share_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.net_return_pct, missing="n/a"),
                    row.exit_reason or row.trade_status,
                    row.features.error or "",
                )
                for row in report.episodes
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Describe pre-registered HYP-005 candle anomaly buckets"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=CANDLE_ANOMALY_COHORT_START,
        help="inclusive UTC cutoff; fixed to the registered HYP-005 cohort",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="allow fallback outcomes in a separately identified sensitivity run",
    )
    parser.add_argument(
        "--taker-fee-bps-per-side",
        type=float,
        default=DEFAULT_COSTS.taker_fee_bps_per_side,
    )
    parser.add_argument(
        "--funding-cost-bps-per-8h",
        type=float,
        default=DEFAULT_COSTS.funding_cost_bps_per_8h,
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
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_candle_anomaly_paths

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for candle-anomaly-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=CANDLE_ANOMALY_STRATEGY_VERSIONS,
        resolver_version=args.resolver_version,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    costs = CostParameters(
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
    )
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)
    paths = await fetch_candle_anomaly_paths(
        dataset.eligible_episodes,
        EXCHANGE_FACTORIES,
    )
    report = build_candle_anomaly_report(
        dataset,
        filters,
        paths,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
