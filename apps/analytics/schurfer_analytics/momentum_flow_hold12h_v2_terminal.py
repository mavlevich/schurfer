"""HYP-015 confirmation: the HYP-012 v2 terminal state and its closed windows.

The confirmation design (docs/research/hyp015-confirmation-stage-v1.md) may be registered
only after HYP-012 v2 is terminal, and must exclude v2's closed windows
(docs/research/source-lead-forward-cohort-v2-administrative-stop.md): no later study
may use an asset's data inside a window a stopped v2 episode depended on, through any
feature, label or outcome window.

The confirmation starts after v2's terminal state, and every closed window ends before
it. So an overlap can only mean a registration error, and the read is refused rather
than quietly losing decisions. Before any claim it verifies:

- **the terminal state**: the pinned artifact's sha256, that it is v2's administrative
  stop, and that `app.formal_read_claims` records that stop (`admin_stopped`) for v2's
  study, contract and cohort with that same artifact as its fingerprint. A file alone
  never proves v2 is terminal;
- **the data the cohort actually depends on**: from the cohort's own WATCH rows, the
  earliest decision bucket, minus everything a WATCH decision can depend on before it.
  That is its state (`watch_cooldown_minutes`, `rearm_clear_minutes`; the longer one),
  the feature lookback (`lookback_minutes`) and an OI value up to
  `MOMENTUM_FLOW_OI_MAX_STALENESS_MINUTES` older than the window. Every closed window
  must end no later than that instant. Outcome windows start after their decisions, so
  the same bound covers them.

It reads no return and no v2 outcome: the artifact's windows, the stop record and WATCH
decision times only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .momentum_flow_protocol import MOMENTUM_FLOW_OI_MAX_STALENESS_MINUTES
from .source_lead_forward_cohort import CONTRACT_VERSION, SOURCE_LEAD_FORWARD_COHORT_START
from .source_lead_v2_administrative_stop import DECISION_STOP, STUDY_ID

if TYPE_CHECKING:
    from pathlib import Path

    from .momentum_flow_watch_contract import WatchContract

STATUS_ADMIN_STOPPED = "admin_stopped"


@dataclass(frozen=True)
class V2TerminalPin:
    """The v2 terminal artifact a confirmation contract names: file and sha256."""

    file_name: str
    sha256: str


def watch_dependency(contract: WatchContract) -> timedelta:
    """How far before its bucket a WATCH decision can depend on data."""
    state = max(contract.watch_cooldown_minutes, contract.rearm_clear_minutes)
    return timedelta(
        minutes=state + contract.lookback_minutes + MOMENTUM_FLOW_OI_MAX_STALENESS_MINUTES
    )


def load_v2_stop(directory: Path, pin: V2TerminalPin) -> list[dict[str, Any]]:
    """The pinned artifact's closed windows, after checking its bytes and its kind."""
    path = directory / pin.file_name
    if not path.is_file():
        raise SystemExit(f"formal-run refused: the v2 terminal artifact {path} is missing")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != pin.sha256:
        raise SystemExit(
            f"formal-run refused: {pin.file_name} is not the pinned v2 terminal artifact"
        )
    payload = json.loads(body)
    if (
        payload.get("study_id") != STUDY_ID
        or payload.get("contract_version") != CONTRACT_VERSION
        or payload.get("cohort_start") != SOURCE_LEAD_FORWARD_COHORT_START.isoformat()
        or payload.get("decision") != DECISION_STOP
    ):
        raise SystemExit(
            f"formal-run refused: {pin.file_name} is not the {STUDY_ID} v2 administrative stop"
        )
    windows = payload.get("closed_windows")
    if not isinstance(windows, list):
        raise SystemExit(f"formal-run refused: {pin.file_name} has no closed_windows list")
    return windows


async def verify_v2_stop_recorded(db_url: str, pin: V2TerminalPin) -> None:
    """v2's stop must be the database's terminal record, with this artifact."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT status, result_fingerprint FROM app.formal_read_claims "
            "WHERE study_id = %s AND contract_version = %s AND cohort_start = %s",
            (STUDY_ID, CONTRACT_VERSION, SOURCE_LEAD_FORWARD_COHORT_START),
        )
        row = await cur.fetchone()
    if row is None or row[0] != STATUS_ADMIN_STOPPED:
        raise SystemExit(
            f"formal-run refused: no recorded administrative stop for {STUDY_ID} "
            f"{CONTRACT_VERSION}; v2 is not terminal"
        )
    if row[1] != pin.sha256:
        raise SystemExit(
            f"formal-run refused: the recorded {STUDY_ID} stop names another artifact "
            f"than {pin.file_name}"
        )


def check_v2_closed_windows(
    windows: list[dict[str, Any]], *, earliest_use: datetime, pin: V2TerminalPin
) -> dict[str, Any]:
    """Every closed window ends no later than `earliest_use`, or the read is refused.
    Returns the summary the confirmation artifact records."""
    ends = [datetime.fromisoformat(w["end"]) for w in windows]
    late = sum(1 for end in ends if end > earliest_use)
    if late:
        raise SystemExit(
            f"formal-run refused: {late} v2 closed windows end after "
            f"{earliest_use.isoformat()}, inside data this cohort depends on "
            "(a registration error: the cohort must start after them)"
        )
    return {
        "v2_terminal_artifact": pin.file_name,
        "v2_terminal_sha256": pin.sha256,
        "closed_windows": len(windows),
        "latest_closed_window_end": max(ends).isoformat() if ends else None,
        "earliest_data_used": earliest_use.isoformat(),
    }
