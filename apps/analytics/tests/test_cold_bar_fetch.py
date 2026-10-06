from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
from schurfer_analytics import cold_bar_fetch as f
from schurfer_analytics.cold_bar_gated_deletion_job import RECEIPT_SUFFIX

DAY = "2026-09-10"


def _parquet(path: Path, rows: int) -> str:
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT range AS bucket, 1.0 AS close FROM range(?)", [rows])
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup(
    tmp_path: Path,
    *,
    rows: int = 5,
    receipt: dict[str, Any] | None = None,
    manifest_bytes: int | None = None,
    borg_prelude: str = "",
) -> dict[str, Any]:
    stored = tmp_path / "stored.parquet"
    sha = _parquet(stored, rows)
    cold = tmp_path / "cold-bars"
    cold.mkdir()
    manifest = cold / f"bars-{DAY}.manifest.json"
    size = stored.stat().st_size if manifest_bytes is None else manifest_bytes
    manifest.write_text(json.dumps({"day": DAY, "file_bytes": size}))
    body = {
        "day": DAY,
        "archive_name": "bars-2026-09-18T19:16:45",
        "parquet_sha256": sha,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "row_count": rows,
    }
    body.update(receipt or {})
    (cold / f"bars-{DAY}{RECEIPT_SUFFIX}").write_text(json.dumps(body))
    borg = tmp_path / "borg"
    # Fake `borg extract --stdout repo::archive member`: serves the stored parquet only
    # for the right archive and member.
    borg.write_text(
        "#!/usr/bin/env bash\n"
        f"{borg_prelude}"
        f'[[ "$3" == "repo::bars-2026-09-18T19:16:45" && "$4" == *"bars-{DAY}.parquet" ]]'
        f' || {{ echo "no such member" >&2; exit 2; }}\n'
        f'cat "{stored}"\n'
    )
    borg.chmod(borg.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "out"
    out.mkdir()
    env = {"PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return {"cold": cold, "out": out, "env": env, "sha": sha, "stored": stored}


def _fetch(s: dict[str, Any], reserve_bytes: int = 0) -> f.Fetched:
    return f.fetch_day(
        DAY,
        cold_bars_dir=s["cold"],
        out_dir=s["out"],
        repo="repo",
        env=s["env"],
        reserve_bytes=reserve_bytes,
    )


def _rewrite_receipt(s: dict[str, Any], **changes: Any) -> None:
    path = s["cold"] / f"bars-{DAY}{RECEIPT_SUFFIX}"
    path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))


def test_a_day_is_fetched_only_when_sha_and_rows_match_its_receipt(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    got = _fetch(s)
    assert (got.rows, got.sha256, got.already_present) == (5, s["sha"], False)
    assert got.path.read_bytes() and not list(s["out"].glob("*.partial"))
    again = _fetch(s)  # same file already there: kept, not refetched
    assert again.already_present


def test_a_fetched_file_is_readable_by_the_deploy_user(tmp_path: Path) -> None:
    # The fetch runs as root in the container; a 0600 temp file left the bars
    # unreadable by the deploy user on the host.
    got = _fetch(_setup(tmp_path))
    assert stat.S_IMODE(got.path.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"parquet_sha256": "0" * 64}, "extracted sha256"),
        ({"row_count": 6}, "rows != receipt"),
        ({"archive_name": "bars-2026-01-01T00:00:00"}, "borg extract failed"),
        ({"day": "2026-09-11"}, "another day"),
    ],
)
def test_a_mismatch_is_refused_and_leaves_nothing(
    tmp_path: Path, override: dict[str, Any], match: str
) -> None:
    s = _setup(tmp_path, receipt=override)
    with pytest.raises(f.FetchError, match=match):
        _fetch(s)
    assert list(s["out"].iterdir()) == []


def test_an_existing_different_file_is_never_overwritten(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    (s["out"] / f"bars-{DAY}.parquet").write_bytes(b"something else")
    with pytest.raises(f.FetchError, match="sha256"):
        _fetch(s)
    assert (s["out"] / f"bars-{DAY}.parquet").read_bytes() == b"something else"


def test_an_already_present_file_is_recounted_against_the_receipt(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    _fetch(s)
    _rewrite_receipt(s, row_count=6)  # same bytes, but the receipt now disagrees on rows
    with pytest.raises(f.FetchError, match="5 rows != receipt 6"):
        _fetch(s)


def test_a_concurrent_winner_is_verified_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another run publishes the day between our check and our publish: the link must
    not replace it, and the winner is accepted only if it verifies."""
    s = _setup(tmp_path)
    real_link = os.link

    def racing_link(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(s["stored"].read_bytes())
        real_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    got = _fetch(s)
    assert got.already_present
    assert not list(s["out"].glob("*.partial"))


def test_an_already_present_0600_file_is_made_readable(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    dest = _fetch(s).path
    dest.chmod(0o600)  # what runs before the fix left behind
    again = _fetch(s)
    assert again.already_present
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644


def test_a_0600_concurrent_winner_is_made_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _setup(tmp_path)
    real_link = os.link

    def racing_link(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(s["stored"].read_bytes())
        Path(dst).chmod(0o600)
        real_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    got = _fetch(s)
    assert got.already_present
    assert stat.S_IMODE(got.path.stat().st_mode) == 0o644


def test_a_bad_concurrent_winner_is_refused_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _setup(tmp_path)
    real_link = os.link

    def racing_link(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(b"a different file")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(f.FetchError, match="sha256"):
        _fetch(s)
    assert (s["out"] / f"bars-{DAY}.parquet").read_bytes() == b"a different file"
    assert not list(s["out"].glob("*.partial"))


def test_a_fetch_that_would_eat_the_disk_reserve_is_refused(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    with pytest.raises(f.FetchError, match="reserve"):
        _fetch(s, reserve_bytes=10**18)
    assert list(s["out"].iterdir()) == []


def test_a_day_without_a_manifest_cannot_be_sized_and_is_refused(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    (s["cold"] / f"bars-{DAY}.manifest.json").unlink()
    with pytest.raises(f.FetchError, match="no manifest"):
        _fetch(s)


def test_a_manifest_the_receipt_did_not_pin_cannot_size_the_fetch(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    (s["cold"] / f"bars-{DAY}.manifest.json").write_text(json.dumps({"file_bytes": 1}))
    with pytest.raises(f.FetchError, match="manifest_sha256"):
        _fetch(s)
    assert list(s["out"].iterdir()) == []


def test_a_stream_larger_than_the_expected_size_is_cut_off(tmp_path: Path) -> None:
    """A manifest that understates the size (pinned, so consistent with the receipt)
    must not let the stream grow past what the reserve check allowed for."""
    s = _setup(tmp_path, manifest_bytes=100)
    with pytest.raises(f.FetchError, match="larger than the expected 100 bytes"):
        _fetch(s)
    assert list(s["out"].iterdir()) == []


def test_a_borg_that_floods_stderr_and_fails_does_not_hang(tmp_path: Path) -> None:
    """Far more stderr than a pipe buffer holds: with stderr on an unread pipe Borg would
    block and the fetch would wait forever."""
    flood = 'head -c 1000000 /dev/zero | tr "\\0" x >&2; echo; echo "repo locked" >&2; exit 2\n'
    s = _setup(tmp_path, borg_prelude=flood)
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            _fetch(s)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive(), "fetch hung on a full stderr pipe"
    [exc] = outcome
    assert isinstance(exc, f.FetchError) and "repo locked" in str(exc)
    assert list(s["out"].iterdir()) == []


def test_a_range_longer_than_max_days_is_refused_before_any_fetch(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    env_file = tmp_path / "backup.env"
    env_file.write_text("BORG_REPO=repo\n")
    argv = [
        "--cold-bars-dir", str(s["cold"]), "--backup-env", str(env_file),
        "--from", "2026-09-01", "--to", "2026-09-04", "--out-dir", str(s["out"]),
    ]  # fmt: skip
    with pytest.raises(SystemExit, match="more than --max-days 3"):
        f.main(argv)
    assert list(s["out"].iterdir()) == []


def test_a_day_without_a_receipt_is_not_archived(tmp_path: Path) -> None:
    s = _setup(tmp_path)
    with pytest.raises(f.FetchError, match="no offsite receipt"):
        f.fetch_day("2026-09-12", cold_bars_dir=s["cold"], out_dir=s["out"], repo="r", env={})


def test_days_between_is_inclusive() -> None:
    assert f.days_between(date(2026, 9, 9), date(2026, 9, 11)) == [
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
    ]
    with pytest.raises(ValueError, match="before"):
        f.days_between(date(2026, 9, 11), date(2026, 9, 9))
