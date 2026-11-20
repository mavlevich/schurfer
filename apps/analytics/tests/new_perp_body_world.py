"""A synthetic sealed metadata root and a synthetic Bybit for the I25 Stage 1b body tests.

No network and no holdout data: every launch, size, price and rate here is made up. Prices
and rates in the bodies are SENTINELS, so a test can prove that no file of a body run root
holds one. The sealed root is written with the same files the real metadata run writes, and
its rows are built by the real metadata functions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from schurfer_analytics import new_perp_stage1 as s
from schurfer_analytics import new_perp_stage1b as meta
from schurfer_analytics import new_perp_stage1b_body as body
from schurfer_analytics.new_perp_window import HOLDOUT

from .new_perp_world import Clock, Launch, World, ms

if TYPE_CHECKING:
    from pathlib import Path

HOLDOUT_START = "2023-03-01T10:00:00"


def slot_of(symbol: str) -> str:
    return hashlib.sha256(b"slot:" + symbol.encode()).hexdigest()


def sealed_row(launch: Launch, *, size: int = 4096, **override: Any) -> dict[str, Any]:
    """One sealed catalog fact, as the metadata run writes it for a launch with a first file."""
    item = {
        "symbol": launch.symbol,
        "launchTime": str(launch.launch_ms),
        "deliveryTime": str(launch.delivery_ms or 0),
        "contractType": "LinearPerpetual",
        "settleCoin": "USDT",
        "fundingInterval": launch.funding_interval_min,
        "lotSizeFilter": {
            "minNotionalValue": launch.min_notional,
            "qtyStep": "1",
            "minOrderQty": "1",
        },
        "leverageFilter": {"maxLeverage": launch.max_leverage},
    }
    page = {"retCode": 0, "result": {"list": [item], "nextPageCursor": ""}}
    parsed = meta.catalog_page(json.dumps(page).encode())["rows"][0]
    first = s.day_of(launch.launch_ms + launch.first_file_offset_days * s.DAY_MS)
    days = [s.add_days(first, i) for i in range(launch.listing_days)]
    days = [d for d in days if d < HOLDOUT.cut_day]
    facts = meta.catalog_facts(parsed, {launch.symbol: set(days)}, "ok")
    facts["catalog_identity"] = "not_contradicted"
    if "first_file_day" in facts:
        facts["head"] = {"key": "0" * 64, "outcome": "ok", "content_length": size}
    facts.update(override)
    return facts


@dataclass
class Sealed:
    root: Path
    pins: body.Pins
    sealed: body.Sealed
    slots: dict[str, str]  # symbol -> slot, for tests only


def write_sealed(
    tmp_path: Path,
    world: World,
    *,
    sizes: dict[str, int] | None = None,
    rows: dict[str, dict[str, Any]] | None = None,
    name: str = "metadata",
    tweak_report: dict[str, Any] | None = None,
) -> Sealed:
    root = tmp_path / name
    (root / "catalog").mkdir(parents=True)
    slots: dict[str, str] = {}
    facts: dict[str, dict[str, Any]] = {}
    for launch in world.launches:
        slot = slot_of(launch.symbol)
        slots[launch.symbol] = slot
        row = (rows or {}).get(launch.symbol)
        facts[slot] = row or sealed_row(launch, size=(sizes or {}).get(launch.symbol, 4096))
    for slot, row in facts.items():
        (root / "catalog" / f"{slot}.json").write_text(json.dumps(row))
    manifest = {
        "complete": True,
        "catalog_slots": len(facts),
        "catalog_keys": sorted(facts),
        "admitted_keys": [],
        "body_phase_authorized": False,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "run.json").write_text(json.dumps({"synthetic": True}))
    mapping = {
        str(p.relative_to(root)): s.digest(p.read_bytes()) for p in sorted(root.rglob("*.json"))
    }
    (root / "evidence-hashes.json").write_text(json.dumps(mapping))
    files = [r for r in facts.values() if "head" in r]
    near = sum(
        r.get("first_file_within_1d_of_launch") is False
        and r.get("filename_identity_unverified", True) is False
        for r in facts.values()
    )
    report: dict[str, Any] = {
        "outcome": "metadata_complete_coverage_pending",
        "complete": True,
        "binding_valid": True,
        "stop_reason": None,
        "known_coverage_failures": [],
        "body_phase_authorized": False,
        "request_list_sha256": "c" * 64,
        "manifest_sha256": s.digest((root / "manifest.json").read_bytes()),
        "evidence_map_sha256": s.digest((root / "evidence-hashes.json").read_bytes()),
        "catalog_count": len(facts),
        "m2_i_no_file_near_launch": near,
        "m2_i_unproven": 0,
        "m2ii": {
            "share": 0.0,
            "archive_only_in_window": 0,
            "archive_only_pre_window": 3,
            "unverified_directories": 0,
            "collision_directories": 0,
        },
        **(tweak_report or {}),
    }
    (root / "metadata-report.json").write_text(json.dumps(report))
    (root / "metadata_done.json").write_text(json.dumps({"complete": True}))
    pins = body.Pins(
        metadata_contract_sha256="c" * 64,
        metadata_report_sha256=s.digest((root / "metadata-report.json").read_bytes()),
        manifest_sha256=s.digest((root / "manifest.json").read_bytes()),
        catalog_slots=len(facts),
        first_files=len(files),
        first_bytes=sum(r["head"]["content_length"] for r in files),
    )
    return Sealed(root, pins, body.load_sealed(root, pins), slots)


def contract_for(pins: body.Pins, **limits: dict[str, int]) -> dict[str, Any]:
    """The contract of a test, with optional go limit overrides, re-hashed."""
    contract = body.build_contract(pins)
    for go, values in limits.items():
        contract["go_limits"][go.removeprefix("go")].update(values)
    contract["request_list_sha256"] = s.contract_hash(contract)
    return contract


def body_fetcher(
    world: World,
    root: Path,
    sealed: Sealed,
    *,
    go: int = 1,
    contract: dict[str, Any] | None = None,
    code: dict[str, Any] | None = None,
    clock: Clock | None = None,
    client: httpx.Client | None = None,
) -> tuple[body.BodyFetcher, Clock]:
    clock = clock or Clock()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    http = client or httpx.Client(transport=world.transport())
    fetcher = body.BodyFetcher(
        root,
        contract or contract_for(sealed.pins),
        http,
        go=go,
        code=code or {"synthetic": True},
        specs=body.class_specs(sealed.pins),
        now=clock.now,
        sleep=clock.sleep,
        wall=clock.wall,
    )
    return fetcher, clock


def run_both(
    world: World, tmp_path: Path, sealed: Sealed, *, contract: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    root = tmp_path / "body"
    a, _ = body_fetcher(world, root, sealed, contract=contract)
    report_a = body.run_a(a, sealed.sealed, sealed.pins)
    if not report_a["go_b_allowed"]:
        return report_a, report_a, root
    b, _ = body_fetcher(world, root, sealed, go=2, contract=contract)
    report = body.run_b(b, sealed.sealed, body.queue_b_sha256(root))
    return report_a, report, root


def holdout_world(count: int, *, step_days: int = 2, start: str = HOLDOUT_START) -> World:
    return World.evenly(count, start=start, step_days=step_days)


def launch_at(symbol: str, launch_iso: str, *, delay_min: int = 10, **kwargs: Any) -> Launch:
    return Launch(
        symbol,
        symbol.removesuffix("USDT"),
        ms(launch_iso),
        first_trade_delay_ms=delay_min * s.MINUTE_MS,
        **kwargs,
    )


def all_text(root: Path) -> str:
    return "".join(p.read_text(errors="replace") for p in sorted(root.rglob("*")) if p.is_file())


def request_numbers(world: World) -> list[int]:
    """Every numeric request parameter that is a millisecond stamp."""
    out = []
    for request in world.requests:
        for value in request.url.params.values():
            if value.isdigit() and len(value) == 13:
                out.append(int(value))
    return out
