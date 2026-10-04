from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import TYPE_CHECKING

import duckdb
import pytest
from schurfer_analytics import edge_loss_bars as e
from schurfer_analytics.cold_bar_gated_deletion_job import RECEIPT_SUFFIX

if TYPE_CHECKING:
    from pathlib import Path

DAY = "2026-09-10"
ARCHIVE = "bars-2026-09-18T19:16:45"


def _source(path: Path) -> int:
    con = duckdb.connect()
    con.execute(
        """
        CREATE TABLE t AS
        SELECT * FROM (VALUES
          ('bybit', 'linear', 'AAAUSDT', 'v1'),
          ('binance', 'linear', 'AAAUSDT', 'v1'),
          ('bybit', 'spot', 'AAAUSDT', 'v1'),
          ('okx', 'linear', 'AAAUSDT', 'v1'),
          ('bybit', 'linear', 'BBBUSDT', 'v0')
        ) AS k(exchange, market_type, symbol, capture_version),
        (SELECT TIMESTAMPTZ '2026-09-10 00:00:00+00' + INTERVAL (m) MINUTE AS bucket_start
         FROM range(3) r(m))
        """
    )
    for column in e.COLUMNS[3:]:
        kind = "BOOLEAN" if "complete" in column else "INTEGER" if "count" in column else "DOUBLE"
        value = "true" if kind == "BOOLEAN" else "1"
        con.execute(f"ALTER TABLE t ADD COLUMN {column} {kind} DEFAULT {value}")
    con.execute("ALTER TABLE t ADD COLUMN universe_version VARCHAR DEFAULT 'u'")
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
    return 15


def _setup(tmp_path: Path) -> dict[str, Path]:
    stored = tmp_path / "stored.parquet"
    rows = _source(stored)
    cold = tmp_path / "cold-bars"
    cold.mkdir()
    manifest = cold / f"bars-{DAY}.manifest.json"
    manifest.write_text(json.dumps({"day": DAY, "file_bytes": stored.stat().st_size}))
    (cold / f"bars-{DAY}{RECEIPT_SUFFIX}").write_text(
        json.dumps(
            {
                "day": DAY,
                "archive_name": ARCHIVE,
                "parquet_sha256": hashlib.sha256(stored.read_bytes()).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "row_count": rows,
            }
        )
    )
    borg = tmp_path / "borg"
    borg.write_text(f'#!/usr/bin/env bash\ncat "{stored}"\n')
    borg.chmod(borg.stat().st_mode | stat.S_IEXEC)
    backup_env = tmp_path / "backup.env"
    backup_env.write_text(f"BORG_REPO=repo\nPATH={tmp_path}:{os.environ['PATH']}\n")
    fetch, out = tmp_path / "fetch", tmp_path / "out"
    fetch.mkdir()
    return {"cold": cold, "env": backup_env, "fetch": fetch, "out": out, "stored": stored}


def _run(s: dict[str, Path], first: str = DAY, last: str = DAY) -> int:
    return e.main(
        [
            *("--cold-bars-dir", str(s["cold"]), "--backup-env", str(s["env"])),
            *("--fetch-dir", str(s["fetch"]), "--out-dir", str(s["out"])),
            *("--from", first, "--to", last),
        ]
    )


def test_a_day_is_reduced_to_linear_v1_bybit_and_binance_and_the_fetch_deleted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _setup(tmp_path)
    assert _run(s) == 0
    reduced = s["out"] / e.reduced_name(DAY)
    manifest = json.loads(reduced.with_suffix(".manifest.json").read_text())
    assert manifest["rows_by_exchange"] == {"binance": 3, "bybit": 3}
    assert manifest["source_sha256"] == hashlib.sha256(s["stored"].read_bytes()).hexdigest()
    assert manifest["reduced_sha256"] == hashlib.sha256(reduced.read_bytes()).hexdigest()
    columns = [r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{reduced}'").fetchall()]  # noqa: S608
    assert tuple(columns) == e.COLUMNS
    assert list(s["fetch"].iterdir()) == []  # the full file this run fetched is gone
    assert "1.0" not in capsys.readouterr().out  # counts only, no prices
    assert _run(s) == 0  # a rerun keeps the reduced day
    assert '"already_reduced"' in capsys.readouterr().out


def test_a_full_file_that_was_already_present_is_kept(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    present = s["fetch"] / f"bars-{DAY}.parquet"
    present.write_bytes(s["stored"].read_bytes())
    assert _run(s) == 0
    assert present.exists()


def test_a_bad_fetch_reduces_nothing(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    receipt = s["cold"] / f"bars-{DAY}{RECEIPT_SUFFIX}"
    receipt.write_text(json.dumps({**json.loads(receipt.read_text()), "row_count": 99}))
    assert _run(s) == 1
    assert not (s["out"] / e.reduced_name(DAY)).exists()


@pytest.mark.parametrize(
    ("first", "last"), [("2026-08-12", "2026-08-12"), ("2026-09-29", "2026-09-29")]
)
def test_days_outside_the_registered_window_are_refused(
    tmp_path: Path, first: str, last: str
) -> None:
    s = _setup(tmp_path)
    with pytest.raises(SystemExit, match="outside the registered window"):
        _run(s, first, last)


def test_more_than_three_days_are_refused(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    with pytest.raises(SystemExit, match="per run"):
        _run(s, "2026-09-01", "2026-09-04")
