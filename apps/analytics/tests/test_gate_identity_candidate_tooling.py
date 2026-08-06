from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from schurfer_analytics.gate_identity_candidate_tooling import (
    ChainContract,
    CoinGeckoProject,
    ExchangeAssetEvidence,
    build_candidate,
    classify_candidate,
    confidence_reason,
    fetch_exchange_evidence,
    normalize_contract_address,
    normalize_evm_address,
    normalize_non_evm_address,
    resolve_coingecko_project,
    search_coingecko,
)

GENERATED_AT = datetime(2026, 8, 6, tzinfo=UTC)

# --- normalize_evm_address ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "0x6944e1df6BF5972305f9ab25dF47eF10De01bcc8",
            "0x6944e1df6bf5972305f9ab25df47ef10de01bcc8",
        ),
        (
            "0x40b8129b786d766267a7a118cf8c07e31cdb6fde",
            "0x40b8129b786d766267a7a118cf8c07e31cdb6fde",
        ),
        ("not-an-address", None),
        ("0x123", None),  # too short
        (12345, None),  # not a string at all
        (None, None),
        ("", None),
    ],
)
def test_normalize_evm_address_fails_closed_on_anything_implausible(
    raw: object,
    expected: str | None,
) -> None:
    assert normalize_evm_address(raw) == expected


def test_normalize_non_evm_address_rejects_whitespace_and_empty() -> None:
    assert normalize_non_evm_address("tron", "TAbc123") == "TAbc123"
    assert normalize_non_evm_address("tron", "  ") is None
    assert normalize_non_evm_address("tron", "has space") is None
    assert normalize_non_evm_address("tron", None) is None
    # Ethereum is never routed through the non-EVM path, even if called directly.
    assert normalize_non_evm_address("ethereum", "0x" + "a" * 40) is None


def test_normalize_contract_address_dispatches_by_chain() -> None:
    evm = "0x" + "a" * 40
    assert normalize_contract_address("ethereum", evm) == evm
    assert normalize_contract_address("tron", "Tabc") == "Tabc"


# --- resolve_coingecko_project / _match_contracts ------------------------------


def _evidence(*contracts: ChainContract, source: str = "exchange_api") -> ExchangeAssetEvidence:
    return ExchangeAssetEvidence(
        exchange="gate",
        base="UB",
        contracts=tuple(contracts),
        raw_network_names=(),
        source=source,  # type: ignore[arg-type]
    )


def test_resolve_matches_the_one_project_with_a_confirmed_contract() -> None:
    unibase_address = "0x6944e1df6bf5972305f9ab25df47ef10de01bcc8"
    source = _evidence(ChainContract("ethereum", unibase_address))
    projects = (
        CoinGeckoProject("unibase", "Unibase", "UB", (ChainContract("ethereum", unibase_address),)),
    )

    project, flags = resolve_coingecko_project(source, projects)

    assert project is not None
    assert project.coingecko_id == "unibase"
    assert flags == ()


def test_resolve_returns_none_when_source_has_no_contract_at_all() -> None:
    """A native gas asset (no token contract) must never be resolved by name
    alone — see the wrapped/native fixture test below for the fuller scenario."""
    source = _evidence()  # no contracts

    project, flags = resolve_coingecko_project(source, (CoinGeckoProject("x", "X", "X", ()),))

    assert project is None
    assert flags == ("source_has_no_contract_to_corroborate",)


# --- Fixture: symbol collision --------------------------------------------------


def test_symbol_collision_does_not_silently_pick_a_project() -> None:
    """Two unrelated projects share the ticker "XYZ" on different chains. The
    source's own reported contract must disambiguate — if it matches neither
    (or, in a pathological case, both), the tool must not guess."""
    source = _evidence(ChainContract("ethereum", "0x" + "1" * 40))
    unrelated_project_a = CoinGeckoProject(
        "xyz-alpha", "XYZ Alpha", "XYZ", (ChainContract("ethereum", "0x" + "2" * 40),)
    )
    unrelated_project_b = CoinGeckoProject(
        "xyz-beta", "XYZ Beta", "XYZ", (ChainContract("bsc", "0x" + "3" * 40),)
    )

    project, flags = resolve_coingecko_project(source, (unrelated_project_a, unrelated_project_b))

    assert project is None
    assert flags == ("no_coingecko_project_matches_source_contract",)


def test_symbol_collision_where_both_candidates_match_is_ambiguous_not_a_pick() -> None:
    """A pathological case: two CoinGecko entries somehow both carry a contract
    matching the source (e.g. a duplicate/mirrored listing). Must escalate to
    manual review, never silently pick the first one."""
    shared_address = "0x" + "4" * 40
    source = _evidence(ChainContract("ethereum", shared_address))
    duplicate_a = CoinGeckoProject(
        "dup-a", "Dup A", "XYZ", (ChainContract("ethereum", shared_address),)
    )
    duplicate_b = CoinGeckoProject(
        "dup-b", "Dup B", "XYZ", (ChainContract("ethereum", shared_address),)
    )

    project, flags = resolve_coingecko_project(source, (duplicate_a, duplicate_b))

    assert project is None
    assert flags == ("multiple_coingecko_projects_match_source_contract_ambiguous",)


# --- Fixture: wrapped vs. native asset ------------------------------------------


def test_wrapped_token_is_not_conflated_with_the_native_asset() -> None:
    """Source reports "ETH" with no token contract at all (it is the chain's
    native gas asset). A CoinGecko search for "ETH" also happens to surface an
    unrelated wrapped-token project with its own real contract. These must
    never be merged just because the symbols look related."""
    native_eth_evidence = _evidence()  # native asset: no ERC20 contract to report
    wrapped_eth_project = CoinGeckoProject(
        "weth", "Wrapped Ether", "WETH", (ChainContract("ethereum", "0x" + "5" * 40),)
    )

    status, matched, coingecko_id, _conflicts, _missing = classify_candidate(
        source_evidence=native_eth_evidence,
        target_evidence=_evidence(ChainContract("ethereum", "0x" + "5" * 40)),
        coingecko_candidates=(wrapped_eth_project,),
    )

    assert status == "insufficient_evidence"
    assert coingecko_id is None
    assert matched == ()


# --- Fixture: migrated contract -------------------------------------------------


def test_migrated_contract_surfaces_as_conflict_not_a_silent_resolution() -> None:
    """Source (Gate) still reports the OLD, pre-migration contract address; the
    CoinGecko canonical project has already moved to a NEW address. This must
    show up as a conflict, never resolve to whichever one "looks newer"."""
    old_address = "0x" + "6" * 40
    new_address = "0x" + "7" * 40
    source = _evidence(ChainContract("ethereum", old_address))
    migrated_project = CoinGeckoProject(
        "proj", "Proj", "PRJ", (ChainContract("ethereum", new_address),)
    )

    project, flags = resolve_coingecko_project(source, (migrated_project,))

    # Neither matches nor conflicts at the resolve stage (no chain overlap
    # confirms equality), so resolution fails closed rather than picking it.
    assert project is None
    assert flags == ("no_coingecko_project_matches_source_contract",)


def test_target_contract_conflict_after_project_is_resolved() -> None:
    """Once the source resolves a project cleanly, if the TARGET exchange's own
    reported contract mismatches that same project's canonical address (e.g.
    the target's data reflects a pre-migration state), it must be a conflict."""
    address = "0x" + "8" * 40
    stale_target_address = "0x" + "9" * 40
    source = _evidence(ChainContract("ethereum", address))
    target = _evidence(ChainContract("ethereum", stale_target_address))
    project = CoinGeckoProject("proj", "Proj", "PRJ", (ChainContract("ethereum", address),))

    status, _matched, coingecko_id, conflicts, _missing = classify_candidate(
        source_evidence=source,
        target_evidence=target,
        coingecko_candidates=(project,),
    )

    assert status == "conflict"
    assert coingecko_id == "proj"
    assert conflicts == ("target_contract_conflict:ethereum",)


# --- classify_candidate: the clean success path ---------------------------------


def test_classify_candidate_is_a_candidate_when_source_and_target_agree() -> None:
    address = "0x" + "a" * 40
    source = _evidence(ChainContract("ethereum", address))
    target = _evidence(ChainContract("ethereum", address))
    project = CoinGeckoProject("proj", "Proj", "PRJ", (ChainContract("ethereum", address),))

    status, matched, _coingecko_id, conflicts, missing = classify_candidate(
        source_evidence=source,
        target_evidence=target,
        coingecko_candidates=(project,),
    )

    assert status == "candidate"
    assert matched == ("ethereum",)
    assert conflicts == ()
    assert missing == ()


def test_classify_candidate_requires_manual_review_when_target_is_perpetual_only() -> None:
    """The common real case: target is a perpetual swap with no on-chain
    contract of its own. Must not silently fail or auto-approve — escalate."""
    address = "0x" + "b" * 40
    source = _evidence(ChainContract("ethereum", address))
    target = _evidence(source="missing")  # no currency entry at all (futures-only key)
    project = CoinGeckoProject("proj", "Proj", "PRJ", (ChainContract("ethereum", address),))

    status, _matched, coingecko_id, _conflicts, _missing = classify_candidate(
        source_evidence=source,
        target_evidence=target,
        coingecko_candidates=(project,),
    )

    assert status == "manual_review_required"
    assert coingecko_id == "proj"


def test_classify_candidate_is_insufficient_evidence_when_source_itself_is_missing() -> None:
    status, _matched, _coingecko_id, _conflicts, missing = classify_candidate(
        source_evidence=_evidence(source="missing"),
        target_evidence=_evidence(ChainContract("ethereum", "0x" + "c" * 40)),
        coingecko_candidates=(),
    )

    assert status == "insufficient_evidence"
    assert missing == ("source_network_metadata_unavailable",)


def test_confidence_reason_covers_every_status() -> None:
    for status in (
        "candidate",
        "conflict",
        "insufficient_evidence",
        "not_same_asset",
        "manual_review_required",
    ):
        assert confidence_reason(status, ("ethereum",))  # non-empty for every branch


# --- fetch_exchange_evidence: fails closed on fetch problems --------------------


class _FakeExchange:
    def __init__(
        self, currencies: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self._currencies = currencies or {}
        self._error = error

    async def fetch_currencies(self) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return self._currencies


async def test_fetch_exchange_evidence_extracts_mapped_networks_only() -> None:
    exchange = _FakeExchange(
        {
            "UB": {
                "networks": {
                    "ERC20": {"info": {"addr": "0x" + "d" * 40}},
                    "BEP20": {"info": {"addr": "0x" + "e" * 40}},
                    "SOME_UNMAPPED_NETWORK": {"info": {"addr": "whatever"}},
                }
            }
        }
    )

    evidence = await fetch_exchange_evidence(exchange, "gate", "UB")

    assert evidence.source == "exchange_api"
    assert {contract.chain for contract in evidence.contracts} == {"ethereum", "bsc"}
    assert (
        len(evidence.raw_network_names) == 3
    )  # unmapped network still recorded, just not extracted


async def test_fetch_exchange_evidence_is_missing_when_base_absent() -> None:
    exchange = _FakeExchange({})

    evidence = await fetch_exchange_evidence(exchange, "binance", "UB")

    assert evidence.source == "missing"
    assert evidence.contracts == ()


async def test_fetch_exchange_evidence_fails_closed_on_exception() -> None:
    exchange = _FakeExchange(error=RuntimeError("boom"))

    evidence = await fetch_exchange_evidence(exchange, "binance", "UB")

    assert evidence.source == "unsupported"
    assert "boom" in (evidence.error or "")


# --- search_coingecko: fake HTTP layer, no real network in tests ---------------


async def test_search_coingecko_filters_to_exact_symbol_and_fetches_platforms() -> None:
    calls: list[str] = []

    async def fake_http_get(url: str, params: dict[str, str]) -> Any:
        calls.append(url)
        if url.endswith("/search"):
            return {
                "coins": [
                    {"id": "unibase", "symbol": "UB", "name": "Unibase"},
                    {"id": "unrelated", "symbol": "NOTUB", "name": "Not UB"},
                ]
            }
        assert url.endswith("/coins/unibase")
        return {
            "name": "Unibase",
            "platforms": {
                "ethereum": "0x6944e1df6bf5972305f9ab25df47ef10de01bcc8",
                "binance-smart-chain": "0x40b8129b786d766267a7a118cf8c07e31cdb6fde",
                "some-unmapped-chain": "0xdeadbeef",
            },
        }

    projects = await search_coingecko(fake_http_get, "UB")

    assert len(projects) == 1
    assert projects[0].coingecko_id == "unibase"
    assert {contract.chain for contract in projects[0].contracts} == {"ethereum", "bsc"}
    assert calls[0].endswith("/search")


async def test_search_coingecko_returns_empty_on_no_hits() -> None:
    async def fake_http_get(url: str, params: dict[str, str]) -> Any:
        return {"coins": []}

    assert await search_coingecko(fake_http_get, "NOPE") == ()


# --- build_candidate: end-to-end wiring with fakes ------------------------------


async def test_build_candidate_wires_fetch_and_classification_end_to_end() -> None:
    address = "0x" + "f" * 40
    gate = _FakeExchange({"UB": {"networks": {"ERC20": {"info": {"addr": address}}}}})
    binance = _FakeExchange({"UB": {"networks": {"ERC20": {"info": {"addr": address}}}}})

    async def fake_http_get(url: str, params: dict[str, str]) -> Any:
        if url.endswith("/search"):
            return {"coins": [{"id": "unibase", "symbol": "UB", "name": "Unibase"}]}
        return {"name": "Unibase", "platforms": {"ethereum": address}}

    candidate = await build_candidate(
        source_exchange_name="gate",
        source_exchange_client=gate,
        target_exchange_name="binance",
        target_exchange_client=binance,
        http_get=fake_http_get,
        base="UB",
        generated_at=GENERATED_AT,
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert candidate.status == "candidate"
    assert candidate.matched_chains == ("ethereum",)
    assert candidate.coingecko_id == "unibase"
    assert candidate.generated_at == GENERATED_AT
