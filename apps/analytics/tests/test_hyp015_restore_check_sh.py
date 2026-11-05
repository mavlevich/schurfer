"""Exercise the restore script across the image's initialization restart."""

import os
import shutil
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "infra" / "scripts" / "hyp015-restore-check.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def test_restore_waits_for_final_postgres_server(tmp_path: Path) -> None:
    script = tmp_path / "infra" / "scripts" / _SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copyfile(_SCRIPT, script)
    compose = tmp_path / "infra" / "docker" / "docker-compose.prod.yml"
    compose.parent.mkdir()
    compose.write_text("  image: timescale/timescaledb@sha256:test\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "mkdir": "exit 0\n",
        "sleep": "exit 0\n",
        "docker": """\
case "$1" in
    inspect) echo fake-network ;;
    run) echo fake-container ;;
    exec)
        if [ -e "$PROBE_SEEN" ]; then
            count=$(cat "$PROBE_SEEN")
        else
            count=0
        fi
        echo "$((count + 1))" > "$PROBE_SEEN"
        case " $* " in
            *' -h 127.0.0.1 '*)
                if [ "$count" -eq 0 ]; then exit 2; fi ;;
            *)
                if [ "$count" -ne 0 ]; then exit 1; fi ;;
        esac ;;
    compose) echo restore-reader-started ;;
    rm) exit 0 ;;
    *) exit 42 ;;
esac
""",
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}")
        stub.chmod(0o755)

    result = subprocess.run(  # noqa: S603
        [_BASH, str(script), "test-snapshot"],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PROBE_SEEN": str(tmp_path / "probes"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "restore-reader-started" in result.stdout
