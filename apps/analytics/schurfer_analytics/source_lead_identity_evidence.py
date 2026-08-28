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

## A second colleague review round (2026-08-28) found real defects in the
## first implementation, all fixed

- **`identity_class` was never checked against the evidence just fetched.**
  The first version trusted the candidate table's classification outright;
  EDEN was recorded as `exact_contract` with a source contract on ethereum
  and a target contract on bsc -- different chains, different addresses,
  the opposite of what exact_contract means. `_validate_identity_class` now
  recomputes the relationship from `gate_evidence`/`coingecko_evidence`/
  `target_catalog_evidence` themselves before a bundle is allowed to save,
  and `capture_bundle` calls it unconditionally.
- **Provenance was taken from CLI arguments, not determined by the tool.**
  Every bundle the first version produced recorded a `code_revision` from
  before its own commit (a caller cannot know, in advance, the hash of the
  commit that will contain the evidence) and `working_tree_dirty=False`
  despite the tool's own new files being uncommitted at capture time.
  `_current_git_state` now asks git directly; `code_revision`/
  `working_tree_dirty` are no longer CLI flags at all.
- **`decimals()` was read against `"latest"`, with the block number recorded
  from a separate, later call** -- not provably the same block.
  `fetch_onchain_decimals` now reads the block number and hash first, then
  pins `eth_call` to that exact block.
- **A partially failed run could leave a mixed-vintage evidence
  directory** (some files fresh, some stale from a prior run). `_run` now
  captures into a temporary staging directory and only replaces
  `EVIDENCE_DIR` -- one atomic directory swap -- when every candidate
  succeeds; any failure discards the whole staged run. A `manifest.json`
  (run id, candidate list, an overall bundle fingerprint) is published
  alongside the bundles.
- **`load_all_evidence_bundles` returned `()` for a missing or empty
  directory**, which reads identically to "nothing captured yet" for a
  future registry consumer. It now raises `EvidenceIntegrityError` in both
  cases unless the caller passes `allow_empty=True` (used by tests that
  intentionally exercise an empty directory).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .reporting import json_ready

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


def _current_git_state() -> tuple[str, bool]:
    """Determine (HEAD commit, working-tree-is-dirty) by asking git directly
    -- never trust an externally supplied value for this. A colleague review
    (2026-08-28) found every evidence file this tool first produced recorded
    a code_revision from BEFORE the tool's own commit (the caller cannot
    know, in advance, the hash of the very commit that will contain the
    evidence) and working_tree_dirty=False even though the tool's own new
    files were uncommitted at capture time. Raises if git itself is
    unavailable or this is not a git checkout -- fail closed rather than
    recording an unknown/fabricated provenance."""
    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("git executable not found on PATH")
    repo_dir = Path(__file__).resolve().parent
    head = subprocess.run(  # noqa: S603 -- fixed argv, resolved executable, no shell, no user input
        [git_executable, "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 -- fixed argv, resolved executable, no shell, no user input
        [git_executable, "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return head, bool(status.strip())


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
    block_hash: str


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
    """decimals() at a specific, recorded block, via a public keyless RPC.

    Pins the block BEFORE calling eth_call (block number and hash first, then
    eth_call against that exact block, never "latest") -- an earlier version
    of this function called eth_call against "latest" and only recorded the
    block number afterward with a separate eth_blockNumber call, so the
    recorded block_number was not provably the block eth_call actually ran
    against (colleague review, 2026-08-28: a verifier re-running eth_call
    pinned to the recorded block_number could get a different result than
    what this tool captured, if a new block landed between the two calls).
    decimals() is for all practical purposes immutable per ERC-20 contract,
    so this was not a live correctness bug, but it made reproducibility a
    claim this function could not actually back up.

    Fails closed: any RPC error, or a response with no numeric result,
    raises rather than defaulting decimals to 18 -- colleague review,
    2026-08-28: NIL's real decimals is 6; a silent default would corrupt any
    future raw-amount conversion by a factor of 10**12."""
    if chain not in CHAIN_RPC:
        raise ValueError(f"no RPC endpoint registered for chain {chain!r}")
    chain_id, rpc_endpoint = CHAIN_RPC[chain]

    block_number_call = await _rpc_post(
        client, rpc_endpoint, {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
    )
    block_hex = (
        block_number_call.payload.get("result")
        if isinstance(block_number_call.payload, dict)
        else None
    )
    if not isinstance(block_hex, str) or not block_hex.startswith("0x"):
        raise ValueError(
            f"eth_blockNumber on {chain} returned no usable result: {block_number_call.payload!r}"
        )
    block_number = int(block_hex, 16)

    block_detail_call = await _rpc_post(
        client,
        rpc_endpoint,
        {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [block_hex, False],
            "id": 2,
        },
    )
    block_detail = (
        block_detail_call.payload.get("result")
        if isinstance(block_detail_call.payload, dict)
        else None
    )
    block_hash = block_detail.get("hash") if isinstance(block_detail, dict) else None
    if not isinstance(block_hash, str):
        raise ValueError(
            f"eth_getBlockByNumber({block_hex}) on {chain} returned no usable hash: "
            f"{block_detail_call.payload!r}"
        )

    decimals_call = await _rpc_post(
        client,
        rpc_endpoint,
        {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": contract_address, "data": _DECIMALS_SELECTOR}, block_hex],
            "id": 3,
        },
    )
    result = (
        decimals_call.payload.get("result") if isinstance(decimals_call.payload, dict) else None
    )
    if not isinstance(result, str) or result in ("0x", ""):
        raise ValueError(
            f"decimals() call for {contract_address} on {chain} at block {block_hex} "
            f"returned no usable result: {decimals_call.payload!r}"
        )
    decimals = int(result, 16)

    return ChainContractEvidence(
        chain=chain,
        chain_id=chain_id,
        contract_address=contract_address.lower(),
        decimals=decimals,
        decimals_evidence=decimals_call,
        block_number=block_number,
        block_hash=block_hash,
    )


# Gate network code -> canonical chain id, and CoinGecko `platforms` key ->
# canonical chain id -- both reused verbatim from
# gate_identity_candidate_tooling.py rather than redefined, so a chain this
# tool can validate against is exactly a chain that module's own fail-closed
# classifier already recognizes.
def _chain_maps() -> tuple[dict[str, str], dict[str, str]]:
    from .gate_identity_candidate_tooling import COINGECKO_PLATFORM_TO_CHAIN, NETWORK_TO_CHAIN

    return NETWORK_TO_CHAIN, COINGECKO_PLATFORM_TO_CHAIN


def _gate_reports_contract(gate_evidence: RawFetch, chain: str, contract_address: str) -> bool:
    """True only if gate_evidence's own payload -- not the caller-supplied
    source_contract_address parameter -- actually names this exact
    (chain, address) pair under one of its reported networks. Guards against
    a candidate table entry whose address was never actually confirmed by
    Gate itself (colleague review, 2026-08-28: EDEN's table entry named an
    address/chain pair that did not match what capture_bundle went on to
    treat as the source of truth)."""
    network_to_chain, _ = _chain_maps()
    networks = (
        gate_evidence.payload.get("networks") if isinstance(gate_evidence.payload, dict) else None
    )
    if not isinstance(networks, dict):
        return False
    for network_code, network_data in networks.items():
        if network_to_chain.get(str(network_code).upper()) != chain:
            continue
        info = network_data.get("info") if isinstance(network_data, dict) else None
        addr = (
            (info or {}).get("addr")
            or (info or {}).get("contractAddress")
            or (info or {}).get("contract")
        )
        if isinstance(addr, str) and addr.lower() == contract_address.lower():
            return True
    return False


def _coingecko_reports_contract(
    coingecko_evidence: RawFetch, chain: str, contract_address: str
) -> bool:
    _, platform_to_chain = _chain_maps()
    platforms = (
        coingecko_evidence.payload.get("platforms")
        if isinstance(coingecko_evidence.payload, dict)
        else None
    )
    if not isinstance(platforms, dict):
        return False
    for platform_key, address in platforms.items():
        if platform_to_chain.get(platform_key) != chain:
            continue
        if isinstance(address, str) and address.lower() == contract_address.lower():
            return True
    return False


def _validate_identity_class(
    *,
    identity_class: IdentityClass,
    base: str,
    gate_evidence: RawFetch,
    coingecko_evidence: RawFetch,
    target_catalog_evidence: RawFetch | None,
    source_contract: ChainContractEvidence,
    target_contract: ChainContractEvidence | None,
) -> None:
    """Reject a bundle whose evidence does not actually support the
    identity_class it claims -- computed from the same evidence just
    fetched, never trusted from the caller's classification alone. Colleague
    review, 2026-08-28: EDEN was recorded as exact_contract with a source on
    ethereum and a target on bsc (different chains, different addresses) --
    the classification and the underlying evidence had silently diverged,
    and nothing before this caught it. Raises on any mismatch; a bundle that
    fails this is not saved."""
    if not _gate_reports_contract(
        gate_evidence, source_contract.chain, source_contract.contract_address
    ):
        raise ValueError(
            f"{base}: gate's own currency evidence does not report "
            f"{source_contract.contract_address} on {source_contract.chain}"
        )

    if identity_class == "third_party_bridge_only":
        if target_contract is not None or target_catalog_evidence is not None:
            raise ValueError(
                f"{base}: third_party_bridge_only must not carry target chain evidence"
            )
        return

    if target_contract is None or target_catalog_evidence is None:
        raise ValueError(f"{base}: {identity_class} requires target chain evidence")

    catalog_address = target_catalog_evidence.payload.get("contractAddress")
    if (
        not isinstance(catalog_address, str)
        or catalog_address.lower() != target_contract.contract_address
    ):
        raise ValueError(
            f"{base}: target_catalog_evidence contract {catalog_address!r} does not match "
            f"target_contract {target_contract.contract_address!r}"
        )
    catalog_decimals = target_catalog_evidence.payload.get("decimals")
    if catalog_decimals is not None and catalog_decimals != target_contract.decimals:
        raise ValueError(
            f"{base}: catalog claims decimals={catalog_decimals!r}, "
            f"on-chain decimals()={target_contract.decimals!r}"
        )

    same_chain = source_contract.chain == target_contract.chain
    same_address = source_contract.contract_address == target_contract.contract_address

    if identity_class == "exact_contract":
        if not (same_chain and same_address):
            raise ValueError(
                f"{base}: exact_contract requires identical chain and address; got "
                f"source={source_contract.chain}:{source_contract.contract_address}, "
                f"target={target_contract.chain}:{target_contract.contract_address}"
            )
    elif identity_class == "same_asset_multichain_candidate":
        if same_chain and same_address:
            raise ValueError(
                f"{base}: same_asset_multichain_candidate but source and target are "
                "identical -- this is exact_contract, reclassify"
            )
        if not _coingecko_reports_contract(
            coingecko_evidence, source_contract.chain, source_contract.contract_address
        ):
            raise ValueError(f"{base}: coingecko does not corroborate the source-side contract")
        if not _coingecko_reports_contract(
            coingecko_evidence, target_contract.chain, target_contract.contract_address
        ):
            raise ValueError(f"{base}: coingecko does not corroborate the target-side contract")
    else:  # pragma: no cover - IdentityClass is a closed Literal
        raise ValueError(f"{base}: unknown identity_class {identity_class!r}")


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
    candidate -- see fetch_binance_alpha_catalog's docstring. Validates the
    claimed identity_class against the evidence it just fetched
    (_validate_identity_class) before returning -- a bundle whose evidence
    does not actually support its classification raises rather than being
    saved."""
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

    _validate_identity_class(
        identity_class=identity_class,
        base=base,
        gate_evidence=gate_evidence,
        coingecko_evidence=coingecko_evidence,
        target_catalog_evidence=target_catalog_evidence,
        source_contract=source_contract,
        target_contract=target_contract,
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
        block_hash=payload["block_hash"],
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


MANIFEST_FILENAME = "manifest.json"


def _bundle_filename(base: str, source_exchange: str, target_exchange: str) -> str:
    return f"{base.lower()}-{source_exchange.lower()}-{target_exchange.lower()}.json"


def evidence_bundle_path(base: str, source_exchange: str, target_exchange: str) -> Path:
    return EVIDENCE_DIR / _bundle_filename(base, source_exchange, target_exchange)


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


def load_all_evidence_bundles(
    directory: Path | None = None, *, allow_empty: bool = False
) -> tuple[EvidenceBundle, ...]:
    """Load and integrity-check every evidence bundle in directory (default
    EVIDENCE_DIR), sorted by filename for a deterministic order (excludes
    MANIFEST_FILENAME, which is not a bundle). Raises on the first bundle
    that fails its integrity check -- fail closed, never silently skip a
    corrupted file.

    Also fails closed (raises EvidenceIntegrityError) when the directory is
    missing or contains zero bundle files, unless allow_empty=True --
    colleague review, 2026-08-28: silently returning () for a missing
    directory reads identically to "genuinely nothing captured yet" to a
    future registry-activation consumer, hiding an accidentally deleted or
    renamed evidence directory. Tests that intentionally exercise an empty
    directory must pass allow_empty=True explicitly."""
    target_dir = directory or EVIDENCE_DIR
    if not target_dir.is_dir():
        if allow_empty:
            return ()
        raise EvidenceIntegrityError(f"evidence directory not found: {target_dir}")
    paths = sorted(path for path in target_dir.glob("*.json") if path.name != MANIFEST_FILENAME)
    if not paths and not allow_empty:
        raise EvidenceIntegrityError(f"no evidence bundles found in {target_dir}")
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
        # Gate reports EDEN on BOTH ethereum and bsc; the bsc contract is the
        # one that matches Binance Alpha exactly (confirmed 2026-08-28), so
        # that is the correct source_contract for exact_contract -- an
        # earlier version of this table used Gate's ethereum contract here
        # instead, which a colleague review caught: the classification said
        # exact_contract while the recorded source/target chains and
        # addresses actually differed (see _validate_identity_class, added
        # in response, which would now reject that combination outright).
        "EDEN",
        "binance",
        "bsc",
        "0x235b6fe22b4642ada16d311855c49ce7de260841",
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


async def _run(_args: argparse.Namespace) -> None:
    """Capture every candidate into a staging directory first, and only
    publish to EVIDENCE_DIR (a single atomic directory swap) if every single
    candidate succeeds. A colleague review (2026-08-28) found the previous
    version wrote each bundle straight into EVIDENCE_DIR as it completed --
    a run that captured 22 fresh candidates and failed on the 23rd would
    leave that last file at its stale value from a previous run, silently
    mixing two runs' worth of evidence in one directory with no way to tell
    which files were actually refreshed together. code_revision and
    working_tree_dirty are never taken from args -- see _current_git_state."""
    import httpx

    from .exchange_registry import EXCHANGE_FACTORIES

    code_revision, working_tree_dirty = _current_git_state()
    gate = EXCHANGE_FACTORIES["gate"]()
    bundles: list[EvidenceBundle] = []
    failed: list[tuple[str, str]] = []
    staging_dir = Path(tempfile.mkdtemp(prefix="source_lead_identity_evidence_"))
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
                        working_tree_dirty=working_tree_dirty,
                    )
                    save_evidence_bundle(
                        bundle,
                        staging_dir
                        / _bundle_filename(
                            bundle.base, bundle.source_exchange, bundle.target_exchange
                        ),
                    )
                    bundles.append(bundle)
                    sys.stderr.write(f"captured {base} -> {target_exchange} ({identity_class})\n")
                except Exception as exc:  # a single candidate's failure must not abort the run
                    failed.append((base, f"{type(exc).__name__}: {exc}"))
                    sys.stderr.write(f"FAILED {base} -> {target_exchange}: {exc}\n")
    finally:
        await gate.close()

    if failed:
        shutil.rmtree(staging_dir, ignore_errors=True)
        sys.stderr.write(
            f"\n{len(bundles)} captured, {len(failed)} failed -- "
            "nothing published (all-or-nothing run).\n"
        )
        for base, error in failed:
            sys.stderr.write(f"  {base}: {error}\n")
        sys.exit(1)

    manifest = {
        "run_id": str(uuid.uuid4()),
        "evidence_version": EVIDENCE_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "candidate_count": len(bundles),
        "candidates": sorted(bundle.base for bundle in bundles),
        "bundle_fingerprint": _sha256_canonical(sorted(bundle.bundle_sha256 for bundle in bundles)),
    }
    (staging_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    if EVIDENCE_DIR.exists():
        shutil.rmtree(EVIDENCE_DIR)
    EVIDENCE_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging_dir), str(EVIDENCE_DIR))
    sys.stderr.write(f"\npublished {len(bundles)} bundles atomically to {EVIDENCE_DIR}\n")


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
