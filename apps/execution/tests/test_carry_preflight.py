from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from schurfer_execution import bybit_preflight as bp
from schurfer_execution import carry_preflight as cp

if TYPE_CHECKING:
    from pathlib import Path

READ_ONLY_KEY = {
    "apiKey": "THE-KEY-ID",
    "userID": 12345,
    "readOnly": 1,
    "permissions": {"ContractTrade": [], "Spot": []},
    "ips": [],
}
TIERS = [
    {"symbol": "ABCUSDT", "riskLimitValue": "100", "maintenanceMargin": "0.005"},
    {"symbol": "ABCUSDT", "riskLimitValue": "1000", "maintenanceMargin": "0.01"},
    {"symbol": "ABCUSDT", "riskLimitValue": "100000", "maintenanceMargin": "0.025"},
]
CONFIRMED = date(2026, 10, 7)
LIMIT = Decimal(150)


# --- the client can only read ------------------------------------------------------


def test_the_client_refuses_every_path_outside_its_allow_list() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"retCode": 0, "result": {}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = bp.BybitReadOnlyClient("k", "s", http, allowed_paths=cp.ALLOWED_GET_PATHS)
    for path in ("/v5/order/create", "/v5/position/add-margin", "/v5/asset/transfer"):
        with pytest.raises(bp.ForbiddenPathError):
            asyncio.run(client.get(path, {}))
    assert calls == []


def test_the_module_has_no_write_path() -> None:
    assert all(p.startswith(("/v5/user/", "/v5/account/")) for p in cp.ALLOWED_GET_PATHS)
    source = inspect.getsource(cp)
    for forbidden in ("http.post", ".post(", "ccxt", "create_order", "/v5/position/"):
        assert forbidden not in source


# --- the arithmetic ----------------------------------------------------------------


def test_the_maintenance_rate_is_the_lowest_tier_covering_the_stressed_notional() -> None:
    assert cp.maintenance_rate(TIERS, Decimal(200)) == Decimal("0.01")
    assert cp.maintenance_rate(TIERS, Decimal(100)) == Decimal("0.005")
    assert cp.maintenance_rate(TIERS, Decimal(10**6)) is None  # no tier covers it
    bad = [{"riskLimitValue": "x", "maintenanceMargin": "0.01"}]
    assert cp.maintenance_rate(bad, Decimal(1)) is None


def test_the_capital_rule_covers_the_stress_maintenance_and_fees() -> None:
    got = cp.capital_for_pair(Decimal("0.001"), Decimal("0.00055"), Decimal("0.01"))
    # spot 50 + 0.05; short: loss 150 + mm 200*0.01 + fees 50*0.00055 + 200*0.00055
    assert Decimal(got["spot_cost_usd"]) == Decimal("50.05")
    assert Decimal(got["short_collateral_usd"]) == Decimal("152.1375")
    assert Decimal(got["margin_to_add_usd"]) == Decimal("102.1375")  # above the 1x margin
    assert Decimal(got["total_usd"]) == Decimal("202.1875")
    assert got["fits"] == "true"
    big = cp.capital_for_pair(Decimal("0.001"), Decimal("0.00055"), Decimal("0.5"))
    assert big["fits"] == "false"


# --- the verdict -------------------------------------------------------------------


def _snapshot(**pair: Any) -> dict[str, Any]:
    raw = {
        "spot_fee": [{"symbol": "ABCUSDT", "takerFeeRate": "0.001"}],
        "perp_fee": [{"symbol": "ABCUSDT", "takerFeeRate": "0.00055"}],
        "collateral": [{"currency": "ABC", "marginCollateral": True, "collateralSwitch": False}],
        "risk_limits": TIERS,
    }
    raw.update(pair)
    return {
        "api_key": READ_ONLY_KEY,
        "account": {"marginMode": "ISOLATED_MARGIN"},
        "pairs": {"ABCUSDT": raw},
        "taken_at": "2026-10-31T10:00:00+00:00",
    }


def _evaluate(snapshot: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "symbols": ["ABCUSDT"],
        "owner_confirmed_trading": CONFIRMED,
        "owner_confirmed_add_margin": CONFIRMED,
        "add_margin_limit_usd": LIMIT,
    }
    kwargs.update(overrides)
    return cp.evaluate(snapshot, **kwargs)


def test_a_pair_that_fits_passes_and_records_every_fact() -> None:
    got = _evaluate(_snapshot())
    assert got["verdict"] == "pass", got["reasons"]
    pair = got["facts"]["pairs"]["ABCUSDT"]
    assert pair["maintenance_rate_at_stress"] == "0.01"
    assert pair["spot_can_be_collateral"] is True
    assert pair["spot_collateral_switched_on"] is False
    assert got["facts"]["add_margin_limit_usd"] == "150"


def test_isolated_mode_alone_does_not_pass_without_the_add_margin_confirmation() -> None:
    """Review of #507, P1: account/info reports the account's mode, not whether ~USD 102
    can be added to the position."""
    for overrides in (
        {"owner_confirmed_add_margin": None},
        {"add_margin_limit_usd": None},
    ):
        got = _evaluate(_snapshot(), **overrides)
        assert got["verdict"] == "blocked"
        assert "add_margin_unconfirmed" in got["reasons"]
    got = _evaluate(_snapshot(), add_margin_limit_usd=Decimal(100))
    assert "pair_exceeds_add_margin_limit:ABCUSDT" in got["reasons"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"owner": None}, "trading_access_unconfirmed"),
        ({"account": {"marginMode": "REGULAR_MARGIN"}}, "margin_mode_not_isolated:REGULAR_MARGIN"),
        ({"pair": {"perp_fee": []}}, "pair_unverified:ABCUSDT:perp_fee"),
        ({"pair": {"risk_limits": []}}, "pair_unverified:ABCUSDT:mm"),
        (
            {"pair": {"risk_limits": [{"riskLimitValue": "1000", "maintenanceMargin": "0.6"}]}},
            "pair_exceeds_bank:ABCUSDT",
        ),
        ({"key": {"readOnly": 0, "permissions": {}}}, "key_not_read_only"),
    ],
)
def test_every_unmet_condition_blocks_with_its_reason(change: dict[str, Any], reason: str) -> None:
    snapshot = _snapshot(**change.get("pair", {}))
    if "account" in change:
        snapshot["account"] = change["account"]
    if "key" in change:
        snapshot["api_key"] = change["key"]
    owner = change.get("owner", CONFIRMED)
    got = _evaluate(snapshot, owner_confirmed_trading=owner)
    assert got["verdict"] == "blocked"
    assert reason in got["reasons"]


# --- the canary and the date, before any request -----------------------------------


def _canary(tmp_path: Path, **changes: Any) -> tuple[Path, str]:
    report: dict[str, Any] = {
        "version": cp.CANARY_VERSION,
        "started_at": "2026-10-31T09:00:00+00:00",
        "run": {"code_revision": "abc123", "working_tree_dirty": False},
        "feasibility_gate": {
            "decision": cp.CANARY_PASS,
            "repeatably_book_feasible_bases": ["ABC"],
        },
        "selection": [{"base": "ABC", "spot_id": "ABCUSDT", "perp_id": "ABCUSDT"}],
    }
    report.update(changes)
    path = tmp_path / "result.json"
    payload = json.dumps(report).encode()
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def test_the_pairs_come_only_from_a_verified_passing_canary(tmp_path: Path) -> None:
    path, digest = _canary(tmp_path)
    pairs, canary = cp.canary_pairs(path, digest)
    assert pairs == ["ABCUSDT"]
    assert canary["sha256"] == digest and canary["code_revision"] == "abc123"


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"version": "other"}, "not a"),
        ({"started_at": "2026-10-09T09:00:00+00:00"}, "before its own boundary"),
        ({"run": {"code_revision": "abc", "working_tree_dirty": True}}, "clean"),
        (
            {"feasibility_gate": {"decision": "stop_bybit_50_usd_carry_feasibility"}},
            "decision",
        ),
        (
            {"selection": [{"base": "ABC", "spot_id": "ABCUSDT", "perp_id": "ABCPERP"}]},
            "single native symbol",
        ),
    ],
)
def test_a_canary_that_does_not_hold_is_refused(
    tmp_path: Path, changes: dict[str, Any], match: str
) -> None:
    path, digest = _canary(tmp_path, **changes)
    with pytest.raises(cp.StageARefused, match=match):
        cp.canary_pairs(path, digest)


def test_a_tampered_canary_is_refused(tmp_path: Path) -> None:
    path, _ = _canary(tmp_path)
    with pytest.raises(cp.StageARefused, match="sha256"):
        cp.canary_pairs(path, "0" * 64)


def test_main_refuses_before_the_boundary_and_never_calls_the_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #507, P2: on 2026-10-09 a run with no canary got `pass`."""
    path, digest = _canary(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(cp, "run", lambda **_: called.append("run"))
    argv = [
        "--canary", str(path), "--canary-sha256", digest,
        "--code-revision", "abc", "--out-dir", str(tmp_path / "out"),
    ]  # fmt: skip
    with pytest.raises(cp.StageARefused, match="before"):
        cp.main(argv, now=lambda: datetime(2026, 10, 9, tzinfo=UTC))
    with pytest.raises(SystemExit):
        cp.main([*argv[:1], str(tmp_path / "missing.json"), *argv[2:]])
    assert called == []


# --- the record --------------------------------------------------------------------


def test_run_reads_only_allowed_paths_and_records_safe_inputs(tmp_path: Path) -> None:
    """Review of #507, P2: the record keeps the tiers, fees, request parameters and their
    hashes, the canary and the revision, and never the key or the user id."""
    seen: list[tuple[str, str, dict[str, str]]] = []
    bodies: dict[str, Any] = {
        "/v5/user/query-api": READ_ONLY_KEY,
        "/v5/account/info": {"marginMode": "ISOLATED_MARGIN", "unifiedMarginStatus": 5},
        "/v5/account/collateral-info": {"list": [{"currency": "ABC", "marginCollateral": True}]},
        "/v5/market/risk-limit": {"list": TIERS},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(parse_qsl(urlsplit(str(request.url)).query))
        seen.append((request.method, request.url.path, query))
        if request.url.path == "/v5/account/fee-rate":
            rate = "0.001" if query["category"] == "spot" else "0.00055"
            result: Any = {"list": [{"symbol": query["symbol"], "takerFeeRate": rate}]}
        else:
            result = bodies[request.url.path]
        return httpx.Response(200, json={"retCode": 0, "result": result})

    real_client = httpx.AsyncClient

    def mocked(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cp.httpx, "AsyncClient", mocked)
        verdict, requests = asyncio.run(
            cp.run(
                api_key="k",
                api_secret="s",  # noqa: S106
                symbols=["ABCUSDT"],
                owner_confirmed_trading=CONFIRMED,
                owner_confirmed_add_margin=CONFIRMED,
                add_margin_limit_usd=LIMIT,
            )
        )
    assert verdict["verdict"] == "pass", verdict["reasons"]
    assert {method for method, _, _ in seen} == {"GET"}
    assert {path for _, path, _ in seen} == cp.ALLOWED_GET_PATHS | {cp.RISK_LIMIT_PATH}

    record = cp.build_record(
        verdict,
        requests,
        canary={"sha256": "c" * 64},
        code_revision="abc",
        working_tree_dirty=False,
    )
    path, digest = cp.write_once(tmp_path, record)
    text = path.read_text()
    assert "THE-KEY-ID" not in text and "12345" not in text
    saved = json.loads(text)
    tiers = [r for r in saved["requests"] if r["path"] == cp.RISK_LIMIT_PATH]
    assert tiers[0]["response"] == TIERS and tiers[0]["params"]["symbol"] == "ABCUSDT"
    assert all(len(r["sha256"]) == 64 for r in saved["requests"])
    assert saved["canary"]["sha256"] == "c" * 64 and saved["run"]["code_revision"] == "abc"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert (tmp_path / f"{path.name}.sha256").read_text().strip() == digest
    with pytest.raises(FileExistsError):
        cp.write_once(tmp_path, record)
