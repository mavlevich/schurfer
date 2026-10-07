"""infra/scripts/restore_check.py (ENG-025): the pure parts and the refusals.

The full backup-and-restore path needs Docker and a real TimescaleDB, so it is run
by hand (see docs/runbooks/offsite-backup-restore.md, "Automated restore drill");
these tests cover the logic that decides what is restored and when it refuses.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "infra/scripts/restore_check.py"
_spec = importlib.util.spec_from_file_location("restore_check", _SCRIPT)
assert _spec is not None and _spec.loader is not None
rc = importlib.util.module_from_spec(_spec)
sys.modules["restore_check"] = rc
_spec.loader.exec_module(rc)

TOC = """\
;
; Archive created at 2026-09-28 04:07:59 UTC
;
7; 2615 16390 SCHEMA - app schurfer
8; 2615 16391 SCHEMA - timeseries schurfer
2; 3079 16385 EXTENSION - timescaledb
5500; 0 0 COMMENT - EXTENSION timescaledb
301; 1247 16500 TYPE app trade_side schurfer
400; 1259 16600 TABLE app trades schurfer
401; 1259 16601 SEQUENCE app trades_id_seq schurfer
402; 0 0 SEQUENCE OWNED BY app trades_id_seq schurfer
403; 1259 16700 TABLE app strategies schurfer
404; 1259 16800 TABLE app trade_decisions schurfer
405; 1259 16900 TABLE timeseries bybit_momentum_bars_1m schurfer
406; 2604 16610 DEFAULT app trades id schurfer
407; 1255 16620 FUNCTION app trades_guard() schurfer
408; 1255 16621 FUNCTION app unrelated_fn() schurfer
900; 0 16600 TABLE DATA app trades schurfer
901; 0 16700 TABLE DATA app strategies schurfer
902; 0 16800 TABLE DATA app trade_decisions schurfer
903; 0 16900 TABLE DATA timeseries bybit_momentum_bars_1m schurfer
904; 0 0 SEQUENCE SET app trades_id_seq schurfer
1000; 2606 17000 CONSTRAINT app trades trades_pkey schurfer
1001; 1259 17001 INDEX app ix_trades_symbol schurfer
1002; 1259 17002 INDEX app ix_trade_decisions_at schurfer
1003; 2606 17003 FK CONSTRAINT app trades trades_strategy_fk schurfer
1004; 2606 17004 FK CONSTRAINT app trade_decisions fk_x schurfer
1005; 2620 17005 TRIGGER app trades trg_trades schurfer
1006; 0 0 ACL - SCHEMA app schurfer
"""

RECEIPT: dict[str, Any] = {
    "tables": ["app.strategies", "app.trades"],
    "owned_sequences": ["app.trades_id_seq"],
    "indexes": ["app.ix_trades_symbol", "app.trades_pkey"],
    "trigger_functions": ["app.trades_guard"],
}


def test_toc_lines_parse_multi_word_types() -> None:
    assert rc.parse_toc_line("900; 0 16600 TABLE DATA app trades schurfer") == (
        "TABLE DATA",
        ["app", "trades", "schurfer"],
    )
    assert rc.parse_toc_line("402; 0 0 SEQUENCE OWNED BY app s schurfer")[0] == "SEQUENCE OWNED BY"
    assert rc.parse_toc_line("; comment") is None
    assert rc.parse_toc_line("") is None


def test_the_restore_list_is_the_critical_set_closed_and_nothing_else() -> None:
    kept = rc.restore_list(TOC.splitlines(), RECEIPT)
    ids = [line.split(";")[0] for line in kept]
    # schema app, extension, the app type, both tables with data, the owned
    # sequence (all three entries), the default, the function the set's trigger
    # calls, the pkey, the mapped index, the FK and the trigger of the set
    assert ids == [
        "7", "2", "301", "400", "401", "402", "403", "406", "407",
        "900", "901", "904", "1000", "1001", "1003", "1005",
    ]  # fmt: skip
    joined = "\n".join(kept)
    excluded_names = (
        "timeseries", "trade_decisions", "COMMENT", "ACL", "ix_trade_decisions_at", "unrelated_fn",
    )  # fmt: skip
    for excluded in excluded_names:
        assert excluded not in joined


def test_a_dump_without_the_data_of_a_set_table_is_refused() -> None:
    toc = [line for line in TOC.splitlines() if not line.startswith("901;")]
    with pytest.raises(rc.CheckError, match="no data entry"):
        rc.restore_list(toc, RECEIPT)


def _fake_borg(tmp_path: Path, archives: dict[str, str]) -> None:
    store = tmp_path / "store"
    store.mkdir()
    for name, body in archives.items():
        (store / name).write_text(body)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    borg = bin_dir / "borg"
    borg.write_text(
        "#!/usr/bin/env bash\n"
        f'STORE="{store}"\n'
        'case "$1" in\n'
        '  list) ls "$STORE" ;;\n'
        '  extract) for a in "$@"; do case "$a" in ::*) cat "$STORE/${a#::}";; esac; done ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    docker = bin_dir / "docker"
    # `inspect` answers "gone" unless LEFTOVER is set: a drill container or volume
    # that survives removal.
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{tmp_path}/docker.log"\n'
        'if [[ "$*" == *inspect* ]]; then [[ -n "${LEFTOVER:-}" ]] && exit 0; exit 1; fi\n'
    )
    for f in (borg, docker):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"


def _receipt(archive: str, total: int = 1000, taken_at: str | None = None) -> str:
    from datetime import UTC, datetime

    return json.dumps(
        {
            "version": rc.RECEIPT_VERSION,
            "db_archive": archive,
            "snapshot_taken_at": taken_at or datetime.now(UTC).isoformat(),
            "tables": [],
            "total_relation_bytes": total,
        }
    )


def test_only_the_newest_db_archive_with_a_matching_receipt_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _fake_borg(
        tmp_path,
        {
            "db-2026-09-26T04:00:00": "dump",
            "dbreceipt-2026-09-26T04:00:00": _receipt("db-2026-09-26T04:00:00"),
            "db-2026-09-27T04:00:00": "dump",
            "dbreceipt-2026-09-27T04:00:00": _receipt("db-2026-09-27T04:00:00"),
            "db-2026-09-28T04:00:00": "dump without a receipt",
        },
    )
    archive, receipt = rc.newest_verified_archive()
    assert archive == "db-2026-09-27T04:00:00"
    assert receipt["db_archive"] == archive


def test_a_receipt_naming_another_archive_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _fake_borg(
        tmp_path,
        {
            "db-2026-09-27T04:00:00": "dump",
            "dbreceipt-2026-09-27T04:00:00": _receipt("db-2026-09-20T04:00:00"),
        },
    )
    with pytest.raises(rc.CheckError, match="does not belong"):
        rc.newest_verified_archive()


def test_too_little_disk_refuses_before_any_container_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _fake_borg(
        tmp_path,
        {
            "db-2026-09-27T04:00:00": "dump",
            "dbreceipt-2026-09-27T04:00:00": _receipt("db-2026-09-27T04:00:00"),
        },
    )
    state = tmp_path / "state"
    code = rc.main(
        [
            "check", "--image", "img", "--state-dir", str(state),
            "--reserve-bytes", str(10**18),
        ]
    )  # fmt: skip
    assert code == 1
    [record_path] = (state / "restore-check").glob("*.json")
    record = json.loads(record_path.read_text())
    assert record["ok"] is False and "reserve" in record["error"]
    assert not (state / "restore-check.stamp").exists()
    log = (tmp_path / "docker.log").read_text()
    assert "docker run" not in log and "run -d" not in log  # never started
    assert "rm -f" in log  # the cleanup still runs


def _check(state: Path, **extra: str) -> int:
    argv = ["check", "--image", "img", "--state-dir", str(state)]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", value]
    code: int = rc.main(argv)
    return code


def _record(state: Path) -> dict[str, Any]:
    [path] = (state / "restore-check").glob("*.json")
    record: dict[str, Any] = json.loads(path.read_text())
    return record


def test_a_stale_restore_point_fails_instead_of_certifying_an_old_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipts that stop being created must turn the drill red, not keep it green on
    an ever older archive."""
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _fake_borg(
        tmp_path,
        {
            "db-2026-09-01T04:00:00": "dump",
            "dbreceipt-2026-09-01T04:00:00": _receipt(
                "db-2026-09-01T04:00:00", taken_at="2026-09-01T04:00:00+00:00"
            ),
        },
    )
    state = tmp_path / "state"
    assert _check(state) == 1
    record = _record(state)
    assert "receipts have stopped" in record["error"]
    assert not (state / "restore-check.stamp").exists()
    assert "run -d" not in (tmp_path / "docker.log").read_text()


class _FakePsql:
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing

    def query(self, sql: str) -> list[str]:
        return self.missing if "IS NULL" in sql else ["app.trades"]


def test_a_missing_critical_table_refuses_the_receipt() -> None:
    with pytest.raises(rc.CheckError, match="missing from the database"):
        rc.critical_set(_FakePsql(["app.trades"]), ("app.trades", "app.strategies"))
    assert rc.critical_set(_FakePsql([]), ("app.trades",)) == ["app.trades"]


def test_a_drill_volume_that_survives_removal_is_a_failure_and_never_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", os.environ["PATH"])
    monkeypatch.setenv("LEFTOVER", "1")
    _fake_borg(
        tmp_path,
        {
            "db-2026-09-27T04:00:00": "dump",
            "dbreceipt-2026-09-27T04:00:00": _receipt("db-2026-09-27T04:00:00"),
        },
    )
    state = tmp_path / "state"
    assert _check(state, reserve_bytes="0") == 1
    record = _record(state)
    assert "could not be cleaned up" in record["error"]
    assert "volume" in record["cleanup_error"]
    assert not (state / "restore-check.stamp").exists()
    assert "run -d" not in (tmp_path / "docker.log").read_text()  # never started on it
