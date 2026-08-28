from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics.source_lead_identity_evidence import (
    CANDIDATES,
    ChainContractEvidence,
    EvidenceBundle,
    EvidenceIntegrityError,
    RawFetch,
    _alpha_identity_fields,
    capture_bundle,
    compute_bundle_sha256,
    fetch_binance_alpha_catalog,
    fetch_gate_currency,
    fetch_onchain_decimals,
    find_alpha_entry,
    load_all_evidence_bundles,
    load_evidence_bundle,
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
    eth_call then eth_blockNumber against the same RPC endpoint)."""

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


# --- fetch_onchain_decimals: fail-closed, the NIL regression -------------------


async def test_fetch_onchain_decimals_returns_nil_6_not_default_18() -> None:
    """The exact NIL regression (colleague review, 2026-08-28): a real
    non-18-decimals contract must flow through as its true value, never a
    silently assumed default."""
    client = _FakeHTTPClient()
    rpc = "https://ethereum-rpc.publicnode.com"
    client.queue_post(rpc, _FakeResponse(_rpc_result("0x" + "0" * 63 + "6")))  # decimals() = 6
    client.queue_post(rpc, _FakeResponse(_rpc_result("0x64")))  # block number = 100

    evidence = await fetch_onchain_decimals(
        client, "ethereum", "0x7cf9a80db3b29ee8efe3710aadb7b95270572d47"
    )

    assert evidence.decimals == 6
    assert evidence.block_number == 100
    assert evidence.chain_id == 1


async def test_fetch_onchain_decimals_ordinary_contract_is_18() -> None:
    client = _FakeHTTPClient()
    rpc = "https://bsc-dataseed.binance.org/"
    client.queue_post(rpc, _FakeResponse(_rpc_result("0x" + "0" * 62 + "12")))  # decimals() = 18
    client.queue_post(rpc, _FakeResponse(_rpc_result("0x1")))

    evidence = await fetch_onchain_decimals(
        client, "bsc", "0x92aa03137385f18539301349dcfc9ebc923ffb10"
    )

    assert evidence.decimals == 18
    assert evidence.chain_id == 56


async def test_fetch_onchain_decimals_stored_per_chain_id_and_contract() -> None:
    """Two different (chain_id, contract) pairs for what a naive scheme might
    treat as "the same asset" must carry their own independent decimals --
    never inherited from one chain to another."""
    client = _FakeHTTPClient()
    client.queue_post(
        "https://ethereum-rpc.publicnode.com", _FakeResponse(_rpc_result("0x" + "0" * 63 + "6"))
    )
    client.queue_post("https://ethereum-rpc.publicnode.com", _FakeResponse(_rpc_result("0x1")))
    client.queue_post(
        "https://bsc-dataseed.binance.org/", _FakeResponse(_rpc_result("0x" + "0" * 62 + "12"))
    )
    client.queue_post("https://bsc-dataseed.binance.org/", _FakeResponse(_rpc_result("0x2")))

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


async def test_fetch_onchain_decimals_rejects_empty_hex_result() -> None:
    client = _FakeHTTPClient()
    rpc = "https://bsc-dataseed.binance.org/"
    client.queue_post(rpc, _FakeResponse(_rpc_result("0x")))  # contract has no code / no decimals()

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
    (ChainContractEvidence.decimals), never for unit conversion. If a future
    change adds such a function, it must not live in or be imported by this
    module without updating this test and the module docstring's "What raw
    means here" section to explain the new, deliberately separate contract."""
    import schurfer_analytics.source_lead_identity_evidence as module

    forbidden_names = {"scale_amount", "convert_amount", "apply_decimals", "normalize_amount"}
    assert not (forbidden_names & set(dir(module)))


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
    )


def _sample_bundle() -> EvidenceBundle:
    raw = RawFetch(
        source="test",
        endpoint="https://fake/",
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
        raw_sha256="b" * 64,
        wire_exact=True,
        payload={"ok": True},
    )
    return EvidenceBundle(
        evidence_version="source_lead_identity_evidence_v2",
        base="TEST",
        source_exchange="gate",
        target_exchange="binance",
        identity_class="exact_contract",
        source_contract=_sample_chain_evidence(),
        target_contract=_sample_chain_evidence(),
        gate_evidence=raw,
        target_catalog_evidence=raw,
        coingecko_evidence=raw,
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
    import dataclasses

    mutated = dataclasses.replace(bundle, source_contract=_sample_chain_evidence(decimals=6))
    assert compute_bundle_sha256(mutated) != original


def test_save_and_load_evidence_bundle_round_trips(tmp_path: Path) -> None:
    bundle = _sample_bundle()
    digest = compute_bundle_sha256(bundle)
    import dataclasses

    finalized = dataclasses.replace(bundle, bundle_sha256=digest)
    path = save_evidence_bundle(finalized, tmp_path / "test-gate-binance.json")

    loaded = load_evidence_bundle(path)

    assert loaded.base == "TEST"
    assert loaded.source_contract.decimals == 18
    assert loaded.bundle_sha256 == digest


def test_load_evidence_bundle_rejects_tampered_content(tmp_path: Path) -> None:
    """The actual integrity check missing from source_lead_qualification.py's
    registry loader (colleague review, 2026-08-28: it only validates
    evidence_sha256's string format, never its content) -- this loader must
    recompute and compare, not trust the stored hash."""
    bundle = _sample_bundle()
    digest = compute_bundle_sha256(bundle)
    import dataclasses

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


def test_load_all_evidence_bundles_returns_empty_for_missing_directory(tmp_path: Path) -> None:
    assert load_all_evidence_bundles(tmp_path / "nope") == ()


def test_load_all_evidence_bundles_is_deterministically_ordered(tmp_path: Path) -> None:
    for name in ("zzz", "aaa", "mmm"):
        bundle = dataclasses_replace_base(_sample_bundle(), name)
        digest = compute_bundle_sha256(bundle)
        import dataclasses

        finalized = dataclasses.replace(bundle, bundle_sha256=digest)
        save_evidence_bundle(finalized, tmp_path / f"{name}-gate-binance.json")

    bundles = load_all_evidence_bundles(tmp_path)
    assert [b.base for b in bundles] == ["AAA", "MMM", "ZZZ"]


def dataclasses_replace_base(bundle: EvidenceBundle, base: str) -> EvidenceBundle:
    import dataclasses

    return dataclasses.replace(bundle, base=base.upper())


# --- the real 2026-08-28 evidence set, if present -------------------------------


def test_captured_evidence_set_loads_and_verifies_if_present() -> None:
    """Integration-style check against whatever this branch actually shipped
    in evidence/source_lead/v2/ -- skipped (not failed) when the directory is
    absent, since evidence capture requires live network access this test
    suite must not depend on."""
    bundles = load_all_evidence_bundles()
    if not bundles:
        pytest.skip("no captured evidence bundles present on this checkout")
    nil = next((b for b in bundles if b.base == "NIL"), None)
    if nil is not None:
        assert nil.source_contract.decimals == 6


def test_candidates_table_has_no_duplicate_routes() -> None:
    routes = [(base, target) for base, target, *_ in CANDIDATES]
    assert len(routes) == len(set(routes))


# --- capture_bundle: full end-to-end flow against fakes, no real network ------


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
    for rpc in ("https://bsc-dataseed.binance.org/",):
        client.queue_post(
            rpc, _FakeResponse(_rpc_result("0x" + "0" * 62 + "12"))
        )  # source decimals=18
        client.queue_post(rpc, _FakeResponse(_rpc_result("0x64")))  # source block
        if with_alpha_entry:
            client.queue_post(
                rpc, _FakeResponse(_rpc_result("0x" + "0" * 62 + "12"))
            )  # target decimals=18
            client.queue_post(rpc, _FakeResponse(_rpc_result("0x65")))  # target block
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
    gate = _FakeExchange({"TUT": {"networks": {"BEP20": {}}}})
    alpha_catalog = await fetch_binance_alpha_catalog(client)

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
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert bundle.base == "TUT"
    assert bundle.source_contract.decimals == 18
    assert bundle.target_contract is not None
    assert bundle.target_contract.decimals == 18
    assert bundle.target_catalog_evidence is not None
    assert bundle.bundle_sha256 == compute_bundle_sha256(bundle)


async def test_capture_bundle_exact_contract_requires_alpha_entry() -> None:
    """A candidate classified exact_contract but with no matching catalog
    entry at capture time is a data problem, not something to silently
    downgrade -- capture_bundle must raise, not guess."""
    client = _queue_gate_style_client(with_alpha_entry=True)
    client.queue_get("https://www.binance.com/", _FakeResponse({"data": []}))
    gate = _FakeExchange({"TUT": {"networks": {"BEP20": {}}}})
    alpha_catalog = await fetch_binance_alpha_catalog(client)

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
    client.queue_post(
        "https://ethereum-rpc.publicnode.com", _FakeResponse(_rpc_result("0x" + "0" * 63 + "6"))
    )
    client.queue_post("https://ethereum-rpc.publicnode.com", _FakeResponse(_rpc_result("0x1")))
    client.queue_get("https://www.binance.com/", _FakeResponse({"data": []}))
    gate = _FakeExchange({"NIL": {"networks": {"ERC20": {}}}})
    alpha_catalog = await fetch_binance_alpha_catalog(client)

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
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert bundle.target_contract is None
    assert bundle.target_catalog_evidence is None
    assert bundle.source_contract.decimals == 6


def test_candidates_table_third_party_bridge_only_has_no_target_chain() -> None:
    for base, _target, _sc, _sca, target_chain, target_contract, _cg, identity_class in CANDIDATES:
        if identity_class == "third_party_bridge_only":
            assert target_chain is None, base
            assert target_contract is None, base
        else:
            assert target_chain is not None, base
            assert target_contract is not None, base
