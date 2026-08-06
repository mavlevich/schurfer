"""Read-only Gate/Binance/Bybit identity candidate generator.

Registered 2026-08-06 (see ROADMAP.md item 6). This tool exists to speed up the
human review that gates advancing Gate source-lead: it fetches each exchange's
own official currency/network metadata plus CoinGecko as secondary
corroboration, and proposes a candidate identity link with its raw evidence.

**It never writes to the approved registry and never produces `approved=true`.**
Every output is one of the five candidate statuses below; turning a `candidate`
into an approved registry entry is a separate, human, out-of-band action (the
next PR, `gate-source-lead-registry-qualification-v2`, consumes a human's
decision, not this tool's output directly).

Primary identity is chain + normalized contract address, never a ticker alone
— two exchanges can list unrelated assets under the same symbol (a "symbol
collision"), and a native gas asset must never be silently merged with an
unrelated wrapped/bridged token that happens to share a name. A contract that
has visibly migrated (the source's recorded address no longer matches the
project's current canonical address) must surface as a conflict, not silently
resolve to whichever address looks newer.

Human review checklist (apply this manually before ever approving a
`candidate`-status link into the registry):
- same chain
- same contract address
- same decimals
- native vs. wrapped/bridged asset correctly distinguished, not conflated
- the exchange's own official data (not just this tool's derived summary) was
  actually opened and read
- no unresolved symbol collision
- the evidence predates the event it is being used to justify (not gathered
  only after the fact to rationalize a link)
- a second person independently confirmed the link
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import structlog

from .reporting import json_ready, markdown_table, normalize_code_revision, parse_utc_datetime

log = structlog.get_logger()

GATE_IDENTITY_CANDIDATE_VERSION = "gate_identity_candidate_tooling_v1"
COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"
# Bounded: a symbol search returning more than this many hits is itself a
# strong signal of ambiguity worth a human's attention, not something to keep
# paging through.
MAX_COINGECKO_SEARCH_HITS = 5

CandidateStatus = Literal[
    "candidate",
    "conflict",
    "insufficient_evidence",
    "not_same_asset",
    "manual_review_required",
]

# ccxt network code -> canonical chain id. Deliberately small and explicit —
# an unmapped network code fails closed (treated as unknown, never guessed).
NETWORK_TO_CHAIN: dict[str, str] = {
    "ERC20": "ethereum",
    "BEP20": "bsc",
    "TRC20": "tron",
    "POLYGON": "polygon",
    "ARBITRUM": "arbitrum",
    "OPTIMISM": "optimism",
    "AVAXC": "avalanche",
    "SOL": "solana",
}

# CoinGecko `platforms` key -> canonical chain id. Same fail-closed policy.
COINGECKO_PLATFORM_TO_CHAIN: dict[str, str] = {
    "ethereum": "ethereum",
    "binance-smart-chain": "bsc",
    "tron": "tron",
    "polygon-pos": "polygon",
    "arbitrum-one": "arbitrum",
    "optimistic-ethereum": "optimism",
    "avalanche": "avalanche",
    "solana": "solana",
}

_EVM_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def normalize_evm_address(address: Any) -> str | None:
    """Lowercase canonicalization for comparison only — this is not full EIP-55
    checksum validation, just a length/charset sanity gate. Anything that does
    not look like a plausible EVM address fails closed (returns None) rather
    than being guessed at."""
    if not isinstance(address, str):
        return None
    candidate = address.strip()
    if not _EVM_ADDRESS_RE.fullmatch(candidate):
        return None
    return candidate.lower()


def normalize_non_evm_address(chain: str, address: Any) -> str | None:
    """Non-EVM chains get only a conservative non-empty/whitespace check — we do
    not implement chain-specific checksum validation for Tron/Solana here.
    Still fails closed on anything empty, non-string, or containing whitespace."""
    if chain == "ethereum" or not isinstance(address, str):
        return None
    candidate = address.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    return candidate


def normalize_contract_address(chain: str, address: Any) -> str | None:
    if chain == "ethereum":
        return normalize_evm_address(address)
    return normalize_non_evm_address(chain, address)


@dataclass(frozen=True)
class ChainContract:
    chain: str
    address: str


@dataclass(frozen=True)
class ExchangeAssetEvidence:
    exchange: str
    base: str
    contracts: tuple[ChainContract, ...]
    raw_network_names: tuple[str, ...]
    source: Literal["exchange_api", "missing", "unsupported"]
    error: str | None = None


@dataclass(frozen=True)
class CoinGeckoProject:
    coingecko_id: str
    name: str
    symbol: str
    contracts: tuple[ChainContract, ...]


def _match_contracts(
    evidence_contracts: tuple[ChainContract, ...],
    project_contracts: tuple[ChainContract, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compare one exchange's reported contracts against one CoinGecko project's
    canonical contracts, chain by chain. Returns (matched_chains, conflict_chains).
    A chain absent from the project's side is neither a match nor a conflict —
    it is simply not corroborated, which the caller treats as insufficient
    evidence, not as proof of anything."""
    project_by_chain = {contract.chain: contract.address for contract in project_contracts}
    matched: list[str] = []
    conflicts: list[str] = []
    for contract in evidence_contracts:
        canonical = project_by_chain.get(contract.chain)
        if canonical is None:
            continue
        if canonical == contract.address:
            matched.append(contract.chain)
        else:
            conflicts.append(contract.chain)
    return tuple(matched), tuple(conflicts)


def resolve_coingecko_project(
    source_evidence: ExchangeAssetEvidence,
    candidates: tuple[CoinGeckoProject, ...],
) -> tuple[CoinGeckoProject | None, tuple[str, ...]]:
    """Pick the one CoinGecko project (if any) whose own canonical contracts
    match the source exchange's reported contracts on at least one chain, with
    no conflicting chain. This is what prevents a symbol collision (multiple
    unrelated projects sharing a ticker) or a wrapped/native mismatch (a native
    asset with no contract at all cannot match anything by contract, and is
    correctly left unresolved rather than guessed at by name)."""
    if not source_evidence.contracts:
        return None, ("source_has_no_contract_to_corroborate",)
    matches = []
    for project in candidates:
        matched, conflicts = _match_contracts(source_evidence.contracts, project.contracts)
        if matched and not conflicts:
            matches.append(project)
    if len(matches) == 1:
        return matches[0], ()
    if not matches:
        return None, ("no_coingecko_project_matches_source_contract",)
    return None, ("multiple_coingecko_projects_match_source_contract_ambiguous",)


@dataclass(frozen=True)
class IdentityCandidate:
    source_exchange: str
    source_base: str
    target_exchange: str
    target_base: str
    status: CandidateStatus
    matched_chains: tuple[str, ...]
    coingecko_id: str | None
    source_evidence: ExchangeAssetEvidence
    target_evidence: ExchangeAssetEvidence
    conflict_flags: tuple[str, ...]
    missing_fields: tuple[str, ...]
    confidence_reason: str
    generated_at: datetime
    code_revision: str
    working_tree_dirty: bool
    contract_version: str = GATE_IDENTITY_CANDIDATE_VERSION


def classify_candidate(
    *,
    source_evidence: ExchangeAssetEvidence,
    target_evidence: ExchangeAssetEvidence,
    coingecko_candidates: tuple[CoinGeckoProject, ...],
) -> tuple[CandidateStatus, tuple[str, ...], str | None, tuple[str, ...], tuple[str, ...]]:
    """Pure classification (no I/O). Returns (status, matched_chains,
    coingecko_id, conflict_flags, missing_fields)."""
    if source_evidence.source != "exchange_api" or not source_evidence.contracts:
        return (
            "insufficient_evidence",
            (),
            None,
            (),
            ("source_network_metadata_unavailable",),
        )
    project, resolve_flags = resolve_coingecko_project(source_evidence, coingecko_candidates)
    if project is None:
        if "multiple_coingecko_projects_match_source_contract_ambiguous" in resolve_flags:
            return "manual_review_required", (), None, resolve_flags, ()
        return "insufficient_evidence", (), None, resolve_flags, ()

    if target_evidence.source != "exchange_api" or not target_evidence.contracts:
        # Common for a perpetual-only listing: the target has no on-chain
        # contract at all because it is a derivative, not a token transfer.
        # This is a real, structural limitation, not a bug — see the module
        # docstring's escalation to manual review rather than guessing.
        missing = (
            ("target_network_metadata_unavailable",)
            if target_evidence.source != "exchange_api"
            else ("target_has_no_reported_contract",)
        )
        return "manual_review_required", (), project.coingecko_id, (), missing

    target_matched, target_conflicts = _match_contracts(
        target_evidence.contracts, project.contracts
    )
    if target_conflicts:
        return (
            "conflict",
            (),
            project.coingecko_id,
            tuple(f"target_contract_conflict:{chain}" for chain in target_conflicts),
            (),
        )
    if not target_matched:
        return (
            "insufficient_evidence",
            (),
            project.coingecko_id,
            ("no_coingecko_corroboration_for_target",),
            (),
        )
    source_matched, _ = _match_contracts(source_evidence.contracts, project.contracts)
    shared = tuple(sorted(set(source_matched) & set(target_matched)))
    if shared:
        return "candidate", shared, project.coingecko_id, (), ()
    return (
        "manual_review_required",
        (),
        project.coingecko_id,
        ("source_and_target_match_different_chains",),
        (),
    )


def confidence_reason(status: CandidateStatus, matched_chains: tuple[str, ...]) -> str:
    if status == "candidate":
        return (
            "source and target contracts both match CoinGecko's canonical address "
            f"on: {', '.join(matched_chains)}"
        )
    if status == "conflict":
        return "an exchange's reported contract does not match CoinGecko's canonical address"
    if status == "not_same_asset":
        return "evidence positively identifies these as different underlying assets"
    if status == "manual_review_required":
        return (
            "automated evidence is inconclusive; needs a human to inspect official sources directly"
        )
    return "not enough official evidence exists to propose or reject this link"


# --- fetch layer (I/O) --------------------------------------------------------


class GateFetcher(Protocol):
    async def fetch_currencies(self) -> dict[str, Any]: ...


async def fetch_exchange_evidence(
    exchange: Any, exchange_name: str, base: str
) -> ExchangeAssetEvidence:
    """Fetch official network/contract metadata for one base asset. Fails
    closed to source="missing"/"unsupported" on any error or absent data
    rather than raising — a fetch failure is evidence of "we don't know", not
    a reason to crash the whole batch."""
    try:
        currencies = await exchange.fetch_currencies()
    except Exception as exc:
        return ExchangeAssetEvidence(
            exchange=exchange_name,
            base=base,
            contracts=(),
            raw_network_names=(),
            source="unsupported",
            error=str(exc)[:500],
        )
    info = currencies.get(base) if isinstance(currencies, dict) else None
    if not isinstance(info, dict):
        return ExchangeAssetEvidence(
            exchange=exchange_name,
            base=base,
            contracts=(),
            raw_network_names=(),
            source="missing",
            error="exchange reported no currency entry for this base",
        )
    networks = info.get("networks")
    if not isinstance(networks, dict):
        networks = {}
    contracts: list[ChainContract] = []
    raw_names: list[str] = []
    for network_code, network_data in networks.items():
        raw_names.append(str(network_code))
        chain = NETWORK_TO_CHAIN.get(str(network_code).upper())
        if chain is None:
            continue  # unmapped network code -- fail closed, never guessed
        raw_info = network_data.get("info", {}) if isinstance(network_data, dict) else {}
        address = None
        if isinstance(raw_info, dict):
            address = (
                raw_info.get("addr") or raw_info.get("contractAddress") or raw_info.get("contract")
            )
        normalized = normalize_contract_address(chain, address)
        if normalized is not None:
            contracts.append(ChainContract(chain=chain, address=normalized))
    return ExchangeAssetEvidence(
        exchange=exchange_name,
        base=base,
        contracts=tuple(contracts),
        raw_network_names=tuple(raw_names),
        source="exchange_api",
    )


class HttpGetter(Protocol):
    async def __call__(self, url: str, params: dict[str, str]) -> Any: ...


async def search_coingecko(http_get: HttpGetter, symbol: str) -> tuple[CoinGeckoProject, ...]:
    """Search CoinGecko by symbol, then fetch each hit's canonical contract
    addresses. Bounded to MAX_COINGECKO_SEARCH_HITS -- a wider hit list is
    itself worth a human's attention, not something to fetch further into.

    Fails closed on any HTTP error (rate limiting, timeout, outage): a
    CoinGecko fetch failure means "no corroboration available right now", the
    same honest signal as a missing project, never a crashed batch — a 429 on
    base #3 of a 20-base run must not discard the other 19 results. A failure
    on the /search call itself skips the whole symbol; a failure on one coin's
    /coins/{id} detail call skips only that one candidate, not the others."""
    try:
        search_payload = await http_get(f"{COINGECKO_API_BASE}/search", {"query": symbol})
    except Exception as exc:
        # Deliberately fail closed, not crash the batch — see the module-level
        # note above this function for why a 429 here must not lose every other
        # base's results.
        log.warning("gate_identity.coingecko_search_failed", symbol=symbol, error=str(exc)[:300])
        return ()
    coins = search_payload.get("coins") if isinstance(search_payload, dict) else None
    if not isinstance(coins, list):
        return ()
    matching = [
        coin
        for coin in coins
        if isinstance(coin, dict)
        and isinstance(coin.get("symbol"), str)
        and coin["symbol"].casefold() == symbol.casefold()
    ][:MAX_COINGECKO_SEARCH_HITS]
    projects: list[CoinGeckoProject] = []
    for coin in matching:
        coin_id = coin.get("id")
        if not isinstance(coin_id, str):
            continue
        try:
            detail = await http_get(
                f"{COINGECKO_API_BASE}/coins/{coin_id}",
                {"localization": "false", "tickers": "false", "market_data": "false"},
            )
        except Exception as exc:
            log.warning(
                "gate_identity.coingecko_coin_detail_failed",
                coin_id=coin_id,
                error=str(exc)[:300],
            )
            continue
        platforms = detail.get("platforms") if isinstance(detail, dict) else None
        contracts: list[ChainContract] = []
        if isinstance(platforms, dict):
            for platform_key, address in platforms.items():
                chain = COINGECKO_PLATFORM_TO_CHAIN.get(platform_key)
                if chain is None or not address:
                    continue
                normalized = normalize_contract_address(chain, address)
                if normalized is not None:
                    contracts.append(ChainContract(chain=chain, address=normalized))
        projects.append(
            CoinGeckoProject(
                coingecko_id=coin_id,
                name=str(detail.get("name", coin.get("name", coin_id))),
                symbol=str(coin.get("symbol", symbol)),
                contracts=tuple(contracts),
            )
        )
    return tuple(projects)


async def build_candidate(
    *,
    source_exchange_name: str,
    source_exchange_client: Any,
    target_exchange_name: str,
    target_exchange_client: Any,
    coingecko_candidates: tuple[CoinGeckoProject, ...],
    base: str,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> IdentityCandidate:
    """coingecko_candidates is fetched once per base by the caller (see _run)
    and reused across every target exchange evaluated for that base — CoinGecko
    has nothing to do with which target we're checking, and re-querying it per
    target only burns rate-limit budget for no new information."""
    source_evidence = await fetch_exchange_evidence(
        source_exchange_client, source_exchange_name, base
    )
    target_evidence = await fetch_exchange_evidence(
        target_exchange_client, target_exchange_name, base
    )
    status, matched_chains, coingecko_id, conflict_flags, missing_fields = classify_candidate(
        source_evidence=source_evidence,
        target_evidence=target_evidence,
        coingecko_candidates=coingecko_candidates,
    )
    return IdentityCandidate(
        source_exchange=source_exchange_name,
        source_base=base,
        target_exchange=target_exchange_name,
        target_base=base,
        status=status,
        matched_chains=matched_chains,
        coingecko_id=coingecko_id,
        source_evidence=source_evidence,
        target_evidence=target_evidence,
        conflict_flags=conflict_flags,
        missing_fields=missing_fields,
        confidence_reason=confidence_reason(status, matched_chains),
        generated_at=generated_at,
        code_revision=normalize_code_revision(code_revision),
        working_tree_dirty=working_tree_dirty,
    )


# --- rendering -----------------------------------------------------------------


def render_json(candidates: tuple[IdentityCandidate, ...]) -> str:
    return json.dumps(
        json_ready([asdict(candidate) for candidate in candidates]),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )


def render_markdown(candidates: tuple[IdentityCandidate, ...]) -> str:
    lines = [
        "# Gate Identity Candidates",
        "",
        (
            "> Candidates only. This tool never approves a registry entry — "
            "every row below needs the human review checklist in the module "
            "docstring applied before it can be used."
        ),
        "",
    ]
    lines.extend(
        markdown_table(
            ("Base", "Source", "Target", "Status", "Matched chains", "CoinGecko", "Reason"),
            [
                (
                    candidate.source_base,
                    candidate.source_exchange,
                    candidate.target_exchange,
                    candidate.status,
                    ", ".join(candidate.matched_chains) or "none",
                    candidate.coingecko_id or "n/a",
                    candidate.confidence_reason,
                )
                for candidate in candidates
            ],
        )
    )
    lines.extend(["", "## Conflict / missing-evidence flags", ""])
    lines.extend(
        markdown_table(
            ("Base", "Target", "Flags"),
            [
                (
                    candidate.source_base,
                    candidate.target_exchange,
                    ", ".join((*candidate.conflict_flags, *candidate.missing_fields)) or "none",
                )
                for candidate in candidates
            ],
        )
    )
    return "\n".join(lines) + "\n"


# --- CLI -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        action="append",
        required=True,
        help="candidate base ticker to evaluate; repeat for multiple",
    )
    parser.add_argument(
        "--target-exchange",
        action="append",
        choices=("binance", "bybit"),
        help="target exchange(s) to evaluate against; repeat for multiple (default: both)",
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--generated-at",
        type=parse_utc_datetime,
        default=None,
        help="override generated_at for reproducible test output; defaults to run start",
    )
    return parser


# CoinGecko's public tier rate-limits aggressively (observed 429s after a
# handful of requests in quick succession). Bounded retry with backoff, plus a
# small pause between bases in _run below, keeps a multi-base batch from
# failing outright on a transient limit.
_HTTP_MAX_ATTEMPTS = 4
_HTTP_BACKOFF_SECONDS = 2.0
_INTER_BASE_DELAY_SECONDS = 1.5


async def _http_get(url: str, params: dict[str, str]) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(_HTTP_MAX_ATTEMPTS):
            response = await client.get(url, params=params)
            if response.status_code == 429 and attempt < _HTTP_MAX_ATTEMPTS - 1:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else _HTTP_BACKOFF_SECONDS * (2**attempt)
                )
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        response.raise_for_status()
        return response.json()


async def _run(args: argparse.Namespace) -> str:
    from .exchange_registry import EXCHANGE_FACTORIES

    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    target_exchanges = tuple(args.target_exchange or ("binance", "bybit"))
    generated_at = args.generated_at or datetime.now(UTC)

    gate = EXCHANGE_FACTORIES["gate"]()
    targets = {name: EXCHANGE_FACTORIES[name]() for name in target_exchanges}
    try:
        candidates = []
        for index, base in enumerate(args.base):
            if index > 0:
                await asyncio.sleep(_INTER_BASE_DELAY_SECONDS)
            coingecko_candidates = await search_coingecko(_http_get, base)
            for target_name, target_client in targets.items():
                candidates.append(
                    await build_candidate(
                        source_exchange_name="gate",
                        source_exchange_client=gate,
                        target_exchange_name=target_name,
                        target_exchange_client=target_client,
                        coingecko_candidates=coingecko_candidates,
                        base=base,
                        generated_at=generated_at,
                        code_revision=args.code_revision,
                        working_tree_dirty=args.working_tree_dirty,
                    )
                )
    finally:
        await gate.close()
        for client in targets.values():
            await client.close()
    result = tuple(candidates)
    return render_json(result) if args.format == "json" else render_markdown(result)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
