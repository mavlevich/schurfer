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
    entry_at: datetime | None = None,
    saved_gross_pnl_usd: float | None = None,
    saved_gross_pnl_pct: float | None = None,
    saved_net_pnl_usd: float | None = None,
    saved_net_pnl_pct: float | None = None,
    saved_fees_usd: float | None = None,
    saved_funding_usd: float | None = None,
    saved_slippage_usd: float | None = None,
    saved_accounting_status: str | None = None,
) -> tuple:
    # No exit_slippage_bps column: close_trade never reads the row's
    # entry-time proxy for exit accounting -- only a fresh
    # fresh_exit_slippage_bps passed in by the caller (see
    # test_close_paper_trade_persists_versioned_net_accounting). The
    # saved_* fields are only meaningful when status="closed" -- the
    # already-closed retry path reads them back verbatim.
    return (
        100.0,
        100.0,
        "short",
        status,
        entry_at or datetime.now(tz=UTC),
        entry_slippage_bps,
        accounting_version,
        saved_gross_pnl_usd,
        saved_gross_pnl_pct,
        saved_net_pnl_usd,
        saved_net_pnl_pct,
        saved_fees_usd,
        saved_funding_usd,
        saved_slippage_usd,
        saved_accounting_status,
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


def test_paper_accounting_contract_defaults_to_short_bid_entry_ask_exit() -> None:
    # pump-short's existing behavior, unchanged: entry sells (bid), exit
    # buys (ask).
    _version, _status, entry_bps, exit_bps = journal.accounting_contract(
        {
            "paper": True,
            "market_quality": {"bid_impact_bps": 3.5, "ask_impact_bps": 4.5},
        },
        side="short",
    )
    assert entry_bps == 3.5
    assert exit_bps == 4.5


def test_paper_accounting_contract_long_entry_reads_ask_exit_reads_bid() -> None:
    # early_momentum/liquidation_cascade: LONG entry buys (ask), exit sells
    # (bid) -- the inverse of short.
    _version, _status, entry_bps, exit_bps = journal.accounting_contract(
        {
            "paper": True,
            "market_quality": {"bid_impact_bps": 3.5, "ask_impact_bps": 4.5},
        },
        side="long",
    )
    assert entry_bps == 4.5
    assert exit_bps == 3.5


def test_paper_accounting_contract_zeroes_entry_slippage_when_price_already_includes_it() -> None:
    # Regression (colleague review): when the entry price itself is a VWAP
    # already walked across the book (early_momentum.py), charging
    # market_quality's entry-side impact_bps again would double count the
    # same cost -- once implicitly via the VWAP-adjusted entry price, once
    # explicitly via slippage_bps. 0.0 (not None) so accounting still
    # reaches "complete".
    _version, _status, entry_bps, exit_bps = journal.accounting_contract(
        {
            "paper": True,
            "entry_price_includes_impact": True,
            "market_quality": {"bid_impact_bps": 3.5, "ask_impact_bps": 4.5},
        },
        side="long",
    )
    assert entry_bps == 0.0
    # The exit leg is untouched by this flag -- it's overridden by
    # close_trade's own fresh_exit_slippage_bps at close time regardless.
    assert exit_bps == 3.5


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
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id="ord-1",
            exit_price=90.0,
            reason="test",
        )

    assert outcome.committed is True
    update_call = cur.execute.call_args_list[1]
    params = update_call.args[1]
    # short, entry 100 -> exit 90 = +10% move, on $100 size = $10 pnl_usd
    pnl_usd = params[11]
    assert pnl_usd == 10.0
    assert params[3] == 10.0  # explicit gross_pnl_usd
    assert params[5] is None  # legacy net is unknown
    assert params[14] == "legacy"


async def test_close_paper_trade_persists_versioned_net_accounting() -> None:
    entry_at = datetime(2026, 7, 28, 12, tzinfo=UTC)
    closed_at = entry_at + timedelta(hours=3)
    conn, cur = _mock_conn(
        [
            _trade_row(
                accounting_version=PAPER_ACCOUNTING_VERSION,
                entry_slippage_bps=3,
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
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
            # Fresh exit-time reading -- not sourced from the row, which no
            # longer even stores an exit_slippage_bps column.
            fresh_exit_slippage_bps=4,
        )

    assert outcome.committed is True
    params = cur.execute.call_args_list[1].args[1]
    assert params[3] == 10.0
    assert params[5] == pytest.approx(9.7112)
    assert params[7] == pytest.approx(0.2)
    assert params[8] == pytest.approx(0.0187)
    assert params[9] == pytest.approx(0.07)
    # exit_slippage_bps persisted verbatim -- the gap the colleague found in
    # PR1: this column used to stay frozen at its stale entry-time value.
    assert params[10] == 4
    assert params[11] == params[5]
    assert params[14] == "complete"
    assert params[15] is None
    assert outcome.net_pnl_pct == pytest.approx(9.7112)
    assert outcome.accounting_status == "complete"


async def test_close_trade_does_not_double_count_impact_already_inside_vwap_prices() -> None:
    """Regression (colleague review): when both entry_price and exit_price
    are themselves executed VWAPs (the cost of crossing the book is already
    inside the price move), entry_slippage_bps=0.0 and
    fresh_exit_slippage_bps=0.0 (as accounting_contract/paper.py now
    produce for that case) must mean the net PnL only sheds fees and
    funding -- no separate slippage_usd on top of a cost that's already
    reflected in the raw price difference."""
    conn, cur = _mock_conn(
        [
            _trade_row(accounting_version=PAPER_ACCOUNTING_VERSION, entry_slippage_bps=0.0),
            (1,),
        ]
    )

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
            fresh_exit_slippage_bps=0.0,
        )

    assert outcome.accounting_status == "complete"
    assert outcome.slippage_usd == 0.0
    params = cur.execute.call_args_list[1].args[1]
    # gross_pnl_usd (index 3) minus only fees_usd (7) and funding_usd (8)
    # equals net_pnl_usd (5) -- nothing else was subtracted.
    gross_pnl_usd, net_pnl_usd, fees_usd, funding_usd = (
        params[3],
        params[5],
        params[7],
        params[8],
    )
    assert net_pnl_usd == pytest.approx(gross_pnl_usd - fees_usd - funding_usd)


async def test_close_trade_uses_the_fresh_exit_reading_not_a_stale_one() -> None:
    """Regression: exit accounting must reflect the order book actually
    observed at close time, not whatever the entry-time snapshot's opposite
    side happened to look like. Same row, same exit_price, two different
    fresh_exit_slippage_bps readings must produce two different net results."""

    async def _close(fresh_exit_slippage_bps: float) -> journal.CloseOutcome:
        conn, _cur = _mock_conn(
            [
                _trade_row(accounting_version=PAPER_ACCOUNTING_VERSION, entry_slippage_bps=3),
                (1,),
            ]
        )
        with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
            return await journal.close_trade(
                "postgresql://x",
                trade_id=1,
                exit_order_id=None,
                exit_price=90.0,
                reason="test",
                fresh_exit_slippage_bps=fresh_exit_slippage_bps,
            )

    tight_book = await _close(2.0)
    wide_book = await _close(50.0)

    assert tight_book.accounting_status == "complete"
    assert wide_book.accounting_status == "complete"
    assert tight_book.net_pnl_pct is not None
    assert wide_book.net_pnl_pct is not None
    assert wide_book.net_pnl_pct < tight_book.net_pnl_pct


async def test_close_paper_trade_withholds_net_when_entry_slippage_is_missing() -> None:
    conn, cur = _mock_conn(
        [
            _trade_row(
                accounting_version=PAPER_ACCOUNTING_VERSION,
                entry_slippage_bps=None,
            ),
            (1,),
        ]
    )

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
            fresh_exit_slippage_bps=4,
        )

    assert outcome.committed is True
    params = cur.execute.call_args_list[1].args[1]
    assert params[3] == 10.0
    assert params[5] is None
    assert params[10] == 4  # exit_slippage_bps still persisted even though net accounting failed
    assert params[11] is None
    assert params[14] == "incomplete"
    assert params[15] == "missing entry_slippage_bps"
    assert outcome.net_pnl_usd is None
    assert outcome.accounting_status == "incomplete"


async def test_close_paper_trade_withholds_net_when_exit_capture_failed() -> None:
    """A failed/insufficient-depth exit-book capture must leave net PnL
    unresolved, never silently fall back to a stale entry-time proxy."""
    conn, cur = _mock_conn(
        [
            _trade_row(
                accounting_version=PAPER_ACCOUNTING_VERSION,
                entry_slippage_bps=3,
            ),
            (1,),
        ]
    )

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
            fresh_exit_slippage_bps=None,
        )

    assert outcome.committed is True
    params = cur.execute.call_args_list[1].args[1]
    assert params[10] is None  # exit_slippage_bps: nothing to persist, capture failed
    assert params[14] == "incomplete"
    assert params[15] == "missing exit_slippage_bps"
    assert outcome.net_pnl_usd is None
    assert outcome.accounting_status == "incomplete"


async def test_close_trade_writes_exit_observation_atomically_with_the_close() -> None:
    """Regression: the exit-liquidity evidence row must land in the SAME
    transaction as the trades UPDATE (same connection/cursor), not a
    separate best-effort call afterward that could succeed or fail
    independently of the close itself."""
    conn, cur = _mock_conn([_trade_row(), (1,)])
    observation = {
        "observed_at": datetime.now(tz=UTC),
        "exchange": "bybit",
        "symbol": "BEAT/USDT:USDT",
        "status": "sampled",
        "requested_notional_usd": 50,
        "latency_ms": 12,
    }

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
            exit_observation=observation,
        )

    assert outcome.committed is True
    # SELECT, UPDATE, INSERT -- all three on the one cursor/connection that
    # close_trade opened, so they commit or roll back together.
    assert cur.execute.call_count == 3
    insert_query, insert_params = cur.execute.call_args_list[2].args
    assert "trade_exit_liquidity_observations" in insert_query
    assert insert_params[0] == 1


async def test_close_trade_omits_exit_observation_insert_when_none_given() -> None:
    conn, cur = _mock_conn([_trade_row(), (1,)])

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
        )

    assert outcome.committed is True
    assert cur.execute.call_count == 2  # SELECT + UPDATE only


async def test_close_trade_returns_false_on_db_error() -> None:
    """Regression: callers (monitor.py, paper.py) must only discard the Redis
    trade-id pointer when the close is durably committed — otherwise a DB
    outage at close time permanently loses that trade's realized PnL."""
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id="ord-1",
            exit_price=90.0,
            reason="test",
        )

    assert outcome.committed is False


async def test_close_trade_missing_row_returns_false() -> None:
    """Regression: a trade_id with no matching row must return False, not
    silently proceed with size_usd=0 and report success — that would let a
    caller believe a close was recorded when nothing was written."""
    conn, cur = _mock_conn([None])  # SELECT returns no row

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=999,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
        )

    assert outcome.committed is False
    cur.execute.assert_called_once()  # never attempted the UPDATE


async def test_close_trade_zero_rows_updated_returns_false() -> None:
    """Regression: RETURNING id with no row (e.g. deleted between SELECT and
    UPDATE) must be treated as a failed commit, not a silent success."""
    conn, _cur = _mock_conn([_trade_row(), None])  # UPDATE matches nothing

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=999,
            exit_order_id=None,
            exit_price=90.0,
            reason="test",
        )

    assert outcome.committed is False


async def test_close_trade_already_closed_is_idempotent_success() -> None:
    """Regression (P1): a retry after an ambiguous commit (the transaction
    landed server-side but the connection dropped before the ack) must not
    re-run the UPDATE — that would overwrite exit_at with the retry's own
    timestamp, which can shift the trade into a different UTC day for
    realized_pnl_today() if the retry lands after midnight."""
    conn, cur = _mock_conn([_trade_row(status="closed")])  # already closed

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.close_trade(
            "postgresql://x",
            trade_id=1,
            exit_order_id="ord-1",
            exit_price=90.0,
            reason="test",
        )

    assert outcome.committed is True
    cur.execute.assert_called_once()  # only the SELECT — no UPDATE attempted


async def test_record_exit_liquidity_is_append_once() -> None:
    conn, cur = _mock_conn([])
    observed_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    observation = {
        "observed_at": observed_at,
        "exchange": "bybit",
        "symbol": "BEAT/USDT:USDT",
        "market_id": "BEATUSDT",
        "status": "sampled",
        "requested_notional_usd": 50,
        "filled_notional_usd": 50,
        "best_bid": 99.9,
        "best_ask": 100.1,
        "mid": 100,
        "spread_bps": 20,
        "bid_vwap": 99.9,
        "bid_impact_bps": 8,
        "ask_vwap": 100.1,
        "ask_impact_bps": 10,
        "contract_size": 1,
        "latency_ms": 25,
        "error": None,
    }

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        recorded = await journal.record_exit_liquidity(
            "postgresql://x",
            trade_id=7,
            observation=observation,
        )

    assert recorded is True
    query, params = cur.execute.call_args.args
    assert "ON CONFLICT (trade_id) DO NOTHING" in query
    assert params[0] == 7
    assert params[1] == observed_at
    assert params[5] == "sampled"
    assert params[13] == 8  # bid_impact_bps
    assert params[15] == 10  # ask_impact_bps


async def test_record_exit_liquidity_failure_is_non_throwing() -> None:
    with patch(
        "psycopg.AsyncConnection.connect",
        AsyncMock(side_effect=Exception("connection refused")),
    ):
        recorded = await journal.record_exit_liquidity(
            "postgresql://x",
            trade_id=7,
            observation={
                "observed_at": datetime.now(tz=UTC),
                "exchange": "bybit",
                "symbol": "BEAT/USDT:USDT",
                "status": "fetch_failed",
                "requested_notional_usd": 50,
                "latency_ms": 25,
                "error": "venue unavailable",
            },
        )

    assert recorded is False


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


async def test_find_open_trade_id_returns_matching_row() -> None:
    conn, cur = _mock_conn([(77,)])

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        result = await journal.find_open_trade_id(
            "postgresql://x",
            exchange="bybit",
            symbol="BEAT/USDT:USDT",
        )

    assert result == 77
    query, params = cur.execute.call_args_list[0].args
    assert "status = 'open'" in query
    assert params == ("bybit", "BEAT/USDT:USDT")


async def test_find_open_trade_id_returns_none_when_no_open_trade() -> None:
    conn, _cur = _mock_conn([None])

    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        result = await journal.find_open_trade_id(
            "postgresql://x",
            exchange="bybit",
            symbol="BEAT/USDT:USDT",
        )

    assert result is None


async def test_find_open_trade_id_returns_none_on_db_error() -> None:
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        result = await journal.find_open_trade_id(
            "postgresql://x",
            exchange="bybit",
            symbol="BEAT/USDT:USDT",
        )

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


@pytest.mark.asyncio
async def test_open_trade_side_validation() -> None:
    with pytest.raises(ValueError, match="invalid side: middle"):
        await journal.open_trade(
            "dummy_url",
            symbol="BTC/USDT:USDT",
            exchange="bybit",
            side="middle",  # invalid
            order_id="123",
            size_usd=100.0,
            leverage=1,
            entry_price=50000.0,
            setup_context={},
        )


@pytest.mark.asyncio
@patch("psycopg.AsyncConnection.connect")
async def test_open_trade_early_momentum_v1_registration(mock_connect) -> None:
    conn, cur = _mock_conn([(1,), (123,)])  # strategy_id=1, trade_id=123
    mock_connect.return_value = conn

    trade_id = await journal.open_trade(
        "dummy_url",
        symbol="BTC/USDT:USDT",
        exchange="bybit",
        side="long",
        order_id="123",
        size_usd=100.0,
        leverage=1,
        entry_price=50000.0,
        setup_context={"strategy": "early_momentum_v1"},
    )
    assert trade_id == 123

    # Check that it extracted 'early_momentum' and '1'
    upsert_call = cur.execute.call_args_list[0]
    assert upsert_call[0][1][0] == "early_momentum"
    assert upsert_call[0][1][1] == "1"

    # Check that LONG was passed to the 4th SQL placeholder
    insert_call = cur.execute.call_args_list[1]
    # placeholders: strategy_id, symbol, exchange, side, entry_order_id, ...
    assert insert_call[0][1][3] == "long"


@pytest.mark.asyncio
@patch("psycopg.AsyncConnection.connect")
async def test_open_trade_pump_caller_preserves_context(mock_connect) -> None:
    conn, cur = _mock_conn([(1,), (123,)])
    mock_connect.return_value = conn

    # Pump caller passes strategy_version directly and market_quality
    setup = {
        "pump_pct": 5.0,
        "strategy_version": "pump_short_v1_market_quality",
        "market_quality": {"bid_impact_bps": 1.5, "ask_impact_bps": 2.0},
    }

    await journal.open_trade(
        "dummy_url",
        symbol="ETH/USDT:USDT",
        exchange="binance",
        side="short",
        order_id="456",
        size_usd=50.0,
        leverage=1,
        entry_price=2000.0,
        setup_context=setup,
    )

    upsert_call = cur.execute.call_args_list[0]
    assert upsert_call[0][1][0] == "pump_short"
    assert upsert_call[0][1][1] == "1_market_quality"

    insert_call = cur.execute.call_args_list[1]
    assert insert_call[0][1][3] == "short"

    # The setup_context JSON is the 13th parameter (index 12)
    saved_context = json.loads(insert_call[0][1][13])
    assert saved_context["pump_pct"] == 5.0
    assert saved_context["market_quality"]["bid_impact_bps"] == 1.5


@pytest.mark.asyncio
@patch("psycopg.AsyncConnection.connect")
async def test_open_trade_strategy_version_exceeds_limit(mock_connect) -> None:
    with pytest.raises(ValueError, match="strategy_version exceeds 16 chars"):
        await journal.open_trade(
            "dummy",
            symbol="BTC/USDT:USDT",
            exchange="bybit",
            side="long",
            order_id="1",
            size_usd=1.0,
            leverage=1,
            entry_price=1.0,
            setup_context={"strategy_version": "12345678901234567"},  # 17 chars
        )
    mock_connect.assert_not_awaited()


# --- open_trade_for_episode ---


def _episode_claim_row(
    status: str = "claimed", *, lease_fresh: bool = True, window_fresh: bool = True
) -> tuple:
    return (status, lease_fresh, window_fresh)


async def test_open_trade_for_episode_aborts_on_invalid_claim() -> None:
    """A reclaimed/expired claim must abort before any trade row is ever
    written -- not insert first and discover the mismatch afterward."""
    conn, cur = _mock_conn([None])  # SELECT ... FOR UPDATE finds no matching row
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.open_trade_for_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key="e1:entry:base",
            setup_context={"strategy": "early_momentum_v3"},
        )
    assert outcome.claim_valid is False
    assert outcome.trade_id is None
    assert outcome.created is False
    # Only the claim SELECT ran -- no strategy upsert, no trade insert.
    cur.execute.assert_called_once()


async def test_open_trade_for_episode_aborts_when_lease_expired_at_commit_time() -> None:
    """Regression: a slow quote/liquidity check can eat the whole claim
    lease between claim_episode() and this commit. Even though the row is
    still 'claimed' with this exact claim_token, a lapsed lease (or a
    lapsed overall episode window) at the moment of commit must abort --
    someone else may have already reclaimed it, or reap_overdue may have
    already decided to terminate it."""
    conn, cur = _mock_conn([_episode_claim_row("claimed", lease_fresh=False, window_fresh=True)])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.open_trade_for_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key="e1:entry:base",
            setup_context={"strategy": "early_momentum_v3"},
        )
    assert outcome.claim_valid is False
    assert outcome.trade_id is None
    cur.execute.assert_called_once()


async def test_open_trade_for_episode_aborts_when_episode_window_expired_at_commit_time() -> None:
    """Same as above, for the other half of the freshness check: the lease
    itself is still fresh but the episode's own expires_at window ran out
    underneath it."""
    conn, cur = _mock_conn([_episode_claim_row("claimed", lease_fresh=True, window_fresh=False)])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.open_trade_for_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key="e1:entry:base",
            setup_context={"strategy": "early_momentum_v3"},
        )
    assert outcome.claim_valid is False
    assert outcome.trade_id is None
    cur.execute.assert_called_once()


async def test_open_trade_for_episode_creates_trade_and_opens_episode() -> None:
    conn, cur = _mock_conn(
        [
            _episode_claim_row("claimed"),  # SELECT ... FOR UPDATE
            (7,),  # strategy upsert
            (42,),  # trade insert RETURNING id
        ]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.open_trade_for_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key="e1:entry:base",
            setup_context={"strategy": "early_momentum_v3"},
        )
    assert outcome == journal.OpenTradeOutcome(
        trade_id=42, created=True, recovered=False, claim_valid=True
    )
    # SELECT claim, UPSERT strategy, INSERT trade, UPDATE episode -- one
    # connection, one transaction, four statements.
    assert cur.execute.call_count == 4
    mark_opened_call = cur.execute.call_args_list[3]
    assert "SET status = 'opened'" in mark_opened_call.args[0]
    assert mark_opened_call.args[1] == ("e1", "tok-1")


async def test_open_trade_for_episode_retry_after_own_commit_recovers() -> None:
    """Regression: a retry with the SAME claim_token after this exact claim's
    own attempt already committed (episode status is now 'opened', not
    'claimed') must still recover the trade via entry_idempotency_key, not
    get rejected as an invalid claim."""
    conn, _cur = _mock_conn(
        [
            _episode_claim_row("opened"),
            (7,),
            None,  # INSERT ... ON CONFLICT DO NOTHING -> no row, already exists
            (42, "e1", 7, "BEAT/USDT:USDT"),
        ]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.open_trade_for_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key="e1:entry:base",
            setup_context={"strategy": "early_momentum_v3"},
        )
    assert outcome == journal.OpenTradeOutcome(
        trade_id=42, created=False, recovered=True, claim_valid=True
    )


async def test_open_trade_for_episode_recovers_existing_trade_on_retry() -> None:
    """A retry after a restart must return the SAME trade_id, not create a
    second trade or silently drop the episode-opened transition."""
    conn, _cur = _mock_conn(
        [
            _episode_claim_row("claimed"),
            (7,),  # strategy upsert
            None,  # INSERT ... ON CONFLICT DO NOTHING -> no row this time
            (42, "e1", 7, "BEAT/USDT:USDT"),  # fallback SELECT by idempotency key
        ]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.open_trade_for_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key="e1:entry:base",
            setup_context={"strategy": "early_momentum_v3"},
        )
    assert outcome == journal.OpenTradeOutcome(
        trade_id=42, created=False, recovered=True, claim_valid=True
    )


async def test_open_trade_for_episode_rejects_idempotency_key_collision() -> None:
    """The fallback row must genuinely belong to this episode/strategy/
    instrument -- a bare key match is never trusted on its own."""
    conn, _cur = _mock_conn(
        [
            _episode_claim_row("claimed"),
            (7,),
            None,
            (99, "some-other-episode", 7, "BEAT/USDT:USDT"),  # wrong episode_id
        ]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        outcome = await journal.open_trade_for_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key="e1:entry:base",
            setup_context={"strategy": "early_momentum_v3"},
        )
    assert outcome.trade_id is None
    assert outcome.created is False
    assert outcome.recovered is False
    assert outcome.claim_valid is True


async def test_open_trade_for_episode_db_error_returns_invalid_claim() -> None:
    with patch(
        "psycopg.AsyncConnection.connect", AsyncMock(side_effect=Exception("connection refused"))
    ):
        outcome = await journal.open_trade_for_episode(
            "postgresql://x",
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key="e1:entry:base",
            setup_context={"strategy": "early_momentum_v3"},
        )
    assert outcome.trade_id is None
    assert outcome.claim_valid is False


async def test_open_trade_untouched_return_type_still_int_or_none() -> None:
    """Regression (colleague review): open_trade's own return contract must
    stay exactly int | None -- pump-short/liquidation_cascade write it
    straight to Redis as str(trade_id); a dataclass there would silently
    write its repr instead."""
    conn, _cur = _mock_conn([(3,), (42,)])
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        trade_id = await journal.open_trade(
            "postgresql://x",
            symbol="BEAT/USDT:USDT",
            exchange="bybit",
            side="short",
            order_id=None,
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            setup_context={},
        )
    assert trade_id == 42
    assert isinstance(trade_id, int)
