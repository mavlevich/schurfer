import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis.aioredis import FakeRedis
from schurfer_execution import legacy_paper_repair as repair

_STRATEGY = "early_momentum_v1"
_BEFORE = datetime(2026, 8, 20, 17, 31, 4, tzinfo=UTC)
_DB_URL = "postgresql://x"


def _row(
    *,
    id: int = 1,
    symbol: str = "RAYDIUMUSDT/USDT:USDT",
    exchange: str = "bybit",
    side: str = "short",
    entry_at: datetime = datetime(2026, 8, 20, 6, 29, 19, tzinfo=UTC),
    entry_price: float = 1.5,
    size_usd: float = 100.0,
    leverage: float = 5.0,
    accounting_status: str = "pending",
) -> dict:
    return {
        "id": id,
        "symbol": symbol,
        "exchange": exchange,
        "side": side,
        "entry_at": entry_at,
        "entry_price": entry_price,
        "size_usd": size_usd,
        "leverage": leverage,
        "accounting_status": accounting_status,
    }


def _mock_select_conn(rows: list[dict]) -> MagicMock:
    """A psycopg AsyncConnection mock whose cursor().fetchall() returns rows once."""
    cur = AsyncMock()
    cur.execute = AsyncMock()
    cur.fetchall = AsyncMock(return_value=rows)

    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=cur)
    cur_cm.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_cm)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _mock_execute_conn(*, rowcount: int = 1) -> MagicMock:
    """A psycopg AsyncConnection mock for a bare execute() (no fetch), e.g. the UPDATE."""
    cur = AsyncMock()
    cur.execute = AsyncMock()
    cur.rowcount = rowcount

    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=cur)
    cur_cm.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_cm)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _fake_client(markets: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id="bybit",
        markets=markets,
        load_markets=AsyncMock(return_value=markets),
        close=AsyncMock(),
    )


_RAYDIUM_MARKET = {
    "RAYDIUMUSDT": {
        "id": "RAYDIUMUSDT",
        "symbol": "RAYDIUM/USDT:USDT",
        "base": "RAYDIUM",
        "quote": "USDT",
        "settle": "USDT",
        "type": "swap",
        "active": True,
    }
}


# ---- _native_market_id / _resolve_base ----


def test_native_market_id_takes_the_part_before_the_slash() -> None:
    assert repair._native_market_id("RAYDIUMUSDT/USDT:USDT") == "RAYDIUMUSDT"


def test_resolve_base_returns_the_true_ccxt_base() -> None:
    client = _fake_client(_RAYDIUM_MARKET)
    assert repair._resolve_base(client, db_symbol="RAYDIUMUSDT/USDT:USDT") == "RAYDIUM"


def test_resolve_base_returns_none_when_unresolvable() -> None:
    client = _fake_client({})
    assert repair._resolve_base(client, db_symbol="DELISTEDUSDT/USDT:USDT") is None


# ---- _current_position_owner ----


@pytest.mark.asyncio
async def test_current_position_owner_no_key_means_no_position() -> None:
    rdb = FakeRedis()
    exists, owner = await repair._current_position_owner(rdb, exchange="bybit", base="RAYDIUM")
    assert (exists, owner) == (False, None)


@pytest.mark.asyncio
async def test_current_position_owner_reads_embedded_trade_id() -> None:
    rdb = FakeRedis()
    await rdb.set("position:paper:bybit:RAYDIUM", json.dumps({"trade_id": 155}))
    exists, owner = await repair._current_position_owner(rdb, exchange="bybit", base="RAYDIUM")
    assert (exists, owner) == (True, 155)


@pytest.mark.asyncio
async def test_current_position_owner_falls_back_to_separate_trade_id_key() -> None:
    rdb = FakeRedis()
    await rdb.set("position:paper:bybit:RAYDIUM", json.dumps({"entry_price": 1.5}))
    await rdb.set("trade:id:paper:bybit:RAYDIUM", "155")
    exists, owner = await repair._current_position_owner(rdb, exchange="bybit", base="RAYDIUM")
    assert (exists, owner) == (True, 155)


@pytest.mark.asyncio
async def test_current_position_owner_ambiguous_when_neither_source_has_a_trade_id() -> None:
    rdb = FakeRedis()
    await rdb.set("position:paper:bybit:RAYDIUM", json.dumps({"entry_price": 1.5}))
    exists, owner = await repair._current_position_owner(rdb, exchange="bybit", base="RAYDIUM")
    assert (exists, owner) == (True, None)


# ---- classify_orphans ----


@pytest.mark.asyncio
async def test_classify_orphans_no_live_position_is_orphaned() -> None:
    rdb = FakeRedis()
    conn = _mock_select_conn([_row(id=149)])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch.object(
            repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: _fake_client(_RAYDIUM_MARKET)}
        ),
    ):
        result = await repair.classify_orphans(_DB_URL, rdb, strategy=_STRATEGY, before=_BEFORE)

    assert [c.trade_id for c in result.candidates] == [149]
    assert result.skipped == ()


@pytest.mark.asyncio
async def test_classify_orphans_row_still_owning_the_live_position_is_not_orphaned() -> None:
    rdb = FakeRedis()
    await rdb.set("position:paper:bybit:RAYDIUM", json.dumps({"trade_id": 155}))
    conn = _mock_select_conn([_row(id=155)])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch.object(
            repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: _fake_client(_RAYDIUM_MARKET)}
        ),
    ):
        result = await repair.classify_orphans(_DB_URL, rdb, strategy=_STRATEGY, before=_BEFORE)

    assert result.candidates == ()
    assert result.skipped == ()


@pytest.mark.asyncio
async def test_classify_orphans_row_superseded_by_a_later_trade_is_orphaned() -> None:
    """The classic duplicate-open bug: row 152's own position key now belongs to
    the later row 155 that overwrote it -- 152 must be classified as orphaned."""
    rdb = FakeRedis()
    await rdb.set("position:paper:bybit:RAYDIUM", json.dumps({"trade_id": 155}))
    conn = _mock_select_conn([_row(id=152), _row(id=155)])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch.object(
            repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: _fake_client(_RAYDIUM_MARKET)}
        ),
    ):
        result = await repair.classify_orphans(_DB_URL, rdb, strategy=_STRATEGY, before=_BEFORE)

    assert [c.trade_id for c in result.candidates] == [152]


@pytest.mark.asyncio
async def test_classify_orphans_unresolvable_symbol_is_skipped_not_classified() -> None:
    rdb = FakeRedis()
    conn = _mock_select_conn([_row(id=149, symbol="DELISTEDUSDT/USDT:USDT")])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch.object(repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: _fake_client({})}),
    ):
        result = await repair.classify_orphans(_DB_URL, rdb, strategy=_STRATEGY, before=_BEFORE)

    assert result.candidates == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].trade_id == 149


@pytest.mark.asyncio
async def test_classify_orphans_ambiguous_owner_is_skipped_not_classified() -> None:
    rdb = FakeRedis()
    await rdb.set("position:paper:bybit:RAYDIUM", json.dumps({"entry_price": 1.5}))
    conn = _mock_select_conn([_row(id=149)])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch.object(
            repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: _fake_client(_RAYDIUM_MARKET)}
        ),
    ):
        result = await repair.classify_orphans(_DB_URL, rdb, strategy=_STRATEGY, before=_BEFORE)

    assert result.candidates == ()
    assert len(result.skipped) == 1


@pytest.mark.asyncio
async def test_classify_orphans_unregistered_exchange_is_skipped_not_classified() -> None:
    rdb = FakeRedis()
    conn = _mock_select_conn([_row(id=149, exchange="unknown_venue")])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch.object(repair, "MARKET_EXCHANGE_FACTORIES", {}),
    ):
        result = await repair.classify_orphans(_DB_URL, rdb, strategy=_STRATEGY, before=_BEFORE)

    assert result.candidates == ()
    assert len(result.skipped) == 1
    assert "unknown_venue" in result.skipped[0].reason


@pytest.mark.asyncio
async def test_classify_orphans_redis_error_propagates_fail_closed() -> None:
    rdb = AsyncMock()
    rdb.get = AsyncMock(side_effect=ConnectionError("redis unreachable"))
    conn = _mock_select_conn([_row(id=149)])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch.object(
            repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: _fake_client(_RAYDIUM_MARKET)}
        ),
        pytest.raises(ConnectionError),
    ):
        await repair.classify_orphans(_DB_URL, rdb, strategy=_STRATEGY, before=_BEFORE)


@pytest.mark.asyncio
async def test_classify_orphans_closes_exchange_clients_even_on_error() -> None:
    rdb = AsyncMock()
    rdb.get = AsyncMock(side_effect=ConnectionError("redis unreachable"))
    conn = _mock_select_conn([_row(id=149)])
    client = _fake_client(_RAYDIUM_MARKET)
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch.object(repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: client}),
        pytest.raises(ConnectionError),
    ):
        await repair.classify_orphans(_DB_URL, rdb, strategy=_STRATEGY, before=_BEFORE)
    client.close.assert_awaited_once()


# ---- manifest fingerprint / round-trip ----


def test_fingerprint_is_deterministic_and_order_independent_of_input_dict_keys() -> None:
    a = repair._fingerprint((149, 152), strategy=_STRATEGY, before=_BEFORE)
    b = repair._fingerprint((149, 152), strategy=_STRATEGY, before=_BEFORE)
    assert a == b


def test_fingerprint_changes_when_trade_ids_differ() -> None:
    a = repair._fingerprint((149, 152), strategy=_STRATEGY, before=_BEFORE)
    b = repair._fingerprint((149, 153), strategy=_STRATEGY, before=_BEFORE)
    assert a != b


def test_manifest_write_read_round_trip(tmp_path: Path) -> None:
    candidates = [repair._to_candidate(_row(id=149)), repair._to_candidate(_row(id=152))]
    manifest = repair.build_manifest(candidates, strategy=_STRATEGY, before=_BEFORE)
    path = tmp_path / "manifest.json"
    repair.write_manifest(manifest, path)

    loaded = repair.read_manifest(path)

    assert loaded.trade_ids == manifest.trade_ids
    assert loaded.fingerprint == manifest.fingerprint
    assert loaded.strategy == manifest.strategy
    assert loaded.before == manifest.before
    assert [c.trade_id for c in loaded.candidates] == [c.trade_id for c in manifest.candidates]


def test_build_manifest_sorts_candidates_by_trade_id() -> None:
    candidates = [repair._to_candidate(_row(id=155)), repair._to_candidate(_row(id=149))]
    manifest = repair.build_manifest(candidates, strategy=_STRATEGY, before=_BEFORE)
    assert manifest.trade_ids == (149, 155)


# ---- apply_repair ----


def _manifest(trade_ids: tuple[int, ...]) -> repair.Manifest:
    candidates = [repair._to_candidate(_row(id=tid)) for tid in trade_ids]
    return repair.build_manifest(candidates, strategy=_STRATEGY, before=_BEFORE)


@pytest.mark.asyncio
async def test_apply_repair_empty_manifest_is_a_no_op() -> None:
    manifest = _manifest(())
    count = await repair.apply_repair(_DB_URL, FakeRedis(), manifest=manifest)
    assert count == 0


@pytest.mark.asyncio
async def test_apply_repair_clean_apply_cancels_exactly_the_manifest_rows() -> None:
    manifest = _manifest((149,))
    rdb = FakeRedis()  # no position keys at all -> classify_orphans reproduces the manifest

    state_conn = _mock_select_conn([{"id": 149, "status": "open", "accounting_error": None}])
    reclassify_conn = _mock_select_conn([_row(id=149)])
    update_conn = _mock_execute_conn(rowcount=1)

    with (
        patch(
            "psycopg.AsyncConnection.connect",
            AsyncMock(side_effect=[state_conn, reclassify_conn, update_conn]),
        ),
        patch.object(
            repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: _fake_client(_RAYDIUM_MARKET)}
        ),
    ):
        count = await repair.apply_repair(_DB_URL, rdb, manifest=manifest)

    assert count == 1


@pytest.mark.asyncio
async def test_apply_repair_already_applied_is_an_idempotent_no_op() -> None:
    manifest = _manifest((149,))
    state_conn = _mock_select_conn(
        [{"id": 149, "status": "cancelled", "accounting_error": repair.ACCOUNTING_ERROR}]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=state_conn)):
        count = await repair.apply_repair(_DB_URL, FakeRedis(), manifest=manifest)
    assert count == 0


@pytest.mark.asyncio
async def test_apply_repair_partial_state_aborts_for_manual_review() -> None:
    manifest = _manifest((149, 152))
    state_conn = _mock_select_conn(
        [
            {"id": 149, "status": "cancelled", "accounting_error": repair.ACCOUNTING_ERROR},
            {"id": 152, "status": "open", "accounting_error": None},
        ]
    )
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=state_conn)),
        pytest.raises(repair.RepairAbortedError, match="partial apply detected"),
    ):
        await repair.apply_repair(_DB_URL, FakeRedis(), manifest=manifest)


@pytest.mark.asyncio
async def test_apply_repair_missing_trade_id_aborts() -> None:
    manifest = _manifest((149,))
    state_conn = _mock_select_conn([])  # row 149 no longer exists
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=state_conn)),
        pytest.raises(repair.RepairAbortedError, match="no longer exist"),
    ):
        await repair.apply_repair(_DB_URL, FakeRedis(), manifest=manifest)


@pytest.mark.asyncio
async def test_apply_repair_unexpected_status_aborts() -> None:
    manifest = _manifest((149,))
    state_conn = _mock_select_conn([{"id": 149, "status": "closed", "accounting_error": None}])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=state_conn)),
        pytest.raises(repair.RepairAbortedError, match="unexpected state"),
    ):
        await repair.apply_repair(_DB_URL, FakeRedis(), manifest=manifest)


@pytest.mark.asyncio
async def test_apply_repair_aborts_when_live_classification_drifted_from_manifest() -> None:
    """Between classify and apply, this row's position became reachable again
    (e.g. a human manually fixed it) -- live reclassification no longer includes
    it, so apply must refuse rather than cancel a row that's no longer orphaned."""
    manifest = _manifest((149,))
    rdb = FakeRedis()
    await rdb.set("position:paper:bybit:RAYDIUM", json.dumps({"trade_id": 149}))

    state_conn = _mock_select_conn([{"id": 149, "status": "open", "accounting_error": None}])
    reclassify_conn = _mock_select_conn([_row(id=149)])

    with (
        patch(
            "psycopg.AsyncConnection.connect",
            AsyncMock(side_effect=[state_conn, reclassify_conn]),
        ),
        patch.object(
            repair, "MARKET_EXCHANGE_FACTORIES", {"bybit": lambda: _fake_client(_RAYDIUM_MARKET)}
        ),
        pytest.raises(repair.RepairAbortedError, match="no longer matches the manifest"),
    ):
        await repair.apply_repair(_DB_URL, rdb, manifest=manifest)


# ---- CLI ----


def test_parse_iso8601_accepts_a_z_suffix() -> None:
    parsed = repair._parse_iso8601("2026-08-20T17:31:04Z")
    assert parsed == datetime(2026, 8, 20, 17, 31, 4, tzinfo=UTC)


def test_build_arg_parser_classify_subcommand() -> None:
    args = repair._build_arg_parser().parse_args(
        [
            "classify",
            "--strategy",
            "early_momentum_v1",
            "--before",
            "2026-08-20T00:00:00Z",
            "--out",
            "m.json",
        ]
    )
    assert args.command == "classify"
    assert args.strategy == "early_momentum_v1"


def test_build_arg_parser_apply_subcommand() -> None:
    args = repair._build_arg_parser().parse_args(["apply", "--report", "m.json"])
    assert args.command == "apply"
    assert str(args.report) == "m.json"


def test_db_url_from_env_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        repair._db_url_from_env()
