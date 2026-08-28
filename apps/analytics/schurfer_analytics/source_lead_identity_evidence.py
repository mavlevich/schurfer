"""Immutable evidence-bundle capture for gate-source-lead identity candidates.

research/source-lead-identity-evidence-v2 -- evidence collection only. This
module never writes to `app.source_lead_qualifications`, never touches the
identity registry (`source_lead_qualification.py`), and never changes what
`SourceLeadCaptureWorker` (source_lead_capture.py) does at runtime. Its only
job is to capture, hash, and persist the raw evidence a human reviewer or a
later registry-activation change would need -- see the module docstring of
`gate_identity_candidate_tooling.py` for the human review checklist this
feeds, and `docs/research/discovery-ledger.md` HYP-012 for why this exists.

## Why a separate evidence step, before any registry activation

A colleague review (2026-08-28) of an earlier draft of this work caught two
problems with folding evidence capture and registry activation into one
change:

1. **Evidence-URL provenance cannot reference its own commit.** A registry
   entry's `evidence_url` (see `source_lead_qualification.py`) needs a
   permanent https link to the raw evidence. A `raw.githubusercontent.com/
   <commit-sha>/...` link written in the SAME commit as the evidence file is
   circular (the commit's SHA is not known until the commit exists), and a
   link to an intermediate PR-branch commit becomes unreachable after this
   repository's standard squash-merge (every merge to `main` so far this
   project is a squash -- `git log --oneline main` shows one commit per PR).
   This module sidesteps the whole problem: evidence is packaged as files
   inside the analytics package itself (`evidence/source_lead/v2/`), shipped
   in the same distribution as the code that reads it, and integrity is
   checked by `load_evidence_bundle` recomputing the bundle's own SHA-256
   against `bundle_sha256` at load time -- no external URL is the source of
   truth.
2. **Registry activation has its own, separable risk surface** (a new
   `qualification_version`, a new DB CHECK CONSTRAINT pinning the registry's
   fingerprint, a new prospective-only cohort cutoff so already-observed
   history is never retroactively "qualified", and a real fix to
   `source_lead_capture.py`'s naive `f"{base}/USDT:USDT"` market lookup,
   which is the same class of bug fixed today in `ohlcv.go` for bingx/TRUMP
   -- confirmed real, not yet fixed, out of scope for this PR). Bundling
   that into the same change as evidence capture would make either piece
   harder to review and revert independently.

## What "raw" means here, honestly

`RawFetch.raw_sha256` is the SHA-256 of the exact response bytes this tool
received, for every source fetched directly over HTTP (Binance Alpha
catalog, CoinGecko `/coins/{id}`, the RPC `eth_call`/`eth_blockNumber`
responses) -- a verifier can re-fetch the same endpoint and compare byte for
byte (`RawFetch.wire_exact = True`). Gate's currency data goes through
ccxt's `fetch_currencies()`, which returns already-parsed Python objects,
not the wire response; for that one source (`wire_exact = False`)
`raw_sha256` is over this tool's own canonical JSON re-serialization of the
specific currency entry it extracted, not the original HTTP bytes.

## What this does not do

No candidate here is written anywhere as "approved" or "qualified". Every
bundle's `identity_class` is a descriptive label from evidence already
gathered earlier in this research thread (session chat, 2026-08-28), not a
new automated verdict -- `capture_bundle` fetches fresh evidence and computes
fresh hashes, but which candidates to capture and what tier they landed in
was human-reviewed synchronously, immediately before this tool existed. A
later, separate change decides whether/how the registry consumes any of
this.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .reporting import json_ready, normalize_code_revision

EVIDENCE_VERSION = "source_lead_identity_evidence_v2"

EVIDENCE_DIR = Path(__file__).parent / "evidence" / "source_lead" / "v2"

IdentityClass = Literal[
    "exact_contract",
    "same_asset_multichain_candidate",
    "third_party_bridge_only",
]

# chain label -> (EIP-155 chain id, public keyless RPC endpoint). Deliberately
# small and explicit -- an unmapped chain fails closed (raises), never
# guessed. Public endpoints only: no evidence artifact may ever contain an
# API key (colleague review requirement, 2026-08-28).
CHAIN_RPC: dict[str, tuple[int, str]] = {
    "bsc": (56, "https://bsc-dataseed.binance.org/"),
    "ethereum": (1, "https://ethereum-rpc.publicnode.com"),
}

_DECIMALS_SELECTOR = "0x313ce567"  # keccak256("decimals()")[:4]
_HTTP_TIMEOUT_SECONDS = 15.0
# CoinGecko's anonymous tier rate-limits aggressively (observed directly this
# session running gate_identity_candidate_tooling.py without a key). A small
# per-candidate delay plus an optional key (never logged, never stored in an
# evidence artifact -- see fetch_coingecko_coin) keeps a full run of ~23
# candidates from needing its own retry/backoff machinery.
_INTER_CANDIDATE_DELAY_SECONDS = 2.0


def _coingecko_headers() -> dict[str, str]:
    """Read-only, env-only: never passed as a CLI argument or logged, and
    never written into a saved evidence bundle -- fetch_coingecko_coin only
    stores the response body, never the request headers."""
    api_key = os.getenv("COINGECKO_API_KEY", "").strip()
    return {"x-cg-demo-api-key": api_key} if api_key else {}


@dataclass(frozen=True)
class RawFetch:
    """One captured response. `raw_sha256` is over the exact bytes received
    when `wire_exact` is True (every HTTP source here except Gate); when
    False (Gate only, via ccxt) it is over this tool's own canonical
    re-serialization of the extracted data -- see the module docstring's
    "What raw means here" section."""

    source: str
    endpoint: str
    observed_at: datetime
    raw_sha256: str
    wire_exact: bool
    payload: Any


@dataclass(frozen=True)
class ChainContractEvidence:
    chain: str
    chain_id: int
    contract_address: str
    decimals: int
    decimals_evidence: RawFetch
    block_number: int


@dataclass(frozen=True)
class EvidenceBundle:
    evidence_version: str
    base: str
    source_exchange: str
    target_exchange: str
    identity_class: IdentityClass
    source_contract: ChainContractEvidence
    # None only for third_party_bridge_only, where no target-side chain
    # evidence was found at all.
    target_contract: ChainContractEvidence | None
    gate_evidence: RawFetch
    # None only for third_party_bridge_only.
    target_catalog_evidence: RawFetch | None
    coingecko_evidence: RawFetch
    code_revision: str
    working_tree_dirty: bool
    captured_at: datetime
    # Computed last, over the canonical JSON of every field above (this
    # field excluded) -- see compute_bundle_sha256.
    bundle_sha256: str = field(default="")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_canonical(value: Any) -> str:
    encoded = json.dumps(
        json_ready(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def compute_bundle_sha256(bundle: EvidenceBundle) -> str:
    """Hash every field of `bundle` except `bundle_sha256` itself -- both
    `capture_bundle` (computing it) and `load_evidence_bundle` (reverifying
    it) call this the same way, so a bundle that was hand-edited after
    capture fails to load rather than silently being trusted."""
    payload = asdict(bundle)
    payload.pop("bundle_sha256", None)
    return _sha256_canonical(payload)


# --- fetch layer (I/O) -------------------------------------------------------


async def _http_get_json(
    client: Any, url: str, *, params: dict[str, str] | None = None
) -> RawFetch:
    response = await client.get(url, params=params or {})
    response.raise_for_status()
    raw_bytes = response.content
    return RawFetch(
        source=url,
        endpoint=str(response.url),
        observed_at=datetime.now(UTC),
        raw_sha256=_sha256_bytes(raw_bytes),
        wire_exact=True,
        payload=json.loads(raw_bytes),
    )


async def _rpc_post(client: Any, url: str, body: dict[str, Any]) -> RawFetch:
    response = await client.post(url, json=body)
    response.raise_for_status()
    raw_bytes = response.content
    return RawFetch(
        source=f"rpc:{body.get('method')}",
        endpoint=url,
        observed_at=datetime.now(UTC),
        raw_sha256=_sha256_bytes(raw_bytes),
        wire_exact=True,
        payload=json.loads(raw_bytes),
    )


async def fetch_gate_currency(exchange: Any, base: str) -> RawFetch:
    """Fetch base's currency entry via ccxt. Not wire-exact -- see the module
    docstring. Fails closed (raises) rather than returning a partial bundle:
    unlike gate_identity_candidate_tooling.py's classifier, this tool only
    ever captures evidence for a candidate a human has already decided is
    worth capturing, so a fetch failure here should stop the run and be
    investigated, not be silently recorded as "missing"."""
    currencies = await exchange.fetch_currencies()
    if not isinstance(currencies, dict) or base not in currencies:
        raise ValueError(f"gate reported no currency entry for {base!r}")
    entry = currencies[base]
    return RawFetch(
        source="gate:fetch_currencies",
        endpoint="https://api.gateio.ws/api/v4/spot/currencies",
        observed_at=datetime.now(UTC),
        raw_sha256=_sha256_canonical(entry),
        wire_exact=False,
        payload=entry,
    )


async def fetch_binance_alpha_catalog(client: Any) -> RawFetch:
    return await _http_get_json(
        client,
        "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list",
    )


def find_alpha_entry(catalog: RawFetch, symbol: str) -> dict[str, Any] | None:
    """The catalog fetch is shared across every candidate in one run (one
    HTTP call, not one per candidate); this extracts one symbol's entry from
    it. Returns None when the symbol is absent -- a real, meaningful result
    for third_party_bridge_only candidates, not an error."""
    tokens = catalog.payload.get("data") if isinstance(catalog.payload, dict) else None
    if not isinstance(tokens, list):
        raise ValueError("binance alpha catalog payload has no data array")
    matches = [
        t
        for t in tokens
        if isinstance(t, dict) and str(t.get("symbol", "")).upper() == symbol.upper()
    ]
    if len(matches) > 1:
        raise ValueError(f"binance alpha catalog has {len(matches)} entries for symbol {symbol!r}")
    return matches[0] if matches else None


# The Binance Alpha catalog's full entry includes fields with no identity
# value here (price, volume, holders, icon URLs) and one field
# (`tokenId`, Binance's own internal catalog identifier) that is a
# high-entropy string flagged as a likely secret by this repo's
# gitleaks pre-commit hook even though it is plain public data returned by
# an unauthenticated GET -- narrowing the stored evidence to exactly the
# fields a reviewer needs also sidesteps that false positive.
_ALPHA_IDENTITY_FIELDS = (
    "symbol",
    "name",
    "chainId",
    "chainName",
    "contractAddress",
    "decimals",
    "listingCex",
    "offline",
    "fullyDelisted",
)


def _alpha_identity_fields(entry: dict[str, Any]) -> dict[str, Any]:
    return {field: entry.get(field) for field in _ALPHA_IDENTITY_FIELDS}


async def fetch_coingecko_coin(client: Any, coingecko_id: str) -> RawFetch:
    response = await client.get(
        f"https://api.coingecko.com/api/v3/coins/{coingecko_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "false",
            "developer_data": "false",
        },
        headers=_coingecko_headers(),
    )
    response.raise_for_status()
    raw_bytes = response.content
    return RawFetch(
        source=f"https://api.coingecko.com/api/v3/coins/{coingecko_id}",
        endpoint=str(response.url),
        observed_at=datetime.now(UTC),
        raw_sha256=_sha256_bytes(raw_bytes),
        wire_exact=True,
        payload=json.loads(raw_bytes),
    )


async def fetch_onchain_decimals(
    client: Any, chain: str, contract_address: str
) -> ChainContractEvidence:
    """decimals() plus the block number it was read at, via a public keyless
    RPC. Fails closed: any RPC error, or a response with no numeric result,
    raises rather than defaulting decimals to 18 -- colleague review,
    2026-08-28: NIL's real decimals is 6; a silent default would corrupt any
    future raw-amount conversion by a factor of 10**12."""
    if chain not in CHAIN_RPC:
        raise ValueError(f"no RPC endpoint registered for chain {chain!r}")
    chain_id, rpc_endpoint = CHAIN_RPC[chain]

    decimals_call = await _rpc_post(
        client,
        rpc_endpoint,
        {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": contract_address, "data": _DECIMALS_SELECTOR}, "latest"],
            "id": 1,
        },
    )
    result = (
        decimals_call.payload.get("result") if isinstance(decimals_call.payload, dict) else None
    )
    if not isinstance(result, str) or result in ("0x", ""):
        raise ValueError(
            f"decimals() call for {contract_address} on {chain} returned no usable result: "
            f"{decimals_call.payload!r}"
        )
    decimals = int(result, 16)

    block_call = await _rpc_post(
        client, rpc_endpoint, {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 2}
    )
    block_result = (
        block_call.payload.get("result") if isinstance(block_call.payload, dict) else None
    )
    if not isinstance(block_result, str):
        raise ValueError(
            f"eth_blockNumber on {chain} returned no usable result: {block_call.payload!r}"
        )
    block_number = int(block_result, 16)

    return ChainContractEvidence(
        chain=chain,
        chain_id=chain_id,
        contract_address=contract_address.lower(),
        decimals=decimals,
        decimals_evidence=decimals_call,
        block_number=block_number,
    )


async def capture_bundle(
    *,
    http_client: Any,
    gate_exchange: Any,
    base: str,
    source_exchange: str,
    target_exchange: str,
    source_chain: str,
    source_contract_address: str,
    coingecko_id: str,
    identity_class: IdentityClass,
    target_chain: str | None,
    target_contract_address: str | None,
    alpha_catalog: RawFetch,
    code_revision: str,
    working_tree_dirty: bool,
) -> EvidenceBundle:
    """Capture one candidate's full evidence bundle. `alpha_catalog` is
    fetched once per run by the caller and passed in, not refetched per
    candidate -- see fetch_binance_alpha_catalog's docstring."""
    if (target_chain is None) != (target_contract_address is None):
        raise ValueError("target_chain and target_contract_address must both be set, or both None")
    if identity_class == "third_party_bridge_only" and target_chain is not None:
        raise ValueError("third_party_bridge_only must not carry target chain evidence")
    if identity_class != "third_party_bridge_only" and target_chain is None:
        raise ValueError(f"identity_class {identity_class!r} requires target chain evidence")

    gate_evidence = await fetch_gate_currency(gate_exchange, base)
    coingecko_evidence = await fetch_coingecko_coin(http_client, coingecko_id)
    source_contract = await fetch_onchain_decimals(
        http_client, source_chain, source_contract_address
    )

    target_contract: ChainContractEvidence | None = None
    target_catalog_evidence: RawFetch | None = None
    if target_chain is not None and target_contract_address is not None:
        target_contract = await fetch_onchain_decimals(
            http_client, target_chain, target_contract_address
        )
        alpha_entry = find_alpha_entry(alpha_catalog, base)
        if alpha_entry is None:
            raise ValueError(f"expected a binance alpha catalog entry for {base!r}, found none")
        identity_fields = _alpha_identity_fields(alpha_entry)
        target_catalog_evidence = RawFetch(
            source="binance:alpha_catalog_entry",
            endpoint=alpha_catalog.endpoint,
            observed_at=alpha_catalog.observed_at,
            raw_sha256=_sha256_canonical(identity_fields),
            wire_exact=False,  # extracted from the shared catalog response, see find_alpha_entry
            payload=identity_fields,
        )

    bundle = EvidenceBundle(
        evidence_version=EVIDENCE_VERSION,
        base=base,
        source_exchange=source_exchange,
        target_exchange=target_exchange,
        identity_class=identity_class,
        source_contract=source_contract,
        target_contract=target_contract,
        gate_evidence=gate_evidence,
        target_catalog_evidence=target_catalog_evidence,
        coingecko_evidence=coingecko_evidence,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        captured_at=datetime.now(UTC),
        bundle_sha256="",
    )
    return _finalize_bundle(bundle)


def _finalize_bundle(bundle: EvidenceBundle) -> EvidenceBundle:
    digest = compute_bundle_sha256(bundle)
    payload = asdict(bundle)
    payload["bundle_sha256"] = digest
    return _bundle_from_dict(payload)


# --- (de)serialization --------------------------------------------------------


def _rawfetch_from_dict(payload: dict[str, Any]) -> RawFetch:
    return RawFetch(
        source=payload["source"],
        endpoint=payload["endpoint"],
        observed_at=_parse_dt(payload["observed_at"]),
        raw_sha256=payload["raw_sha256"],
        wire_exact=payload["wire_exact"],
        payload=payload["payload"],
    )


def _chain_evidence_from_dict(payload: dict[str, Any]) -> ChainContractEvidence:
    return ChainContractEvidence(
        chain=payload["chain"],
        chain_id=payload["chain_id"],
        contract_address=payload["contract_address"],
        decimals=payload["decimals"],
        decimals_evidence=_rawfetch_from_dict(payload["decimals_evidence"]),
        block_number=payload["block_number"],
    )


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _bundle_from_dict(payload: dict[str, Any]) -> EvidenceBundle:
    return EvidenceBundle(
        evidence_version=payload["evidence_version"],
        base=payload["base"],
        source_exchange=payload["source_exchange"],
        target_exchange=payload["target_exchange"],
        identity_class=payload["identity_class"],
        source_contract=_chain_evidence_from_dict(payload["source_contract"]),
        target_contract=(
            _chain_evidence_from_dict(payload["target_contract"])
            if payload.get("target_contract") is not None
            else None
        ),
        gate_evidence=_rawfetch_from_dict(payload["gate_evidence"]),
        target_catalog_evidence=(
            _rawfetch_from_dict(payload["target_catalog_evidence"])
            if payload.get("target_catalog_evidence") is not None
            else None
        ),
        coingecko_evidence=_rawfetch_from_dict(payload["coingecko_evidence"]),
        code_revision=payload["code_revision"],
        working_tree_dirty=payload["working_tree_dirty"],
        captured_at=_parse_dt(payload["captured_at"]),
        bundle_sha256=payload["bundle_sha256"],
    )


def render_bundle_json(bundle: EvidenceBundle) -> str:
    return (
        json.dumps(json_ready(asdict(bundle)), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def evidence_bundle_path(base: str, source_exchange: str, target_exchange: str) -> Path:
    filename = f"{base.lower()}-{source_exchange.lower()}-{target_exchange.lower()}.json"
    return EVIDENCE_DIR / filename


def save_evidence_bundle(bundle: EvidenceBundle, path: Path | None = None) -> Path:
    target_path = path or evidence_bundle_path(
        bundle.base, bundle.source_exchange, bundle.target_exchange
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(render_bundle_json(bundle), encoding="utf-8")
    return target_path


class EvidenceIntegrityError(ValueError):
    """Raised by load_evidence_bundle when a bundle's stored bundle_sha256
    does not match its own recomputed hash -- a corrupted or hand-edited
    evidence file, never silently trusted."""


def load_evidence_bundle(path: Path) -> EvidenceBundle:
    """Load one evidence bundle from disk and fail closed if its stored
    bundle_sha256 does not match a fresh recomputation over its own content.
    This is the actual integrity check a colleague review (2026-08-28) found
    missing from source_lead_qualification.py's registry loader (which only
    validates evidence_sha256's string *format*, never its content) -- any
    future registry-activation change that reads these bundles must call
    this, not read the JSON file directly."""
    if not path.is_file():
        raise EvidenceIntegrityError(f"evidence bundle not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    stored_sha256 = raw.get("bundle_sha256")
    bundle = _bundle_from_dict(raw)
    recomputed = compute_bundle_sha256(bundle)
    if recomputed != stored_sha256:
        raise EvidenceIntegrityError(
            f"evidence bundle {path} failed integrity check: "
            f"stored bundle_sha256={stored_sha256!r}, recomputed={recomputed!r}"
        )
    return bundle


def load_all_evidence_bundles(directory: Path | None = None) -> tuple[EvidenceBundle, ...]:
    """Load and integrity-check every evidence bundle in directory (default
    EVIDENCE_DIR), sorted by filename for a deterministic order. Raises on
    the first bundle that fails its integrity check -- fail closed, never
    silently skip a corrupted file."""
    target_dir = directory or EVIDENCE_DIR
    if not target_dir.is_dir():
        return ()
    paths = sorted(target_dir.glob("*.json"))
    return tuple(load_evidence_bundle(path) for path in paths)


# --- CLI -----------------------------------------------------------------------

# The exact candidate set decided by human review earlier in this research
# thread (session chat, 2026-08-28) -- see the module docstring's "What this
# does not do" section. Each tuple is
# (base, target_exchange, source_chain, source_contract, target_chain,
#  target_contract, coingecko_id, identity_class).
CANDIDATES: tuple[tuple[str, str, str, str, str | None, str | None, str, IdentityClass], ...] = (
    (
        "SKYAI",
        "binance",
        "bsc",
        "0x92aa03137385f18539301349dcfc9ebc923ffb10",
        "bsc",
        "0x92aa03137385f18539301349dcfc9ebc923ffb10",
        "skyai",
        "exact_contract",
    ),
    (
        "TUT",
        "binance",
        "bsc",
        "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
        "bsc",
        "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
        "tutorial",
        "exact_contract",
    ),
    (
        "BR",
        "binance",
        "bsc",
        "0xff7d6a96ae471bbcd7713af9cb1feeb16cf56b41",
        "bsc",
        "0xff7d6a96ae471bbcd7713af9cb1feeb16cf56b41",
        "bedrock-token",
        "exact_contract",
    ),
    (
        "ARIA",
        "binance",
        "bsc",
        "0x5d3a12c42e5372b2cc3264ab3cdcf660a1555238",
        "bsc",
        "0x5d3a12c42e5372b2cc3264ab3cdcf660a1555238",
        "aria-ai",
        "exact_contract",
    ),
    (
        "BTR",
        "binance",
        "bsc",
        "0xfed13d0c40790220fbde712987079eda1ed75c51",
        "bsc",
        "0xfed13d0c40790220fbde712987079eda1ed75c51",
        "bitlayer-bitvm",
        "exact_contract",
    ),
    (
        "UB",
        "binance",
        "bsc",
        "0x40b8129b786d766267a7a118cf8c07e31cdb6fde",
        "bsc",
        "0x40b8129b786d766267a7a118cf8c07e31cdb6fde",
        "unibase",
        "exact_contract",
    ),
    (
        "CYS",
        "binance",
        "bsc",
        "0x0c69199c1562233640e0db5ce2c399a88eb507c7",
        "bsc",
        "0x0c69199c1562233640e0db5ce2c399a88eb507c7",
        "cysic",
        "exact_contract",
    ),
    (
        "VELVET",
        "binance",
        "bsc",
        "0x8b194370825e37b33373e74a41009161808c1488",
        "bsc",
        "0x8b194370825e37b33373e74a41009161808c1488",
        "velvet",
        "exact_contract",
    ),
    (
        "HOME",
        "binance",
        "bsc",
        "0x4bfaa776991e85e5f8b1255461cbbd216cfc714f",
        "bsc",
        "0x4bfaa776991e85e5f8b1255461cbbd216cfc714f",
        "home",
        "exact_contract",
    ),
    (
        "BEAT",
        "binance",
        "bsc",
        "0xcf3232b85b43bca90e51d38cc06cc8bb8c8a3e36",
        "bsc",
        "0xcf3232b85b43bca90e51d38cc06cc8bb8c8a3e36",
        "audiera",
        "exact_contract",
    ),
    (
        "BAS",
        "binance",
        "bsc",
        "0x0f0df6cb17ee5e883eddfef9153fc6036bdb4e37",
        "bsc",
        "0x0f0df6cb17ee5e883eddfef9153fc6036bdb4e37",
        "bas",
        "exact_contract",
    ),
    (
        "EDEN",
        "binance",
        "ethereum",
        "0x24a3d725c37a8d1a66eb87f0e5d07fe67c120035",
        "bsc",
        "0x235b6fe22b4642ada16d311855c49ce7de260841",
        "openeden",
        "exact_contract",
    ),
    (
        "AIOT",
        "binance",
        "bsc",
        "0x55ad16bd573b3365f43a9daeb0cc66a73821b4a5",
        "bsc",
        "0x55ad16bd573b3365f43a9daeb0cc66a73821b4a5",
        "okzoo",
        "exact_contract",
    ),
    (
        "AKE",
        "binance",
        "bsc",
        "0x2c3a8ee94ddd97244a93bc48298f97d2c412f7db",
        "bsc",
        "0x2c3a8ee94ddd97244a93bc48298f97d2c412f7db",
        "akedo",
        "exact_contract",
    ),
    (
        "HEMI",
        "binance",
        "bsc",
        "0x5ffd0eadc186af9512542d0d5e5eafc65d5afc5b",
        "bsc",
        "0x5ffd0eadc186af9512542d0d5e5eafc65d5afc5b",
        "hemi",
        "exact_contract",
    ),
    (
        "GWEI",
        "binance",
        "ethereum",
        "0x2798b1cc5a993085e8a9d46e80499f1b63f42204",
        "bsc",
        "0x30117e4bc17d7b044194b76a38365c53b72f7d49",
        "ethgas-2",
        "same_asset_multichain_candidate",
    ),
    (
        "ENSO",
        "binance",
        "ethereum",
        "0x699f088b5dddcafb7c4824db5b10b57b37cb0c66",
        "bsc",
        "0xfeb339236d25d3e415f280189bc7c2fbab6ae9ef",
        "enso",
        "same_asset_multichain_candidate",
    ),
    (
        "RESOLV",
        "binance",
        "ethereum",
        "0x259338656198ec7a76c729514d3cb45dfbf768a1",
        "bsc",
        "0xda6cef7f667d992a60eb823ab215493aa0c6b360",
        "resolv",
        "same_asset_multichain_candidate",
    ),
    (
        "ROBO",
        "binance",
        "ethereum",
        "0x32b4d049fe4c888d2b92eecaf729f44df6b1f36e",
        "bsc",
        "0x475cbf5919608e0c6af00e7bf87fab83bf3ef6e2",
        "robo-token-2",
        "same_asset_multichain_candidate",
    ),
    (
        "NIL",
        "binance",
        "ethereum",
        "0x7cf9a80db3b29ee8efe3710aadb7b95270572d47",
        None,
        None,
        "nillion",
        "third_party_bridge_only",
    ),
    (
        "COTI",
        "binance",
        "ethereum",
        "0xddb3422497e61e13543bea06989c0789117555c5",
        None,
        None,
        "coti",
        "third_party_bridge_only",
    ),
    (
        "BICO",
        "binance",
        "ethereum",
        "0xf17e65822b568b3903685a7c9f496cf7656cc6c2",
        None,
        None,
        "biconomy",
        "third_party_bridge_only",
    ),
    (
        "CTSI",
        "binance",
        "ethereum",
        "0x491604c0fdf08347dd1fa4ee062a822a5dd06b5d",
        None,
        None,
        "cartesi",
        "third_party_bridge_only",
    ),
)


async def _run(args: argparse.Namespace) -> None:
    import httpx

    from .exchange_registry import EXCHANGE_FACTORIES

    code_revision = normalize_code_revision(args.code_revision) if args.code_revision else "unknown"
    gate = EXCHANGE_FACTORIES["gate"]()
    saved: list[Path] = []
    failed: list[tuple[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            alpha_catalog = await fetch_binance_alpha_catalog(client)
            for index, (
                base,
                target_exchange,
                source_chain,
                source_contract,
                target_chain,
                target_contract,
                coingecko_id,
                identity_class,
            ) in enumerate(CANDIDATES):
                if index > 0:
                    await asyncio.sleep(_INTER_CANDIDATE_DELAY_SECONDS)
                try:
                    bundle = await capture_bundle(
                        http_client=client,
                        gate_exchange=gate,
                        base=base,
                        source_exchange="gate",
                        target_exchange=target_exchange,
                        source_chain=source_chain,
                        source_contract_address=source_contract,
                        coingecko_id=coingecko_id,
                        identity_class=identity_class,
                        target_chain=target_chain,
                        target_contract_address=target_contract,
                        alpha_catalog=alpha_catalog,
                        code_revision=code_revision,
                        working_tree_dirty=args.working_tree_dirty,
                    )
                    saved.append(save_evidence_bundle(bundle))
                    sys.stderr.write(f"captured {base} -> {target_exchange} ({identity_class})\n")
                except Exception as exc:  # a single candidate's failure must not abort the run
                    failed.append((base, f"{type(exc).__name__}: {exc}"))
                    sys.stderr.write(f"FAILED {base} -> {target_exchange}: {exc}\n")
    finally:
        await gate.close()

    sys.stderr.write(f"\n{len(saved)} bundles captured, {len(failed)} failed.\n")
    if failed:
        for base, error in failed:
            sys.stderr.write(f"  {base}: {error}\n")
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty", action=argparse.BooleanOptionalAction, required=True
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
