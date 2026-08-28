from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics.source_lead_identity_evidence import (
    CANDIDATES,
    EVIDENCE_DIR_V3,
    EVIDENCE_VERSION,
    ChainContractEvidence,
    DerivativeMarketEvidence,
    EvidenceBundle,
    EvidenceIntegrityError,
    RawFetch,
    _alpha_identity_fields,
    _current_git_state,
    _sha256_canonical,
    _validate_identity_class,
    _validate_route_evidence,
    capture_bundle,
    compute_bundle_sha256,
    fetch_binance_alpha_catalog,
    fetch_binance_futures_exchange_info,
    fetch_gate_currency,
    fetch_gate_futures_contract,
    fetch_onchain_decimals,
    find_alpha_entry,
    find_binance_futures_market,
    load_all_evidence_bundles,
    load_evidence_bundle,
    revalidate_bundle_route_evidence,
    save_evidence_bundle,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- fakes, mirroring the _FakeExchange pattern in test_gate_identity_candidate_tooling.py ---


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


class _FakeResponse:
    def __init__(
        self, payload: Any, url: str = "https://fake.test/", status_code: int = 200
    ) -> None:
        self.content = json.dumps(payload).encode()
        self.url = url
        self._status_code = status_code

    def raise_for_status(self) -> None:
        if self._status_code >= 400:
            raise RuntimeError(f"HTTP {self._status_code}")


class _FakeHTTPClient:
    """Routes .get()/.post() to pre-queued responses keyed by URL prefix, in
    call order per key -- lets a test script a sequence of responses (e.g.
    eth_blockNumber, then eth_getBlockByNumber, then eth_call, all against
    the same RPC endpoint)."""

    def __init__(self) -> None:
        self._get_queue: dict[str, list[_FakeResponse]] = {}
        self._post_queue: dict[str, list[_FakeResponse]] = {}
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    def queue_get(self, url_prefix: str, response: _FakeResponse) -> None:
        self._get_queue.setdefault(url_prefix, []).append(response)

    def queue_post(self, url_prefix: str, response: _FakeResponse) -> None:
        self._post_queue.setdefault(url_prefix, []).append(response)

    async def get(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, Any] | None = None
    ) -> _FakeResponse:
        self.get_calls.append((url, params or {}))
        for prefix, queue in self._get_queue.items():
            if url.startswith(prefix) and queue:
                return queue.pop(0)
        raise AssertionError(f"no fake GET response queued for {url}")

    async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        self.post_calls.append((url, json))
        for prefix, queue in self._post_queue.items():
            if url.startswith(prefix) and queue:
                return queue.pop(0)
        raise AssertionError(f"no fake POST response queued for {url}")


def _rpc_result(hex_value: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": hex_value}


_BLOCK_HASH = "0x" + "ab" * 32


def _queue_decimals_rpc(
    client: _FakeHTTPClient,
    rpc: str,
    *,
    decimals_hex: str,
    block_hex: str = "0x64",
    block_hash: str = _BLOCK_HASH,
) -> None:
    """Queues the exact 3-call sequence fetch_onchain_decimals now makes:
    eth_blockNumber, eth_getBlockByNumber, eth_call -- in that order, block
    pinned before the decimals read (colleague review, 2026-08-28)."""
    client.queue_post(rpc, _FakeResponse(_rpc_result(block_hex)))
    client.queue_post(
        rpc, _FakeResponse({"jsonrpc": "2.0", "id": 2, "result": {"hash": block_hash}})
    )
    client.queue_post(rpc, _FakeResponse(_rpc_result(decimals_hex)))


# --- fetch_onchain_decimals: fail-closed, the NIL regression, block pinning ----


async def test_fetch_onchain_decimals_returns_nil_6_not_default_18() -> None:
    """The exact NIL regression (colleague review, 2026-08-28): a real
    non-18-decimals contract must flow through as its true value, never a
    silently assumed default."""
    client = _FakeHTTPClient()
    rpc = "https://ethereum-rpc.publicnode.com"
    _queue_decimals_rpc(client, rpc, decimals_hex="0x" + "0" * 63 + "6")

    evidence = await fetch_onchain_decimals(
        client, "ethereum", "0x7cf9a80db3b29ee8efe3710aadb7b95270572d47"
    )

    assert evidence.decimals == 6
    assert evidence.block_number == 100
    assert evidence.block_hash == _BLOCK_HASH
    assert evidence.chain_id == 1


async def test_fetch_onchain_decimals_ordinary_contract_is_18() -> None:
    client = _FakeHTTPClient()
    rpc = "https://bsc-dataseed.binance.org/"
    _queue_decimals_rpc(client, rpc, decimals_hex="0x" + "0" * 62 + "12")

    evidence = await fetch_onchain_decimals(
        client, "bsc", "0x92aa03137385f18539301349dcfc9ebc923ffb10"
    )

    assert evidence.decimals == 18
    assert evidence.chain_id == 56


async def test_fetch_onchain_decimals_pins_block_before_calling_eth_call() -> None:
    """eth_call must be sent with the specific block hex just read from
    eth_blockNumber, never "latest" -- a colleague review (2026-08-28) found
    the previous version called eth_call against "latest" and only recorded
    a block number from a separate, later call, so the two were not
    provably the same block."""
    client = _FakeHTTPClient()
    rpc = "https://bsc-dataseed.binance.org/"
    _queue_decimals_rpc(client, rpc, decimals_hex="0x" + "0" * 62 + "12", block_hex="0x2a")

    await fetch_onchain_decimals(client, "bsc", "0xdead")

    methods = [body["method"] for _url, body in client.post_calls]
    assert methods == ["eth_blockNumber", "eth_getBlockByNumber", "eth_call"]
    eth_call_body = client.post_calls[2][1]
    assert eth_call_body["params"][1] == "0x2a"


async def test_fetch_onchain_decimals_stored_per_chain_id_and_contract() -> None:
    """Two different (chain_id, contract) pairs for what a naive scheme might
    treat as "the same asset" must carry their own independent decimals --
    never inherited from one chain to another."""
    client = _FakeHTTPClient()
    _queue_decimals_rpc(
        client, "https://ethereum-rpc.publicnode.com", decimals_hex="0x" + "0" * 63 + "6"
    )
    _queue_decimals_rpc(
        client, "https://bsc-dataseed.binance.org/", decimals_hex="0x" + "0" * 62 + "12"
    )

    eth_evidence = await fetch_onchain_decimals(client, "ethereum", "0xaaaa")
    bsc_evidence = await fetch_onchain_decimals(client, "bsc", "0xbbbb")

    assert (eth_evidence.chain_id, eth_evidence.decimals) == (1, 6)
    assert (bsc_evidence.chain_id, bsc_evidence.decimals) == (56, 18)


async def test_fetch_onchain_decimals_rejects_missing_rpc_result_instead_of_defaulting() -> None:
    """An RPC error or empty result must raise, never silently fall back to
    18 -- fail-closed, matching every other identity check in this repo."""
    client = _FakeHTTPClient()
    rpc = "https://bsc-dataseed.binance.org/"
    client.queue_post(
        rpc,
        _FakeResponse({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}),
    )

    with pytest.raises(ValueError, match="no usable result"):
        await fetch_onchain_decimals(client, "bsc", "0xdead")


async def test_fetch_onchain_decimals_rejects_missing_block_hash() -> None:
    client = _FakeHTTPClient()
    rpc = "https://bsc-dataseed.binance.org/"
    client.queue_post(rpc, _FakeResponse(_rpc_result("0x64")))
    client.queue_post(rpc, _FakeResponse({"jsonrpc": "2.0", "id": 2, "result": None}))

    with pytest.raises(ValueError, match="no usable hash"):
        await fetch_onchain_decimals(client, "bsc", "0xdead")


async def test_fetch_onchain_decimals_rejects_empty_hex_result() -> None:
    client = _FakeHTTPClient()
    rpc = "https://bsc-dataseed.binance.org/"
    _queue_decimals_rpc(client, rpc, decimals_hex="0x")  # contract has no code / no decimals()

    with pytest.raises(ValueError, match="no usable result"):
        await fetch_onchain_decimals(client, "bsc", "0xdead")


def test_fetch_onchain_decimals_rejects_unmapped_chain() -> None:
    """Not async -- ValueError raises before any await, on the chain lookup
    itself. Confirms an unmapped chain fails closed rather than silently
    picking an arbitrary RPC endpoint."""
    import asyncio

    client = _FakeHTTPClient()
    with pytest.raises(ValueError, match="no RPC endpoint"):
        asyncio.run(fetch_onchain_decimals(client, "solana", "somewhere"))


# --- CCXT amount must never be scaled by on-chain decimals ---------------------


def test_no_amount_scaling_helper_exists_in_this_module() -> None:
    """Documents the actual safety property (colleague review, 2026-08-28):
    ccxt already returns amounts in human-readable units, so this module
    must never expose a function that multiplies/divides a ccxt amount by
    10**decimals -- decimals here is used only for identity comparison
    (ChainContractEvidence.decimals), never for unit conversion."""
    import schurfer_analytics.source_lead_identity_evidence as module

    forbidden_names = {"scale_amount", "convert_amount", "apply_decimals", "normalize_amount"}
    assert not (forbidden_names & set(dir(module)))


# --- _current_git_state: real provenance, never trusted from the caller -------


def test_current_git_state_returns_the_real_head_and_dirty_flag() -> None:
    """Regression for the exact defect a colleague review found (2026-08-28):
    every evidence file this tool first produced recorded a code_revision
    from before the tool's own commit, because the CLI trusted an externally
    supplied --code-revision/--working-tree-dirty instead of asking git.
    This only asserts the shape is plausible (a real 40-char hex commit and
    a bool) since the actual values depend on this checkout's live state."""
    head, dirty = _current_git_state()
    assert len(head) == 40
    assert all(char in "0123456789abcdef" for char in head)
    assert isinstance(dirty, bool)


# --- find_alpha_entry -----------------------------------------------------------


def test_find_alpha_entry_extracts_matching_symbol() -> None:
    catalog = RawFetch(
        source="test",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=True,
        payload={
            "data": [
                {"symbol": "TUT", "contractAddress": "0xabc"},
                {"symbol": "BR", "contractAddress": "0xdef"},
            ]
        },
    )
    entry = find_alpha_entry(catalog, "tut")
    assert entry == {"symbol": "TUT", "contractAddress": "0xabc"}


def test_find_alpha_entry_returns_none_when_absent() -> None:
    catalog = RawFetch(
        source="test",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=True,
        payload={"data": []},
    )
    assert find_alpha_entry(catalog, "NIL") is None


def test_find_alpha_entry_rejects_duplicate_symbols() -> None:
    catalog = RawFetch(
        source="test",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=True,
        payload={"data": [{"symbol": "TUT"}, {"symbol": "TUT"}]},
    )
    with pytest.raises(ValueError, match="2 entries"):
        find_alpha_entry(catalog, "TUT")


def test_alpha_identity_fields_drops_tokenid_and_other_non_identity_fields() -> None:
    """A gitleaks pre-commit check flagged Binance Alpha's own `tokenId` field
    (a high-entropy internal catalog id, not a secret) as a likely API key.
    Narrowing what this tool stores to identity-relevant fields sidesteps
    that false positive and keeps evidence bundles focused."""
    entry = {
        # deliberately not a realistic high-entropy id -- this test only
        # needs *some* value under "tokenId" to prove it gets dropped, and a
        # random-looking hex string here previously tripped this repo's
        # gitleaks pre-commit hook the same way the real evidence data did.
        "tokenId": "placeholder-not-a-secret",
        "symbol": "TUT",
        "name": "Tutorial",
        "chainId": "56",
        "chainName": "BSC",
        "contractAddress": "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
        "decimals": 18,
        "listingCex": True,
        "offline": False,
        "fullyDelisted": False,
        "iconUrl": "https://example.test/icon.png",
        "price": "1.23",
        "holders": "10600",
    }
    identity = _alpha_identity_fields(entry)
    assert "tokenId" not in identity
    assert "price" not in identity
    assert "holders" not in identity
    assert "iconUrl" not in identity
    assert identity["symbol"] == "TUT"
    assert identity["contractAddress"] == "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3"
    assert identity["chainId"] == "56"


# --- fetch_gate_futures_contract / find_binance_futures_market / route evidence -


async def test_fetch_gate_futures_contract_extracts_market_evidence() -> None:
    client = _FakeHTTPClient()
    client.queue_get(
        "https://api.gateio.ws/",
        _FakeResponse(
            {
                "name": "ARIA_USDT",
                "launch_time": 1_755_781_800,
                "create_time": 1_755_758_876,
                "status": "trading",
                "in_delisting": False,
            }
        ),
    )
    market = await fetch_gate_futures_contract(client, "ARIA")
    assert market.exchange == "gate"
    assert market.native_market_id == "ARIA_USDT"
    # launch_time, not create_time -- confirmed live against the real API
    # these two genuinely differ (see this module's onboarded_at_ms import).
    assert market.onboarded_at_ms == 1_755_781_800_000
    assert market.status == "trading"
    # Gate's payload has no base_asset/quote_asset/settle_asset fields --
    # reported_* must stay None, inferred_* is this tool's own parse.
    assert market.reported_base_asset is None
    assert market.reported_quote_asset is None
    assert market.reported_settle_asset is None
    assert market.inferred_base_asset == "ARIA"
    assert market.inferred_quote_asset == "USDT"
    assert market.inferred_settle_asset == "USDT"


async def test_fetch_gate_futures_contract_fails_closed_on_name_mismatch() -> None:
    """A malformed or wrong response (e.g. an error page) must never be
    silently trusted as if it named the requested market."""
    client = _FakeHTTPClient()
    client.queue_get(
        "https://api.gateio.ws/",
        _FakeResponse({"name": "WRONG_USDT", "launch_time": 1, "status": "trading"}),
    )
    with pytest.raises(ValueError, match="malformed"):
        await fetch_gate_futures_contract(client, "ARIA")


def test_find_binance_futures_market_extracts_matching_symbol() -> None:
    exchange_info = RawFetch(
        source="test",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=True,
        payload={"symbols": [_binance_futures_market_response("ARIAUSDT", "ARIA")]},
    )
    market = find_binance_futures_market(exchange_info, "ariausdt")
    assert market is not None
    assert market.native_market_id == "ARIAUSDT"
    # Binance genuinely reports baseAsset as a distinct field.
    assert market.reported_base_asset == "ARIA"
    assert market.inferred_base_asset == "ARIA"
    assert market.onboarded_at_ms == 1_700_000_000_000


def test_find_binance_futures_market_returns_none_when_absent() -> None:
    exchange_info = RawFetch(
        source="test",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=True,
        payload={"symbols": []},
    )
    assert find_binance_futures_market(exchange_info, "NILUSDT") is None


def test_find_binance_futures_market_rejects_duplicate_symbols() -> None:
    exchange_info = RawFetch(
        source="test",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=True,
        payload={
            "symbols": [
                _binance_futures_market_response("TUTUSDT", "TUT"),
                _binance_futures_market_response("TUTUSDT", "TUT"),
            ]
        },
    )
    with pytest.raises(ValueError, match="2 entries"):
        find_binance_futures_market(exchange_info, "TUTUSDT")


def test_validate_route_evidence_accepts_matching_markets() -> None:
    _validate_route_evidence(
        base="TUT",
        source_market=_sample_market_evidence("gate", "TUT_USDT"),
        target_market=_sample_market_evidence("binance", "TUTUSDT", base_asset="TUT"),
    )


def test_validate_route_evidence_rejects_wrong_gate_market_id() -> None:
    with pytest.raises(ValueError, match="does not match the expected"):
        _validate_route_evidence(
            base="TUT",
            source_market=_sample_market_evidence("gate", "WRONG_USDT"),
            target_market=_sample_market_evidence("binance", "TUTUSDT", base_asset="TUT"),
        )


def test_validate_route_evidence_rejects_wrong_binance_market_id() -> None:
    with pytest.raises(ValueError, match="does not match the expected"):
        _validate_route_evidence(
            base="TUT",
            source_market=_sample_market_evidence("gate", "TUT_USDT"),
            target_market=_sample_market_evidence("binance", "WRONGUSDT"),
        )


def test_validate_route_evidence_rejects_base_asset_mismatch() -> None:
    """The one piece of independent corroboration Binance's own exchangeInfo
    response adds beyond the symbol string already used to look it up --
    reported_base_asset is a genuinely distinct field, not this tool's own
    parse (see the module docstring's PR1 fix-round note)."""
    mismatched = dataclasses.replace(
        _sample_market_evidence("binance", "TUTUSDT", base_asset="TUT"),
        reported_base_asset="OTHER",
    )
    with pytest.raises(ValueError, match="baseAsset"):
        _validate_route_evidence(
            base="TUT",
            source_market=_sample_market_evidence("gate", "TUT_USDT"),
            target_market=mismatched,
        )


def test_validate_route_evidence_rejects_non_usdt_quote_or_settle() -> None:
    non_usdt = dataclasses.replace(
        _sample_market_evidence("gate", "TUT_USDT"), inferred_quote_asset="USDC"
    )
    with pytest.raises(ValueError, match="does not parse to a USDT-quoted"):
        _validate_route_evidence(
            base="TUT",
            source_market=non_usdt,
            target_market=_sample_market_evidence("binance", "TUTUSDT", base_asset="TUT"),
        )


def test_validate_route_evidence_rejects_binance_non_usdt_quote_or_settle() -> None:
    non_usdt = dataclasses.replace(
        _sample_market_evidence("binance", "TUTUSDT", base_asset="TUT"),
        reported_quote_asset="USDC",
    )
    with pytest.raises(ValueError, match="not USDT-quoted"):
        _validate_route_evidence(
            base="TUT",
            source_market=_sample_market_evidence("gate", "TUT_USDT"),
            target_market=non_usdt,
        )


def test_validate_route_evidence_rejects_gate_in_delisting() -> None:
    delisting = dataclasses.replace(
        _sample_market_evidence("gate", "TUT_USDT"),
        raw_evidence=dataclasses.replace(
            _sample_market_evidence("gate", "TUT_USDT").raw_evidence,
            payload={"in_delisting": True},
        ),
    )
    with pytest.raises(ValueError, match="in_delisting"):
        _validate_route_evidence(
            base="TUT",
            source_market=delisting,
            target_market=_sample_market_evidence("binance", "TUTUSDT", base_asset="TUT"),
        )


def test_validate_route_evidence_rejects_non_perpetual_contract_type() -> None:
    market = _sample_market_evidence("binance", "TUTUSDT", base_asset="TUT")
    delivery = dataclasses.replace(
        market,
        raw_evidence=dataclasses.replace(
            market.raw_evidence,
            payload={**market.raw_evidence.payload, "contractType": "CURRENT_QUARTER"},
        ),
    )
    with pytest.raises(ValueError, match="not PERPETUAL"):
        _validate_route_evidence(
            base="TUT",
            source_market=_sample_market_evidence("gate", "TUT_USDT"),
            target_market=delivery,
        )


def test_validate_route_evidence_rejects_inactive_market() -> None:
    inactive = dataclasses.replace(_sample_market_evidence("gate", "TUT_USDT"), status="delisted")
    with pytest.raises(ValueError, match="not trading"):
        _validate_route_evidence(
            base="TUT",
            source_market=inactive,
            target_market=_sample_market_evidence("binance", "TUTUSDT", base_asset="TUT"),
        )


# --- revalidate_bundle_route_evidence: read-time re-check, not just capture-time -


def test_revalidate_bundle_route_evidence_is_noop_for_pre_v3_bundle() -> None:
    """A bundle captured before source_market_evidence/target_market_evidence
    existed has nothing to revalidate -- must not raise."""
    bundle = _sample_bundle(with_market_evidence=False)
    revalidate_bundle_route_evidence(bundle)  # must not raise


def test_revalidate_bundle_route_evidence_rejects_invalid_v3_bundle() -> None:
    """Mirrors revalidate_bundle_identity_class's role: load_evidence_bundle
    only re-checks bundle_sha256 (content not tampered with after writing),
    which alone does not prove the route evidence was ever semantically
    valid -- a hand-crafted file can compute a correct hash over content
    that never actually passed _validate_route_evidence."""
    bundle = _sample_bundle()
    assert bundle.target_market_evidence is not None
    tampered_market = dataclasses.replace(
        bundle.target_market_evidence, reported_base_asset="NOT_THE_BASE"
    )
    bundle = dataclasses.replace(bundle, target_market_evidence=tampered_market)
    with pytest.raises(ValueError, match="baseAsset"):
        revalidate_bundle_route_evidence(bundle)


# --- fetch_gate_currency: fails closed like fetch_exchange_evidence ------------


async def test_fetch_gate_currency_fails_closed_when_base_absent() -> None:
    exchange = _FakeExchange({})
    with pytest.raises(ValueError, match="no currency entry"):
        await fetch_gate_currency(exchange, "UB")


async def test_fetch_gate_currency_extracts_the_named_entry() -> None:
    exchange = _FakeExchange({"UB": {"networks": {"BEP20": {}}}})
    evidence = await fetch_gate_currency(exchange, "UB")
    assert evidence.payload == {"networks": {"BEP20": {}}}
    assert evidence.wire_exact is False


# --- _validate_identity_class: the EDEN regression -----------------------------


def _gate_raw(networks: dict[str, Any]) -> RawFetch:
    return RawFetch(
        source="gate:fetch_currencies",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=False,
        payload={"networks": networks},
    )


def _coingecko_raw(platforms: dict[str, Any]) -> RawFetch:
    return RawFetch(
        source="test",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=True,
        payload={"platforms": platforms},
    )


def _alpha_raw(contract_address: str, decimals: int = 18) -> RawFetch:
    return RawFetch(
        source="binance:alpha_catalog_entry",
        endpoint="test",
        observed_at=datetime.now(UTC),
        raw_sha256="x",
        wire_exact=False,
        payload={"contractAddress": contract_address, "decimals": decimals},
    )


def _contract(chain: str, chain_id: int, address: str, decimals: int = 18) -> ChainContractEvidence:
    return ChainContractEvidence(
        chain=chain,
        chain_id=chain_id,
        contract_address=address.lower(),
        decimals=decimals,
        decimals_evidence=RawFetch(
            source="rpc:eth_call",
            endpoint="test",
            observed_at=datetime.now(UTC),
            raw_sha256="x",
            wire_exact=True,
            payload={"result": "0x12"},
        ),
        block_number=1,
        block_hash="0x" + "a" * 64,
    )


def test_validate_identity_class_rejects_eden_style_mismatch() -> None:
    """The exact defect a colleague review found live in committed evidence
    (2026-08-28): identity_class said exact_contract while source and target
    were on different chains with different addresses. This must now raise
    instead of silently saving."""
    source = _contract("ethereum", 1, "0x24a3d725c37a8d1a66eb87f0e5d07fe67c120035")
    target = _contract("bsc", 56, "0x235b6fe22b4642ada16d311855c49ce7de260841")
    gate_evidence = _gate_raw({"ERC20": {"info": {"addr": source.contract_address}}})
    with pytest.raises(ValueError, match="exact_contract requires identical chain and address"):
        _validate_identity_class(
            identity_class="exact_contract",
            base="EDEN",
            gate_evidence=gate_evidence,
            coingecko_evidence=_coingecko_raw({}),
            target_catalog_evidence=_alpha_raw(target.contract_address),
            source_contract=source,
            target_contract=target,
        )


def test_validate_identity_class_accepts_true_exact_contract() -> None:
    contract = _contract("bsc", 56, "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3")
    gate_evidence = _gate_raw({"BEP20": {"info": {"addr": contract.contract_address}}})
    _validate_identity_class(
        identity_class="exact_contract",
        base="TUT",
        gate_evidence=gate_evidence,
        coingecko_evidence=_coingecko_raw({}),
        target_catalog_evidence=_alpha_raw(contract.contract_address),
        source_contract=contract,
        target_contract=contract,
    )


def test_validate_identity_class_rejects_gate_evidence_mismatch() -> None:
    """The source_contract_address a candidate table entry names must
    actually appear in Gate's own fetched currency data -- not just be
    asserted by the caller."""
    source = _contract("bsc", 56, "0xaaaa000000000000000000000000000000aaaa")
    gate_evidence = _gate_raw(
        {"BEP20": {"info": {"addr": "0xbbbb000000000000000000000000000000bbbb"}}}
    )
    with pytest.raises(ValueError, match="does not report"):
        _validate_identity_class(
            identity_class="exact_contract",
            base="X",
            gate_evidence=gate_evidence,
            coingecko_evidence=_coingecko_raw({}),
            target_catalog_evidence=_alpha_raw(source.contract_address),
            source_contract=source,
            target_contract=source,
        )


def test_validate_identity_class_rejects_alpha_catalog_address_mismatch() -> None:
    contract = _contract("bsc", 56, "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3")
    gate_evidence = _gate_raw({"BEP20": {"info": {"addr": contract.contract_address}}})
    with pytest.raises(ValueError, match="does not match"):
        _validate_identity_class(
            identity_class="exact_contract",
            base="TUT",
            gate_evidence=gate_evidence,
            coingecko_evidence=_coingecko_raw({}),
            target_catalog_evidence=_alpha_raw("0xdeaddeaddeaddeaddeaddeaddeaddeaddeaddead"),
            source_contract=contract,
            target_contract=contract,
        )


def test_validate_identity_class_rejects_catalog_decimals_mismatch() -> None:
    contract = _contract("bsc", 56, "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3", decimals=18)
    gate_evidence = _gate_raw({"BEP20": {"info": {"addr": contract.contract_address}}})
    with pytest.raises(ValueError, match="catalog claims decimals"):
        _validate_identity_class(
            identity_class="exact_contract",
            base="TUT",
            gate_evidence=gate_evidence,
            coingecko_evidence=_coingecko_raw({}),
            target_catalog_evidence=_alpha_raw(contract.contract_address, decimals=6),
            source_contract=contract,
            target_contract=contract,
        )


def test_validate_identity_class_multichain_requires_coingecko_corroboration() -> None:
    source = _contract("ethereum", 1, "0x2798b1cc5a993085e8a9d46e80499f1b63f42204")
    target = _contract("bsc", 56, "0x30117e4bc17d7b044194b76a38365c53b72f7d49")
    gate_evidence = _gate_raw({"ERC20": {"info": {"addr": source.contract_address}}})
    with pytest.raises(ValueError, match="does not corroborate"):
        _validate_identity_class(
            identity_class="same_asset_multichain_candidate",
            base="GWEI",
            gate_evidence=gate_evidence,
            coingecko_evidence=_coingecko_raw({}),  # neither address present
            target_catalog_evidence=_alpha_raw(target.contract_address),
            source_contract=source,
            target_contract=target,
        )


def test_validate_identity_class_multichain_accepts_coingecko_corroborated_pair() -> None:
    source = _contract("ethereum", 1, "0x2798b1cc5a993085e8a9d46e80499f1b63f42204")
    target = _contract("bsc", 56, "0x30117e4bc17d7b044194b76a38365c53b72f7d49")
    gate_evidence = _gate_raw({"ERC20": {"info": {"addr": source.contract_address}}})
    coingecko_evidence = _coingecko_raw(
        {"ethereum": source.contract_address, "binance-smart-chain": target.contract_address}
    )
    _validate_identity_class(
        identity_class="same_asset_multichain_candidate",
        base="GWEI",
        gate_evidence=gate_evidence,
        coingecko_evidence=coingecko_evidence,
        target_catalog_evidence=_alpha_raw(target.contract_address),
        source_contract=source,
        target_contract=target,
    )


def test_validate_identity_class_multichain_rejects_identical_source_and_target() -> None:
    contract = _contract("bsc", 56, "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3")
    gate_evidence = _gate_raw({"BEP20": {"info": {"addr": contract.contract_address}}})
    with pytest.raises(ValueError, match="reclassify"):
        _validate_identity_class(
            identity_class="same_asset_multichain_candidate",
            base="TUT",
            gate_evidence=gate_evidence,
            coingecko_evidence=_coingecko_raw({}),
            target_catalog_evidence=_alpha_raw(contract.contract_address),
            source_contract=contract,
            target_contract=contract,
        )


def test_validate_identity_class_third_party_bridge_only_requires_no_target() -> None:
    source = _contract("ethereum", 1, "0x7cf9a80db3b29ee8efe3710aadb7b95270572d47", decimals=6)
    gate_evidence = _gate_raw({"ERC20": {"info": {"addr": source.contract_address}}})
    _validate_identity_class(
        identity_class="third_party_bridge_only",
        base="NIL",
        gate_evidence=gate_evidence,
        coingecko_evidence=_coingecko_raw({}),
        target_catalog_evidence=None,
        source_contract=source,
        target_contract=None,
    )


def test_validate_identity_class_third_party_bridge_only_rejects_target_evidence() -> None:
    source = _contract("ethereum", 1, "0x7cf9a80db3b29ee8efe3710aadb7b95270572d47", decimals=6)
    target = _contract("bsc", 56, "0xaaaa000000000000000000000000000000aaaa")
    gate_evidence = _gate_raw({"ERC20": {"info": {"addr": source.contract_address}}})
    with pytest.raises(ValueError, match="must not carry target"):
        _validate_identity_class(
            identity_class="third_party_bridge_only",
            base="NIL",
            gate_evidence=gate_evidence,
            coingecko_evidence=_coingecko_raw({}),
            target_catalog_evidence=_alpha_raw(target.contract_address),
            source_contract=source,
            target_contract=target,
        )


# --- bundle hashing: deterministic, and detects tampering ----------------------


def _sample_chain_evidence(decimals: int = 18, contract: str = "0xaaaa") -> ChainContractEvidence:
    return ChainContractEvidence(
        chain="bsc",
        chain_id=56,
        contract_address=contract,
        decimals=decimals,
        decimals_evidence=RawFetch(
            source="rpc:eth_call",
            endpoint="https://fake/",
            observed_at=datetime(2026, 8, 28, tzinfo=UTC),
            raw_sha256="a" * 64,
            wire_exact=True,
            payload={"result": "0x12"},
        ),
        block_number=100,
        block_hash="0x" + "a" * 64,
    )


def _sample_market_evidence(
    exchange: str, native_market_id: str, *, base_asset: str = "TEST"
) -> DerivativeMarketEvidence:
    is_gate = exchange == "gate"
    return DerivativeMarketEvidence(
        exchange=exchange,
        native_market_id=native_market_id,
        reported_base_asset=None if is_gate else base_asset,
        reported_quote_asset=None if is_gate else "USDT",
        reported_settle_asset=None if is_gate else "USDT",
        inferred_base_asset=base_asset,
        inferred_quote_asset="USDT",
        inferred_settle_asset="USDT",
        inference_basis="test fixture",
        onboarded_at_ms=1_700_000_000_000,
        status="trading" if is_gate else "TRADING",
        raw_evidence=RawFetch(
            source="test",
            endpoint="https://fake/",
            observed_at=datetime(2026, 8, 28, tzinfo=UTC),
            raw_sha256="c" * 64,
            wire_exact=True,
            payload=(
                {"contractType": "PERPETUAL"} if exchange == "binance" else {"in_delisting": False}
            ),
        ),
    )


def _sample_bundle(*, with_market_evidence: bool = True) -> EvidenceBundle:
    raw = RawFetch(
        source="test",
        endpoint="https://fake/",
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
        raw_sha256="b" * 64,
        wire_exact=True,
        payload={"ok": True},
    )
    return EvidenceBundle(
        evidence_version=EVIDENCE_VERSION
        if with_market_evidence
        else "source_lead_identity_evidence_v2",
        base="TEST",
        source_exchange="gate",
        target_exchange="binance",
        identity_class="exact_contract",
        source_contract=_sample_chain_evidence(),
        target_contract=_sample_chain_evidence(),
        gate_evidence=raw,
        target_catalog_evidence=raw,
        coingecko_evidence=raw,
        source_market_evidence=(
            _sample_market_evidence("gate", "TEST_USDT") if with_market_evidence else None
        ),
        target_market_evidence=(
            _sample_market_evidence("binance", "TESTUSDT") if with_market_evidence else None
        ),
        code_revision="abc123",
        working_tree_dirty=False,
        captured_at=datetime(2026, 8, 28, tzinfo=UTC),
        bundle_sha256="",
    )


def test_compute_bundle_sha256_is_deterministic() -> None:
    """Repeat capture with identical inputs must produce the identical hash
    -- the actual, testable meaning of "capture is deterministic" for a
    function with no randomness or wall-clock dependence in its hashing."""
    bundle = _sample_bundle()
    first = compute_bundle_sha256(bundle)
    second = compute_bundle_sha256(bundle)
    assert first == second
    assert len(first) == 64


def test_compute_bundle_sha256_changes_when_decimals_change() -> None:
    bundle = _sample_bundle()
    original = compute_bundle_sha256(bundle)
    mutated = dataclasses.replace(bundle, source_contract=_sample_chain_evidence(decimals=6))
    assert compute_bundle_sha256(mutated) != original


def test_save_and_load_evidence_bundle_round_trips(tmp_path: Path) -> None:
    bundle = _sample_bundle()
    digest = compute_bundle_sha256(bundle)
    finalized = dataclasses.replace(bundle, bundle_sha256=digest)
    path = save_evidence_bundle(finalized, tmp_path / "test-gate-binance.json")

    loaded = load_evidence_bundle(path)

    assert loaded.base == "TEST"
    assert loaded.source_contract.decimals == 18
    assert loaded.source_contract.block_hash == "0x" + "a" * 64
    assert loaded.bundle_sha256 == digest


def test_save_and_load_pre_v3_bundle_without_market_evidence_still_round_trips(
    tmp_path: Path,
) -> None:
    """A genuinely old (evidence_version < v3) bundle on disk, captured
    before source_market_evidence/target_market_evidence existed, must
    still load and pass its own integrity check under the current code --
    the currently-deployed registry v2 depends on exactly this (colleague
    review, 2026-08-28, third round)."""
    bundle = _sample_bundle(with_market_evidence=False)
    digest = compute_bundle_sha256(bundle)
    finalized = dataclasses.replace(bundle, bundle_sha256=digest)
    path = save_evidence_bundle(finalized, tmp_path / "test-gate-binance.json")

    loaded = load_evidence_bundle(path)

    assert loaded.source_market_evidence is None
    assert loaded.target_market_evidence is None
    assert loaded.bundle_sha256 == digest
    # Matters regardless of whether the field is literally absent from the
    # JSON (a genuinely old file) or present-as-null (round-tripped through
    # current code, as here): compute_bundle_sha256 operates on the
    # deserialized object, which is None either way, not on raw file bytes.
    assert json.loads(path.read_text())["source_market_evidence"] is None


def test_load_evidence_bundle_rejects_tampered_content(tmp_path: Path) -> None:
    """The actual integrity check missing from source_lead_qualification.py's
    registry loader (colleague review, 2026-08-28: it only validates
    evidence_sha256's string format, never its content) -- this loader must
    recompute and compare, not trust the stored hash."""
    bundle = _sample_bundle()
    digest = compute_bundle_sha256(bundle)
    finalized = dataclasses.replace(bundle, bundle_sha256=digest)
    path = save_evidence_bundle(finalized, tmp_path / "test-gate-binance.json")

    raw = json.loads(path.read_text())
    raw["source_contract"]["decimals"] = 6  # tamper, keep the old bundle_sha256
    path.write_text(json.dumps(raw))

    with pytest.raises(EvidenceIntegrityError, match="integrity check"):
        load_evidence_bundle(path)


def test_load_evidence_bundle_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(EvidenceIntegrityError, match="not found"):
        load_evidence_bundle(tmp_path / "does-not-exist.json")


def test_load_all_evidence_bundles_rejects_missing_directory_by_default(tmp_path: Path) -> None:
    """Fail closed (colleague review, 2026-08-28): a missing evidence
    directory must not read identically to "genuinely nothing captured
    yet"."""
    with pytest.raises(EvidenceIntegrityError, match="not found"):
        load_all_evidence_bundles(tmp_path / "nope")


def test_load_all_evidence_bundles_allows_missing_directory_when_explicit(tmp_path: Path) -> None:
    assert load_all_evidence_bundles(tmp_path / "nope", allow_empty=True) == ()


def test_load_all_evidence_bundles_rejects_empty_directory_by_default(tmp_path: Path) -> None:
    with pytest.raises(EvidenceIntegrityError, match="no evidence bundles"):
        load_all_evidence_bundles(tmp_path)


def test_load_all_evidence_bundles_ignores_manifest_file(tmp_path: Path) -> None:
    bundle = _sample_bundle()
    digest = compute_bundle_sha256(bundle)
    finalized = dataclasses.replace(bundle, bundle_sha256=digest)
    save_evidence_bundle(finalized, tmp_path / "test-gate-binance.json")
    (tmp_path / "manifest.json").write_text("{}")

    bundles = load_all_evidence_bundles(tmp_path)

    assert [b.base for b in bundles] == ["TEST"]


def test_load_all_evidence_bundles_rejects_manifest_candidate_set_mismatch(
    tmp_path: Path,
) -> None:
    """A manifest.json is written by _run alongside every bundle it
    publishes but, before this fix, was never read back by anything --
    colleague review, 2026-08-28, PR1 fix round: a bundle file added,
    removed, or replaced by hand without regenerating the manifest passed
    silently. This is the actual check that closes that gap."""
    bundle = _sample_bundle()
    digest = compute_bundle_sha256(bundle)
    finalized = dataclasses.replace(bundle, bundle_sha256=digest)
    save_evidence_bundle(finalized, tmp_path / "test-gate-binance.json")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"candidates": ["SOMETHING_ELSE"], "bundle_fingerprint": None})
    )

    with pytest.raises(EvidenceIntegrityError, match="candidate set"):
        load_all_evidence_bundles(tmp_path)


def test_load_all_evidence_bundles_rejects_manifest_fingerprint_mismatch(tmp_path: Path) -> None:
    bundle = _sample_bundle()
    digest = compute_bundle_sha256(bundle)
    finalized = dataclasses.replace(bundle, bundle_sha256=digest)
    save_evidence_bundle(finalized, tmp_path / "test-gate-binance.json")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"candidates": ["TEST"], "bundle_fingerprint": "0" * 64})
    )

    with pytest.raises(EvidenceIntegrityError, match="bundle_fingerprint"):
        load_all_evidence_bundles(tmp_path)


def test_load_all_evidence_bundles_accepts_matching_manifest(tmp_path: Path) -> None:
    bundle = _sample_bundle()
    digest = compute_bundle_sha256(bundle)
    finalized = dataclasses.replace(bundle, bundle_sha256=digest)
    save_evidence_bundle(finalized, tmp_path / "test-gate-binance.json")
    fingerprint = _sha256_canonical([digest])
    (tmp_path / "manifest.json").write_text(
        json.dumps({"candidates": ["TEST"], "bundle_fingerprint": fingerprint})
    )

    bundles = load_all_evidence_bundles(tmp_path)
    assert [b.base for b in bundles] == ["TEST"]


def test_load_all_evidence_bundles_is_deterministically_ordered(tmp_path: Path) -> None:
    for name in ("zzz", "aaa", "mmm"):
        bundle = dataclasses.replace(_sample_bundle(), base=name.upper())
        digest = compute_bundle_sha256(bundle)
        finalized = dataclasses.replace(bundle, bundle_sha256=digest)
        save_evidence_bundle(finalized, tmp_path / f"{name}-gate-binance.json")

    bundles = load_all_evidence_bundles(tmp_path)
    assert [b.base for b in bundles] == ["AAA", "MMM", "ZZZ"]


# --- the real 2026-08-28 evidence set, if present -------------------------------


def test_captured_evidence_set_loads_and_verifies_if_present() -> None:
    """Integration-style check against whatever this branch actually shipped
    in evidence/source_lead/v2/."""
    bundles = load_all_evidence_bundles(allow_empty=True)
    if not bundles:
        pytest.skip("no captured evidence bundles present on this checkout")
    nil = next((b for b in bundles if b.base == "NIL"), None)
    if nil is not None:
        assert nil.source_contract.decimals == 6
    eden = next((b for b in bundles if b.base == "EDEN"), None)
    if eden is not None:
        # The exact colleague-review regression: EDEN's source and target
        # must now genuinely agree when identity_class is exact_contract.
        assert eden.identity_class != "exact_contract" or (
            eden.target_contract is not None
            and eden.source_contract.chain == eden.target_contract.chain
            and eden.source_contract.contract_address == eden.target_contract.contract_address
        )


def test_candidates_table_has_no_duplicate_routes() -> None:
    routes = [(base, target) for base, target, *_ in CANDIDATES]
    assert len(routes) == len(set(routes))


def test_captured_v3_evidence_set_loads_verifies_and_carries_route_evidence() -> None:
    """Integration-style check against the committed evidence/source_lead/v3/
    bundles -- this directory is checked in, not gitignored, and must always
    be present and complete on a real checkout of this branch. Colleague
    review, 2026-08-28, PR1 fix round: the previous version passed
    allow_empty=True and skipped when the directory was empty, which makes a
    genuinely broken or deleted evidence set silently pass CI instead of
    failing it -- a missing/incomplete required directory must raise, like
    every other integrity check in this module."""
    bundles = load_all_evidence_bundles(EVIDENCE_DIR_V3)
    assert len(bundles) == len(CANDIDATES)
    for bundle in bundles:
        assert bundle.evidence_version == EVIDENCE_VERSION
        assert bundle.source_market_evidence is not None
        assert bundle.source_market_evidence.native_market_id == f"{bundle.base}_USDT"
        assert bundle.target_market_evidence is not None
        assert bundle.target_market_evidence.native_market_id == f"{bundle.base}USDT"
        assert bundle.target_market_evidence.reported_base_asset == bundle.base
        # Route evidence must still pass a fresh semantic re-check at read
        # time, not just have passed once at capture time.
        revalidate_bundle_route_evidence(bundle)


# --- capture_bundle: full end-to-end flow against fakes, no real network ------


def _gate_futures_contract_response(
    market_id: str, *, launch_time: int = 1_700_000_000, status: str = "trading"
) -> dict[str, Any]:
    return {"name": market_id, "launch_time": launch_time, "status": status, "in_delisting": False}


def _binance_futures_market_response(
    symbol: str,
    base_asset: str,
    *,
    onboard_date: int = 1_700_000_000_000,
    contract_type: str = "PERPETUAL",
    status: str = "TRADING",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "baseAsset": base_asset,
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractType": contract_type,
        "onboardDate": onboard_date,
        "status": status,
    }


def _queue_gate_style_client(*, with_alpha_entry: bool) -> _FakeHTTPClient:
    client = _FakeHTTPClient()
    client.queue_get(
        "https://api.coingecko.com/",
        _FakeResponse(
            {
                "id": "tut",
                "platforms": {"binance-smart-chain": "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3"},
            }
        ),
    )
    rpc = "https://bsc-dataseed.binance.org/"
    _queue_decimals_rpc(client, rpc, decimals_hex="0x" + "0" * 62 + "12")  # source decimals=18
    if with_alpha_entry:
        _queue_decimals_rpc(client, rpc, decimals_hex="0x" + "0" * 62 + "12")  # target decimals=18
    client.queue_get(
        "https://api.gateio.ws/", _FakeResponse(_gate_futures_contract_response("TUT_USDT"))
    )
    return client


async def test_capture_bundle_exact_contract_end_to_end() -> None:
    client = _queue_gate_style_client(with_alpha_entry=True)
    client.queue_get(
        "https://www.binance.com/",
        _FakeResponse(
            {
                "data": [
                    {
                        "symbol": "TUT",
                        "chainId": "56",
                        "contractAddress": "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
                        "decimals": 18,
                    }
                ]
            }
        ),
    )
    client.queue_get(
        "https://fapi.binance.com/",
        _FakeResponse({"symbols": [_binance_futures_market_response("TUTUSDT", "TUT")]}),
    )
    gate = _FakeExchange(
        {
            "TUT": {
                "networks": {
                    "BEP20": {"info": {"addr": "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3"}}
                }
            }
        }
    )
    alpha_catalog = await fetch_binance_alpha_catalog(client)
    binance_futures_exchange_info = await fetch_binance_futures_exchange_info(client)

    bundle = await capture_bundle(
        http_client=client,
        gate_exchange=gate,
        base="TUT",
        source_exchange="gate",
        target_exchange="binance",
        source_chain="bsc",
        source_contract_address="0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
        coingecko_id="tutorial",
        identity_class="exact_contract",
        target_chain="bsc",
        target_contract_address="0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
        alpha_catalog=alpha_catalog,
        binance_futures_exchange_info=binance_futures_exchange_info,
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert bundle.base == "TUT"
    assert bundle.source_contract.decimals == 18
    assert bundle.target_contract is not None
    assert bundle.target_contract.decimals == 18
    assert bundle.target_catalog_evidence is not None
    assert bundle.source_market_evidence is not None
    assert bundle.source_market_evidence.native_market_id == "TUT_USDT"
    assert bundle.target_market_evidence is not None
    assert bundle.target_market_evidence.native_market_id == "TUTUSDT"
    assert bundle.bundle_sha256 == compute_bundle_sha256(bundle)


async def test_capture_bundle_rejects_eden_style_chain_mismatch() -> None:
    """capture_bundle itself must reject a mismatched exact_contract claim,
    not just the standalone _validate_identity_class function -- proves the
    validation is actually wired into the real capture path."""
    client = _queue_gate_style_client(with_alpha_entry=True)
    client.queue_get(
        "https://www.binance.com/",
        _FakeResponse(
            {
                "data": [
                    {
                        "symbol": "TUT",
                        "contractAddress": "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
                        "decimals": 18,
                    }
                ]
            }
        ),
    )
    client.queue_get(
        "https://fapi.binance.com/",
        _FakeResponse({"symbols": [_binance_futures_market_response("TUTUSDT", "TUT")]}),
    )
    # Gate only reports an ethereum contract, but the candidate table claims
    # a bsc source_contract_address that gate never actually confirmed.
    gate = _FakeExchange({"TUT": {"networks": {"ERC20": {"info": {"addr": "0xdifferent"}}}}})
    alpha_catalog = await fetch_binance_alpha_catalog(client)
    binance_futures_exchange_info = await fetch_binance_futures_exchange_info(client)

    with pytest.raises(ValueError, match="does not report"):
        await capture_bundle(
            http_client=client,
            gate_exchange=gate,
            base="TUT",
            source_exchange="gate",
            target_exchange="binance",
            source_chain="bsc",
            source_contract_address="0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
            coingecko_id="tutorial",
            identity_class="exact_contract",
            target_chain="bsc",
            target_contract_address="0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
            alpha_catalog=alpha_catalog,
            binance_futures_exchange_info=binance_futures_exchange_info,
            code_revision="abc123",
            working_tree_dirty=False,
        )


async def test_capture_bundle_exact_contract_requires_alpha_entry() -> None:
    """A candidate classified exact_contract but with no matching catalog
    entry at capture time is a data problem, not something to silently
    downgrade -- capture_bundle must raise, not guess."""
    client = _queue_gate_style_client(with_alpha_entry=True)
    client.queue_get("https://www.binance.com/", _FakeResponse({"data": []}))
    client.queue_get(
        "https://fapi.binance.com/",
        _FakeResponse({"symbols": [_binance_futures_market_response("TUTUSDT", "TUT")]}),
    )
    gate = _FakeExchange(
        {
            "TUT": {
                "networks": {
                    "BEP20": {"info": {"addr": "0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3"}}
                }
            }
        }
    )
    alpha_catalog = await fetch_binance_alpha_catalog(client)
    binance_futures_exchange_info = await fetch_binance_futures_exchange_info(client)

    with pytest.raises(ValueError, match="found none"):
        await capture_bundle(
            http_client=client,
            gate_exchange=gate,
            base="TUT",
            source_exchange="gate",
            target_exchange="binance",
            source_chain="bsc",
            source_contract_address="0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
            coingecko_id="tutorial",
            identity_class="exact_contract",
            target_chain="bsc",
            target_contract_address="0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3",
            alpha_catalog=alpha_catalog,
            binance_futures_exchange_info=binance_futures_exchange_info,
            code_revision="abc123",
            working_tree_dirty=False,
        )


async def test_capture_bundle_third_party_bridge_only_has_no_target_evidence() -> None:
    client = _FakeHTTPClient()
    client.queue_get(
        "https://api.coingecko.com/",
        _FakeResponse(
            {
                "id": "nillion",
                "platforms": {"ethereum": "0x7cf9a80db3b29ee8efe3710aadb7b95270572d47"},
            }
        ),
    )
    _queue_decimals_rpc(
        client, "https://ethereum-rpc.publicnode.com", decimals_hex="0x" + "0" * 63 + "6"
    )
    client.queue_get("https://www.binance.com/", _FakeResponse({"data": []}))
    client.queue_get(
        "https://api.gateio.ws/", _FakeResponse(_gate_futures_contract_response("NIL_USDT"))
    )
    client.queue_get(
        "https://fapi.binance.com/",
        _FakeResponse({"symbols": [_binance_futures_market_response("NILUSDT", "NIL")]}),
    )
    gate = _FakeExchange(
        {
            "NIL": {
                "networks": {
                    "ERC20": {"info": {"addr": "0x7cf9a80db3b29ee8efe3710aadb7b95270572d47"}}
                }
            }
        }
    )
    alpha_catalog = await fetch_binance_alpha_catalog(client)
    binance_futures_exchange_info = await fetch_binance_futures_exchange_info(client)

    bundle = await capture_bundle(
        http_client=client,
        gate_exchange=gate,
        base="NIL",
        source_exchange="gate",
        target_exchange="binance",
        source_chain="ethereum",
        source_contract_address="0x7cf9a80db3b29ee8efe3710aadb7b95270572d47",
        coingecko_id="nillion",
        identity_class="third_party_bridge_only",
        target_chain=None,
        target_contract_address=None,
        alpha_catalog=alpha_catalog,
        binance_futures_exchange_info=binance_futures_exchange_info,
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert bundle.target_contract is None
    assert bundle.target_catalog_evidence is None
    assert bundle.source_contract.decimals == 6
    assert bundle.source_market_evidence is not None
    assert bundle.source_market_evidence.native_market_id == "NIL_USDT"
    assert bundle.target_market_evidence is not None
    assert bundle.target_market_evidence.native_market_id == "NILUSDT"


def test_candidates_table_third_party_bridge_only_has_no_target_chain() -> None:
    for base, _target, _sc, _sca, target_chain, target_contract, _cg, identity_class in CANDIDATES:
        if identity_class == "third_party_bridge_only":
            assert target_chain is None, base
            assert target_contract is None, base
        else:
            assert target_chain is not None, base
            assert target_contract is not None, base


def test_candidates_table_exact_contract_entries_have_matching_source_and_target() -> None:
    """The exact class of error a colleague review found live in the EDEN
    entry: an exact_contract row whose source_chain/contract and
    target_chain/contract actually differ. Every exact_contract row in the
    table itself must agree, independent of what capture_bundle would later
    catch at fetch time."""
    for (
        base,
        _target,
        source_chain,
        source_contract,
        target_chain,
        target_contract,
        _cg,
        identity_class,
    ) in CANDIDATES:
        if identity_class == "exact_contract":
            assert source_chain == target_chain, base
            assert source_contract.lower() == (target_contract or "").lower(), base
