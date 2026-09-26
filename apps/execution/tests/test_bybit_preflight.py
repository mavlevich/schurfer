from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from schurfer_execution import bybit_preflight as bp

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
REQUIRED = Decimal(55)


def test_sign_matches_the_bybit_v5_get_recipe() -> None:
    expected = hmac.new(
        b"secret", b"1700000000000key5000category=linear&symbol=BTCUSDT", hashlib.sha256
    ).hexdigest()
    assert bp.sign("secret", 1700000000000, "key", 5000, "category=linear&symbol=BTCUSDT") == (
        expected
    )


# --- the client can only read ------------------------------------------------------


def _client(handler: Any) -> tuple[bp.BybitReadOnlyClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return bp.BybitReadOnlyClient("key", "secret", http, clock=lambda: 1700000000.0), http


def test_the_client_refuses_any_path_outside_the_allow_list() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"retCode": 0, "result": {}})

    client, _ = _client(handler)
    for path in ("/v5/order/create", "/v5/position/set-leverage", "/v5/asset/transfer"):
        with pytest.raises(bp.ForbiddenPathError):
            asyncio.run(client.get(path, {}))
    assert calls == []


def test_the_module_has_no_write_path() -> None:
    assert all(
        p.startswith(("/v5/user/", "/v5/account/", "/v5/position/list", "/v5/order/"))
        for p in bp.ALLOWED_GET_PATHS
    )
    assert "/v5/order/realtime" in bp.ALLOWED_GET_PATHS
    assert not any(
        p.endswith(("create", "amend", "cancel", "cancel-all")) for p in bp.ALLOWED_GET_PATHS
    )
    public = {n for n, _ in inspect.getmembers(bp.BybitReadOnlyClient) if not n.startswith("_")}
    assert public == {"get", "get_all"}
    source = inspect.getsource(bp)
    for forbidden in ("http.post", "http.put", "http.delete", ".post(", "ccxt", "create_order"):
        assert forbidden not in source


def test_requests_are_signed_gets_over_the_exact_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"retCode": 0, "result": {"ok": 1}})

    client, _ = _client(handler)
    assert asyncio.run(client.get("/v5/position/list", {"category": "linear", "symbol": "X"})) == {
        "ok": 1
    }
    request = seen[0]
    assert request.method == "GET"
    query = urlsplit(str(request.url)).query
    assert request.headers["X-BAPI-SIGN"] == bp.sign("secret", 1700000000000, "key", 5000, query)
    assert "secret" not in str(request.url) and "secret" not in str(dict(request.headers))


def test_an_api_error_is_raised_not_swallowed() -> None:
    client, _ = _client(lambda _: httpx.Response(200, json={"retCode": 10003, "retMsg": "bad key"}))
    with pytest.raises(bp.BybitApiError, match="10003"):
        asyncio.run(client.get("/v5/account/info", {}))


def test_pagination_reads_every_page_and_ignores_bybits_repeated_last_page() -> None:
    pages = {
        "": {"list": [{"symbol": "A", "positionIdx": 0}], "nextPageCursor": "c1"},
        "c1": {"list": [{"symbol": "B", "positionIdx": 0}], "nextPageCursor": "c2"},
        # Bybit hands out a cursor after the last page and then repeats rows.
        "c2": {"list": [{"symbol": "B", "positionIdx": 0}], "nextPageCursor": "c3"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = dict(parse_qsl(request.url.query.decode())).get("cursor", "")
        return httpx.Response(200, json={"retCode": 0, "result": pages[cursor]})

    client, _ = _client(handler)
    rows = asyncio.run(
        client.get_all("/v5/position/list", {"category": "linear"}, bp._position_key)
    )
    assert [r["symbol"] for r in rows] == ["A", "B"]


# --- the verdict ---------------------------------------------------------------------


def _snapshot(**overrides: Any) -> bp.Snapshot:
    values: dict[str, Any] = {
        "taken_at": NOW,
        "api_key": {
            "readOnly": 1,
            "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": []},
            "ips": ["203.0.113.7"],
            "expiredAt": "2026-12-25T00:00:00Z",
        },
        "account": {"marginMode": "ISOLATED_MARGIN"},
        "usdt": {
            "coin": "USDT",
            "walletBalance": "80",
            "totalPositionIM": "0",
            "totalOrderIM": "",
            "locked": "0",
            "bonus": "",
        },
        "open_positions": [],
        "open_orders": [],
        "symbol_positions": {
            "BTCUSDT": [{"symbol": "BTCUSDT", "positionIdx": 0, "leverage": "1", "size": "0"}]
        },
    }
    values.update(overrides)
    return bp.Snapshot(**values)


def _verdict(snapshot: bp.Snapshot, purpose: str = "diagnostic") -> dict[str, Any]:
    return bp.evaluate(snapshot, purpose=purpose, symbols=["BTCUSDT"], required_usdt=REQUIRED)


def test_a_clean_isolated_one_way_1x_account_is_ready_as_a_dated_snapshot() -> None:
    verdict = _verdict(_snapshot())
    assert verdict["verdict"] == "ready" and verdict["reasons"] == []
    assert verdict["snapshot_at"] == NOW.isoformat()
    assert "re-check before every order" in verdict["note"]
    assert verdict["facts"]["isolated_available_usdt"] == "80"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"account": {"marginMode": "REGULAR_MARGIN"}}, "margin_mode_not_isolated:REGULAR_MARGIN"),
        ({"usdt": None}, "usdt_balance_unverified"),
        ({"open_positions": [{"symbol": "ETHUSDT", "size": "1"}]}, "account_not_empty:positions"),
        ({"open_orders": [{"orderId": "o1"}]}, "account_not_empty:orders"),
        ({"symbol_positions": {}}, "symbol_unverified:BTCUSDT"),
        (
            {
                "symbol_positions": {
                    "BTCUSDT": [{"symbol": "BTCUSDT", "positionIdx": 0, "leverage": ""}]
                }
            },
            "symbol_unverified:BTCUSDT",
        ),
        (
            {
                "symbol_positions": {
                    "BTCUSDT": [
                        {"symbol": "BTCUSDT", "positionIdx": 1, "leverage": "1"},
                        {"symbol": "BTCUSDT", "positionIdx": 2, "leverage": "1"},
                    ]
                }
            },
            "symbol_not_one_way:BTCUSDT",
        ),
        (
            {
                "symbol_positions": {
                    "BTCUSDT": [{"symbol": "BTCUSDT", "positionIdx": 0, "leverage": "10"}]
                }
            },
            "symbol_leverage_above_1x:BTCUSDT",
        ),
    ],
)
def test_each_account_failure_blocks(overrides: dict[str, Any], reason: str) -> None:
    verdict = _verdict(_snapshot(**overrides))
    assert verdict["verdict"] == "blocked"
    assert reason in verdict["reasons"]


def test_isolated_available_subtracts_margin_locks_and_bonus_not_total_available() -> None:
    usdt = {
        "coin": "USDT",
        "walletBalance": "70",
        "totalPositionIM": "5",
        "totalOrderIM": "4",
        "locked": "3",
        "bonus": "6",
        "totalAvailableBalance": "999",  # never used
    }
    assert bp.isolated_available_usdt(usdt) == Decimal(52)
    verdict = _verdict(_snapshot(usdt=usdt))
    assert "insufficient_isolated_usdt" in verdict["reasons"]  # 52 < 50 + 5 fee reserve


def test_no_target_symbol_is_never_ready() -> None:
    verdict = bp.evaluate(_snapshot(), purpose="diagnostic", symbols=[], required_usdt=REQUIRED)
    assert verdict["reasons"] == ["no_target_symbols"]


@pytest.mark.parametrize(
    ("purpose", "key", "reasons"),
    [
        # today's diagnostic key: read-only, no withdraw
        ("diagnostic", {"readOnly": 1}, []),
        ("diagnostic", {"readOnly": 0}, ["key_not_read_only"]),
        ("diagnostic", {"readOnly": 1, "Wallet": ["Withdraw"]}, ["key_can_withdraw"]),
        # the future trading key is checked for trading, not for read-only
        ("live_probe", {"readOnly": 1}, ["key_read_only"]),
        ("live_probe", {"readOnly": 0}, []),
        (
            "live_probe",
            {"readOnly": 0, "ContractTrade": ["Position"]},
            ["key_missing_contract_trade"],
        ),
        ("live_probe", {"readOnly": 0, "ips": ["*"]}, ["key_no_ip_whitelist"]),
        ("live_probe", {"readOnly": 0, "ips": []}, ["key_no_ip_whitelist"]),
        ("live_probe", {"readOnly": 0, "Wallet": ["Withdraw"]}, ["key_can_withdraw"]),
    ],
)
def test_the_key_is_checked_for_the_purpose_it_is_used_for(
    purpose: str, key: dict[str, Any], reasons: list[str]
) -> None:
    api_key = {
        "readOnly": key["readOnly"],
        "permissions": {
            "ContractTrade": key.get("ContractTrade", ["Order", "Position"]),
            "Wallet": key.get("Wallet", ["AccountTransfer"]),
        },
        "ips": key.get("ips", ["203.0.113.7"]),
    }
    verdict = _verdict(_snapshot(api_key=api_key), purpose=purpose)
    assert verdict["reasons"] == reasons


def test_a_failed_read_is_blocked_unverified_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(*_: Any, **__: Any) -> bp.Snapshot:
        raise bp.BybitApiError("/v5/account/info", 10003, "invalid key")

    monkeypatch.setattr(bp, "take_snapshot", failing)
    verdict = asyncio.run(
        bp.run(
            api_key="k",
            api_secret="s",  # noqa: S106
            purpose="diagnostic",
            symbols=["BTCUSDT"],
            required_usdt=REQUIRED,
        )
    )
    assert verdict["verdict"] == "blocked"
    assert verdict["reasons"] == ["unverified:/v5/account/info:10003"]


def test_the_snapshot_reads_every_settle_coin_and_order_kind() -> None:
    requested: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.url.query.decode()))
        requested.append((request.url.path, params))
        result: dict[str, Any] = {"list": []}
        if request.url.path == "/v5/account/wallet-balance":
            result = {"list": [{"coin": [{"coin": "USDT", "walletBalance": "60"}]}]}
        if request.url.path == "/v5/position/list" and params.get("symbol") == "BTCUSDT":
            result = {
                "list": [{"symbol": "BTCUSDT", "positionIdx": 0, "leverage": "1", "size": "0"}]
            }
        if request.url.path == "/v5/position/list" and params.get("settleCoin") == "USDC":
            result = {"list": [{"symbol": "ETHPERP", "positionIdx": 0, "size": "0.5"}]}
        if request.url.path == "/v5/order/realtime" and params.get("orderFilter") == "StopOrder":
            result = {"list": [{"orderId": "stop-1"}]}
        return httpx.Response(200, json={"retCode": 0, "result": result})

    client, _ = _client(handler)
    snapshot = asyncio.run(bp.take_snapshot(client, ["BTCUSDT"], lambda: NOW))
    order_reads = {
        (p["settleCoin"], p["orderFilter"]) for path, p in requested if path == "/v5/order/realtime"
    }
    assert order_reads == {(c, f) for c in bp.SETTLE_COINS for f in bp.ORDER_FILTERS}
    assert [p["symbol"] for p in snapshot.open_positions] == ["ETHPERP"]
    assert [o["orderId"] for o in snapshot.open_orders] == ["stop-1"]
    assert snapshot.usdt == {"coin": "USDT", "walletBalance": "60"}
    verdict = bp.evaluate(
        snapshot, purpose="diagnostic", symbols=["BTCUSDT"], required_usdt=REQUIRED
    )
    assert {"account_not_empty:positions", "account_not_empty:orders"} <= set(verdict["reasons"])
