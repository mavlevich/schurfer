from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.momentum_flow_discovery_report import (
    PUMP_LEAD_MINUTES,
    VENUE_VERSIONS,
    _run,
    build_discovery_report,
)
from schurfer_analytics.momentum_flow_discovery_repository import (
    DiscoveryDataset,
    PaperProbe,
    Pump,
    PumpObservability,
    RunContract,
    WatchDecision,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

NOW = datetime(2026, 8, 20, 18, tzinfo=UTC)
CAPTURE_START = datetime(2026, 8, 1, tzinfo=UTC)
SINCE = datetime(2026, 8, 2, tzinfo=UTC)
UNTIL = datetime(2026, 8, 12, tzinfo=UTC)


def _watch(at: datetime, *, exchange: str = "bybit", symbol: str = "EDGEUSDT") -> WatchDecision:
    return WatchDecision(
        exchange=exchange,
        symbol=symbol,
        bucket_start=at - timedelta(minutes=1),
        decision_at=at,
        source_event_at=at - timedelta(seconds=3),
        source_received_at=at - timedelta(seconds=2),
        bucket_ready_at=at - timedelta(seconds=1),
        evaluator_started_at=at - timedelta(milliseconds=20),
        evaluator_completed_at=at,
    )


def _probe(
    paper_id: str,
    *,
    net_return_pct: float | None,
    net_pnl_usd: float | None,
    symbol: str = "EDGEUSDT",
) -> PaperProbe:
    entry_at = SINCE + timedelta(days=2, minutes=int(paper_id))
    complete = net_return_pct is not None
    return PaperProbe(
        paper_id=paper_id,
        paper_version=VENUE_VERSIONS[0].paper_version,
        exchange="bybit",
        symbol=symbol,
        watch_decision_at=entry_at - timedelta(seconds=2),
        entry_status="opened",
        entry_reason=None,
        entry_quote_latency_ms=50,
        entry_filled_notional_usd=50.0,
        entry_spread_bps=2.0,
        entry_impact_bps=1.0,
        entry_at=entry_at,
        position_status="closed" if complete else "exit_unresolved",
        exit_reason="max_hold" if complete else None,
        exit_quote_latency_ms=80 if complete else None,
        exit_spread_bps=3.0 if complete else None,
        exit_impact_bps=1.5 if complete else None,
        exit_at=entry_at + timedelta(hours=4) if complete else None,
        max_favorable_return_pct=4.0 if complete else None,
        max_adverse_return_pct=-1.0 if complete else None,
        net_return_pct=net_return_pct,
        net_pnl_usd=net_pnl_usd,
        fees_usd=0.10 if complete else None,
        funding_usd=0.01 if complete else None,
        accounting_status="complete" if complete else "incomplete",
    )


def _runs() -> tuple[dict[str, RunContract], dict[str, RunContract]]:
    watch_runs = {
        item.watch_version: RunContract(item.watch_version, item.watch_contract_sha256, SINCE)
        for item in VENUE_VERSIONS
    }
    paper_runs = {
        item.paper_version: RunContract(item.paper_version, item.paper_contract_sha256, SINCE)
        for item in VENUE_VERSIONS
    }
    return watch_runs, paper_runs


def _dataset(
    *,
    watches: Sequence[WatchDecision] = (),
    pumps: Sequence[Pump] = (),
    observations: dict[int, PumpObservability] | None = None,
    probes: Sequence[PaperProbe] = (),
    minutes: dict[str, tuple[datetime, ...]] | None = None,
) -> DiscoveryDataset:
    watch_runs, paper_runs = _runs()
    return DiscoveryDataset(
        watch_runs=watch_runs,
        paper_runs=paper_runs,
        available_minutes=minutes or {"bybit": (), "binance": ()},
        watches=tuple(watches),
        pumps=tuple(pumps),
        pump_observability=observations or {},
        probes=tuple(probes),
    )


@pytest.fixture
def base_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        since=SINCE,
        until=UNTIL,
        capture_epoch_started_at=CAPTURE_START,
        accept_new_cohort_boundary=False,
        cohort_state_path=str(tmp_path / "discovery-cohort.json"),
        code_revision="abc123",
        no_working_tree_dirty=True,
        working_tree_dirty=False,
    )


class _FakeRepository:
    def __init__(self, dataset: DiscoveryDataset, *, fail: bool = False) -> None:
        self.dataset = dataset
        self.fail = fail
        self.load_calls = 0

    async def load(self, **_: object) -> DiscoveryDataset:
        self.load_calls += 1
        if self.fail:
            raise RuntimeError("database failed")
        return self.dataset


@pytest.mark.asyncio
async def test_run_requires_database_url(
    base_args: argparse.Namespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        await _run(base_args, repository=_FakeRepository(_dataset()), now=NOW)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_parses_datetime_boundary_and_persists_separate_state_after_success(
    base_args: argparse.Namespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    repository = _FakeRepository(_dataset())

    payload = json.loads(
        await _run(base_args, repository=repository, now=NOW)  # type: ignore[arg-type]
    )

    state_path = Path(base_args.cohort_state_path)
    assert repository.load_calls == 1
    assert state_path.name == "discovery-cohort.json"
    assert state_path.exists()
    assert payload["cohort_boundaries"]["capture_epoch_started_at"] == (CAPTURE_START.isoformat())


@pytest.mark.asyncio
async def test_failed_read_does_not_mutate_cohort_state(
    base_args: argparse.Namespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    with pytest.raises(RuntimeError, match="database failed"):
        await _run(
            base_args,
            repository=_FakeRepository(_dataset(), fail=True),  # type: ignore[arg-type]
            now=NOW,
        )
    assert not Path(base_args.cohort_state_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda args: setattr(args, "since", args.until), "strictly before"),
        (lambda args: setattr(args, "until", NOW), "not fully mature"),
        (
            lambda args: setattr(args, "since", args.until - timedelta(days=29)),
            "maximum window width",
        ),
        (
            lambda args: setattr(args, "working_tree_dirty", True),
            "exactly one working-tree dirty flag",
        ),
    ],
)
async def test_run_validation_is_fail_closed(
    base_args: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
    message: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    mutate(base_args)  # type: ignore[operator]
    with pytest.raises(ValueError, match=message):
        await _run(base_args, repository=_FakeRepository(_dataset()), now=NOW)  # type: ignore[arg-type]


def test_report_uses_real_observations_and_keeps_missing_values_unresolved() -> None:
    watch_at = SINCE + timedelta(days=2)
    pump_at = watch_at + timedelta(hours=2)
    pump = Pump(
        pump_id=7,
        exchange="bybit",
        symbol="EDGEUSDT",
        trigger_at=pump_at,
        watch_version=VENUE_VERSIONS[0].watch_version,
    )
    expected_pump_minutes = PUMP_LEAD_MINUTES + 1
    dataset = _dataset(
        watches=(_watch(watch_at),),
        pumps=(pump,),
        observations={
            7: PumpObservability(
                pump_id=7,
                operational_minutes=expected_pump_minutes,
                quality_minutes=expected_pump_minutes,
                earliest_watch_at=watch_at,
            )
        },
        probes=(_probe("1", net_return_pct=2.0, net_pnl_usd=1.0),),
        minutes={"bybit": (watch_at.replace(second=0),), "binance": ()},
    )

    report = build_discovery_report(
        dataset,
        since=SINCE,
        until=UNTIL,
        capture_epoch_started_at=CAPTURE_START,
        generated_at=NOW,
        code_revision="abc123",
        working_tree_dirty=False,
    )
    bybit = report["results"]["bybit"]

    assert bybit["watch_precision"]["value"] == 1.0
    assert bybit["precursor_recall"]["value"] == 1.0
    assert bybit["precursor_recall"]["median_lead_minutes"] == 120.0
    assert bybit["paper_win_rate"]["value"] == 1.0
    assert bybit["entry_funnel"]["entry_status"] == {"opened": 1}
    assert bybit["entry_funnel"]["complete_exits"] == 1
    assert bybit["trade_expectancy_pct"] == 2.0
    assert bybit["mfe_pct"]["mean"] == 4.0
    assert bybit["entry_capacity"]["liquidity_capacity_usd"] is None
    assert bybit["stability"]["btc_regime"]["status"] == "unresolved"
    assert report["recommendation"] == "NOT_READY"


def test_unresolved_probe_is_not_counted_as_zero_return() -> None:
    dataset = _dataset(probes=(_probe("2", net_return_pct=None, net_pnl_usd=None),))

    report = build_discovery_report(
        dataset,
        since=SINCE,
        until=UNTIL,
        capture_epoch_started_at=CAPTURE_START,
        generated_at=NOW,
        code_revision="abc123",
        working_tree_dirty=False,
    )
    bybit = report["results"]["bybit"]

    assert bybit["paper_win_rate"]["denominator"] == 0
    assert bybit["paper_win_rate"]["unresolved"] == 1
    assert bybit["entry_funnel"]["unresolved"] == 1
    assert bybit["trade_expectancy_pct"] is None
    assert bybit["mfe_pct"]["mean"] is None
    assert bybit["max_drawdown_usd"] is None
    assert "no_complete_paper_probes" in report["readiness"]["venues"]["bybit"]["reasons"]


def test_make_report_targets_keep_local_and_prod_entrypoints_separate() -> None:
    project_root = Path(__file__).resolve().parents[3]
    targets = {
        "momentum-flow-episode-study-report": "momentum-flow-episode-study-report",
        "momentum-flow-discovery-report": "momentum-flow-discovery-report",
        "prod-momentum-flow-episode-study-report": "momentum-flow-episode-study-report",
        "prod-momentum-flow-discovery-report": "momentum-flow-discovery-report",
    }
    for target, expected_entrypoint in targets.items():
        completed = subprocess.run(  # noqa: S603 - fixed executable and reviewed targets
            [
                "/usr/bin/make",
                "-n",
                target,
                "ARGS=--since 2026-08-02T00:00:00Z --until 2026-08-12T00:00:00Z "
                "--capture-epoch-started-at 2026-08-01T00:00:00Z",
            ],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
        output = completed.stdout + completed.stderr
        assert "overriding commands for target" not in output
        assert output.count(expected_entrypoint) >= 1
        other = (
            "momentum-flow-discovery-report"
            if expected_entrypoint == "momentum-flow-episode-study-report"
            else "momentum-flow-episode-study-report"
        )
        assert other not in output
