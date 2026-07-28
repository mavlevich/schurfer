import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution import journal
from schurfer_performance import (
    LEGACY_ACCOUNTING_VERSION,
    PAPER_ACCOUNTING_VERSION,
)


def _mock_conn(fetchone_results: list[tuple | None]) -> MagicMock:
    """Build a mock psycopg AsyncConnection whose cursor().fetchone() returns
    successive values from fetchone_results, one per execute() call."""
    cur = AsyncMock()
    cur.execute = AsyncMock()
    cur.fetchone = AsyncMock(side_effect=fetchone_results)

    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=cur)
    cur_cm.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_cm)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn, cur


def _trade_row(
    *,
    status: str = "open",
    accounting_version: str = LEGACY_ACCOUNTING_VERSION,
    entry_slippage_bps: float | None = None,
    exit_slippage_bps: float | None = None,
    entry_at: datetime | None = None,
) -> tuple:
    return (
        100.0,
        100.0,
        "short",
        status,
        entry_at or datetime.now(tz=UTC),
        entry_slippage_bps,
        exit_slippage_bps,
        accounting_version,
    )


def test_paper_accounting_contract_uses_measured_two_sided_impact() -> None:
    version, status, entry_bps, exit_bps = journal.accounting_contract(
        {
            "paper": True,
            "market_quality": {
                "bid_impact_bps": 3.5,
                "ask_impact_bps": 4.5,
            },
        }
    )

    assert version == PAPER_ACCOUNTING_VERSION
    assert status == "pending"
    assert entry_bps == 3.5
    assert exit_bps == 4.5


def test_real_trade_keeps_legacy_accounting_until_fill_reconciliation_exists() -> None:
    version, status, _entry_bps, _exit_bps = journal.accounting_contract(
        {
            "paper": False,
            "market_quality": {
                "bid_impact_bps": 3.5,
                "ask_impact_bps": 4.5,
            },
        }
    )

    assert version == LEGACY_ACCOUNTING_VERSION
    assert status == "legacy"


async def test_close_trade_loads_entry_price_and_side_from_db() -> None:
    """Regression: entry_price/side used to be caller-supplied (sourced from a
    Redis cache that can be evicted). They're now loaded from the trade's own
    row by trade_id, so a close can always be recorded even if that cache is
    gone — Redis is a hint for the monitor loop, not the accounting source."""
    # SELECT size_usd, entry_price, side -> then UPDATE ... RETURNING id
    conn, cur = _mock_conn([_trade_row(), (1,)])

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        committed = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id="ord-1",
            exit_price=90.0,
            reason="test",
        )

    assert committed is True
    update_call = cur.execute.call_args_list[1]
    params = update_call.args[1]
    # short, entry 100 -> exit 90 = +10% move, on $100 size = $10 pnl_usd
    pnl_usd = params[10]
    assert pnl_usd == 10.0
    assert params[3] == 10.0  # explicit gross_pnl_usd
    assert params[5] is None  # legacy net is unknown
    assert params[13] == "legacy"


async def test_close_paper_trade_persists_versioned_net_accounting() -> None:
    entry_at = datetime(2026, 7, 28, 12, tzinfo=UTC)
    closed_at = entry_at + timedelta(hours=3)
    conn, cur = _mock_conn(
        [
            _trade_row(
                accounting_version=PAPER_ACCOUNTING_VERSION,
                entry_slippage_bps=3,
                exit_slippage_bps=4,
                entry_at=entry_at,
            ),
            (1,),
        ]
    )

    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)),
        patch("schurfer_execution.journal.datetime") as datetime_mock,
    ):
        datetime_mock.now.return_value = closed_at
        committed = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
        )

    assert committed is True
    params = cur.execute.call_args_list[1].args[1]
    assert params[3] == 10.0
    assert params[5] == pytest.approx(9.7112)
    assert params[7] == pytest.approx(0.2)
    assert params[8] == pytest.approx(0.0187)
    assert params[9] == pytest.approx(0.07)
    assert params[10] == params[5]
    assert params[13] == "complete"
    assert params[14] is None


async def test_close_paper_trade_withholds_net_when_slippage_is_missing() -> None:
    conn, cur = _mock_conn(
        [
            _trade_row(
                accounting_version=PAPER_ACCOUNTING_VERSION,
                entry_slippage_bps=None,
                exit_slippage_bps=4,
            ),
            (1,),
        ]
    )

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        committed = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
        )

    assert committed is True
    params = cur.execute.call_args_list[1].args[1]
    assert params[3] == 10.0
    assert params[5] is None
    assert params[10] is None
    assert params[13] == "incomplete"
    assert params[14] == "missing entry_slippage_bps"


async def test_close_trade_returns_false_on_db_error() -> None:
    """Regression: callers (monitor.py, paper.py) must only discard the Redis
    trade-id pointer when the close is durably committed — otherwise a DB
    outage at close time permanently loses that trade's realized PnL."""
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        committed = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id="ord-1",
            exit_price=90.0,
            reason="test",
        )

    assert committed is False


async def test_close_trade_missing_row_returns_false() -> None:
    """Regression: a trade_id with no matching row must return False, not
    silently proceed with size_usd=0 and report success — that would let a
    caller believe a close was recorded when nothing was written."""
    conn, cur = _mock_conn([None])  # SELECT returns no row

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        committed = await journal.close_trade(
            "postgresql://x",
            trade_id=999,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
        )

    assert committed is False
    cur.execute.assert_called_once()  # never attempted the UPDATE


async def test_close_trade_zero_rows_updated_returns_false() -> None:
    """Regression: RETURNING id with no row (e.g. deleted between SELECT and
    UPDATE) must be treated as a failed commit, not a silent success."""
    conn, _cur = _mock_conn([_trade_row(), None])  # UPDATE matches nothing

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        committed = await journal.close_trade(
            "postgresql://x",
            trade_id=999,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
        )

    assert committed is False


async def test_close_trade_already_closed_is_idempotent_success() -> None:
    """Regression (P1): a retry after an ambiguous commit (the transaction
    landed server-side but the connection dropped before the ack) must not
    re-run the UPDATE — that would overwrite exit_at with the retry's own
    timestamp, which can shift the trade into a different UTC day for
    realized_pnl_today() if the retry lands after midnight."""
    conn, cur = _mock_conn([_trade_row(status="closed")])  # already closed

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        committed = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id="ord-1",
            exit_price=90.0,
            reason="test",
        )

    assert committed is True
    cur.execute.assert_called_once()  # only the SELECT — no UPDATE attempted


async def test_realized_pnl_today_returns_summed_value() -> None:
    conn, cur = _mock_conn([(42.5,)])

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        result = await journal.realized_pnl_today("postgresql://x")

    assert result == 42.5
    query, params = cur.execute.call_args_list[0].args
    assert "app.trades" in query
    assert "paper" in query  # excludes paper trades
    start_of_day = params[0]
    assert isinstance(start_of_day, datetime)
    now = datetime.now(tz=UTC)
    assert start_of_day.date() == now.date()
    assert start_of_day.hour == 0 and start_of_day.minute == 0


async def test_realized_pnl_today_returns_none_on_db_error() -> None:
    """Regression: a DB error must be distinguishable from '$0 realized today' —
    returning 0.0 here would let a transient outage silently reset the daily
    loss circuit breaker."""
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        result = await journal.realized_pnl_today("postgresql://x")

    assert result is None


class TestTryCommitClose:
    async def test_success_clears_any_pending_marker(self) -> None:
        conn, _cur = _mock_conn([_trade_row(), (1,)])
        rdb = MagicMock()
        rdb.set = AsyncMock()
        rdb.delete = AsyncMock()

        with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
            committed = await journal.try_commit_close(
                "postgresql://x",
                rdb,
                exchange="bingx",
                base="BEAT",
                trade_id=1,
                exit_order_id="ord-1",
                exit_price=90.0,
                reason="test",
            )

        assert committed is True
        # Both the readiness lease (revoked unconditionally, up front) and
        # the pending-close marker (cleared on success) go through delete.
        rdb.delete.assert_any_call("risk:pnl_ready")
        rdb.delete.assert_any_call("journal:pending_close:bingx:BEAT:1")

    async def test_failure_writes_durable_pending_marker(self) -> None:
        """Regression: a failed commit must leave enough state to retry later
        — not just an ephemeral in-memory log line."""
        rdb = MagicMock()
        rdb.set = AsyncMock()
        rdb.delete = AsyncMock()

        with patch(
            "psycopg.AsyncConnection.connect",
            AsyncMock(side_effect=Exception("connection refused")),
        ):
            committed = await journal.try_commit_close(
                "postgresql://x",
                rdb,
                exchange="bingx",
                base="BEAT",
                trade_id=1,
                exit_order_id="ord-1",
                exit_price=90.0,
                reason="test",
            )

        assert committed is False
        rdb.delete.assert_any_call("risk:pnl_ready")
        rdb.set.assert_called_once()
        key, payload = rdb.set.call_args.args[:2]
        assert key == "journal:pending_close:bingx:BEAT:1"
        data = json.loads(payload)
        assert data["trade_id"] == 1
        assert data["exit_price"] == 90.0

    async def test_revokes_readiness_before_attempting_commit(self) -> None:
        """Regression (P0): revocation must happen unconditionally and first —
        not only after a failure is detected — since an existing lease is
        stale the instant a real close is confirmed, regardless of whether
        the journal write itself succeeds."""
        conn, _cur = _mock_conn([_trade_row(), (1,)])
        rdb = MagicMock()
        rdb.set = AsyncMock()
        calls: list[str] = []

        async def _delete(key: str) -> None:
            calls.append(key)

        rdb.delete = AsyncMock(side_effect=_delete)

        with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
            await journal.try_commit_close(
                "postgresql://x",
                rdb,
                exchange="bingx",
                base="BEAT",
                trade_id=1,
                exit_order_id="ord-1",
                exit_price=90.0,
                reason="test",
            )

        assert calls[0] == "risk:pnl_ready"


class TestRevokePnlReadiness:
    async def test_deletes_ready_key(self) -> None:
        rdb = MagicMock()
        rdb.delete = AsyncMock()

        await journal.revoke_pnl_readiness(rdb)

        rdb.delete.assert_called_once_with("risk:pnl_ready")


class TestWritePendingClose:
    async def test_revokes_readiness_on_its_own(self) -> None:
        """Regression (P0, race): write_pending_close revokes the lease itself
        rather than relying on the caller having already done so earlier —
        closes the window where a concurrent tracker tick republishes the
        lease between try_commit_close's upfront revoke and this call
        actually landing the pending marker (see tracker.py's own re-check
        for the other half of this fix)."""
        rdb = MagicMock()
        rdb.set = AsyncMock()
        rdb.delete = AsyncMock()

        await journal.write_pending_close(
            rdb,
            exchange="bingx",
            base="BEAT",
            trade_id=1,
            exit_order_id="ord-1",
            exit_price=90.0,
            reason="test",
        )

        rdb.set.assert_called_once()
        assert rdb.set.call_args.args[0] == "journal:pending_close:bingx:BEAT:1"
        rdb.delete.assert_called_once_with("risk:pnl_ready")


class TestPendingCloseKeyHelpers:
    def test_parse_pending_close_key(self) -> None:
        assert journal.parse_pending_close_key("journal:pending_close:bingx:BEAT:1") == (
            "bingx",
            "BEAT",
            1,
        )

    def test_parse_rejects_malformed_key(self) -> None:
        assert journal.parse_pending_close_key("garbage") is None
        assert journal.parse_pending_close_key("journal:pending_close:bingx:BEAT") is None

    def test_parse_rejects_non_integer_trade_id(self) -> None:
        assert journal.parse_pending_close_key("journal:pending_close:bingx:BEAT:abc") is None

    def test_key_pattern_matches_expected_prefix(self) -> None:
        assert journal.pending_close_key_pattern() == "journal:pending_close:*:*:*"

    def test_different_trades_get_different_pending_keys(self) -> None:
        """Regression: the pending-close key must be scoped per trade_id, not
        just exchange:base — otherwise a slow retry of an old close (e.g. a
        DB outage lasting days) could race with a brand new trade opened on
        the same symbol in the meantime."""
        assert journal._pending_close_key("bingx", "BEAT", 1) != journal._pending_close_key(
            "bingx", "BEAT", 2
        )


class TestDeleteTradeIdIfMatches:
    async def test_deletes_when_value_matches(self) -> None:
        rdb = MagicMock()
        rdb.eval = AsyncMock(return_value=1)

        result = await journal.delete_trade_id_if_matches(rdb, "trade:id:bingx:BEAT", 42)

        assert result is True
        rdb.eval.assert_called_once()

    async def test_does_not_delete_when_value_differs(self) -> None:
        """Regression: retrying an old close must not delete a newer trade's
        pointer just because it currently occupies the same exchange:base key."""
        rdb = MagicMock()
        rdb.eval = AsyncMock(return_value=0)

        result = await journal.delete_trade_id_if_matches(rdb, "trade:id:bingx:BEAT", 42)

        assert result is False


class TestAnyPendingCloses:
    async def test_true_when_a_pending_close_exists(self) -> None:
        async def _scan(match: str) -> object:  # type: ignore[type-arg]
            yield b"journal:pending_close:bingx:BEAT:1"

        rdb = MagicMock()
        rdb.scan_iter = _scan

        assert await journal.any_pending_closes(rdb) is True

    async def test_false_when_none_exist(self) -> None:
        async def _scan(match: str) -> object:  # type: ignore[type-arg]
            for key in ():  # empty async generator, no unreachable code
                yield key

        rdb = MagicMock()
        rdb.scan_iter = _scan

        assert await journal.any_pending_closes(rdb) is False
