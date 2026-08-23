from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution import legacy_accounting_backfill as backfill

_DB_URL = "postgresql://x"


def _row(
    *,
    id: int = 1,
    symbol: str = "PONS/USDT:USDT",
    exchange: str = "lbank",
    side: str = "short",
    size_usd: float = 100.0,
    entry_price: float = 1.0,
    exit_price: float = 0.95,
    entry_at: datetime = datetime(2026, 7, 17, 22, 0, 0, tzinfo=UTC),
    exit_at: datetime = datetime(2026, 7, 17, 23, 0, 0, tzinfo=UTC),  # 60 min hold
    setup_context: dict[str, Any] | None = None,
    gross_pnl_usd: float | None = 5.0,  # (1.0-0.95)/1.0*100 = 5% of $100
    gross_pnl_pct: float | None = 5.0,
    exit_slippage_bps: float | None = None,
) -> dict:
    return {
        "id": id,
        "symbol": symbol,
        "exchange": exchange,
        "side": side,
        "size_usd": size_usd,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_at": entry_at,
        "exit_at": exit_at,
        "setup_context": {"paper": True} if setup_context is None else setup_context,
        "gross_pnl_usd": gross_pnl_usd,
        "gross_pnl_pct": gross_pnl_pct,
        "exit_slippage_bps": exit_slippage_bps,
    }


def _mock_select_conn(rows: list[dict]) -> MagicMock:
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


def _mock_write_conn(*, rowcount: int = 1) -> MagicMock:
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


# ---- _to_candidate_and_check ----


def test_missing_exit_price_is_skipped() -> None:
    row = _row(exit_price=None)
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.SkippedRow)
    assert "exit_price" in result.reason


def test_non_paper_trade_is_skipped() -> None:
    row = _row(setup_context={"paper": False})
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.SkippedRow)
    assert "paper" in result.reason


def test_row_with_existing_exit_slippage_bps_is_skipped_as_inconsistent() -> None:
    """A legacy-version row that somehow already has a fresh exit capture is
    a data inconsistency worth a human's eyes, never silently reused."""
    row = _row(exit_slippage_bps=3.5)
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.SkippedRow)
    assert "inconsistent state" in result.reason


def test_no_market_quality_yields_incomplete_with_both_legs_missing() -> None:
    row = _row(setup_context={"paper": True})  # no market_quality key at all
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.BackfillCandidate)
    assert result.accounting_status == "incomplete"
    assert result.accounting_error == "missing entry_slippage_bps, exit_slippage_bps"
    assert result.net_pnl_usd is None
    assert result.net_pnl_pct is None
    # fees/funding are still computed even though slippage never will be.
    assert result.fees_usd == pytest.approx(0.20)  # 100 * (10+10)bps / 10_000
    assert result.funding_usd == pytest.approx(0.0063)  # round(100 * (5*60/480)bps / 10_000, 4)


def test_market_quality_present_yields_incomplete_with_only_exit_leg_missing() -> None:
    """entry_slippage_bps IS legitimately recoverable from the entry-time
    snapshot -- only the exit leg (a fresh at-close capture) never happened
    for these rows, so the error must name only the exit leg."""
    row = _row(
        side="short",
        setup_context={
            "paper": True,
            "market_quality": {"bid_impact_bps": 2.5, "ask_impact_bps": 3.1},
        },
    )
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.BackfillCandidate)
    assert result.accounting_status == "incomplete"
    assert result.accounting_error == "missing exit_slippage_bps"
    assert result.net_pnl_usd is None


def test_gross_mismatch_is_skipped_for_manual_review() -> None:
    """The recomputed gross figure must agree with what was already stored --
    disagreement means something about the row is not what it appears, and
    must never be silently overwritten."""
    row = _row(gross_pnl_usd=999.0, gross_pnl_pct=999.0)
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.SkippedRow)
    assert "disagrees with stored gross" in result.reason


def test_gross_agreement_within_tolerance_is_accepted() -> None:
    row = _row(gross_pnl_usd=5.001, gross_pnl_pct=5.001)  # within abs_tol=0.01
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.BackfillCandidate)


def test_long_side_computes_gross_correctly() -> None:
    # long: (exit-entry)/entry*100. entry=1.0, exit=1.1 -> +10%, $10 on $100.
    row = _row(side="long", entry_price=1.0, exit_price=1.1, gross_pnl_usd=10.0, gross_pnl_pct=10.0)
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.BackfillCandidate)
    assert result.side == "long"


def test_missing_gross_on_stored_row_is_skipped() -> None:
    row = _row(gross_pnl_usd=None, gross_pnl_pct=None)
    result = backfill._to_candidate_and_check(row)
    assert isinstance(result, backfill.SkippedRow)


# ---- classify_legacy_accounting ----


@pytest.mark.asyncio
async def test_classify_separates_candidates_from_skipped() -> None:
    rows = [_row(id=1), _row(id=2, setup_context={"paper": False})]
    conn = _mock_select_conn(rows)
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=conn)):
        result = await backfill.classify_legacy_accounting(_DB_URL)

    assert [c.trade_id for c in result.candidates] == [1]
    assert [s.trade_id for s in result.skipped] == [2]


# ---- fingerprint / manifest round-trip ----


def test_fingerprint_is_deterministic() -> None:
    c = backfill._to_candidate_and_check(_row(id=1))
    assert isinstance(c, backfill.BackfillCandidate)
    a = backfill._fingerprint((c,))
    b = backfill._fingerprint((c,))
    assert a == b


def test_fingerprint_changes_when_a_computed_field_changes() -> None:
    c1 = backfill._to_candidate_and_check(_row(id=1, gross_pnl_usd=5.0, gross_pnl_pct=5.0))
    c2 = backfill._to_candidate_and_check(
        _row(id=1, entry_price=1.0, exit_price=0.90, gross_pnl_usd=10.0, gross_pnl_pct=10.0)
    )
    assert isinstance(c1, backfill.BackfillCandidate)
    assert isinstance(c2, backfill.BackfillCandidate)
    assert backfill._fingerprint((c1,)) != backfill._fingerprint((c2,))


def test_manifest_write_read_round_trip(tmp_path: Path) -> None:
    c = backfill._to_candidate_and_check(_row(id=7))
    assert isinstance(c, backfill.BackfillCandidate)
    manifest = backfill.build_manifest([c])
    path = tmp_path / "manifest.json"
    backfill.write_manifest(manifest, path)

    loaded = backfill.read_manifest(path)

    assert loaded.trade_ids == manifest.trade_ids
    assert loaded.fingerprint == manifest.fingerprint
    assert [c.trade_id for c in loaded.candidates] == [c.trade_id for c in manifest.candidates]
    assert loaded.candidates[0].fees_usd == manifest.candidates[0].fees_usd


def test_build_manifest_sorts_candidates_by_trade_id() -> None:
    c155 = backfill._to_candidate_and_check(_row(id=155))
    c149 = backfill._to_candidate_and_check(_row(id=149))
    assert isinstance(c155, backfill.BackfillCandidate)
    assert isinstance(c149, backfill.BackfillCandidate)
    manifest = backfill.build_manifest([c155, c149])
    assert manifest.trade_ids == (149, 155)


# ---- apply_backfill ----


def _manifest(trade_ids: tuple[int, ...]) -> backfill.Manifest:
    candidates = []
    for tid in trade_ids:
        c = backfill._to_candidate_and_check(_row(id=tid))
        assert isinstance(c, backfill.BackfillCandidate)
        candidates.append(c)
    return backfill.build_manifest(candidates)


@pytest.mark.asyncio
async def test_apply_backfill_empty_manifest_is_a_no_op() -> None:
    manifest = _manifest(())
    count = await backfill.apply_backfill(_DB_URL, manifest=manifest)
    assert count == 0


@pytest.mark.asyncio
async def test_apply_backfill_clean_apply_updates_exactly_the_manifest_rows() -> None:
    manifest = _manifest((1,))
    state_conn = _mock_select_conn(
        [{"id": 1, "status": "closed", "accounting_version": backfill.LEGACY_ACCOUNTING_VERSION}]
    )
    reclassify_conn = _mock_select_conn([_row(id=1)])
    write_conn = _mock_write_conn(rowcount=1)

    with patch(
        "psycopg.AsyncConnection.connect",
        AsyncMock(side_effect=[state_conn, reclassify_conn, write_conn]),
    ):
        count = await backfill.apply_backfill(_DB_URL, manifest=manifest)

    assert count == 1


@pytest.mark.asyncio
async def test_apply_backfill_already_applied_is_an_idempotent_no_op() -> None:
    manifest = _manifest((1,))
    state_conn = _mock_select_conn(
        [{"id": 1, "status": "closed", "accounting_version": backfill.PAPER_ACCOUNTING_VERSION}]
    )
    with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=state_conn)):
        count = await backfill.apply_backfill(_DB_URL, manifest=manifest)
    assert count == 0


@pytest.mark.asyncio
async def test_apply_backfill_partial_state_aborts_for_manual_review() -> None:
    manifest = _manifest((1, 2))
    state_conn = _mock_select_conn(
        [
            {"id": 1, "status": "closed", "accounting_version": backfill.PAPER_ACCOUNTING_VERSION},
            {"id": 2, "status": "closed", "accounting_version": backfill.LEGACY_ACCOUNTING_VERSION},
        ]
    )
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=state_conn)),
        pytest.raises(backfill.BackfillAbortedError, match="partial apply detected"),
    ):
        await backfill.apply_backfill(_DB_URL, manifest=manifest)


@pytest.mark.asyncio
async def test_apply_backfill_missing_trade_id_aborts() -> None:
    manifest = _manifest((1,))
    state_conn = _mock_select_conn([])
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=state_conn)),
        pytest.raises(backfill.BackfillAbortedError, match="no longer exist"),
    ):
        await backfill.apply_backfill(_DB_URL, manifest=manifest)


@pytest.mark.asyncio
async def test_apply_backfill_unexpected_accounting_version_aborts() -> None:
    manifest = _manifest((1,))
    state_conn = _mock_select_conn(
        [{"id": 1, "status": "closed", "accounting_version": "some_other_version"}]
    )
    with (
        patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=state_conn)),
        pytest.raises(backfill.BackfillAbortedError, match="unexpected state"),
    ):
        await backfill.apply_backfill(_DB_URL, manifest=manifest)


@pytest.mark.asyncio
async def test_apply_backfill_aborts_when_live_reclassification_drifted() -> None:
    """Between classify and apply, this row picked up a fresh exit_slippage_bps
    (e.g. some other process touched it) -- live reclassification now skips
    it as inconsistent, so apply must refuse rather than overwrite it."""
    manifest = _manifest((1,))
    state_conn = _mock_select_conn(
        [{"id": 1, "status": "closed", "accounting_version": backfill.LEGACY_ACCOUNTING_VERSION}]
    )
    reclassify_conn = _mock_select_conn([_row(id=1, exit_slippage_bps=4.0)])

    with (
        patch(
            "psycopg.AsyncConnection.connect", AsyncMock(side_effect=[state_conn, reclassify_conn])
        ),
        pytest.raises(backfill.BackfillAbortedError, match="no longer matches the manifest"),
    ):
        await backfill.apply_backfill(_DB_URL, manifest=manifest)


@pytest.mark.asyncio
async def test_apply_backfill_aborts_when_row_count_does_not_match() -> None:
    manifest = _manifest((1,))
    state_conn = _mock_select_conn(
        [{"id": 1, "status": "closed", "accounting_version": backfill.LEGACY_ACCOUNTING_VERSION}]
    )
    reclassify_conn = _mock_select_conn([_row(id=1)])
    write_conn = _mock_write_conn(rowcount=0)  # UPDATE matched nothing

    with (
        patch(
            "psycopg.AsyncConnection.connect",
            AsyncMock(side_effect=[state_conn, reclassify_conn, write_conn]),
        ),
        pytest.raises(backfill.BackfillAbortedError, match="expected to update"),
    ):
        await backfill.apply_backfill(_DB_URL, manifest=manifest)


# ---- CLI ----


def test_build_arg_parser_classify_subcommand() -> None:
    args = backfill._build_arg_parser().parse_args(["classify", "--out", "m.json"])
    assert args.command == "classify"
    assert str(args.out) == "m.json"


def test_build_arg_parser_apply_subcommand() -> None:
    args = backfill._build_arg_parser().parse_args(["apply", "--report", "m.json"])
    assert args.command == "apply"
    assert str(args.report) == "m.json"


def test_db_url_from_env_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        backfill._db_url_from_env()
