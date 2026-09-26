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
import gzip
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
    DECISIONS_FILENAME,
    EVIDENCE_DIR_V4,
    MANIFEST_FILENAME,
    ChainContractEvidence,
    DerivativeMarketEvidence,
    EvidenceBundle,
    RawFetch,
    _alpha_identity_fields,
    _atomic_publish,
    _bundle_filename,
    _coingecko_headers,
    _current_git_state,
    _finalize_bundle,
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


ALPHA_URL = (
    "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
)
GATE_PERPS_URL = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_COIN_INFO_URL = "https://api.bybit.com/v5/asset/coin/query-info"
COINGECKO_COINS_URL = "https://api.coingecko.com/api/v3/coins/list"

# Every source the rule reads. A run stores each one's exact response bytes
# (colleague review of PR C: without them, uniqueness and the rejection
# reasons could not be recomputed independently).
SOURCE_ENDPOINTS: dict[str, str] = {
    "gate_currencies": "ccxt gate.fetch_currencies() over /api/v4/spot/currencies",
    "gate_perps": GATE_PERPS_URL,
    "alpha_catalog": ALPHA_URL,
    "binance_exchange_info": BINANCE_EXCHANGE_INFO_URL,
    "bybit_instruments": f"{BYBIT_INSTRUMENTS_URL}?category=linear (every cursor page)",
    "bybit_coin_info": f"{BYBIT_COIN_INFO_URL} (authenticated, all coins)",
    "coingecko_coins": f"{COINGECKO_COINS_URL}?include_platform=true",
}
SOURCES_DIRNAME = "sources"


class TransientFetchError(RuntimeError):
    """A fetch still failing after its retries. Aborts the run; never a rule
    rejection."""


class RuleCheckFailedError(Exception):
    """Captured evidence fails a rule check (check 8 or revalidation). The
    only failure recorded as a route rejection."""


CAPTURE_REJECTED = "capture_rejected"


@dataclass(frozen=True)
class SourceSnapshot:
    """The exact bytes one source returned. `wire_exact` is False only for
    Gate currencies, which ccxt parses; those bytes are the canonical JSON
    of ccxt's result."""

    name: str
    observed_at: datetime
    wire_exact: bool
    pages: tuple[bytes, ...]

    @property
    def page_sha256(self) -> tuple[str, ...]:
        return tuple(_sha256_bytes(page) for page in self.pages)

    @property
    def sha256(self) -> str:
        return _sha256_canonical(list(self.page_sha256))


@dataclass(frozen=True)
class RunSources:
    snapshots: Mapping[str, SourceSnapshot]
    gate_currencies: dict[str, Any]
    gate_perps: dict[str, dict[str, Any]]
    alpha_catalog: RawFetch
    binance_exchange_info: RawFetch
    bybit_instruments: RawFetch
    bybit_coin_info: RawFetch
    coingecko: dict[tuple[str, str], tuple[tuple[str, str], ...]]


def _snapshot_fetch(snapshot: SourceSnapshot, payload: Any) -> RawFetch:
    return RawFetch(
        source=snapshot.name,
        endpoint=SOURCE_ENDPOINTS[snapshot.name],
        observed_at=snapshot.observed_at,
        raw_sha256=snapshot.sha256,
        wire_exact=snapshot.wire_exact and len(snapshot.pages) == 1,
        payload=payload,
    )


class SourceSnapshotError(ValueError):
    """A stored response is not a successful, well-formed answer. Raised
    before any classification, so an error page can never read as "no
    perpetual" or "coin missing" (colleague review of PR C)."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SourceSnapshotError(message)


def sources_from_snapshots(snapshots: Mapping[str, SourceSnapshot]) -> RunSources:
    """The one place raw bytes become rule inputs, used both by `decide` and
    by `recompute`, so a recomputation reads exactly what the run read.
    Every source must be a successful response with its required, non-empty
    structure; otherwise this raises and the run aborts."""
    missing = sorted(set(SOURCE_ENDPOINTS) - set(snapshots))
    _require(not missing, f"missing source snapshots: {missing}")

    def pages(name: str) -> list[Any]:
        _require(bool(snapshots[name].pages), f"{name}: no pages")
        try:
            return [json.loads(page) for page in snapshots[name].pages]
        except ValueError as exc:
            raise SourceSnapshotError(f"{name}: not JSON") from exc

    currencies = pages("gate_currencies")[0]
    _require(
        isinstance(currencies, dict) and bool(currencies), "gate currencies: empty or not a map"
    )
    perps = pages("gate_perps")[0]
    _require(isinstance(perps, list) and bool(perps), "gate perps: empty or not a list")
    alpha = pages("alpha_catalog")[0]
    _require(
        isinstance(alpha, dict)
        and alpha.get("success") is True
        and alpha.get("code") == "000000"
        and isinstance(alpha.get("data"), list)
        and bool(alpha["data"]),
        "binance alpha catalog: not a successful response with a non-empty data list",
    )
    binance = pages("binance_exchange_info")[0]
    _require(
        isinstance(binance, dict)
        and isinstance(binance.get("symbols"), list)
        and bool(binance["symbols"]),
        "binance exchangeInfo: no non-empty symbols list",
    )
    bybit_items: list[Any] = []
    bybit_pages = pages("bybit_instruments")
    for index, page in enumerate(bybit_pages):
        result = page.get("result") if isinstance(page, dict) else None
        _require(
            isinstance(page, dict)
            and page.get("retCode") == 0
            and isinstance(result, dict)
            and isinstance(result.get("list"), list),
            f"bybit instruments page {index}: not a successful response with result.list",
        )
        assert isinstance(result, dict)
        last = index == len(bybit_pages) - 1
        _require(
            bool(result.get("nextPageCursor")) != last,
            f"bybit instruments page {index}: cursor does not match the page order",
        )
        bybit_items.extend(result["list"])
    _require(bool(bybit_items), "bybit instruments: no instruments")
    coin_info = pages("bybit_coin_info")[0]
    coin_result = coin_info.get("result") if isinstance(coin_info, dict) else None
    _require(
        isinstance(coin_info, dict)
        and coin_info.get("retCode") == 0
        and isinstance(coin_result, dict)
        and isinstance(coin_result.get("rows"), list)
        and bool(coin_result["rows"]),
        "bybit coin-info: not a successful response with non-empty rows",
    )
    coins = pages("coingecko_coins")[0]
    _require(isinstance(coins, list) and bool(coins), "coingecko coins: empty or not a list")
    return RunSources(
        snapshots=snapshots,
        gate_currencies=currencies,
        gate_perps={str(item.get("name")): item for item in perps if isinstance(item, dict)},
        alpha_catalog=_snapshot_fetch(snapshots["alpha_catalog"], alpha),
        binance_exchange_info=_snapshot_fetch(snapshots["binance_exchange_info"], binance),
        bybit_instruments=_snapshot_fetch(snapshots["bybit_instruments"], {"list": bybit_items}),
        bybit_coin_info=_snapshot_fetch(snapshots["bybit_coin_info"], coin_info),
        coingecko=coingecko_index(coins),
    )


def save_source_snapshots(
    snapshots: Mapping[str, SourceSnapshot], directory: Path
) -> dict[str, Any]:
    """Gzip each page (mtime 0, so the file is deterministic) under
    `directory/sources/`; return the manifest section naming each file's
    uncompressed SHA-256."""
    target = directory / SOURCES_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    section: dict[str, Any] = {}
    for name, snapshot in sorted(snapshots.items()):
        files = []
        for index, page in enumerate(snapshot.pages):
            filename = f"{name}.{index}.json.gz"
            (target / filename).write_bytes(gzip.compress(page, mtime=0))
            files.append({"file": filename, "raw_sha256": _sha256_bytes(page)})
        section[name] = {
            "endpoint": SOURCE_ENDPOINTS[name],
            "observed_at": snapshot.observed_at.isoformat(),
            "wire_exact": snapshot.wire_exact,
            "sha256": snapshot.sha256,
            "pages": files,
        }
    return section


def load_source_snapshots(directory: Path, section: Mapping[str, Any]) -> dict[str, SourceSnapshot]:
    snapshots: dict[str, SourceSnapshot] = {}
    for name, meta in section.items():
        pages = []
        for entry in meta["pages"]:
            raw = gzip.decompress((directory / SOURCES_DIRNAME / entry["file"]).read_bytes())
            if _sha256_bytes(raw) != entry["raw_sha256"]:
                raise ValueError(f"source snapshot {entry['file']} does not match its sha256")
            pages.append(raw)
        snapshot = SourceSnapshot(
            name=name,
            observed_at=parse_utc_datetime(meta["observed_at"]),
            wire_exact=bool(meta["wire_exact"]),
            pages=tuple(pages),
        )
        if snapshot.sha256 != meta["sha256"]:
            raise ValueError(f"source snapshot {name} does not match its sha256")
        snapshots[name] = snapshot
    return snapshots


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
    """The target's own asset entry exactly as the source reported it,
    including its original `contractAddress` (colleague review of PR C: the
    first version wrote the decided address here, which made the check
    circular). Narrowed to identity fields, as v3 did for Alpha."""
    base = decision.base
    if decision.target_exchange == "bybit":
        row = bybit_coin_rows(sources.bybit_coin_info.payload, base)[0]
        chains = [
            {key: entry.get(key) for key in ("chain", "chainType", "contractAddress")}
            for entry in row.get("chains") or []
            if isinstance(entry, dict)
        ]
        selected = [
            entry
            for entry in chains
            if BYBIT_CHAINS.get(str(entry.get("chain", "")).upper()) == decision.chain
            and normalize_evm_address(entry.get("contractAddress")) == decision.contract_address
        ]
        if len(selected) != 1:
            raise RuleCheckFailedError(
                f"{base}: bybit coin-info has no single entry for the contract"
            )
        payload: dict[str, Any] = {
            "coin": row.get("coin"),
            "name": row.get("name"),
            "chains": chains,
            "selected_chain": selected[0]["chain"],
            "contractAddress": selected[0]["contractAddress"],
        }
        snapshot = sources.bybit_coin_info
        source = "bybit:coin_info_entry"
    else:
        payload = _alpha_identity_fields(alpha_entries(sources.alpha_catalog.payload, base)[0])
        snapshot = sources.alpha_catalog
        source = "binance:alpha_catalog_entry"
    payload["response_sha256"] = snapshot.raw_sha256
    return RawFetch(
        source=source,
        endpoint=snapshot.endpoint,
        observed_at=snapshot.observed_at,
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
    catalog = bundle.target_catalog_evidence
    if catalog is None:
        raise ValueError(f"{bundle.base}: v4 bundle without target catalog evidence")
    if bundle.target_exchange == "bybit":
        catalog_chain = BYBIT_CHAINS.get(str(catalog.payload.get("selected_chain", "")).upper())
    else:
        catalog_chain = ALPHA_CHAIN_IDS.get(str(catalog.payload.get("chainId")))
    if catalog_chain != bundle.source_contract.chain:
        raise ValueError(f"{bundle.base}: target catalog chain is not the chosen chain")
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


async def _retry_429[T](label: str, fetch: Callable[[], Awaitable[T]]) -> T:
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
    reported. Retried; still failing afterwards aborts the run (colleague
    review of PR C: it must never turn into a rule rejection)."""
    import httpx

    for attempt in range(1, _RPC_ATTEMPTS + 1):
        try:
            return await fetch_onchain_decimals(client, chain, contract_address)
        except (ValueError, httpx.HTTPError) as exc:
            if attempt == _RPC_ATTEMPTS:
                raise TransientFetchError(
                    f"decimals() for {contract_address} on {chain}: {exc}"
                ) from exc
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
    """Rule check 8 plus the bundle. Only RuleCheckFailedError, raised when the
    captured evidence fails revalidation, is a recorded rejection; every
    fetch error propagates and aborts the run (nothing published)."""
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
        observed_at=sources.snapshots["gate_currencies"].observed_at,
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
    try:
        revalidate_v4_bundle(bundle)
    except ValueError as exc:
        raise RuleCheckFailedError(str(exc)) from exc
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


def recompute_decisions(
    snapshot: Mapping[str, Any],
    sources: RunSources,
    decisions: Mapping[str, Any],
    bundles: Sequence[EvidenceBundle],
) -> list[str]:
    """Re-derive every route from the stored snapshots with the same code
    and compare. Returns the mismatches (empty means the decisions file is
    exactly what the stored sources imply). Also checks that each approved
    bundle's target catalog entry is the one in the stored snapshot."""
    problems: list[str] = []
    if decisions.get("candidates_sha256") != snapshot.get("candidates_sha256"):
        problems.append("decisions name a different candidate snapshot")
    rows = {(row["base"], row["target_exchange"]): row for row in decisions["routes"]}
    expected = {(base, target) for base in snapshot["bases"] for target in TARGET_EXCHANGES}
    if set(rows) != expected:
        problems.append(f"route set differs: {len(rows)} recorded, {len(expected)} expected")
    bundles_by_route = {(b.base, b.target_exchange): b for b in bundles}
    for base, target in sorted(expected & set(rows)):
        row = rows[(base, target)]
        decision = decide_route(route_inputs(sources, base, target))
        if row["approved"]:
            same = decision.approved and (
                decision.chain,
                decision.contract_address,
                decision.coingecko_id,
            ) == (row["chain"], row["contract_address"], row["coingecko_id"])
            bundle = bundles_by_route.get((base, target))
            if same and (
                bundle is None
                or bundle.target_catalog_evidence is None
                or bundle.target_catalog_evidence.payload
                != _target_catalog_evidence(sources, decision).payload
            ):
                problems.append(f"{base}/{target}: bundle catalog entry differs from the snapshot")
        elif str(row["reason"]).startswith(CAPTURE_REJECTED):
            same = decision.approved
        else:
            same = not decision.approved and decision.reason == row["reason"]
        if not same:
            problems.append(
                f"{base}/{target}: recorded {row['reason']}, recomputed {decision.reason}"
            )
    return problems


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


def _bybit_signed_headers(credentials: tuple[str, str]) -> dict[str, str]:
    key, secret = credentials
    timestamp = str(int(time.time() * 1000))
    recv_window = "10000"
    signature = hmac.new(
        secret.encode(), (timestamp + key + recv_window).encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-BAPI-API-KEY": key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
    }


async def _get_bytes(
    client: Any,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Only the response body is kept; request headers (keys) never are."""
    response = await client.get(url, params=params or {}, headers=headers or {})
    response.raise_for_status()
    content: bytes = response.content
    return content


async def fetch_source_snapshots(
    client: Any, gate: Any, credentials: tuple[str, str]
) -> dict[str, SourceSnapshot]:
    snapshots: dict[str, SourceSnapshot] = {}

    def add(name: str, pages: list[bytes], *, wire_exact: bool = True) -> None:
        snapshots[name] = SourceSnapshot(name, datetime.now(UTC), wire_exact, tuple(pages))

    currencies = await gate.fetch_currencies()
    add(
        "gate_currencies",
        [
            json.dumps(
                json_ready(currencies), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ],
        wire_exact=False,
    )
    add("gate_perps", [await _get_bytes(client, GATE_PERPS_URL)])
    add("alpha_catalog", [await _get_bytes(client, ALPHA_URL)])
    add("binance_exchange_info", [await _get_bytes(client, BINANCE_EXCHANGE_INFO_URL)])
    pages: list[bytes] = []
    cursor = ""
    while True:
        params = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        raw = await _get_bytes(client, BYBIT_INSTRUMENTS_URL, params=params)
        pages.append(raw)
        result = json.loads(raw).get("result") or {}
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
    add("bybit_instruments", pages)
    add(
        "bybit_coin_info",
        [await _get_bytes(client, BYBIT_COIN_INFO_URL, headers=_bybit_signed_headers(credentials))],
    )
    add(
        "coingecko_coins",
        [
            await _retry_429(
                "coins/list",
                lambda: _get_bytes(
                    client,
                    COINGECKO_COINS_URL,
                    params={"include_platform": "true"},
                    headers=_coingecko_headers(),
                ),
            )
        ],
    )
    return snapshots


def _load_published(directory: Path) -> tuple[dict[str, Any], dict[str, Any], RunSources]:
    manifest = json.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    decisions = json.loads((directory / DECISIONS_FILENAME).read_text(encoding="utf-8"))
    body = {key: value for key, value in decisions.items() if key != "decisions_sha256"}
    if _sha256_canonical(body) != decisions.get("decisions_sha256"):
        raise ValueError("decisions file does not match its own decisions_sha256")
    if manifest.get("decisions_sha256") != decisions["decisions_sha256"]:
        raise ValueError("manifest names a different decisions file")
    sources = sources_from_snapshots(load_source_snapshots(directory, manifest["sources"]))
    return manifest, decisions, sources


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
            snapshots = await fetch_source_snapshots(client, gate, credentials)
            sources = sources_from_snapshots(snapshots)
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
                        except RuleCheckFailedError as exc:
                            decision = RouteDecision(
                                base, target, False, f"{CAPTURE_REJECTED}: {str(exc)[:300]}"
                            )
                        else:
                            save_evidence_bundle(
                                bundle, staging / _bundle_filename(base, "gate", target)
                            )
                            bundles.append(bundle)
                    decisions.append(decision)
                    sys.stderr.write(f"{base:>12} -> {target:<8} {decision.reason}\n")

        sources_section = save_source_snapshots(snapshots, staging)
        document = decisions_document(
            snapshot,
            decisions,
            bundles,
            run_id=run_id,
            code_revision=code_revision,
            working_tree_dirty=working_tree_dirty,
            source_hashes={name: meta["sha256"] for name, meta in sources_section.items()},
        )
        _write_json(staging / DECISIONS_FILENAME, document)
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
            "sources": sources_section,
        }
        _write_json(staging / MANIFEST_FILENAME, manifest)
        # Self-check from the files about to be published, before publishing.
        _, stored, stored_sources = _load_published(staging)
        problems = recompute_decisions(
            snapshot, stored_sources, stored, load_all_evidence_bundles(staging)
        )
        if problems:
            raise RuntimeError("recompute disagrees with the run: " + "; ".join(problems[:10]))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        await gate.close()

    # Bundles, sources, decisions and manifest move together in one swap.
    _atomic_publish(staging, EVIDENCE_DIR_V4)
    sys.stderr.write(
        f"\n{len(bundles)} approved routes, {document['approved_asset_count']} assets; "
        f"decisions_sha256={document['decisions_sha256']}\n"
    )
    return 0


def _recompute(_args: argparse.Namespace) -> int:
    _, decisions, sources = _load_published(EVIDENCE_DIR_V4)
    bundles = load_all_evidence_bundles(EVIDENCE_DIR_V4)
    for bundle in bundles:
        revalidate_v4_bundle(bundle)
    problems = recompute_decisions(load_candidate_snapshot(), sources, decisions, bundles)
    for problem in problems:
        sys.stdout.write(f"MISMATCH {problem}\n")
    sys.stdout.write(
        f"{len(decisions['routes'])} routes recomputed from stored sources, "
        f"{len(problems)} mismatches; decisions_sha256={decisions['decisions_sha256']}\n"
    )
    return 1 if problems else 0


def _build_registry(args: argparse.Namespace) -> int:
    _, decisions, _ = _load_published(EVIDENCE_DIR_V4)
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
    sub.add_parser("recompute", help="re-derive every decision from the stored sources")
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
    if args.command == "recompute":
        sys.exit(_recompute(args))
    sys.exit(_build_registry(args))


if __name__ == "__main__":
    main()
