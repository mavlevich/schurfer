"""Frozen capture-cohort boundary acceptance for the momentum-flow episode
study.

`capture_epoch_started_at` (the momentum-capture epoch's own started_at_ms)
used to be re-derived FRESH from `market:momentumcapture:health` on every
single run, per the CLI's own original help text ("never a value relayed
second-hand, and never defaulted, since a restart moves it"). That advice
was right about WHERE the value comes from and wrong about how OFTEN to
re-derive it: momentum-capture restarting between two runs of this report
would silently hand the same logical report a DIFFERENT cohort boundary on
its next run, moving `dataset_since` forward and discarding whatever
history had already accumulated under the earlier boundary (amended after
third colleague review, before any real run -- this stopped being
hypothetical once momentum-capture actually restarted in production while
an earlier epoch value was still the one this research line had been using).

For HYP-014's own measurement prerequisites, the capture-cohort boundary
must be a human-approved decision made ONCE and then reused for every
subsequent run, not a value that silently drifts with the collector's own
uptime. This module persists that decision to a small JSON state file and
refuses a run whose `--capture-epoch-started-at` differs from the already-
accepted value unless the operator explicitly passes `--accept-new-
cohort-boundary` -- a deliberate, logged re-baseline decision, never an
automatic default.

Every `docker compose run --rm --entrypoint <report> analytics ...`
invocation (every `prod-*-report` Makefile target) is a fresh, disposable
container; state written inside it does not survive past that one run
unless the container mounts a host directory for it. See the `analytics`
service's own `MOMENTUM_FLOW_EPISODE_STUDY_COHORT_STATE_PATH` /
`../../runtime:/runtime` entries in `infra/docker/docker-compose.prod.yml`
(and `.dev.yml`) -- without that mount, this module's own freeze guarantee
would silently do nothing in the real deployment.

This module does NOT track capture restarts/gaps as their own provenance
or data-quality intervals within an already-accepted cohort -- that is
acknowledged, explicit follow-up work (see docs/research/momentum-flow-
episode-study-v1.md). What it guarantees is that the cohort's own START
does not move without a human decision recorded here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

COHORT_STATE_ENV_VAR = "MOMENTUM_FLOW_EPISODE_STUDY_COHORT_STATE_PATH"
DEFAULT_COHORT_STATE_PATH = "runtime/momentum-flow-episode-study-cohort.json"


@dataclass(frozen=True)
class CohortAcceptance:
    capture_cohort_started_at: datetime
    accepted_at: datetime


class CohortBoundaryConflictError(ValueError):
    """Raised when a run's requested capture-cohort boundary differs from
    the already-accepted one and the operator did not explicitly opt into
    re-baselining via `--accept-new-cohort-boundary`."""


def resolve_cohort_state_path(explicit: str | None, *, env: Mapping[str, str]) -> Path:
    """`explicit` (a CLI flag) wins over the environment variable, which
    wins over the relative on-disk default -- the same override precedence
    used throughout this project's CLIs."""
    return Path(explicit or env.get(COHORT_STATE_ENV_VAR) or DEFAULT_COHORT_STATE_PATH)


def parse_accepted_cohort(payload: str) -> CohortAcceptance:
    data = json.loads(payload)
    return CohortAcceptance(
        capture_cohort_started_at=datetime.fromisoformat(data["capture_cohort_started_at"]),
        accepted_at=datetime.fromisoformat(data["accepted_at"]),
    )


def serialize_accepted_cohort(acceptance: CohortAcceptance) -> str:
    return (
        json.dumps(
            {
                "capture_cohort_started_at": acceptance.capture_cohort_started_at.isoformat(),
                "accepted_at": acceptance.accepted_at.isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def resolve_capture_cohort_started_at(
    *,
    requested: datetime,
    accepted: CohortAcceptance | None,
    accept_new_cohort: bool,
    now: datetime,
) -> tuple[datetime, CohortAcceptance, bool]:
    """Pure decision logic -- unit-testable without touching a filesystem.

    Returns `(capture_cohort_started_at_to_use, acceptance_record, changed)`.
    `changed` is True only when the caller must persist a NEW record (first
    acceptance, or an explicit re-baseline) -- a normal run that re-supplies
    the already-accepted value does not need to rewrite anything.

    - No prior acceptance on record: `requested` is accepted as the new
      frozen boundary.
    - Prior acceptance matches `requested` exactly: reused as-is -- the
      expected steady state, where the operator re-supplies the SAME
      already-accepted value on every run (they always should).
    - Prior acceptance differs from `requested` and `accept_new_cohort` is
      False: refuse. This is exactly the "capture restarted and Redis now
      reports a newer started_at_ms" failure mode this module exists to
      catch -- the operator must pass `accept_new_cohort=True` to make a
      deliberate, explicit decision to re-baseline, never as a silent
      default.
    - Prior acceptance differs and `accept_new_cohort` is True: `requested`
      is accepted as a new frozen record -- a genuine, human-decided
      re-baseline.
    """
    if requested.utcoffset() is None:
        raise ValueError("requested capture-cohort boundary must be timezone-aware")
    if accepted is None:
        fresh = CohortAcceptance(capture_cohort_started_at=requested, accepted_at=now)
        return requested, fresh, True
    if accepted.capture_cohort_started_at == requested:
        return accepted.capture_cohort_started_at, accepted, False
    if not accept_new_cohort:
        raise CohortBoundaryConflictError(
            "capture cohort boundary already frozen at "
            f"{accepted.capture_cohort_started_at.isoformat()} (accepted "
            f"{accepted.accepted_at.isoformat()}); got a different value "
            f"{requested.isoformat()} for --capture-epoch-started-at -- "
            "pass --accept-new-cohort-boundary to deliberately re-baseline "
            "this research line's cohort (this drops comparability with "
            "any earlier report run against the old boundary)"
        )
    return requested, CohortAcceptance(capture_cohort_started_at=requested, accepted_at=now), True


def load_accepted_cohort(path: Path) -> CohortAcceptance | None:
    if not path.exists():
        return None
    return parse_accepted_cohort(path.read_text())


def save_accepted_cohort(path: Path, acceptance: CohortAcceptance) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_accepted_cohort(acceptance))
