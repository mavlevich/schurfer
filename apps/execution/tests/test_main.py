import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from schurfer_execution.main import _preload_markets, lifespan
from schurfer_execution.supervisor import WorkerState


async def test_preload_markets_isolates_optional_venue_failure() -> None:
    healthy = MagicMock()
    healthy.load_markets = AsyncMock(return_value={})
    unavailable = MagicMock()
    unavailable.load_markets = AsyncMock(side_effect=RuntimeError("maintenance"))

    failed = await _preload_markets({"bybit": healthy, "optional": unavailable})

    assert failed == {"optional"}
    healthy.load_markets.assert_awaited_once_with()
    unavailable.load_markets.assert_awaited_once_with()


async def test_disabled_strategy_workers_do_not_crash_startup() -> None:
    cfg = SimpleNamespace(
        redis_addr="localhost:6379",
        auto_trade=False,
        dry_run=False,
        db_url=None,
        pump_short_mode=None,
        early_momentum_mode=None,
        liquidation_cascade_mode=None,
        source_lead_mode=None,
        market_refresh_interval_seconds=900.0,
        market_refresh_min_interval_seconds=60.0,
    )
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=True)
    rdb.aclose = AsyncMock()
    clients = SimpleNamespace(
        market={},
        trading={},
        strategy_clients=MagicMock(return_value={}),
    )
    app = FastAPI()

    with (
        patch("schurfer_execution.main.Config", return_value=cfg),
        patch("schurfer_execution.main.aioredis.from_url", return_value=rdb),
        patch("schurfer_execution.main.build_exchange_clients", return_value=clients),
        patch("schurfer_execution.main.close_exchange_clients", AsyncMock()),
    ):
        async with lifespan(app):
            workers = app.state.supervisor.workers
            assert workers["signal_trader"].state == WorkerState.STOPPED_INTENTIONALLY
            assert workers["paper_monitor"].state == WorkerState.STOPPED_INTENTIONALLY
            assert workers["liquidation_cascade_scanner"].state == WorkerState.STOPPED_INTENTIONALLY
            assert workers["early_momentum_scanner"].state == WorkerState.STOPPED_INTENTIONALLY


def test_the_early_momentum_trigger_runs_whatever_the_strategy_mode() -> None:
    """The trigger also maintains open trades, so EARLY_MOMENTUM_MODE=disabled must not
    stop it; only paper mode off or no database does (colleague review of #505, P1)."""
    from schurfer_execution.main import early_momentum_trigger_enabled

    def cfg(**fields: object) -> SimpleNamespace:
        base = {"dry_run": True, "db_url": "postgresql://x", "early_momentum_mode": "disabled"}
        return SimpleNamespace(**{**base, **fields})

    assert early_momentum_trigger_enabled(cfg())
    assert not early_momentum_trigger_enabled(cfg(dry_run=False))
    assert not early_momentum_trigger_enabled(cfg(db_url=None))


_PROD_COMPOSE = Path(__file__).resolve().parents[3] / "infra/docker/docker-compose.prod.yml"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "disabled"), ("", ""), ("paper", "paper")],
    ids=["unset", "explicitly_empty", "set"],
)
def test_prod_compose_pump_short_mode_substitution(
    value: str | None, expected: str, tmp_path: Path
) -> None:
    """pump-short is closed, so an unset PUMP_SHORT_MODE disables it; an explicitly
    empty one must reach Config as empty (None), because Config refuses any value under
    AUTO_TRADE=true. Checked with Docker Compose's own substitution, not a string."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker compose is required to check variable substitution")
    match = re.search(r"PUMP_SHORT_MODE: (\$\{PUMP_SHORT_MODE[^}]*\})", _PROD_COMPOSE.read_text())
    assert match is not None
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  execution:\n    image: busybox\n"
        f"    environment:\n      PUMP_SHORT_MODE: {match.group(1)}\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PUMP_SHORT_MODE"}
    if value is not None:
        env["PUMP_SHORT_MODE"] = value
    rendered = subprocess.run(  # noqa: S603 fixed argv, docker resolved via PATH
        [docker, "compose", "-f", str(compose), "config", "--format", "json"],
        capture_output=True,
        check=True,
        env=env,
        text=True,
    )
    environment = json.loads(rendered.stdout)["services"]["execution"]["environment"]
    assert environment["PUMP_SHORT_MODE"] == expected
