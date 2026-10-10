"""Rehearsal of the HYP-015 formal read through the real CLI, on synthetic inputs.

Runs on the disposable database of `make verify` (every migration applied, no production
row). A synthetic cohort lies inside the registered window: 120 WATCH decisions on 32
assets over four ISO weeks, their hold12h paper probes and 240/720-minute outcomes. Its
funding comes from the registered capture (`run_capture`), with only the Bybit endpoint
replaced by a pinned, deterministic fixture. The reader then runs as `main()` with its
real arguments; the only test seam is `_now`, which stands the clock at the registered
read time (there is no flag or setting for that in production).

Checked: the published artifact is exactly the verdict of the snapshot pinned in the
claim, the snapshot carries the seeded WATCH ids and the fixture's rates, the claim ends
completed and names the artifact's hash, a second read is refused without touching a
file, a crash between the claim and the publication resumes only after the lease and
publishes the same result, and an incomplete cohort is refused before any claim (then
read once complete, or read as it is with the recorded flag).

Every seeded row carries the `RHX` marker and is removed before and after each test,
with the cohort's claim row. Nothing here changes the registration, a threshold or the
rules of the live cohort.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from schurfer_analytics import exchange_registry
from schurfer_analytics import momentum_flow_hold12h_verdict_reader as reader
from schurfer_analytics.momentum_flow_hold12h_funding_resolver import run_capture
from schurfer_analytics.momentum_flow_hold12h_snapshot import (
    inputs_from_snapshot,
    publish_once_or_same,
    snapshot_digest,
)
from schurfer_analytics.momentum_flow_hold12h_verdict import HOLD12H_VERDICT_CONTRACT
from schurfer_analytics.momentum_flow_hold12h_verdict_report import (
    cohort_rows_digest,
    evaluate_cohort,
    filter_to_cohort,
    verdict_fingerprint,
)
from schurfer_analytics.momentum_flow_paper_contract import HOLD12H_PAPER_CONTRACT
from schurfer_journal.testing_database import integration_database_url

DSN = integration_database_url()
CONTRACT = HOLD12H_VERDICT_CONTRACT
HOLD = HOLD12H_PAPER_CONTRACT
START = datetime(2026, 10, 5, tzinfo=UTC)
PREFIX_END = datetime(2026, 11, 2, tzinfo=UTC)
READ_OPENS = PREFIX_END + timedelta(hours=CONTRACT.min_read_delay_hours)
CAPTURE_AT = READ_OPENS - timedelta(hours=6)
REVISION = "rehearsal"
ASSETS = 32
WATCHES = 120
STALE = frozenset({5, 40, 77})
FEES_USD = 0.11
NOTIONAL = 50.0
SETTLEMENT_EVERY = timedelta(hours=8)
FIXTURE_FROM = datetime(2026, 10, 3, tzinfo=UTC)
FIXTURE_TO = datetime(2026, 11, 5, tzinfo=UTC)


def _symbol(asset: int) -> str:
    return f"RHX{asset:02d}"


def _market_id(asset: int) -> str:
    return f"{_symbol(asset)}USDT"


def fixture_rate(market_id: str, at: datetime) -> str:
    """The pinned venue answer: a deterministic rate per instrument and settlement."""
    step = int((at - FIXTURE_FROM) / SETTLEMENT_EVERY)
    asset = int(market_id[3:5])
    return f"{((asset * 7 + step * 3) % 11 - 5) * 0.00002:.6f}"


class FixtureBybit:
    """Answers Bybit v5 `/v5/market/funding/history` like the venue: rows in the
    requested range, newest first, at most `limit`, with string fields."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def public_get_v5_market_funding_history(self, params: dict[str, Any]) -> Any:
        self.requests.append(dict(params))
        symbol = params["symbol"]
        rows = []
        at = FIXTURE_FROM
        while at <= FIXTURE_TO:
            ms = int(at.timestamp() * 1000)
            if params["startTime"] <= ms <= params["endTime"]:
                rows.append(
                    {
                        "symbol": symbol,
                        "fundingRate": fixture_rate(symbol, at),
                        "fundingRateTimestamp": str(ms),
                    }
                )
            at += SETTLEMENT_EVERY
        rows.reverse()
        page = rows[: int(params["limit"])]
        return {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": page}}

    async def close(self) -> None:
        return None


def _connect() -> psycopg.Connection[Any]:
    try:
        conn = psycopg.connect(DSN, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise
        pytest.skip(f"no local postgres reachable: {exc}")
    if conn.execute("SELECT to_regclass('app.hold12h_formal_read_claims')").fetchone() == (None,):
        conn.close()
        pytest.skip("migration 0053 is not applied")
    return conn


def _clean(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "DELETE FROM app.hold12h_formal_read_claims WHERE contract_version = %s "
        "AND cohort_start = %s AND decision_prefix_end = %s",
        (CONTRACT.contract_version, START, PREFIX_END),
    )
    conn.execute("DELETE FROM app.momentum_flow_paper_probes WHERE symbol LIKE 'RHX%'")
    conn.execute(
        "DELETE FROM timeseries.momentum_flow_watch_evaluations_1m WHERE symbol LIKE 'RHX%'"
    )
    conn.execute("DELETE FROM app.momentum_universe_instruments WHERE universe_version LIKE 'rhx%'")
    conn.execute("DELETE FROM app.momentum_universe_snapshots WHERE universe_version LIKE 'rhx%'")
    conn.execute("DELETE FROM app.hold12h_funding_coverage_runs WHERE native_market_id LIKE 'RHX%'")
    conn.execute("DELETE FROM app.hold12h_funding_settlements WHERE native_market_id LIKE 'RHX%'")


@pytest.fixture
def db() -> Any:
    conn = _connect()
    _clean(conn)
    yield conn
    _clean(conn)
    conn.close()


def _returns(i: int) -> tuple[float, float]:
    """Deterministic gross returns (percent) at 240 and 720 minutes."""
    return ((i * 53) % 17 - 8) * 0.2, ((i * 37) % 21 - 9) * 0.25


def _decision(i: int) -> datetime:
    return START + timedelta(minutes=60 + i * 324)  # 120 decisions over 27 days


def _seed(conn: psycopg.Connection[Any], *, open_index: int | None = None) -> list[str]:
    """The synthetic cohort; returns the WATCH ids in decision order. `open_index` leaves
    that probe filled but not yet closed (the position is still running)."""
    conn.execute(
        "INSERT INTO app.momentum_flow_paper_runs (paper_version, contract_sha256, "
        "contract_json, cohort_started_at) VALUES (%s, %s, '{}', %s) "
        "ON CONFLICT (paper_version) DO NOTHING",
        (HOLD.paper_version, "c" * 64, START),
    )
    conn.execute(
        "INSERT INTO app.momentum_universe_snapshots (exchange, universe_version, "
        "catalog_version, capture_version, schema_version, captured_at, instrument_count, "
        "payload_hash) VALUES (%s, 'rhx-u1', 'rhx-c1', 'cap1', 's1', %s, %s, %s)",
        (HOLD.source_exchange, START - timedelta(days=1), ASSETS, b"\x31" * 32),
    )
    for asset in range(ASSETS):
        conn.execute(
            "INSERT INTO app.momentum_universe_instruments (exchange, universe_version, "
            "catalog_version, native_market_id, base, quote, settle, native_market_type, "
            "canonical_market_type, identity_status, identity_key, onboarded_at, "
            "metadata_hash) VALUES (%s, 'rhx-u1', 'rhx-c1', %s, %s, 'USDT', 'USDT', "
            "'linear', 'swap', 'ready', %s, %s, %s)",
            (
                HOLD.source_exchange,
                _market_id(asset),
                _symbol(asset),
                f"rhx:asset{asset}",
                START - timedelta(days=30),
                b"\x32" * 32,
            ),
        )
    watch_ids = []
    for i in range(WATCHES):
        watch_ids.append(_watch_and_probe(conn, i, still_open=i == open_index))
    return watch_ids


def _watch_and_probe(conn: psycopg.Connection[Any], i: int, *, still_open: bool) -> str:
    asset = i % ASSETS
    bucket = _decision(i)
    decided = bucket + timedelta(seconds=30)
    watch_id = str(uuid.uuid4())
    episode_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO timeseries.momentum_flow_watch_evaluations_1m (exchange, market_type, "
        "symbol, capture_version, watch_version, bucket_start, universe_version, quality_ready, "
        "raw_qualified, decision_status, reason_codes, price_return_60m_pct, oi_growth_60m_pct, "
        "cross_section_size, evaluator_started_at, evaluator_completed_at, decision_at, "
        "episode_id, watch_id, state_active_after, state_clear_streak_after, input_hash) "
        "VALUES (%s, %s, %s, 'cap1', %s, %s, 'rhx-u1', true, true, 'watch', '{}'::text[], "
        "3.0, 4.0, %s, %s, %s, %s, %s, %s, true, 0, %s)",
        (
            HOLD.source_exchange,
            HOLD.market_type,
            _symbol(asset),
            HOLD.watch_version,
            bucket,
            ASSETS,
            bucket,
            decided + timedelta(seconds=5),
            decided,
            episode_id,
            watch_id,
            b"\x33" * 32,
        ),
    )
    paper_id = str(uuid.uuid4())
    stale = i in STALE
    entry = decided + timedelta(seconds=12)
    exit_at = None if stale or still_open else entry + timedelta(minutes=HOLD.max_hold_minutes)
    g240, g720 = _returns(i)
    conn.execute(
        "INSERT INTO app.momentum_flow_paper_probes (paper_id, paper_version, watch_version, "
        "watch_id, episode_id, exchange, market_type, symbol, unified_symbol, market_id, "
        "watch_bucket_start, watch_decision_at, claimed_at, entry_status, position_status, "
        "entry_at, entry_vwap, entry_filled_notional_usd, exit_at, exit_vwap, exit_reason, "
        "gross_return_pct, fees_usd, max_adverse_return_pct, accounting_status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s, %s)",
        (
            paper_id,
            HOLD.paper_version,
            HOLD.watch_version,
            watch_id,
            episode_id,
            HOLD.source_exchange,
            HOLD.market_type,
            _symbol(asset),
            f"{_symbol(asset)}/USDT:USDT",
            _market_id(asset),
            bucket,
            decided,
            decided + timedelta(seconds=10),
            "rejected_stale" if stale else "opened",
            "not_open" if stale else ("open" if still_open else "closed"),
            None if stale else entry,
            None if stale else 1.0,
            None if stale else NOTIONAL,
            exit_at,
            None if exit_at is None else 1.0 + g720 / 100,
            None if exit_at is None else "max_hold",
            None if exit_at is None else g720,
            None if exit_at is None else FEES_USD,
            None if exit_at is None else -1.0,
            None if exit_at is None else "complete",
        ),
    )
    if not stale:
        for horizon, gross in ((240, g240), (720, g720)):
            if still_open and horizon == 720:
                continue
            due = entry + timedelta(minutes=horizon)
            conn.execute(
                "INSERT INTO app.momentum_flow_paper_outcomes (paper_id, horizon_minutes, "
                "due_at, status, quote_observed_at, bid_vwap, filled_notional_usd, "
                "gross_return_pct, fees_usd, accounting_status) "
                "VALUES (%s, %s, %s, 'complete', %s, %s, %s, %s, %s, 'complete')",
                (paper_id, horizon, due, due, 1.0 + gross / 100, NOTIONAL, gross, FEES_USD),
            )
    return watch_id


def _close(conn: psycopg.Connection[Any], i: int) -> None:
    """The still-running position of decision `i` reaches its 720-minute exit."""
    _, g720 = _returns(i)
    row = conn.execute(
        "SELECT p.paper_id, p.entry_at FROM app.momentum_flow_paper_probes p "
        "WHERE p.symbol = %s AND p.watch_decision_at = %s",
        (_symbol(i % ASSETS), _decision(i) + timedelta(seconds=30)),
    ).fetchone()
    assert row is not None
    paper_id, entry = row
    exit_at = entry + timedelta(minutes=HOLD.max_hold_minutes)
    conn.execute(
        "UPDATE app.momentum_flow_paper_probes SET position_status = 'closed', exit_at = %s, "
        "exit_vwap = %s, exit_reason = 'max_hold', gross_return_pct = %s, fees_usd = %s, "
        "max_adverse_return_pct = -1.0, accounting_status = 'complete' WHERE paper_id = %s",
        (exit_at, 1.0 + g720 / 100, g720, FEES_USD, paper_id),
    )
    conn.execute(
        "INSERT INTO app.momentum_flow_paper_outcomes (paper_id, horizon_minutes, due_at, "
        "status, quote_observed_at, bid_vwap, filled_notional_usd, gross_return_pct, fees_usd, "
        "accounting_status) VALUES (%s, 720, %s, 'complete', %s, %s, %s, %s, %s, 'complete')",
        (paper_id, exit_at, exit_at, 1.0 + g720 / 100, NOTIONAL, g720, FEES_USD),
    )


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """The registered funding capture, with the venue replaced by the fixture."""
    venue = FixtureBybit()
    monkeypatch.setitem(exchange_registry.EXCHANGE_FACTORIES, "bybit", lambda: venue)
    return asyncio.run(run_capture(DSN, now=CAPTURE_AT))


def _argv(out: Path, *extra: str) -> list[str]:
    return [
        "hold12h-verdict-reader",
        "--formal-run",
        "--decision-prefix-end",
        PREFIX_END.isoformat(),
        "--output-dir",
        str(out),
        "--code-revision",
        REVISION,
        "--no-working-tree-dirty",
        *extra,
    ]


def _read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    out: Path,
    *extra: str,
    now: datetime = READ_OPENS + timedelta(seconds=1),
) -> dict[str, Any]:
    """The CLI exactly as production runs it, with the clock at `now`."""
    monkeypatch.setattr(reader, "_now", lambda: now)
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setattr(sys, "argv", _argv(out, *extra))
    reader.main()
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    return result


def _republish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], out: Path
) -> dict[str, Any]:
    """The recovery step, with every cohort read and every recomputation made to fail:
    republishing may only read the claim row and the attempt file it names."""

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("republish read the cohort or recomputed the verdict")

    for name in (
        "load_cohort",
        "load_cohort_watch_ids",
        "load_formal_coverage",
        "load_snapshot_from_db",
        "evaluate_cohort",
        "open_formal_claim",
        "complete_formal_claim",
    ):
        monkeypatch.setattr(reader, name, forbidden)
    monkeypatch.setattr(reader, "_now", lambda: READ_OPENS + timedelta(hours=1))
    monkeypatch.setenv("DATABASE_URL", DSN)
    argv = [
        "hold12h-verdict-reader",
        "--republish",
        "--decision-prefix-end",
        PREFIX_END.isoformat(),
        "--output-dir",
        str(out),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    reader.main()
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    return result


def _crash_on(monkeypatch: pytest.MonkeyPatch, file_name: str) -> None:
    """The process dies right before publishing `file_name` under its stable name."""
    original = publish_once_or_same

    def publish(path: Path, body: bytes) -> None:
        if path.name == file_name:
            raise RuntimeError(f"simulated crash before {file_name}")
        original(path, body)

    monkeypatch.setattr(reader, "publish_once_or_same", publish)


def _claims(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT * FROM app.hold12h_formal_read_claims WHERE contract_version = %s "
            "AND cohort_start = %s AND decision_prefix_end = %s",
            (CONTRACT.contract_version, START, PREFIX_END),
        )
        return list(cur.fetchall())


def _files(out: Path) -> dict[str, str]:
    if not out.exists():
        return {}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())}


def _expected_artifact(snapshot: bytes) -> dict[str, Any]:
    """The verdict computed independently from the pinned snapshot alone."""
    watches, probes, funding = inputs_from_snapshot(snapshot)
    watches = filter_to_cohort(watches, cohort_start=START, decision_prefix_end=PREFIX_END)
    evaluation = evaluate_cohort(CONTRACT, watches, probes, funding)
    fingerprint = verdict_fingerprint(
        contract_sha256=CONTRACT.sha256_hex(),
        cohort_start=START,
        decision_prefix_end=PREFIX_END,
        code_revision=REVISION,
        working_tree_dirty=False,
        funding_source_id=f"StoredFundingSource:{CONTRACT.actual_funding_version}",
        data_versions={"paper_contract": CONTRACT.paper_contract_sha256},
        rows_digest=cohort_rows_digest(watches, probes, funding),
        funnel=evaluation.funnel,
        inputs=evaluation.inputs,
    )
    artifact = reader._formal_artifact(
        CONTRACT,
        evaluation,
        cohort_start=START,
        decision_prefix_end=PREFIX_END,
        fingerprint=fingerprint,
        code_revision=REVISION,
        working_tree_dirty=False,
    )
    loaded: dict[str, Any] = json.loads(json.dumps(artifact, sort_keys=True, allow_nan=False))
    return loaded


def _assert_published(out: Path, claim: dict[str, Any], watch_ids: list[str]) -> dict[str, Any]:
    """The claim is completed and names the published artifact, which is exactly the
    verdict of the snapshot the claim pinned; the snapshot holds the seeded cohort and the
    fixture's rates as the capture stored them."""
    assert claim["status"] == "completed"
    assert claim["completed_at"] is not None
    assert claim["contract_sha256"] == CONTRACT.sha256_hex()
    assert claim["code_revision"] == REVISION and claim["working_tree_dirty"] is False
    assert claim["watch_ids"] == watch_ids
    body = (out / "hold12h_verdict.json").read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    assert (out / "hold12h_verdict.sha256").read_text() == f"sha256:{sha}\n"
    assert claim["artifact_sha256"] == sha
    assert (out / claim["artifact_name"]).read_bytes() == body
    snapshot = (out / f"inputs.{claim['inputs_digest']}.json").read_bytes()
    assert snapshot_digest(snapshot) == claim["inputs_digest"]
    payload = json.loads(snapshot)
    assert sorted(w[0] for w in payload["watches"]) == sorted(watch_ids)
    settlements = 0
    for route in payload["funding"]["routes"]:
        market_id = route["key"][-1]
        for at, rate, version in route["settlements"]:
            assert version == CONTRACT.actual_funding_version
            assert rate == float(fixture_rate(market_id, datetime.fromisoformat(at)))
            settlements += 1
    assert settlements > 0
    artifact: dict[str, Any] = json.loads(body)
    assert artifact == _expected_artifact(snapshot)
    assert artifact["fingerprint"] == claim["result_fingerprint"]
    assert artifact["mode"] == "formal"
    assert artifact["cohort_start"] == START.isoformat()
    assert artifact["decision_prefix_end"] == PREFIX_END.isoformat()
    return artifact


def test_the_formal_read_publishes_the_verdict_of_its_pinned_snapshot_once(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_ids = _seed(db)
    summary = _capture(monkeypatch)
    assert summary == {
        "pending": WATCHES - len(STALE),
        "complete": WATCHES - len(STALE),
        "incomplete": 0,
        "integrity_conflicts": 0,
    }
    out = tmp_path / "verdict"
    printed = _read(monkeypatch, capsys, out)
    (claim,) = _claims(db)
    artifact = _assert_published(out, claim, watch_ids)
    assert printed == artifact
    assert claim["accepted_incomplete_coverage"] is False
    assert claim["coverage_open_positions"] == 0
    assert claim["coverage_closed"] == WATCHES - len(STALE)
    assert claim["coverage_funding_covered"] == WATCHES - len(STALE)
    assert artifact["funnel"]["rejected_stale"] == len(STALE)
    assert artifact["funnel"]["analyzable"] == WATCHES - len(STALE)
    assert artifact["analyzable_pairs"] == WATCHES - len(STALE)
    # The synthetic set clears the floor and diversity gates, so the bootstrap gate decides.
    assert (artifact["verdict"], artifact["gate"]) == ("insufficient_evidence", "D")

    before = _files(out)
    with pytest.raises(SystemExit, match="already claimed and completed"):
        _read(monkeypatch, capsys, out)
    assert _files(out) == before
    assert _claims(db) == [claim]


def test_a_read_before_the_registered_time_changes_nothing(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(db)
    _capture(monkeypatch)
    out = tmp_path / "verdict"
    with pytest.raises(SystemExit, match="too early"):
        _read(monkeypatch, capsys, out, now=READ_OPENS - timedelta(seconds=1))
    assert _claims(db) == []
    assert not out.exists()


def test_preflight_needs_no_database_and_refuses_before_the_read_opens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from schurfer_analytics.source_digest import source_digest

    monkeypatch.delenv("DATABASE_URL", raising=False)
    argv = [
        "hold12h-verdict-reader",
        "--preflight",
        "--decision-prefix-end",
        PREFIX_END.isoformat(),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(reader, "_now", lambda: READ_OPENS - timedelta(seconds=1))
    with pytest.raises(SystemExit, match="too early"):
        reader.main()
    monkeypatch.setattr(reader, "_now", lambda: READ_OPENS)
    reader.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["read_opens_at"] == READ_OPENS.isoformat()
    assert printed["contract_sha256"] == CONTRACT.sha256_hex()
    assert printed["source_digest"] == source_digest(Path(reader.__file__).parent)
    monkeypatch.setattr(sys, "argv", [*argv[:-1], "2026-11-03T00:00:00+00:00"])
    with pytest.raises(SystemExit, match="not the frozen"):
        reader.main()


def test_a_crash_after_the_claim_resumes_after_the_lease_with_the_same_result(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_ids = _seed(db)
    _capture(monkeypatch)
    out = tmp_path / "verdict"

    async def crash(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated crash between the claim and the publication")

    original = reader.publish_formal_result
    monkeypatch.setattr(reader, "publish_formal_result", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _read(monkeypatch, capsys, out)
    (open_claim,) = _claims(db)
    assert open_claim["status"] == "claimed"
    assert open_claim["inputs_digest"] is not None
    assert not (out / "hold12h_verdict.json").exists()
    pinned = (out / f"inputs.{open_claim['inputs_digest']}.json").read_bytes()
    with monkeypatch.context() as scoped, pytest.raises(SystemExit, match="not completed"):
        _republish(scoped, capsys, out)

    monkeypatch.setattr(reader, "publish_formal_result", original)
    with pytest.raises(SystemExit, match="holds the open claim's lease"):
        _read(monkeypatch, capsys, out)
    assert not (out / "hold12h_verdict.json").exists()

    # Test-only: the lease (60 minutes) runs out.
    db.execute(
        "UPDATE app.hold12h_formal_read_claims SET lease_expires_at = now() - interval '1 second' "
        "WHERE id = %s",
        (open_claim["id"],),
    )
    _read(monkeypatch, capsys, out)
    (claim,) = _claims(db)
    assert claim["id"] == open_claim["id"]
    assert claim["inputs_digest"] == open_claim["inputs_digest"]
    assert (out / f"inputs.{claim['inputs_digest']}.json").read_bytes() == pinned
    _assert_published(out, claim, watch_ids)


def test_a_crash_after_the_attempt_file_publishes_one_identical_result(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_ids = _seed(db)
    _capture(monkeypatch)
    out = tmp_path / "verdict"

    async def crash(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated crash before the claim was completed")

    original = reader.complete_formal_claim
    monkeypatch.setattr(reader, "complete_formal_claim", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _read(monkeypatch, capsys, out)
    (stale,) = [p for p in out.iterdir() if p.name.endswith(".json") and "attempt-" in p.name]
    assert not (out / "hold12h_verdict.json").exists()

    monkeypatch.setattr(reader, "complete_formal_claim", original)
    db.execute(
        "UPDATE app.hold12h_formal_read_claims SET lease_expires_at = now() - interval '1 second'"
    )
    _read(monkeypatch, capsys, out)
    (claim,) = _claims(db)
    _assert_published(out, claim, watch_ids)
    assert claim["artifact_name"] != stale.name
    assert stale.read_bytes() == (out / "hold12h_verdict.json").read_bytes()


def test_an_incomplete_cohort_is_refused_before_the_claim_and_read_once_complete(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_ids = _seed(db, open_index=WATCHES - 1)
    _capture(monkeypatch)
    out = tmp_path / "verdict"
    with pytest.raises(SystemExit, match=r"cohort not complete yet.*1 filled positions"):
        _read(monkeypatch, capsys, out)
    assert _claims(db) == []
    assert not out.exists()

    _close(db, WATCHES - 1)
    with pytest.raises(SystemExit, match=r"funding covered for 116/117 closed"):
        _read(monkeypatch, capsys, out)
    assert _claims(db) == []

    _capture(monkeypatch)
    _read(monkeypatch, capsys, out)
    (claim,) = _claims(db)
    assert claim["accepted_incomplete_coverage"] is False
    _assert_published(out, claim, watch_ids)


def test_an_incomplete_cohort_read_as_it_is_records_the_flag(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_ids = _seed(db, open_index=WATCHES - 1)
    _capture(monkeypatch)
    out = tmp_path / "verdict"
    _read(monkeypatch, capsys, out, "--accept-incomplete-coverage")
    (claim,) = _claims(db)
    assert claim["accepted_incomplete_coverage"] is True
    assert claim["coverage_open_positions"] == 1
    _assert_published(out, claim, watch_ids)


@pytest.mark.parametrize("missing", ["hold12h_verdict.json", "hold12h_verdict.sha256"])
def test_a_crash_after_the_completed_claim_is_finished_by_republishing(
    missing: str,
    db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watch_ids = _seed(db)
    _capture(monkeypatch)
    out = tmp_path / "verdict"
    with monkeypatch.context() as scoped:
        _crash_on(scoped, missing)
        with pytest.raises(RuntimeError, match="simulated crash"):
            _read(scoped, capsys, out)
    (claim,) = _claims(db)
    assert claim["status"] == "completed"
    assert not (out / missing).exists()
    attempt = (out / claim["artifact_name"]).read_bytes()

    # The read is done: running it again refuses for good and points to the recovery.
    before = _files(out)
    with pytest.raises(SystemExit, match="finish it with --republish"):
        _read(monkeypatch, capsys, out)
    assert _files(out) == before

    with monkeypatch.context() as scoped:
        printed = _republish(scoped, capsys, out)
    assert (out / "hold12h_verdict.json").read_bytes() == attempt
    assert printed == json.loads(attempt)
    assert _claims(db) == [claim]
    _assert_published(out, claim, watch_ids)

    # Republishing again changes nothing.
    published = _files(out)
    with monkeypatch.context() as scoped:
        _republish(scoped, capsys, out)
    assert _files(out) == published


def test_republishing_refuses_an_attempt_file_that_does_not_match_the_claim(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(db)
    _capture(monkeypatch)
    out = tmp_path / "verdict"
    with monkeypatch.context() as scoped:
        _crash_on(scoped, "hold12h_verdict.json")
        with pytest.raises(RuntimeError, match="simulated crash"):
            _read(scoped, capsys, out)
    (claim,) = _claims(db)
    attempt = out / claim["artifact_name"]
    attempt.chmod(0o644)
    attempt.write_bytes(attempt.read_bytes().replace(b'"mode": "formal"', b'"mode": "formaL"'))
    with monkeypatch.context() as scoped, pytest.raises(SystemExit, match="integrity incident"):
        _republish(scoped, capsys, out)
    assert not (out / "hold12h_verdict.json").exists()
    assert _claims(db) == [claim]
