"""analysis/early-momentum-net-evidence-v1 -- CLI, orchestration, and
Markdown/JSON rendering for the early_momentum_v4 net-edge evidence report.

Provenance (colleague correction #1): this process runs inside the
analytics container, which does not carry `.git`/source -- it cannot call
`git` itself. `--code-revision` and `--working-tree-dirty`/
`--no-working-tree-dirty` are computed by the Makefile (which does have
`.git`, running on the host or in prod via the same pattern every other
report in this package already uses) and passed in as required CLI
arguments; a direct invocation without them simply does not run.
`--code-revision` is validated as a non-empty, SHA-like identifier via
`reporting.normalize_code_revision`. A dirty working tree never changes the
computed verdict itself (that would conflate git hygiene with economics),
but the report is never displayed or reasoned about as a `formal_run` when
dirty -- see `formal_run` on `NetEvidenceReport` and its own docstring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .early_momentum_net_evidence import (
    ACCOUNTING_VERSION,
    COHORT_MATURITY_BUFFER_SECONDS,
    EXPECTED_CONTRACT_SHA256_HEX,
    FORMAL_COHORT_START,
    MODE,
    REPORT_VERSION,
    SIDE,
    STRATEGY_NAME,
    STRATEGY_VERSION,
    VERDICT_PASS_LIVE_MICRO_CANDIDATE,
    ComparableTrade,
    ConcurrencySummary,
    EconomicsSummary,
    FunnelResult,
    LegacyContextRow,
    RawDataset,
    RobustnessSummary,
    Verdict,
    build_funnel,
    compute_capacity,
    compute_concurrency,
    compute_economics,
    compute_robustness,
    dataset_fingerprint,
    evaluate_verdict,
)
from .reporting import (
    format_number as _num,
)
from .reporting import (
    format_percentage as _pct,
)
from .reporting import (
    json_ready as _json_ready,
)
from .reporting import (
    markdown_table as _table,
)
from .reporting import (
    normalize_code_revision,
    parse_utc_datetime,
)

if TYPE_CHECKING:
    from .early_momentum_net_evidence import CapacitySummary


class CohortNotMatureError(ValueError):
    """Raised when --cohort-end is not old enough for every trade to have
    had time to mature (episode TTL + max_hold + operational buffer)."""


@dataclass(frozen=True)
class NetEvidenceReport:
    report_version: str
    generated_at: datetime
    code_revision: str
    working_tree_dirty: bool
    # A PASS is only authoritative -- i.e. eligible to be read as
    # authorization to start the LIVE_MICRO implementation PR -- when this
    # is True. A dirty-tree run's computed verdict is still shown (git
    # hygiene must never silently rewrite economics), but never treat a
    # `pass_live_micro_candidate` alongside `formal_run=False` as a green
    # light; re-run clean before acting on it.
    formal_run: bool
    db_snapshot_at: datetime
    cohort_start: datetime
    cohort_end: datetime
    expected_contract_sha256: str
    observed_contract_sha256: tuple[str, ...]
    dataset_fingerprint: str
    funnel: FunnelResult
    economics: EconomicsSummary
    concurrency: ConcurrencySummary
    robustness: RobustnessSummary
    capacity: CapacitySummary
    verdict: Verdict
    legacy_context: tuple[LegacyContextRow, ...]


async def generate_report(
    *,
    db_url: str,
    cohort_end: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> NetEvidenceReport:
    from .early_momentum_net_evidence_repository import EarlyMomentumNetEvidenceRepository

    repository = EarlyMomentumNetEvidenceRepository.from_url(db_url)
    try:
        dataset: RawDataset = await repository.fetch(
            cohort_start=FORMAL_COHORT_START, cohort_end=cohort_end
        )
        legacy_context = await repository.fetch_legacy_context(cohort_end=cohort_end)
    finally:
        await repository.close()

    maturity_deadline = cohort_end + timedelta(seconds=COHORT_MATURITY_BUFFER_SECONDS)
    if dataset.db_snapshot_at < maturity_deadline:
        raise CohortNotMatureError(
            f"--cohort-end={cohort_end.isoformat()} is not mature yet: the database's own "
            f"clock reads {dataset.db_snapshot_at.isoformat()}, which is earlier than "
            f"{maturity_deadline.isoformat()} (cohort_end + "
            f"{COHORT_MATURITY_BUFFER_SECONDS}s maturity buffer). Retry with an earlier "
            f"--cohort-end or wait."
        )

    funnel = build_funnel(dataset)
    economics = compute_economics(funnel.comparable)
    concurrency = compute_concurrency(funnel.comparable)
    robustness = compute_robustness(funnel.comparable)
    exit_liquidity_by_trade = {row.trade_id: row for row in dataset.exit_liquidity}
    capacity = compute_capacity(funnel.comparable, exit_liquidity_by_trade)
    verdict = evaluate_verdict(funnel=funnel, economics=economics, robustness=robustness)
    observed_hashes = tuple(sorted({e.contract_sha256.hex() for e in dataset.episodes}))

    return NetEvidenceReport(
        report_version=REPORT_VERSION,
        generated_at=datetime.now(UTC),
        code_revision=normalize_code_revision(code_revision),
        working_tree_dirty=working_tree_dirty,
        formal_run=not working_tree_dirty,
        db_snapshot_at=dataset.db_snapshot_at,
        cohort_start=dataset.cohort_start,
        cohort_end=dataset.cohort_end,
        expected_contract_sha256=EXPECTED_CONTRACT_SHA256_HEX,
        observed_contract_sha256=observed_hashes,
        dataset_fingerprint=dataset_fingerprint(dataset),
        funnel=funnel,
        economics=economics,
        concurrency=concurrency,
        robustness=robustness,
        capacity=capacity,
        verdict=verdict,
        legacy_context=legacy_context,
    )


# --- Rendering --------------------------------------------------------


def render_json(report: NetEvidenceReport) -> str:
    return json.dumps(_json_ready(asdict(report)), indent=2, sort_keys=True, default=str)


def _comparable_row(t: ComparableTrade) -> tuple[Any, ...]:
    return (
        t.trade_id,
        t.cluster_key,
        t.exit_at.isoformat(),
        _num(t.net_pnl_usd),
        _pct(t.net_pnl_pct),
        t.exit_reason,
    )


def render_markdown(report: NetEvidenceReport) -> str:
    f = report.funnel
    e = report.economics
    c = report.concurrency
    r = report.robustness
    cap = report.capacity
    v = report.verdict

    lines = [
        f"# early_momentum_v4 net evidence -- {report.verdict.verdict}",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        (
            f"Provenance: code_revision={report.code_revision} "
            f"working_tree_dirty={report.working_tree_dirty} "
            f"formal_run={report.formal_run}"
        ),
        "",
    ]
    if not report.formal_run:
        lines += [
            "> **NOT A FORMAL RUN** -- working tree was dirty. The verdict below is "
            "provisional/local-only and never authorizes LIVE_MICRO on its own; re-run "
            "against a clean, committed revision before acting on a "
            f"`{VERDICT_PASS_LIVE_MICRO_CANDIDATE}` result.",
            "",
        ]

    lines += [
        "## Contract and reproducibility fingerprint",
        "",
    ]
    lines += _table(
        ("Field", "Value"),
        [
            ("report_version", report.report_version),
            ("strategy", f"{STRATEGY_NAME} v{STRATEGY_VERSION}"),
            ("mode / side", f"{MODE} / {SIDE}"),
            ("accounting_version", ACCOUNTING_VERSION),
            ("cohort_start", report.cohort_start.isoformat()),
            ("cohort_end", report.cohort_end.isoformat()),
            ("db_snapshot_at", report.db_snapshot_at.isoformat()),
            ("expected_contract_sha256", report.expected_contract_sha256),
            ("observed_contract_sha256(es)", ", ".join(report.observed_contract_sha256) or "—"),
            ("dataset_fingerprint", report.dataset_fingerprint),
        ],
    )

    lines += ["", "## Verdict", ""]
    lines += _table(
        ("Field", "Value"),
        [
            ("verdict", v.verdict),
            ("is_interim_checkpoint", v.is_interim_checkpoint),
            ("reasons", ", ".join(v.reasons) or "—"),
        ],
    )

    lines += ["", "## Evidence funnel", ""]
    lines += _table(
        ("Step", "Label", "Remaining", "Excluded", "Reason", "Example IDs"),
        [
            (
                step.step,
                step.label,
                step.remaining,
                step.excluded,
                step.exclusion_reason or "—",
                ", ".join(step.example_ids) or "—",
            )
            for step in f.steps
        ],
    )

    lines += ["", "## Comparable trades (final set)", ""]
    lines += _table(
        ("Trade", "Cluster", "Exit at", "Net PnL (USD)", "Net return", "Exit reason"),
        [_comparable_row(t) for t in sorted(f.comparable, key=lambda t: t.exit_at)],
    )

    lines += ["", "## Integrity violations", ""]
    lines += ["### Cohort-level (block the whole report)", ""]
    lines += _table(
        ("Code", "Detail"),
        [(x.code, x.detail) for x in f.cohort_violations],
    )
    lines += ["", "### Row-level (excluded from comparable set, block formal PASS)", ""]
    lines += _table(
        ("Code", "Episode", "Trade", "Detail"),
        [(x.code, x.episode_id or "—", x.trade_id or "—", x.detail) for x in f.row_violations],
    )

    lines += ["", "## Economics", ""]
    lines += _table(
        ("Metric", "Value"),
        [
            ("Closed trades", e.closed_trades),
            ("Wins / losses", f"{e.wins} / {e.losses}"),
            ("Win rate", _pct(e.win_rate_pct)),
            ("Mean net return (notional)", _pct(e.net_return_on_notional.mean_pct)),
            ("Median net return (notional)", _pct(e.net_return_on_notional.median_pct)),
            ("Mean net return (margin)", _pct(e.net_return_on_margin.mean_pct)),
            ("Median net return (margin)", _pct(e.net_return_on_margin.median_pct)),
            (
                "Net return p05/p25/p50/p75/p95 (notional)",
                " / ".join(
                    _pct(x)
                    for x in (
                        e.net_return_on_notional.p05_pct,
                        e.net_return_on_notional.p25_pct,
                        e.net_return_on_notional.p50_pct,
                        e.net_return_on_notional.p75_pct,
                        e.net_return_on_notional.p95_pct,
                    )
                ),
            ),
            (
                "Total gross / net PnL (USD)",
                f"{_num(e.total_gross_pnl_usd)} / {_num(e.total_net_pnl_usd)}",
            ),
            (
                "Total fees / funding / slippage (USD)",
                f"{_num(e.total_fees_usd)} / {_num(e.total_funding_usd)} / "
                f"{_num(e.total_slippage_usd)}",
            ),
            ("Profit factor", _num(e.profit_factor)),
            ("Worst trade net PnL (USD)", _num(e.worst_trade_net_pnl_usd)),
            ("Worst losing streak", e.worst_losing_streak),
            ("Max realized drawdown (USD)", _num(e.max_drawdown_usd)),
        ],
    )

    lines += ["", "### By cluster", ""]
    lines += _table(
        ("Cluster", "Trades", "Net PnL (USD)", "Mean net return"),
        [(g.key, g.trades, _num(g.net_pnl_usd), _pct(g.mean_net_return_pct)) for g in e.by_cluster],
    )
    lines += ["", "### By UTC day", ""]
    lines += _table(
        ("Day", "Trades", "Net PnL (USD)", "Mean net return"),
        [(g.key, g.trades, _num(g.net_pnl_usd), _pct(g.mean_net_return_pct)) for g in e.by_utc_day],
    )
    lines += ["", "### By UTC week", ""]
    lines += _table(
        ("Week", "Trades", "Net PnL (USD)", "Mean net return"),
        [
            (g.key, g.trades, _num(g.net_pnl_usd), _pct(g.mean_net_return_pct))
            for g in e.by_utc_week
        ],
    )
    lines += ["", "### By source exchange", ""]
    lines += _table(
        ("Exchange", "Trades", "Net PnL (USD)", "Mean net return"),
        [
            (g.key, g.trades, _num(g.net_pnl_usd), _pct(g.mean_net_return_pct))
            for g in e.by_source_exchange
        ],
    )
    lines += ["", "### By exit reason", ""]
    lines += _table(
        ("Exit reason", "Trades", "Net PnL (USD)", "Mean net return"),
        [
            (g.key, g.trades, _num(g.net_pnl_usd), _pct(g.mean_net_return_pct))
            for g in e.by_exit_reason
        ],
    )

    lines += ["", "## Concurrency and entry waves", ""]
    lines += _table(
        ("Metric", "Value"),
        [
            ("Max concurrent positions", c.max_concurrent_positions),
            ("Time-weighted mean concurrency", _num(c.time_weighted_mean_concurrency)),
            ("p95 concurrency", _num(c.p95_concurrency)),
            (
                "Max / mean deployed notional (USD)",
                f"{_num(c.max_deployed_notional_usd)} / {_num(c.mean_deployed_notional_usd)}",
            ),
            (
                "Max / mean required margin (USD)",
                f"{_num(c.max_required_margin_usd)} / {_num(c.mean_required_margin_usd)}",
            ),
            ("Top-1 cluster PnL share", _pct(c.top1_cluster_pnl_share_pct)),
            ("Top-5 cluster PnL share", _pct(c.top5_cluster_pnl_share_pct)),
            ("Best UTC day PnL share", _pct(c.best_utc_day_pnl_share_pct)),
            ("Best UTC week PnL share", _pct(c.best_utc_week_pnl_share_pct)),
        ],
    )
    lines += ["", "### Entry waves", ""]
    lines += _table(
        ("Wave", "Start", "End", "Trades", "Net PnL (USD)"),
        [
            (w.wave_id, w.start_at.isoformat(), w.end_at.isoformat(), w.trades, _num(w.net_pnl_usd))
            for w in c.waves
        ],
    )

    lines += ["", "## Robustness", ""]
    lines += [r.caveat, ""]
    bootstrap = r.block_bootstrap
    lines += _table(
        ("Metric", "Value"),
        [
            (
                "Leave-best-asset-out mean net return",
                _pct(r.leave_best_asset_out_mean_net_return_pct),
            ),
            (
                "Mean net return excluding best UTC day",
                _pct(r.mean_net_return_excluding_best_utc_day_pct),
            ),
            (
                f"Block bootstrap {int(r.confidence_level * 100)}% CI "
                "(mean net return, by UTC day)",
                (
                    f"[{_pct(bootstrap.lower_bound)}, {_pct(bootstrap.upper_bound)}]"
                    f" (point {_pct(bootstrap.point_estimate)}, "
                    f"{bootstrap.clusters} day-blocks)"
                    if bootstrap
                    else "—"
                ),
            ),
            ("Bootstrap iterations / seed", f"{r.bootstrap_iterations} / {r.bootstrap_seed}"),
        ],
    )
    lines += ["", "### Leave-one-week-out", ""]
    lines += _table(
        ("Week excluded", "Mean net return with week excluded"),
        [(week, _pct(value)) for week, value in r.leave_one_week_out],
    )

    lines += ["", "## Capacity evidence", ""]
    lines += [cap.caveat, ""]
    lines += _table(
        ("Metric", "Value"),
        [
            ("Comparable trades", cap.comparable_trades),
            ("Entry impact data coverage", _pct(cap.entry_impact_coverage_pct)),
            ("Exit liquidity observation coverage", _pct(cap.exit_liquidity_coverage_pct)),
            (
                "Mean / p95 entry ask impact (bps)",
                f"{_num(cap.mean_entry_ask_impact_bps)} / {_num(cap.p95_entry_ask_impact_bps)}",
            ),
            (
                "Mean / p95 exit bid impact (bps)",
                f"{_num(cap.mean_exit_bid_impact_bps)} / {_num(cap.p95_exit_bid_impact_bps)}",
            ),
            ("Mean exit spread (bps)", _num(cap.mean_exit_spread_bps)),
            (
                "Observed entry notional(s) (USD)",
                ", ".join(_num(x) for x in cap.observed_entry_notional_usd) or "—",
            ),
        ],
    )

    lines += ["", "## Legacy version context (descriptive only, never combined with v4)", ""]
    lines += _table(
        (
            "Strategy label",
            "Total",
            "Closed",
            "Cancelled",
            "Open",
            "Complete-accounting closed",
            "Net PnL (complete only, USD)",
        ),
        [
            (
                row.setup_context_strategy,
                row.total_trades,
                row.closed_trades,
                row.cancelled_trades,
                row.open_trades,
                row.complete_accounting_closed_trades,
                _num(row.total_net_pnl_usd_complete_only),
            )
            for row in report.legacy_context
        ],
    )

    return "\n".join(lines) + "\n"


# --- CLI ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only net-edge evidence report for early_momentum_v4"
    )
    parser.add_argument(
        "--cohort-end",
        type=parse_utc_datetime,
        required=True,
        help="exclusive UTC ISO-8601 cohort end; must be >= 6h older than the DB's own clock",
    )
    parser.add_argument("--code-revision", type=str, required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    dirty.add_argument("--working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for early-momentum-net-evidence-report")
    report = await generate_report(
        db_url=db_url,
        cohort_end=args.cohort_end,
        code_revision=args.code_revision,
        working_tree_dirty=bool(args.working_tree_dirty),
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
