"""Bybit carry, stage A: read-only account and margin preflight (no order).

Design: docs/research/bybit-spot-perp-carry-preflight-v1.md, "Stage A". It runs only
after the execution feasibility canary (#476) has passed, on that canary's passing pairs.
Before any exchange request it checks the date (not before 2026-10-31) and the canary
artifact: its SHA-256 must match the published one, it must be the canary's version, run
from a clean tree on or after 2026-10-31, with the decision that permits this stage. The
pairs come from that artifact, never from the command line.

It reads, with the same signed GET-only client as the LIVE_PROBE preflight and its own
allow-list (no order, position-setting or transfer endpoint is reachable):

- the key: read-only and unable to withdraw (`/v5/user/query-api`);
- the account's margin mode (`/v5/account/info`); the short must be held in isolated
  margin;
- the taker fee actually charged on spot and on the perpetual, per pair
  (`/v5/account/fee-rate`);
- whether the base coin can be, and is, collateral in a unified account, as context only
  (`/v5/account/collateral-info`);
- the perpetual's maintenance-margin tiers (public `/v5/market/risk-limit`).

and applies the design's capital rule to each pair: USD 50 per leg, the short in isolated
margin covering a +300% move (a loss of three times its notional), the maintenance margin
at the quadrupled notional and the fees, together with the spot leg, within the USD 300
bank.

Two things a read-only key cannot establish are owner confirmations, each with its date:
that spot and perpetuals are tradable, and that margin can be added to an isolated
position, with the largest amount the owner can add. A pair whose margin above the 1x
initial margin exceeds that amount is blocked; without either confirmation the verdict is
blocked.

The record keeps what is needed to recheck the verdict: every request's path and
parameters, the safe part of every response (the key only as derived facts, never the key
or the user id) with its SHA-256, the canary's SHA-256, and the code revision. No secret is
written. Fail closed: a field the verdict depends on that is missing or not a number gives
`blocked` with a reason, never a default value.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .bybit_preflight import (
    MAINNET_URL,
    BybitApiError,
    BybitReadOnlyClient,
    _dec,
    _key_reasons,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

VERSION = "bybit_carry_stage_a_v1"
RUN_AFTER = datetime(2026, 10, 31, tzinfo=UTC)
CANARY_VERSION = "bybit_spot_perp_feasibility_v1"
CANARY_PASS = "funding_capture_design_permitted"  # noqa: S105 -- a decision name
ALLOWED_GET_PATHS = frozenset(
    {
        "/v5/user/query-api",
        "/v5/account/info",
        "/v5/account/fee-rate",
        "/v5/account/collateral-info",
    }
)
RISK_LIMIT_PATH = "/v5/market/risk-limit"
LEG_NOTIONAL_USD = Decimal(50)
BANK_USD = Decimal(300)
STRESS_RISE = Decimal(3)  # +300%: the price quadruples
ACCOUNT_FIELDS = ("marginMode", "unifiedMarginStatus", "updatedTime")


class StageARefused(SystemExit):
    """A precondition checked before any exchange request failed; nothing was read."""


def base_coin(symbol: str) -> str:
    if not symbol.endswith("USDT") or symbol == "USDT":
        raise ValueError(f"not a USDT pair: {symbol}")
    return symbol[: -len("USDT")]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def require_run_window(now: datetime) -> None:
    if now.tzinfo is None or now.astimezone(UTC) < RUN_AFTER:
        raise StageARefused(f"stage A cannot run before {RUN_AFTER.isoformat()}")


def canary_pairs(path: Path, expected_sha256: str) -> tuple[list[str], dict[str, Any]]:
    """The canary's passing pairs, from its verified artifact; refuses anything else."""
    payload = path.read_bytes()
    digest = sha256_bytes(payload)
    if digest != expected_sha256.strip().lower():
        raise StageARefused(f"canary sha256 {digest} is not the published {expected_sha256}")
    report = json.loads(payload)
    if report.get("version") != CANARY_VERSION:
        raise StageARefused(f"not a {CANARY_VERSION} artifact")
    started = datetime.fromisoformat(str(report.get("started_at")))
    if started.tzinfo is None or started < RUN_AFTER:
        raise StageARefused("the canary ran before its own boundary")
    run = report.get("run") or {}
    if run.get("working_tree_dirty") is not False or not run.get("code_revision"):
        raise StageARefused("the canary did not run from a clean, named revision")
    gate = report.get("feasibility_gate") or {}
    if gate.get("decision") != CANARY_PASS:
        raise StageARefused(f"the canary decision is {gate.get('decision')!r}, not {CANARY_PASS}")
    bases = set(gate.get("repeatably_book_feasible_bases") or [])
    selection = {str(p.get("base")): p for p in report.get("selection") or []}
    pairs = []
    for base in sorted(bases):
        pair = selection.get(base)
        if pair is None or pair.get("spot_id") != pair.get("perp_id"):
            raise StageARefused(f"canary base {base} has no single native symbol")
        pairs.append(str(pair["spot_id"]))
    if not pairs:
        raise StageARefused("the canary passed no pair")
    canary = {
        "sha256": digest,
        "started_at": started.isoformat(),
        "code_revision": run["code_revision"],
        "decision": gate["decision"],
        "n_pairs": len(pairs),
    }
    return pairs, canary


def maintenance_rate(tiers: Sequence[dict[str, Any]], notional: Decimal) -> Decimal | None:
    """The maintenance-margin rate of the lowest tier whose limit covers the notional.
    The tier's mmDeduction is left out, so the margin is never understated."""
    readable = []
    for tier in tiers:
        limit, rate = _dec(tier.get("riskLimitValue")), _dec(tier.get("maintenanceMargin"))
        if limit is None or rate is None:
            return None
        readable.append((limit, rate))
    covering = sorted(t for t in readable if t[0] >= notional)
    return covering[0][1] if covering else None


def capital_for_pair(spot_taker: Decimal, perp_taker: Decimal, mm_rate: Decimal) -> dict[str, str]:
    """USD a pair needs under the +300% stress, with the bank it must fit and the margin
    to add above the short's 1x initial margin."""
    stressed = LEG_NOTIONAL_USD * (1 + STRESS_RISE)
    spot_cost = LEG_NOTIONAL_USD * (1 + spot_taker)
    short_collateral = (
        LEG_NOTIONAL_USD * STRESS_RISE  # the loss on the short
        + stressed * mm_rate  # maintenance margin at the stressed notional
        + LEG_NOTIONAL_USD * perp_taker  # entry fee
        + stressed * perp_taker  # exit fee at the stressed notional
    )
    total = spot_cost + short_collateral
    return {
        "spot_cost_usd": str(spot_cost),
        "short_collateral_usd": str(short_collateral),
        "margin_to_add_usd": str(max(short_collateral - LEG_NOTIONAL_USD, Decimal(0))),
        "total_usd": str(total),
        "bank_usd": str(BANK_USD),
        "fits": str(total <= BANK_USD).lower(),
    }


def _taker(rows: Sequence[dict[str, Any]], symbol: str) -> Decimal | None:
    for row in rows:
        if row.get("symbol") == symbol:
            return _dec(row.get("takerFeeRate"))
    return None


class Recorder:
    """Every request with its parameters and the safe part of its response."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def add(self, path: str, params: dict[str, str], safe: Any) -> Any:
        self.requests.append(
            {
                "path": path,
                "params": params,
                "response": safe,
                "sha256": sha256_bytes(_canonical(safe)),
            }
        )
        return safe


async def take_snapshot(
    client: BybitReadOnlyClient,
    public: httpx.AsyncClient,
    symbols: Sequence[str],
    recorder: Recorder,
) -> dict[str, Any]:
    api_key = await client.get("/v5/user/query-api", {})
    _, facts = _key_reasons(api_key, "diagnostic")
    recorder.add("/v5/user/query-api", {}, {"derived_facts": facts})
    account = await client.get("/v5/account/info", {})
    recorder.add("/v5/account/info", {}, {k: account.get(k) for k in ACCOUNT_FIELDS})
    snapshot: dict[str, Any] = {"api_key": api_key, "account": account, "pairs": {}}
    for symbol in symbols:
        rows: dict[str, Any] = {}
        for name, path, params in (
            ("spot_fee", "/v5/account/fee-rate", {"category": "spot", "symbol": symbol}),
            ("perp_fee", "/v5/account/fee-rate", {"category": "linear", "symbol": symbol}),
            ("collateral", "/v5/account/collateral-info", {"currency": base_coin(symbol)}),
        ):
            result = await client.get(path, params)
            rows[name] = recorder.add(path, params, result.get("list") or [])
        params = {"category": "linear", "symbol": symbol}
        response = await public.get(f"{MAINNET_URL}{RISK_LIMIT_PATH}", params=params)
        response.raise_for_status()
        body = response.json()
        if body.get("retCode") != 0:
            raise BybitApiError(RISK_LIMIT_PATH, body.get("retCode"), body.get("retMsg"))
        tiers = [
            t for t in (body.get("result") or {}).get("list") or [] if t.get("symbol") == symbol
        ]
        rows["risk_limits"] = recorder.add(RISK_LIMIT_PATH, params, tiers)
        snapshot["pairs"][symbol] = rows
    return snapshot


def evaluate(
    snapshot: dict[str, Any],
    *,
    symbols: Sequence[str],
    owner_confirmed_trading: date | None,
    owner_confirmed_add_margin: date | None,
    add_margin_limit_usd: Decimal | None,
) -> dict[str, Any]:
    reasons, key_facts = _key_reasons(snapshot["api_key"], "diagnostic")
    if owner_confirmed_trading is None:
        reasons.append("trading_access_unconfirmed")
    if owner_confirmed_add_margin is None or add_margin_limit_usd is None:
        reasons.append("add_margin_unconfirmed")
    margin_mode = snapshot["account"].get("marginMode")
    if margin_mode != "ISOLATED_MARGIN":
        reasons.append(f"margin_mode_not_isolated:{margin_mode}")
    if not symbols:
        reasons.append("no_pairs")
    stressed = LEG_NOTIONAL_USD * (1 + STRESS_RISE)
    pairs: dict[str, Any] = {}
    for symbol in symbols:
        raw = snapshot["pairs"].get(symbol) or {}
        spot = _taker(raw.get("spot_fee") or [], symbol)
        perp = _taker(raw.get("perp_fee") or [], symbol)
        mm = maintenance_rate(raw.get("risk_limits") or [], stressed)
        coin = base_coin(symbol)
        collateral = next(
            (c for c in raw.get("collateral") or [] if c.get("currency") == coin), None
        )
        fact: dict[str, Any] = {
            "spot_taker": None if spot is None else str(spot),
            "perp_taker": None if perp is None else str(perp),
            "maintenance_rate_at_stress": None if mm is None else str(mm),
            "spot_can_be_collateral": (
                None if collateral is None else bool(collateral.get("marginCollateral"))
            ),
            "spot_collateral_switched_on": (
                None if collateral is None else bool(collateral.get("collateralSwitch"))
            ),
        }
        missing = [
            name
            for name, value in (("spot_fee", spot), ("perp_fee", perp), ("mm", mm))
            if value is None
        ]
        if missing:
            reasons.append(f"pair_unverified:{symbol}:{','.join(missing)}")
        elif spot is not None and perp is not None and mm is not None:
            capital = capital_for_pair(spot, perp, mm)
            fact["capital"] = capital
            if capital["fits"] != "true":
                reasons.append(f"pair_exceeds_bank:{symbol}")
            if add_margin_limit_usd is not None and Decimal(capital["margin_to_add_usd"]) > (
                add_margin_limit_usd
            ):
                reasons.append(f"pair_exceeds_add_margin_limit:{symbol}")
        pairs[symbol] = fact
    return {
        "version": VERSION,
        "verdict": "blocked" if reasons else "pass",
        "snapshot_at": snapshot.get("taken_at") or datetime.now(UTC).isoformat(),
        "note": "stage A of the carry design; a snapshot, places no order, authorizes none",
        "reasons": reasons,
        "facts": {
            "key": key_facts,
            "margin_mode": margin_mode,
            "trading_access_confirmed_by_owner": _iso(owner_confirmed_trading),
            "add_margin_confirmed_by_owner": _iso(owner_confirmed_add_margin),
            "add_margin_limit_usd": None
            if add_margin_limit_usd is None
            else str(add_margin_limit_usd),
            "leg_notional_usd": str(LEG_NOTIONAL_USD),
            "stress_rise": str(STRESS_RISE),
            "pairs": pairs,
        },
    }


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


async def run(
    *,
    api_key: str,
    api_secret: str,
    symbols: Sequence[str],
    owner_confirmed_trading: date | None,
    owner_confirmed_add_margin: date | None,
    add_margin_limit_usd: Decimal | None,
    base_url: str = MAINNET_URL,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recorder = Recorder()
    async with httpx.AsyncClient(timeout=20) as http:
        client = BybitReadOnlyClient(
            api_key, api_secret, http, base_url=base_url, allowed_paths=ALLOWED_GET_PATHS
        )
        try:
            snapshot = await take_snapshot(client, http, symbols, recorder)
        except (BybitApiError, httpx.HTTPError) as exc:
            path = exc.path if isinstance(exc, BybitApiError) else type(exc).__name__
            code = exc.code if isinstance(exc, BybitApiError) else "http"
            verdict = {
                "version": VERSION,
                "verdict": "blocked",
                "snapshot_at": datetime.now(UTC).isoformat(),
                "reasons": [f"unverified:{path}:{code}"],
                "facts": {},
            }
            return verdict, recorder.requests
    snapshot["taken_at"] = datetime.now(UTC).isoformat()
    verdict = evaluate(
        snapshot,
        symbols=symbols,
        owner_confirmed_trading=owner_confirmed_trading,
        owner_confirmed_add_margin=owner_confirmed_add_margin,
        add_margin_limit_usd=add_margin_limit_usd,
    )
    return verdict, recorder.requests


def build_record(
    verdict: dict[str, Any],
    requests: list[dict[str, Any]],
    *,
    canary: dict[str, Any],
    code_revision: str,
    working_tree_dirty: bool,
) -> dict[str, Any]:
    return {
        **verdict,
        "canary": canary,
        "run": {"code_revision": code_revision, "working_tree_dirty": working_tree_dirty},
        "requests": requests,
    }


def write_once(out_dir: Path, record: dict[str, Any]) -> tuple[Path, str]:
    """The record as a new file named by its snapshot time, with its SHA-256; never
    overwrites."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(record["snapshot_at"]).replace(":", "").replace("+0000", "Z")
    path = out_dir / f"carry-stage-a-{stamp}.json"
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    with path.open("xb") as handle:
        handle.write(payload)
    digest = sha256_bytes(payload)
    path.with_name(path.name + ".sha256").write_text(digest + "\n")
    return path, digest


def main(argv: Sequence[str] | None = None, *, now: Callable[[], datetime] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--canary", type=Path, required=True, help="the #476 result.json")
    parser.add_argument("--canary-sha256", required=True, help="its published SHA-256")
    parser.add_argument("--owner-confirmed-trading", type=date.fromisoformat, default=None)
    parser.add_argument("--owner-confirmed-add-margin", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--add-margin-limit-usd",
        type=Decimal,
        default=None,
        help="the largest margin the owner can add to an isolated position, in USD",
    )
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--working-tree-dirty", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    # Everything below is local: no exchange request before the date and the inputs hold.
    require_run_window((now or (lambda: datetime.now(UTC)))())
    symbols, canary = canary_pairs(args.canary, args.canary_sha256)
    api_key = os.environ.get("BYBIT_API_KEY", "").strip()
    api_secret = os.environ.get("BYBIT_API_SECRET", "").strip()
    if not (api_key and api_secret):
        raise StageARefused("BYBIT_API_KEY and BYBIT_API_SECRET are required")
    verdict, requests = asyncio.run(
        run(
            api_key=api_key,
            api_secret=api_secret,
            symbols=symbols,
            owner_confirmed_trading=args.owner_confirmed_trading,
            owner_confirmed_add_margin=args.owner_confirmed_add_margin,
            add_margin_limit_usd=args.add_margin_limit_usd,
        )
    )
    record = build_record(
        verdict,
        requests,
        canary=canary,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    path, digest = write_once(args.out_dir, record)
    summary = {
        "verdict": verdict["verdict"],
        "reasons": verdict["reasons"],
        "artifact": str(path),
        "sha256": digest,
    }
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    raise SystemExit(0 if verdict["verdict"] == "pass" else 2)


if __name__ == "__main__":
    main()
