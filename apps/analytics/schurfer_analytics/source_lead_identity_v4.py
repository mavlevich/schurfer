"""Source-lead identity registry v4: fixed candidate window, automatic rule, evidence.

HYP-012 v4 (Gate source-lead with Bybit as the primary execution venue and
Binance kept as a descriptive comparison). The v3 registry covered 14 assets
approved by hand; v4 replaces the per-row human review with one identity rule
written down before any route is decided, applied identically to every
candidate, plus one human confirmation of the whole result and an independent
technical re-check by the reviewer (decision register, 2026-09-25).

## Steps

1. `candidates-sql` prints the frozen candidate query for a fixed UTC window
   end. `candidates` turns its output (one base per line, run read-only on
   prod) into a snapshot with a SHA-256 over the window and the sorted list.
2. `decide` fetches identity evidence for every candidate and target venue,
   applies `decide_route`, captures an evidence bundle for each approved
   route, and publishes the bundles plus a decisions file all-or-nothing.
3. `build-registry` turns the published bundles into registry v4, refusing
   unless an approval record names the exact decisions file hash.

## The v4 identity rule (IDENTITY_RULE_VERSION), per route (base, target)

A route is approved only if every check passes, in this order; the first
failing check is the recorded rejection reason:

1. Gate has a trading, non-delisting `{BASE}_USDT` perpetual.
2. The target has exactly one trading USDT-settled linear perpetual whose
   reported base equals BASE (`1000`-prefixed or renamed contracts never
   match a plain base).
3. Gate's own currency data reports exactly one contract per chain on at
   least one supported EVM chain (`RULE_CHAINS`).
4. The target's own asset data names this coin exactly once (Binance: the
   Alpha catalog; Bybit: authenticated coin-info) and reports a contract on
   a supported chain.
5. No supported chain carries different addresses at Gate and the target.
6. At least one supported chain carries the same address at both.
7. Exactly one CoinGecko project lists that (chain, address), and its symbol
   equals BASE.
8. Capture: on-chain `decimals()` is read at a pinned block, and any decimals
   the target catalog reports agree with it. Bybit's `minAccuracy` is a
   deposit/withdrawal precision, not token decimals, and is never used.

Cheapness, canary results and returns play no part in the rule.

Residual risk, accepted and not closed: each exchange's asset data and its
perpetual are linked only by the shared ticker (Gate currency to Gate
futures, Alpha or Bybit coin-info to the target perpetual).

Solana and other non-EVM chains are out of scope for v4: the decimals
evidence here is an EVM `eth_call` at a pinned block. Such routes are
rejected with `gate_no_supported_contract` and counted in the decisions file.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .gate_identity_candidate_tooling import (
    COINGECKO_PLATFORM_TO_CHAIN,
    NETWORK_TO_CHAIN,
    normalize_evm_address,
)
from .reporting import json_ready, parse_utc_datetime
from .source_lead_contract import IDENTITY_REGISTRY_V3_START
from .source_lead_identity_evidence import (
    EVIDENCE_DIR_V4,
    MANIFEST_FILENAME,
    ChainContractEvidence,
    DerivativeMarketEvidence,
    EvidenceBundle,
    RawFetch,
    _atomic_publish,
    _bundle_filename,
    _current_git_state,
    _finalize_bundle,
    _http_get_json,
    _sha256_bytes,
    _sha256_canonical,
    _validate_identity_class,
    _validate_route_evidence,
    fetch_coingecko_coin,
    fetch_gate_futures_contract,
    fetch_onchain_decimals,
    find_binance_futures_market,
    find_bybit_futures_market,
    load_all_evidence_bundles,
    save_evidence_bundle,
)
from .source_lead_qualification import parse_identity_registry, verify_registry_against_evidence

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

IDENTITY_RULE_VERSION = "source_lead_identity_rule_v4"
CANDIDATES_VERSION = "source_lead_identity_v4_candidates"
DECISIONS_VERSION = "source_lead_identity_v4_decisions"
REGISTRY_VERSION_V4 = "source_lead_identity_registry_v4"
EVIDENCE_VERSION_V4 = "source_lead_identity_evidence_v4"

CANDIDATE_WINDOW_START = IDENTITY_REGISTRY_V3_START
# Bybit first: it is the venue the owner can trade; Binance is descriptive.
TARGET_EXCHANGES: tuple[str, ...] = ("bybit", "binance")
# Supported chains, in the order a shared chain is chosen. EVM only.
RULE_CHAINS: tuple[str, ...] = ("ethereum", "bsc", "base", "arbitrum")

ALPHA_CHAIN_IDS: dict[str, str] = {
    "1": "ethereum",
    "56": "bsc",
    "8453": "base",
    "42161": "arbitrum",
}
# Bybit coin-info `chain` codes. Unmapped codes fail closed (ignored).
BYBIT_CHAINS: dict[str, str] = {
    "ETH": "ethereum",
    "BSC": "bsc",
    "BASE": "base",
    "ARBI": "arbitrum",
}

REGISTRY_DIR = Path(__file__).parent / "registry"
CANDIDATES_PATH = REGISTRY_DIR / "source_lead_identity_v4_candidates.json"
DECISIONS_PATH = REGISTRY_DIR / "source_lead_identity_v4_decisions.json"
APPROVAL_PATH = REGISTRY_DIR / "source_lead_identity_v4_approval.json"
REGISTRY_PATH_V4 = REGISTRY_DIR / "source_lead_identity_registry_v4.json"

_HTTP_TIMEOUT_SECONDS = 20.0
_COINGECKO_DELAY_SECONDS = 6.0
# CoinGecko's keyless tier answers 429 after a handful of calls a minute; a
# 429 is waited out, never recorded as a rule rejection.
_COINGECKO_RETRY_SECONDS = 65.0
_COINGECKO_MAX_ATTEMPTS = 6
_RPC_ATTEMPTS = 4
_RPC_RETRY_SECONDS = 3.0

CANDIDATE_SQL = """\
SELECT base
FROM app.source_lead_captures
WHERE source_exchange = 'gate'
  AND source_first_observed_at >= '{start}'
  AND source_first_observed_at < '{end}'
GROUP BY base
ORDER BY base;"""


# --- candidate snapshot ---------------------------------------------------------


def candidate_sql(window_end: datetime) -> str:
    return CANDIDATE_SQL.format(
        start=CANDIDATE_WINDOW_START.isoformat(), end=window_end.astimezone(UTC).isoformat()
    )


def v3_registry_bases() -> list[str]:
    """Every asset already in registry v3, so v4 never silently drops one
    that simply had no lead inside the window. Same rule applies to them."""
    payload = json.loads(
        (REGISTRY_DIR / "source_lead_identity_registry_v3.json").read_text(encoding="utf-8")
    )
    return sorted(
        {str(link["canonical_asset_id"]).split(":", 1)[1].upper() for link in payload["links"]}
    )


def build_candidate_snapshot(
    bases: Sequence[str], window_end: datetime, *, carried_over: Sequence[str] = ()
) -> dict[str, Any]:
    """The fixed candidate list: window bases plus `carried_over` (the v3
    registry assets). Window end must be an exact UTC instant after the
    start; the hash covers the window, both inputs and the merged list."""
    if window_end.tzinfo is None:
        raise ValueError("window_end must be timezone-aware")
    if window_end <= CANDIDATE_WINDOW_START:
        raise ValueError("window_end must be after the candidate window start")
    window_bases = sorted({base.strip() for base in bases if base.strip()})
    if not window_bases:
        raise ValueError("candidate list is empty")
    carried = sorted({base.strip() for base in carried_over if base.strip()})
    body = {
        "version": CANDIDATES_VERSION,
        "window_start": CANDIDATE_WINDOW_START.isoformat(),
        "window_end": window_end.astimezone(UTC).isoformat(),
        "query": candidate_sql(window_end),
        "window_bases": window_bases,
        "carried_over_from_v3": carried,
        "bases": sorted(set(window_bases) | set(carried)),
    }
    return {**body, "candidates_sha256": _sha256_canonical(body)}


def load_candidate_snapshot(path: Path = CANDIDATES_PATH) -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    body = {key: value for key, value in snapshot.items() if key != "candidates_sha256"}
    if _sha256_canonical(body) != snapshot.get("candidates_sha256"):
        raise ValueError(f"{path}: candidates_sha256 does not match its own content")
    if snapshot.get("version") != CANDIDATES_VERSION:
        raise ValueError(f"{path}: unexpected version {snapshot.get('version')!r}")
    return snapshot


# --- the rule (pure) ------------------------------------------------------------


@dataclass(frozen=True)
class RouteInputs:
    """Everything decide_route needs, already extracted from raw evidence.

    `target_contracts` is None when the target's asset data has no usable
    entry for the coin; `target_catalog_problem` then names why."""

    base: str
    target_exchange: str
    gate_perp_ok: bool
    target_perp_count: int
    gate_contracts: Mapping[str, tuple[str, ...]]
    target_contracts: Mapping[str, tuple[str, ...]] | None
    target_catalog_problem: str | None
    coingecko_projects: Mapping[tuple[str, str], tuple[tuple[str, str], ...]]


@dataclass(frozen=True)
class RouteDecision:
    base: str
    target_exchange: str
    approved: bool
    reason: str
    chain: str | None = None
    contract_address: str | None = None
    coingecko_id: str | None = None


def decide_route(inputs: RouteInputs) -> RouteDecision:
    """Apply IDENTITY_RULE_VERSION checks 1-7 (check 8 happens at capture)."""

    def reject(reason: str) -> RouteDecision:
        return RouteDecision(inputs.base, inputs.target_exchange, False, reason)

    if not inputs.gate_perp_ok:
        return reject("no_gate_perp")
    if inputs.target_perp_count == 0:
        return reject("no_target_perp")
    if inputs.target_perp_count > 1:
        return reject("ambiguous_target_perp")
    gate = {chain: inputs.gate_contracts.get(chain, ()) for chain in RULE_CHAINS}
    if any(len(addresses) > 1 for addresses in gate.values()):
        return reject("gate_ambiguous_contract")
    if not any(gate.values()):
        return reject("gate_no_supported_contract")
    if inputs.target_contracts is None:
        return reject(inputs.target_catalog_problem or "target_catalog_missing")
    target = {chain: inputs.target_contracts.get(chain, ()) for chain in RULE_CHAINS}
    if any(len(addresses) > 1 for addresses in target.values()):
        return reject("target_ambiguous_contract")
    if not any(target.values()):
        return reject("target_no_supported_contract")
    shared: list[tuple[str, str]] = []
    for chain in RULE_CHAINS:
        if gate[chain] and target[chain]:
            if gate[chain][0] != target[chain][0]:
                return reject("contract_conflict")
            shared.append((chain, gate[chain][0]))
    if not shared:
        return reject("no_shared_chain")
    chain, address = shared[0]
    projects = inputs.coingecko_projects.get((chain, address), ())
    if not projects:
        return reject("coingecko_no_match")
    if len(projects) > 1:
        return reject("coingecko_ambiguous")
    coingecko_id, symbol = projects[0]
    if symbol.upper() != inputs.base.upper():
        return reject("coingecko_symbol_mismatch")
    return RouteDecision(
        inputs.base,
        inputs.target_exchange,
        True,
        "approved",
        chain=chain,
        contract_address=address,
        coingecko_id=coingecko_id,
    )


# --- extraction from raw payloads (pure) ----------------------------------------


def _group(pairs: Sequence[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, set[str]] = {}
    for chain, address in pairs:
        grouped.setdefault(chain, set()).add(address)
    return {chain: tuple(sorted(addresses)) for chain, addresses in grouped.items()}


def gate_contracts(currency: Any) -> dict[str, tuple[str, ...]]:
    """(chain -> addresses) from one ccxt Gate currency entry, supported chains only."""
    networks = currency.get("networks") if isinstance(currency, dict) else None
    pairs: list[tuple[str, str]] = []
    for code, data in (networks or {}).items():
        chain = NETWORK_TO_CHAIN.get(str(code).upper())
        if chain not in RULE_CHAINS:
            continue
        info = data.get("info") if isinstance(data, dict) else None
        info = info if isinstance(info, dict) else {}
        address = normalize_evm_address(
            info.get("addr") or info.get("contractAddress") or info.get("contract")
        )
        if address is not None:
            pairs.append((chain, address))
    return _group(pairs)


def alpha_entries(catalog: Any, base: str) -> list[dict[str, Any]]:
    tokens = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(tokens, list):
        raise ValueError("binance alpha catalog payload has no data array")
    return [
        token
        for token in tokens
        if isinstance(token, dict) and str(token.get("symbol", "")).upper() == base.upper()
    ]


def alpha_contracts(
    entries: Sequence[dict[str, Any]],
) -> tuple[dict[str, tuple[str, ...]] | None, str | None]:
    if not entries:
        return None, "target_catalog_missing"
    if len(entries) > 1:
        return None, "target_catalog_ambiguous"
    entry = entries[0]
    chain = ALPHA_CHAIN_IDS.get(str(entry.get("chainId")))
    address = normalize_evm_address(entry.get("contractAddress"))
    if chain is None or address is None:
        return {}, None
    return {chain: (address,)}, None


def bybit_coin_rows(coin_info: Any, base: str) -> list[dict[str, Any]]:
    result = coin_info.get("result") if isinstance(coin_info, dict) else None
    rows = result.get("rows") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise ValueError("bybit coin-info payload has no result.rows array")
    return [
        row for row in rows if isinstance(row, dict) and str(row.get("coin", "")) == base.upper()
    ]


def bybit_contracts(
    rows: Sequence[dict[str, Any]],
) -> tuple[dict[str, tuple[str, ...]] | None, str | None]:
    """An empty contractAddress is no confirmation, per Bybit's own docs."""
    if not rows:
        return None, "target_catalog_missing"
    if len(rows) > 1:
        return None, "target_catalog_ambiguous"
    pairs: list[tuple[str, str]] = []
    for chain_entry in rows[0].get("chains") or []:
        if not isinstance(chain_entry, dict):
            continue
        chain = BYBIT_CHAINS.get(str(chain_entry.get("chain", "")).upper())
        address = normalize_evm_address(chain_entry.get("contractAddress"))
        if chain is not None and address is not None:
            pairs.append((chain, address))
    return _group(pairs), None


def exact_perp_count(target_exchange: str, instruments: Any, base: str) -> int:
    """Trading USDT-settled linear perpetuals whose reported base is BASE."""
    if target_exchange == "bybit":
        items = instruments.get("list") if isinstance(instruments, dict) else None
        return sum(
            1
            for item in items or []
            if item.get("baseCoin") == base.upper()
            and item.get("settleCoin") == "USDT"
            and item.get("quoteCoin") == "USDT"
            and item.get("contractType") == "LinearPerpetual"
            and item.get("status") == "Trading"
        )
    symbols = instruments.get("symbols") if isinstance(instruments, dict) else None
    return sum(
        1
        for item in symbols or []
        if item.get("baseAsset") == base.upper()
        and item.get("marginAsset") == "USDT"
        and item.get("quoteAsset") == "USDT"
        and item.get("contractType") == "PERPETUAL"
        and item.get("status") == "TRADING"
    )


def coingecko_index(coins: Any) -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
    """(chain, address) -> ((coingecko_id, symbol), ...) from coins/list?include_platform."""
    index: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for coin in coins if isinstance(coins, list) else []:
        platforms = coin.get("platforms") if isinstance(coin, dict) else None
        for platform, raw_address in (platforms or {}).items():
            chain = COINGECKO_PLATFORM_TO_CHAIN.get(platform)
            address = normalize_evm_address(raw_address)
            if chain in RULE_CHAINS and address is not None:
                index.setdefault((chain, address), []).append(
                    (str(coin.get("id")), str(coin.get("symbol", "")))
                )
    return {key: tuple(sorted(set(value))) for key, value in index.items()}


# --- fetch layer ----------------------------------------------------------------


def bybit_credentials() -> tuple[str, str] | None:
    """Env only; never logged, never written to evidence."""
    key = os.getenv("BYBIT_API_KEY", "").strip()
    secret = os.getenv("BYBIT_API_SECRET", "").strip()
    return (key, secret) if key and secret else None


async def fetch_bybit_coin_info(client: Any, credentials: tuple[str, str]) -> RawFetch:
    """Authenticated v5 coin-info for every coin in one call. Only the
    response body is stored; the signed request headers are not."""
    key, secret = credentials
    timestamp = str(int(time.time() * 1000))
    recv_window = "10000"
    signature = hmac.new(
        secret.encode(), (timestamp + key + recv_window).encode(), hashlib.sha256
    ).hexdigest()
    url = "https://api.bybit.com/v5/asset/coin/query-info"
    response = await client.get(
        url,
        headers={
            "X-BAPI-API-KEY": key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
        },
    )
    response.raise_for_status()
    raw_bytes = response.content
    payload = json.loads(raw_bytes)
    if payload.get("retCode") != 0:
        raise RuntimeError(f"bybit coin-info failed: retCode={payload.get('retCode')}")
    return RawFetch(
        source="bybit:asset_coin_query_info",
        endpoint=url,
        observed_at=datetime.now(UTC),
        raw_sha256=_sha256_bytes(raw_bytes),
        wire_exact=True,
        payload=payload,
    )


async def fetch_bybit_instruments(client: Any) -> RawFetch:
    """Every linear instrument, following the cursor. Hash over the joined list."""
    items: list[Any] = []
    cursor = ""
    url = "https://api.bybit.com/v5/market/instruments-info"
    while True:
        params = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        page = await _http_get_json(client, url, params=params)
        result = page.payload.get("result") if isinstance(page.payload, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise ValueError("bybit instruments-info page has no result.list")
        items.extend(result["list"])
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
    payload = {"list": items}
    return RawFetch(
        source="bybit:instruments_info_linear",
        endpoint=url,
        observed_at=datetime.now(UTC),
        raw_sha256=_sha256_canonical(payload),
        wire_exact=False,
        payload=payload,
    )


@dataclass(frozen=True)
class RunSources:
    gate_currencies: dict[str, Any]
    gate_perps: dict[str, dict[str, Any]]
    alpha_catalog: RawFetch
    binance_exchange_info: RawFetch
    bybit_instruments: RawFetch
    bybit_coin_info: RawFetch
    coingecko: dict[tuple[str, str], tuple[tuple[str, str], ...]]


def route_inputs(sources: RunSources, base: str, target_exchange: str) -> RouteInputs:
    gate_perp = sources.gate_perps.get(f"{base.upper()}_USDT")
    gate_perp_ok = gate_perp is not None and gate_perp.get("in_delisting") is False
    currency = sources.gate_currencies.get(base)
    if target_exchange == "bybit":
        instruments = sources.bybit_instruments.payload
        contracts, problem = bybit_contracts(bybit_coin_rows(sources.bybit_coin_info.payload, base))
    else:
        instruments = sources.binance_exchange_info.payload
        contracts, problem = alpha_contracts(alpha_entries(sources.alpha_catalog.payload, base))
    return RouteInputs(
        base=base,
        target_exchange=target_exchange,
        gate_perp_ok=gate_perp_ok,
        target_perp_count=exact_perp_count(target_exchange, instruments, base),
        gate_contracts=gate_contracts(currency),
        target_contracts=contracts,
        target_catalog_problem=problem,
        coingecko_projects=sources.coingecko,
    )


def _target_catalog_evidence(sources: RunSources, decision: RouteDecision) -> RawFetch:
    """The target's own asset entry, narrowed to identity fields (as v3 did
    for Alpha), with the chosen chain's contract as `contractAddress` so the
    shared v3 validator can check it."""
    base = decision.base
    if decision.target_exchange == "bybit":
        row = bybit_coin_rows(sources.bybit_coin_info.payload, base)[0]
        chains = [
            {key: entry.get(key) for key in ("chain", "chainType", "contractAddress")}
            for entry in row.get("chains") or []
            if isinstance(entry, dict)
        ]
        payload: dict[str, Any] = {
            "coin": row.get("coin"),
            "name": row.get("name"),
            "chains": chains,
            "chain": decision.chain,
            "contractAddress": decision.contract_address,
            "response_sha256": sources.bybit_coin_info.raw_sha256,
        }
        endpoint = sources.bybit_coin_info.endpoint
        observed_at = sources.bybit_coin_info.observed_at
        source = "bybit:coin_info_entry"
    else:
        entry = alpha_entries(sources.alpha_catalog.payload, base)[0]
        payload = {
            key: entry.get(key)
            for key in ("symbol", "name", "chainId", "chainName", "decimals", "offline")
        }
        payload["contractAddress"] = decision.contract_address
        payload["response_sha256"] = sources.alpha_catalog.raw_sha256
        endpoint = sources.alpha_catalog.endpoint
        observed_at = sources.alpha_catalog.observed_at
        source = "binance:alpha_catalog_entry"
    return RawFetch(
        source=source,
        endpoint=endpoint,
        observed_at=observed_at,
        raw_sha256=_sha256_canonical(payload),
        wire_exact=False,
        payload=payload,
    )


def _target_market(sources: RunSources, decision: RouteDecision) -> DerivativeMarketEvidence:
    symbol = f"{decision.base.upper()}USDT"
    market = (
        find_bybit_futures_market(sources.bybit_instruments, symbol)
        if decision.target_exchange == "bybit"
        else find_binance_futures_market(sources.binance_exchange_info, symbol)
    )
    if market is None:
        raise ValueError(f"{decision.base}: no {decision.target_exchange} market {symbol}")
    return market


def revalidate_v4_bundle(bundle: EvidenceBundle) -> None:
    """Rule check 7 against the stored CoinGecko evidence, plus the shared
    identity and route validators."""
    _validate_identity_class(
        identity_class=bundle.identity_class,
        base=bundle.base,
        gate_evidence=bundle.gate_evidence,
        coingecko_evidence=bundle.coingecko_evidence,
        target_catalog_evidence=bundle.target_catalog_evidence,
        source_contract=bundle.source_contract,
        target_contract=bundle.target_contract,
    )
    if bundle.source_market_evidence is None or bundle.target_market_evidence is None:
        raise ValueError(f"{bundle.base}: v4 bundle without derivative-market evidence")
    _validate_route_evidence(
        base=bundle.base,
        source_market=bundle.source_market_evidence,
        target_market=bundle.target_market_evidence,
    )
    coin = bundle.coingecko_evidence.payload
    if not isinstance(coin, dict) or str(coin.get("symbol", "")).upper() != bundle.base.upper():
        raise ValueError(f"{bundle.base}: coingecko symbol does not equal the base")
    platforms = coin.get("platforms") or {}
    contract = bundle.source_contract
    if not any(
        COINGECKO_PLATFORM_TO_CHAIN.get(platform) == contract.chain
        and normalize_evm_address(address) == contract.contract_address
        for platform, address in platforms.items()
    ):
        raise ValueError(f"{bundle.base}: coingecko does not list the chosen contract")


async def _retry_429(label: str, fetch: Callable[[], Awaitable[RawFetch]]) -> RawFetch:
    import httpx

    for attempt in range(1, _COINGECKO_MAX_ATTEMPTS + 1):
        try:
            return await fetch()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or attempt == _COINGECKO_MAX_ATTEMPTS:
                raise
            sys.stderr.write(f"coingecko 429 on {label}, waiting\n")
            await asyncio.sleep(_COINGECKO_RETRY_SECONDS)
    raise AssertionError("unreachable")


async def _fetch_coingecko_with_retry(client: Any, coingecko_id: str) -> RawFetch:
    return await _retry_429(coingecko_id, lambda: fetch_coingecko_coin(client, coingecko_id))


async def _decimals_with_retry(
    client: Any, chain: str, contract_address: str
) -> ChainContractEvidence:
    """Public RPC nodes sometimes return null for a block they just
    reported. Retried; a result still unusable after that is a rejection."""
    for attempt in range(1, _RPC_ATTEMPTS + 1):
        try:
            return await fetch_onchain_decimals(client, chain, contract_address)
        except ValueError:
            if attempt == _RPC_ATTEMPTS:
                raise
            await asyncio.sleep(_RPC_RETRY_SECONDS)
    raise AssertionError("unreachable")


async def capture_route_bundle(
    *,
    client: Any,
    sources: RunSources,
    decision: RouteDecision,
    decimals_cache: dict[tuple[str, str], ChainContractEvidence],
    gate_market_cache: dict[str, DerivativeMarketEvidence],
    coingecko_cache: dict[str, RawFetch],
    code_revision: str,
    working_tree_dirty: bool,
) -> EvidenceBundle:
    """Rule check 8 plus the bundle. A ValueError means the route fails the
    rule (recorded); any other exception aborts the run (nothing published)."""
    assert decision.chain is not None and decision.contract_address is not None
    assert decision.coingecko_id is not None
    key = (decision.chain, decision.contract_address)
    if key not in decimals_cache:
        decimals_cache[key] = await _decimals_with_retry(client, *key)
    contract = decimals_cache[key]
    if decision.base not in gate_market_cache:
        gate_market_cache[decision.base] = await fetch_gate_futures_contract(client, decision.base)
    if decision.coingecko_id not in coingecko_cache:
        await asyncio.sleep(_COINGECKO_DELAY_SECONDS)
        coingecko_cache[decision.coingecko_id] = await _fetch_coingecko_with_retry(
            client, decision.coingecko_id
        )
    currency = sources.gate_currencies[decision.base]
    gate_evidence = RawFetch(
        source="gate:fetch_currencies",
        endpoint="https://api.gateio.ws/api/v4/spot/currencies",
        observed_at=datetime.now(UTC),
        raw_sha256=_sha256_canonical(currency),
        wire_exact=False,
        payload=currency,
    )
    bundle = EvidenceBundle(
        evidence_version=EVIDENCE_VERSION_V4,
        base=decision.base,
        source_exchange="gate",
        target_exchange=decision.target_exchange,
        identity_class="exact_contract",
        source_contract=contract,
        target_contract=contract,
        gate_evidence=gate_evidence,
        target_catalog_evidence=_target_catalog_evidence(sources, decision),
        coingecko_evidence=coingecko_cache[decision.coingecko_id],
        source_market_evidence=gate_market_cache[decision.base],
        target_market_evidence=_target_market(sources, decision),
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        captured_at=datetime.now(UTC),
    )
    revalidate_v4_bundle(bundle)
    return _finalize_bundle(bundle)


# --- decisions and registry ------------------------------------------------------


def _route_order(decision: RouteDecision) -> tuple[str, int]:
    return decision.base, TARGET_EXCHANGES.index(decision.target_exchange)


def decisions_document(
    snapshot: Mapping[str, Any],
    decisions: Sequence[RouteDecision],
    bundles: Sequence[EvidenceBundle],
    *,
    run_id: str,
    code_revision: str,
    working_tree_dirty: bool,
    source_hashes: Mapping[str, str],
) -> dict[str, Any]:
    sha_by_route = {(b.base, b.target_exchange): b.bundle_sha256 for b in bundles}
    rows = [
        {**asdict(d), "bundle_sha256": sha_by_route.get((d.base, d.target_exchange))}
        for d in sorted(decisions, key=_route_order)
    ]
    summary: dict[str, dict[str, int]] = {}
    for d in decisions:
        by_reason = summary.setdefault(d.target_exchange, {})
        by_reason[d.reason] = by_reason.get(d.reason, 0) + 1
    approved_assets = sorted({d.base for d in decisions if d.approved})
    body = {
        "version": DECISIONS_VERSION,
        "rule_version": IDENTITY_RULE_VERSION,
        "run_id": run_id,
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "candidates_sha256": snapshot["candidates_sha256"],
        "source_hashes": dict(source_hashes),
        "summary_by_target": summary,
        "approved_asset_count": len(approved_assets),
        "routes": rows,
    }
    return {**body, "decisions_sha256": _sha256_canonical(body)}


def _evidence_url(commit: str, filename: str) -> str:
    return (
        f"https://raw.githubusercontent.com/mavlevich/schurfer/{commit}/apps/analytics/"
        f"schurfer_analytics/evidence/source_lead/v4/{filename}"
    )


def build_registry_v4(
    bundles: Sequence[EvidenceBundle],
    decisions: Mapping[str, Any],
    approval: Mapping[str, Any],
    *,
    evidence_commit: str,
) -> dict[str, Any]:
    """One Gate link per asset plus one link per approved target. The Gate
    link cites the first approved route's bundle in TARGET_EXCHANGES order.
    Refuses unless the approval names this exact decisions file and every
    approved route has exactly its recorded bundle."""
    if approval.get("decisions_sha256") != decisions.get("decisions_sha256"):
        raise ValueError("approval does not name this decisions file")
    for field_name in ("confirmed_by", "technical_review_by", "confirmed_at"):
        if not approval.get(field_name):
            raise ValueError(f"approval is missing {field_name}")
    by_route = {(b.base, b.target_exchange): b for b in bundles}
    approved = [row for row in decisions["routes"] if row["approved"]]
    if len(approved) != len(bundles):
        raise ValueError(f"{len(approved)} approved routes but {len(bundles)} evidence bundles")
    links: list[dict[str, str]] = []
    for base in sorted({row["base"] for row in approved}):
        routes = sorted(
            (row for row in approved if row["base"] == base),
            key=lambda row: TARGET_EXCHANGES.index(row["target_exchange"]),
        )
        route_bundles = []
        for row in routes:
            bundle = by_route.get((base, row["target_exchange"]))
            if bundle is None or bundle.bundle_sha256 != row["bundle_sha256"]:
                route = f"{base}/{row['target_exchange']}"
                raise ValueError(f"{route}: bundle does not match decisions")
            route_bundles.append(bundle)
        gate_bundle = route_bundles[0]
        assert gate_bundle.source_market_evidence is not None
        asset_id = f"asset:{base.lower()}"
        links.append(
            _link(
                asset_id,
                "gate",
                gate_bundle.source_market_evidence,
                gate_bundle,
                evidence_commit,
            )
        )
        for bundle in route_bundles:
            assert bundle.target_market_evidence is not None
            links.append(
                _link(
                    asset_id,
                    bundle.target_exchange,
                    bundle.target_market_evidence,
                    bundle,
                    evidence_commit,
                )
            )
    return {"schema_version": 1, "registry_version": REGISTRY_VERSION_V4, "links": links}


def _link(
    asset_id: str,
    exchange: str,
    market: DerivativeMarketEvidence,
    bundle: EvidenceBundle,
    evidence_commit: str,
) -> dict[str, str]:
    filename = _bundle_filename(bundle.base, bundle.source_exchange, bundle.target_exchange)
    return {
        "canonical_asset_id": asset_id,
        "exchange": exchange,
        "instrument_identity_key": (
            f"{exchange}:swap:{market.native_market_id}:{market.onboarded_at_ms}"
        ),
        "evidence_url": _evidence_url(evidence_commit, filename),
        "evidence_sha256": bundle.bundle_sha256,
    }


# --- CLI ------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(json_ready(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


async def _load_sources(client: Any, gate: Any, credentials: tuple[str, str]) -> RunSources:
    currencies = await gate.fetch_currencies()
    perps_raw = await _http_get_json(client, "https://api.gateio.ws/api/v4/futures/usdt/contracts")
    perps = {str(item.get("name")): item for item in perps_raw.payload if isinstance(item, dict)}
    alpha = await _http_get_json(
        client,
        "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list",
    )
    binance = await _http_get_json(client, "https://fapi.binance.com/fapi/v1/exchangeInfo")
    bybit_instruments = await fetch_bybit_instruments(client)
    bybit_coins = await fetch_bybit_coin_info(client, credentials)
    coingecko = await _retry_429(
        "coins/list",
        lambda: _http_get_json(
            client,
            "https://api.coingecko.com/api/v3/coins/list",
            params={"include_platform": "true"},
        ),
    )
    return RunSources(
        gate_currencies=currencies,
        gate_perps=perps,
        alpha_catalog=alpha,
        binance_exchange_info=binance,
        bybit_instruments=bybit_instruments,
        bybit_coin_info=bybit_coins,
        coingecko=coingecko_index(coingecko.payload),
    )


async def _decide(_args: argparse.Namespace) -> int:
    import httpx

    from .exchange_registry import EXCHANGE_FACTORIES

    credentials = bybit_credentials()
    if credentials is None:
        sys.stderr.write("BYBIT_API_KEY and BYBIT_API_SECRET must be set (read-only key)\n")
        return 2
    snapshot = load_candidate_snapshot()
    code_revision, working_tree_dirty = _current_git_state()
    run_id = str(uuid.uuid4())
    gate = EXCHANGE_FACTORIES["gate"]()
    EVIDENCE_DIR_V4.parent.mkdir(parents=True, exist_ok=True)
    staging = EVIDENCE_DIR_V4.parent / f".v4.staging.{run_id}"
    staging.mkdir()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            sources = await _load_sources(client, gate, credentials)
            decisions: list[RouteDecision] = []
            bundles: list[EvidenceBundle] = []
            decimals_cache: dict[tuple[str, str], ChainContractEvidence] = {}
            gate_markets: dict[str, DerivativeMarketEvidence] = {}
            coingecko_cache: dict[str, RawFetch] = {}
            for base in snapshot["bases"]:
                for target in TARGET_EXCHANGES:
                    decision = decide_route(route_inputs(sources, base, target))
                    if decision.approved:
                        try:
                            bundle = await capture_route_bundle(
                                client=client,
                                sources=sources,
                                decision=decision,
                                decimals_cache=decimals_cache,
                                gate_market_cache=gate_markets,
                                coingecko_cache=coingecko_cache,
                                code_revision=code_revision,
                                working_tree_dirty=working_tree_dirty,
                            )
                        except ValueError as exc:
                            decision = RouteDecision(
                                base, target, False, f"capture_rejected: {str(exc)[:300]}"
                            )
                        else:
                            save_evidence_bundle(
                                bundle, staging / _bundle_filename(base, "gate", target)
                            )
                            bundles.append(bundle)
                    decisions.append(decision)
                    sys.stderr.write(f"{base:>12} -> {target:<8} {decision.reason}\n")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        await gate.close()

    source_hashes = {
        "alpha_catalog": sources.alpha_catalog.raw_sha256,
        "binance_exchange_info": sources.binance_exchange_info.raw_sha256,
        "bybit_instruments": sources.bybit_instruments.raw_sha256,
        "bybit_coin_info": sources.bybit_coin_info.raw_sha256,
    }
    document = decisions_document(
        snapshot,
        decisions,
        bundles,
        run_id=run_id,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        source_hashes=source_hashes,
    )
    manifest = {
        "run_id": run_id,
        "evidence_version": EVIDENCE_VERSION_V4,
        "rule_version": IDENTITY_RULE_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "candidate_count": len(bundles),
        "candidates": sorted(bundle.base for bundle in bundles),
        "bundle_fingerprint": _sha256_canonical(sorted(b.bundle_sha256 for b in bundles)),
        "decisions_sha256": document["decisions_sha256"],
    }
    _write_json(staging / MANIFEST_FILENAME, manifest)
    _atomic_publish(staging, EVIDENCE_DIR_V4)
    _write_json(DECISIONS_PATH, document)
    sys.stderr.write(
        f"\n{len(bundles)} approved routes, {document['approved_asset_count']} assets; "
        f"decisions_sha256={document['decisions_sha256']}\n"
    )
    return 0


def _build_registry(args: argparse.Namespace) -> int:
    decisions = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    body = {key: value for key, value in decisions.items() if key != "decisions_sha256"}
    if _sha256_canonical(body) != decisions.get("decisions_sha256"):
        raise ValueError("decisions file does not match its own decisions_sha256")
    approval = json.loads(APPROVAL_PATH.read_text(encoding="utf-8"))
    bundles = load_all_evidence_bundles(EVIDENCE_DIR_V4)
    for bundle in bundles:
        revalidate_v4_bundle(bundle)
    registry = build_registry_v4(bundles, decisions, approval, evidence_commit=args.evidence_commit)
    parsed = parse_identity_registry(registry, expected_version=REGISTRY_VERSION_V4)
    verify_registry_against_evidence(parsed.links_by_identity, evidence_dir=EVIDENCE_DIR_V4)
    _write_json(REGISTRY_PATH_V4, registry)
    sys.stdout.write(f"{REGISTRY_VERSION_V4} fingerprint {parsed.fingerprint}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sql = sub.add_parser("candidates-sql", help="print the frozen candidate query")
    sql.add_argument("--window-end", required=True, type=parse_utc_datetime)
    cands = sub.add_parser("candidates", help="snapshot bases (one per line on stdin)")
    cands.add_argument("--window-end", required=True, type=parse_utc_datetime)
    sub.add_parser("decide", help="fetch evidence, apply the rule, publish bundles")
    reg = sub.add_parser("build-registry", help="build registry v4 from approved bundles")
    reg.add_argument("--evidence-commit", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "candidates-sql":
        sys.stdout.write(candidate_sql(args.window_end) + "\n")
        return
    if args.command == "candidates":
        snapshot = build_candidate_snapshot(
            sys.stdin.read().splitlines(), args.window_end, carried_over=v3_registry_bases()
        )
        _write_json(CANDIDATES_PATH, snapshot)
        sys.stdout.write(
            f"{len(snapshot['bases'])} candidates, sha256 {snapshot['candidates_sha256']}\n"
        )
        return
    if args.command == "decide":
        sys.exit(asyncio.run(_decide(args)))
    sys.exit(_build_registry(args))


if __name__ == "__main__":
    main()
