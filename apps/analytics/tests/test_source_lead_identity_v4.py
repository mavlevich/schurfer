from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics import source_lead_identity_v4 as v4
from schurfer_analytics.source_lead_identity_evidence import (
    ChainContractEvidence,
    DerivativeMarketEvidence,
    EvidenceBundle,
    RawFetch,
    compute_bundle_sha256,
    save_evidence_bundle,
)
from schurfer_analytics.source_lead_identity_v4 import (
    RouteDecision,
    RouteInputs,
    alpha_contracts,
    build_candidate_snapshot,
    build_registry_v4,
    bybit_contracts,
    coingecko_index,
    decide_route,
    decisions_document,
    exact_perp_count,
    gate_contracts,
    load_candidate_snapshot,
    revalidate_v4_bundle,
    v3_registry_bases,
)
from schurfer_analytics.source_lead_qualification import (
    parse_identity_registry,
    verify_registry_against_evidence,
)

if TYPE_CHECKING:
    from pathlib import Path

ADDR = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
T0 = datetime(2026, 9, 26, tzinfo=UTC)


def _inputs(**overrides: object) -> RouteInputs:
    values: dict[str, object] = {
        "base": "ABC",
        "target_exchange": "bybit",
        "gate_perp_ok": True,
        "target_perp_count": 1,
        "gate_contracts": {"bsc": (ADDR,)},
        "target_contracts": {"bsc": (ADDR,)},
        "target_catalog_problem": None,
        "coingecko_projects": {("bsc", ADDR): (("abc-token", "abc"),)},
    }
    values.update(overrides)
    return RouteInputs(**values)  # type: ignore[arg-type]


def test_a_route_meeting_every_check_is_approved_with_its_contract() -> None:
    decision = decide_route(_inputs())
    assert decision.approved
    assert (decision.chain, decision.contract_address, decision.coingecko_id) == (
        "bsc",
        ADDR,
        "abc-token",
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"gate_perp_ok": False}, "no_gate_perp"),
        ({"target_perp_count": 0}, "no_target_perp"),
        ({"target_perp_count": 2}, "ambiguous_target_perp"),
        ({"gate_contracts": {"bsc": (ADDR, OTHER)}}, "gate_ambiguous_contract"),
        ({"gate_contracts": {}}, "gate_no_supported_contract"),
        (
            {"target_contracts": None, "target_catalog_problem": "target_catalog_missing"},
            "target_catalog_missing",
        ),
        ({"target_contracts": {"bsc": (ADDR, OTHER)}}, "target_ambiguous_contract"),
        ({"target_contracts": {}}, "target_no_supported_contract"),
        ({"target_contracts": {"bsc": (OTHER,)}}, "contract_conflict"),
        ({"target_contracts": {"ethereum": (ADDR,)}}, "no_shared_chain"),
        ({"coingecko_projects": {}}, "coingecko_no_match"),
        (
            {"coingecko_projects": {("bsc", ADDR): (("a", "abc"), ("b", "abc"))}},
            "coingecko_ambiguous",
        ),
        ({"coingecko_projects": {("bsc", ADDR): (("x", "xyz"),)}}, "coingecko_symbol_mismatch"),
    ],
)
def test_each_failing_check_is_the_recorded_reason(
    overrides: dict[str, object], reason: str
) -> None:
    decision = decide_route(_inputs(**overrides))
    assert not decision.approved
    assert decision.reason == reason


def test_a_conflict_on_any_shared_chain_rejects_even_if_another_chain_agrees() -> None:
    decision = decide_route(
        _inputs(
            gate_contracts={"ethereum": (ADDR,), "bsc": (ADDR,)},
            target_contracts={"ethereum": (ADDR,), "bsc": (OTHER,)},
        )
    )
    assert decision.reason == "contract_conflict"


def test_the_shared_chain_is_chosen_in_rule_order() -> None:
    decision = decide_route(
        _inputs(
            gate_contracts={"ethereum": (OTHER,), "bsc": (ADDR,)},
            target_contracts={"ethereum": (OTHER,), "bsc": (ADDR,)},
            coingecko_projects={("ethereum", OTHER): (("abc-token", "ABC"),)},
        )
    )
    assert decision.approved and decision.chain == "ethereum"


def test_gate_contracts_keep_supported_evm_chains_only() -> None:
    currency = {
        "networks": {
            "BEP20": {"info": {"addr": ADDR.upper().replace("0X", "0x")}},
            "BASE": {"info": {"contractAddress": OTHER}},
            "SOL": {"info": {"addr": "So11111111111111111111111111111111111111112"}},
            "ERC20": {"info": {"addr": "not-an-address"}},
        }
    }
    assert gate_contracts(currency) == {"bsc": (ADDR,), "base": (OTHER,)}
    assert gate_contracts(None) == {}


def test_alpha_catalog_needs_exactly_one_entry_on_a_supported_chain() -> None:
    assert alpha_contracts([]) == (None, "target_catalog_missing")
    assert alpha_contracts([{}, {}]) == (None, "target_catalog_ambiguous")
    assert alpha_contracts([{"chainId": "56", "contractAddress": ADDR}]) == (
        {"bsc": (ADDR,)},
        None,
    )
    assert alpha_contracts([{"chainId": "CT_501", "contractAddress": "abc"}]) == ({}, None)


def test_bybit_empty_contract_address_is_not_a_confirmation() -> None:
    rows = [
        {
            "coin": "ABC",
            "chains": [
                {"chain": "BSC", "contractAddress": ""},
                {"chain": "ETH", "contractAddress": ADDR},
                {"chain": "UNKNOWNCHAIN", "contractAddress": OTHER},
            ],
        }
    ]
    assert bybit_contracts(rows) == ({"ethereum": (ADDR,)}, None)
    assert bybit_contracts([]) == (None, "target_catalog_missing")


def test_exact_perp_count_ignores_renamed_and_non_trading_contracts() -> None:
    bybit = {
        "list": [
            {
                "baseCoin": "ABC",
                "quoteCoin": "USDT",
                "settleCoin": "USDT",
                "contractType": "LinearPerpetual",
                "status": "Trading",
            },
            {
                "baseCoin": "1000ABC",
                "quoteCoin": "USDT",
                "settleCoin": "USDT",
                "contractType": "LinearPerpetual",
                "status": "Trading",
            },
            {
                "baseCoin": "ABC",
                "quoteCoin": "USDT",
                "settleCoin": "USDT",
                "contractType": "LinearFutures",
                "status": "Trading",
            },
        ]
    }
    assert exact_perp_count("bybit", bybit, "ABC") == 1
    binance = {
        "symbols": [
            {
                "baseAsset": "ABC",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "contractType": "PERPETUAL",
                "status": "SETTLING",
            }
        ]
    }
    assert exact_perp_count("binance", binance, "ABC") == 0


def test_coingecko_index_maps_platforms_to_rule_chains() -> None:
    coins = [
        {"id": "abc-token", "symbol": "abc", "platforms": {"binance-smart-chain": ADDR}},
        {"id": "abc-bridged", "symbol": "abc", "platforms": {"solana": "abc"}},
    ]
    assert coingecko_index(coins) == {("bsc", ADDR): (("abc-token", "abc"),)}


def test_candidate_snapshot_is_hashed_and_tamper_evident(tmp_path: Path) -> None:
    snapshot = build_candidate_snapshot(["ZED", "ABC", "ABC", " "], T0)
    assert snapshot["bases"] == ["ABC", "ZED"]
    carried = build_candidate_snapshot(["ABC"], T0, carried_over=["OLD", "ABC"])
    assert carried["bases"] == ["ABC", "OLD"]
    assert carried["carried_over_from_v3"] == ["ABC", "OLD"]
    assert (
        carried["candidates_sha256"] != build_candidate_snapshot(["ABC"], T0)["candidates_sha256"]
    )
    assert snapshot == build_candidate_snapshot(["ABC", "ZED"], T0)
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({**snapshot, "bases": ["ABC"]}))
    with pytest.raises(ValueError, match="candidates_sha256"):
        load_candidate_snapshot(path)
    with pytest.raises(ValueError, match="after the candidate window start"):
        build_candidate_snapshot(["ABC"], datetime(2026, 9, 1, tzinfo=UTC))


# --- bundles and registry ----------------------------------------------------------


def _fetch(payload: object, source: str = "test") -> RawFetch:
    return RawFetch(
        source=source,
        endpoint="https://fake/",
        observed_at=T0,
        raw_sha256="e" * 64,
        wire_exact=False,
        payload=payload,
    )


def _market(exchange: str) -> DerivativeMarketEvidence:
    if exchange == "gate":
        return DerivativeMarketEvidence(
            exchange="gate",
            native_market_id="ABC_USDT",
            reported_base_asset=None,
            reported_quote_asset=None,
            reported_settle_asset=None,
            inferred_base_asset="ABC",
            inferred_quote_asset="USDT",
            inferred_settle_asset="USDT",
            inference_basis="test",
            onboarded_at_ms=1_700_000_000_000,
            status="trading",
            raw_evidence=_fetch({"in_delisting": False}),
        )
    contract_type = "LinearPerpetual" if exchange == "bybit" else "PERPETUAL"
    return DerivativeMarketEvidence(
        exchange=exchange,
        native_market_id="ABCUSDT",
        reported_base_asset="ABC",
        reported_quote_asset="USDT",
        reported_settle_asset="USDT",
        inferred_base_asset="ABC",
        inferred_quote_asset="USDT",
        inferred_settle_asset="USDT",
        inference_basis="test",
        onboarded_at_ms=1_700_000_100_000,
        status="Trading" if exchange == "bybit" else "TRADING",
        raw_evidence=_fetch({"contractType": contract_type}),
    )


def _bundle(target: str, *, coingecko_symbol: str = "abc") -> EvidenceBundle:
    contract = ChainContractEvidence(
        chain="bsc",
        chain_id=56,
        contract_address=ADDR,
        decimals=18,
        decimals_evidence=_fetch({"result": "0x12"}),
        block_number=1,
        block_hash="0x" + "0" * 64,
    )
    bundle = EvidenceBundle(
        evidence_version="source_lead_identity_evidence_v4",
        base="ABC",
        source_exchange="gate",
        target_exchange=target,
        identity_class="exact_contract",
        source_contract=contract,
        target_contract=contract,
        gate_evidence=_fetch({"networks": {"BEP20": {"info": {"addr": ADDR}}}}),
        target_catalog_evidence=_fetch(
            {"selected_chain": "BSC", "contractAddress": ADDR}
            if target == "bybit"
            else {"chainId": "56", "contractAddress": ADDR}
        ),
        coingecko_evidence=_fetch(
            {"symbol": coingecko_symbol, "platforms": {"binance-smart-chain": ADDR}}
        ),
        source_market_evidence=_market("gate"),
        target_market_evidence=_market(target),
        code_revision="abc",
        working_tree_dirty=False,
        captured_at=T0,
    )
    return replace(bundle, bundle_sha256=compute_bundle_sha256(bundle))


def _decisions(bundles: list[EvidenceBundle]) -> dict[str, object]:
    snapshot = build_candidate_snapshot(["ABC"], T0)
    decisions = [
        RouteDecision("ABC", b.target_exchange, True, "approved", "bsc", ADDR, "abc-token")
        for b in bundles
    ]
    return decisions_document(
        snapshot,
        decisions,
        bundles,
        run_id="run",
        code_revision="abc",
        working_tree_dirty=False,
        source_hashes={},
    )


def test_v4_bundle_revalidation_checks_the_coingecko_symbol() -> None:
    revalidate_v4_bundle(_bundle("bybit"))
    with pytest.raises(ValueError, match="coingecko symbol"):
        revalidate_v4_bundle(_bundle("bybit", coingecko_symbol="xyz"))


def test_registry_v4_with_two_routes_per_asset_verifies_against_its_evidence(
    tmp_path: Path,
) -> None:
    bundles = [_bundle("bybit"), _bundle("binance")]
    for bundle in bundles:
        save_evidence_bundle(bundle, tmp_path / f"abc-gate-{bundle.target_exchange}.json")
    decisions = _decisions(bundles)
    approval = {
        "decisions_sha256": decisions["decisions_sha256"],
        "confirmed_by": "owner",
        "technical_review_by": "reviewer",
        "confirmed_at": "2026-09-26T10:00:00Z",
    }
    registry = build_registry_v4(bundles, decisions, approval, evidence_commit="c0ffee")
    exchanges = [link["exchange"] for link in registry["links"]]
    assert exchanges == ["gate", "bybit", "binance"]
    # The Gate link cites the primary (Bybit) route's bundle.
    assert registry["links"][0]["evidence_sha256"] == bundles[0].bundle_sha256
    parsed = parse_identity_registry(registry, expected_version=registry["registry_version"])
    verify_registry_against_evidence(parsed.links_by_identity, evidence_dir=tmp_path)


def test_registry_v4_refuses_an_approval_for_a_different_decisions_file() -> None:
    bundles = [_bundle("bybit")]
    decisions = _decisions(bundles)
    approval = {
        "decisions_sha256": "0" * 64,
        "confirmed_by": "owner",
        "technical_review_by": "reviewer",
        "confirmed_at": "2026-09-26T10:00:00Z",
    }
    with pytest.raises(ValueError, match="approval does not name"):
        build_registry_v4(bundles, decisions, approval, evidence_commit="c0ffee")
    with pytest.raises(ValueError, match="technical_review_by"):
        build_registry_v4(
            bundles,
            decisions,
            {
                **approval,
                "decisions_sha256": decisions["decisions_sha256"],
                "technical_review_by": "",
            },
            evidence_commit="c0ffee",
        )


def test_v3_registry_assets_are_carried_over() -> None:
    bases = v3_registry_bases()
    assert len(bases) == 14
    assert {"BAS", "EDEN", "HOME", "SKYAI"} <= set(bases)


# --- stored source snapshots and recomputation (colleague review of PR C) ---------

MIXED = "0x" + "Ab" * 20  # the address as the venue reported it, mixed case


def _snapshots() -> dict[str, v4.SourceSnapshot]:
    def page(payload: object) -> bytes:
        return json.dumps(payload).encode()

    perp = {
        "baseCoin": "ABC",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "contractType": "LinearPerpetual",
        "status": "Trading",
        "symbol": "ABCUSDT",
    }
    payloads: dict[str, list[object]] = {
        "gate_currencies": [{"ABC": {"networks": {"BEP20": {"info": {"addr": ADDR}}}}}],
        "gate_perps": [[{"name": "ABC_USDT", "in_delisting": False}]],
        "alpha_catalog": [
            {
                "code": "000000",
                "success": True,
                "data": [{"symbol": "ABC", "chainId": "56", "contractAddress": MIXED}],
            }
        ],
        "binance_exchange_info": [{"symbols": [{"symbol": "XYZUSDT"}]}],
        "bybit_instruments": [
            {"retCode": 0, "result": {"list": [perp], "nextPageCursor": "x"}},
            {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}},
        ],
        "bybit_coin_info": [
            {
                "retCode": 0,
                "result": {
                    "rows": [
                        {"coin": "ABC", "chains": [{"chain": "BSC", "contractAddress": MIXED}]}
                    ]
                },
            }
        ],
        "coingecko_coins": [
            [{"id": "abc-token", "symbol": "abc", "platforms": {"binance-smart-chain": ADDR}}]
        ],
    }
    return {
        name: v4.SourceSnapshot(name, T0, name != "gate_currencies", tuple(map(page, pages)))
        for name, pages in payloads.items()
    }


def test_source_snapshots_round_trip_and_detect_tampering(tmp_path: Path) -> None:
    snapshots = _snapshots()
    section = v4.save_source_snapshots(snapshots, tmp_path)
    assert v4.load_source_snapshots(tmp_path, section) == snapshots
    bad = tmp_path / "sources" / "gate_perps.0.json.gz"
    bad.write_bytes(gzip.compress(b"[]"))
    with pytest.raises(ValueError, match="does not match its sha256"):
        v4.load_source_snapshots(tmp_path, section)


def test_recompute_rederives_every_route_from_the_stored_sources() -> None:
    sources = v4.sources_from_snapshots(_snapshots())
    snapshot = build_candidate_snapshot(["ABC"], T0)
    decisions = [v4.decide_route(v4.route_inputs(sources, "ABC", t)) for t in v4.TARGET_EXCHANGES]
    assert [(d.target_exchange, d.reason) for d in decisions] == [
        ("bybit", "approved"),
        ("binance", "no_target_perp"),
    ]
    document = decisions_document(
        snapshot,
        decisions,
        [],
        run_id="r",
        code_revision="c",
        working_tree_dirty=False,
        source_hashes={},
    )
    problems = v4.recompute_decisions(snapshot, sources, document, [])
    # The approved Bybit route has no bundle here, which recompute reports.
    assert problems == ["ABC/bybit: bundle catalog entry differs from the snapshot"]
    document["routes"][1]["reason"] = "target_catalog_missing"
    assert "ABC/binance: recorded target_catalog_missing, recomputed no_target_perp" in (
        v4.recompute_decisions(snapshot, sources, document, [])
    )


def test_target_catalog_evidence_keeps_the_address_the_venue_reported() -> None:
    sources = v4.sources_from_snapshots(_snapshots())
    decision = v4.decide_route(v4.route_inputs(sources, "ABC", "bybit"))
    payload = v4._target_catalog_evidence(sources, decision).payload
    assert payload["contractAddress"] == MIXED
    assert payload["selected_chain"] == "BSC"


def test_rpc_failures_after_retries_abort_instead_of_rejecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def flaky(*_args: object) -> object:
        nonlocal calls
        calls += 1
        raise ValueError("eth_getBlockByNumber returned no usable hash")

    monkeypatch.setattr(v4, "fetch_onchain_decimals", flaky)
    monkeypatch.setattr(v4, "_RPC_RETRY_SECONDS", 0.0)
    with pytest.raises(v4.TransientFetchError):
        asyncio.run(v4._decimals_with_retry(None, "bsc", ADDR))
    assert calls == v4._RPC_ATTEMPTS
    assert not issubclass(v4.TransientFetchError, v4.RuleCheckFailedError)


@pytest.mark.parametrize(
    ("name", "page"),
    [
        # Review repro: an error page must abort, not read as "no perpetual".
        (
            "bybit_instruments",
            {"retCode": 10006, "retMsg": "Too many visits", "result": {"list": []}},
        ),
        # Review repro: an error page must abort, not read as "coin missing".
        ("alpha_catalog", {"code": "000002", "success": False, "data": []}),
        ("binance_exchange_info", {"code": -1003, "msg": "banned"}),
        ("bybit_coin_info", {"retCode": 10003, "result": {}}),
        ("gate_perps", []),
        ("coingecko_coins", {"status": {"error_code": 429}}),
    ],
)
def test_an_unsuccessful_source_response_aborts_before_classification(
    name: str, page: object
) -> None:
    snapshots = _snapshots()
    snapshots[name] = v4.SourceSnapshot(name, T0, True, (json.dumps(page).encode(),))
    with pytest.raises(v4.SourceSnapshotError):
        v4.sources_from_snapshots(snapshots)


def test_a_truncated_bybit_page_sequence_aborts() -> None:
    snapshots = _snapshots()
    first = snapshots["bybit_instruments"].pages[0]
    snapshots["bybit_instruments"] = v4.SourceSnapshot("bybit_instruments", T0, True, (first,))
    with pytest.raises(v4.SourceSnapshotError, match="cursor"):
        v4.sources_from_snapshots(snapshots)
