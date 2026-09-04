"""Immutable serialization + cohort-drift lock for HYP-016's own frozen
discovery cohort (research/cex-activity-discovery-completion-v1).

Consumer-specific wrapper over `research_dataset_artifact.py`, mirroring
`early_momentum_net_evidence_dataset_artifact.py`'s own established
pattern for this codebase: one row per `OutcomeSignalEpisode`, carrying its
own signal path AND the full ordered list of its control requests/paths --
never only the already-selected control, since that would make
`select_matched_pairs`' own matching impossible to independently
reproduce from the artifact alone.

## Why a cohort-drift lock, not just `write_dataset_artifact`'s own
## content-fingerprint addressing

`write_dataset_artifact` already gives first-writer-wins BY CONTENT: two
runs producing byte-identical rows always resolve to the same fingerprint
(`ALREADY_EXISTS`). That is NOT what STOPPING_RULE-style "evaluate exactly
once" needs here -- it only prevents duplicate storage of the SAME
content, it does nothing to stop TWO DIFFERENT contents (a genuine
drift -- e.g. a late-arriving historical bar correction changing an exact
path's own outcome between two freeze attempts) from both being
successfully written as two separate, independently-addressable
artifacts, with nothing to say which one is "the" result for this cohort.

`claim_authoritative_fingerprint` below closes that gap with a second,
SEPARATELY-addressed lock file, keyed by `cohort_key_hash` (a hash of the
frozen parameters themselves -- since/until/exchange/market_type/
capture_version/directions/control-boundary-policy -- not of the rows),
written with `O_CREAT | O_EXCL` (atomic create-if-absent, no read-then-
write race between two concurrent freeze attempts). The FIRST successful
freeze for a given cohort claims that lock, recording its own content
fingerprint as authoritative; every later freeze attempt for the SAME
cohort must reproduce the SAME content fingerprint or `freeze()` raises
`CohortDriftDetectedError` rather than silently treating a different
result as current. There is no automatic "resolve the drift" path --
recovering from a genuine drift is an explicit, reviewed amendment
procedure (a new dataset/schema version), not something this module
decides on its own.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .cex_activity_discovery import (
    MISSING_PATH_RESULT_REASON,
    ExactPricePath,
    OutcomeSignalEpisode,
    PathRequest,
    signal_request,
)
from .research_dataset_artifact import (
    ArtifactWriteOutcome,
    DatasetArtifactManifest,
    ResearchDatasetArtifactCorruptError,
    ResearchDatasetArtifactWriteError,
    artifact_dir,
    read_dataset_artifact,
    write_dataset_artifact,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

DATASET_NAME = "cex_activity_hyp016_discovery"
DATASET_VERSION = "cex_activity_hyp016_discovery_v1"
SCHEMA_VERSION = "cex_activity_hyp016_dataset_schema_v1"
ROW_ID_FIELD = "row_id"
ROW_ORDER = "trigger_at ascending, then episode_id ascending"

_COHORT_LOCK_SUBDIR = "cex_activity_hyp016_cohort_locks"


class CohortDriftDetectedError(Exception):
    """Raised by `freeze()` when a freshly-computed content fingerprint for
    an already-registered cohort differs from the one an earlier
    successful freeze already recorded as authoritative for that SAME
    cohort. Never treated as "the new one is more current" -- the
    underlying data this cohort depends on appears to have changed since
    the first successful freeze (a late backfill/correction is the
    realistic cause), which is an operator-investigated event, not
    something this module resolves silently. Recovering requires an
    explicit, reviewed amendment (a new dataset/schema version), not a
    retry of `freeze()`."""


class WrongDatasetArtifactError(ValueError):
    """Raised by `read()` when a fingerprint resolves to a real, valid
    artifact that is NOT this dataset -- reading it as one anyway (e.g. a
    caller accidentally passing another consumer's fingerprint) would
    silently corrupt the discovery report's own funnel/statistics with
    unrelated rows shaped just similarly enough to not crash outright."""


def build_cohort(
    *,
    hypothesis_id: str,
    since: datetime,
    until_exclusive: datetime,
    exchange: str,
    market_type: str,
    capture_version: str,
    directions: Sequence[str],
    control_boundary_policy_version: str,
) -> dict[str, Any]:
    """The one `cohort` dict used for BOTH `write_dataset_artifact`'s own
    content-fingerprint scoping AND `claim_authoritative_fingerprint`'s own
    cohort-lock key -- these must always be identical (both describe "what
    discovery run is this", not "what did it find"), so they are built in
    exactly one place rather than independently at two call sites that
    could drift apart."""
    return {
        "hypothesis_id": hypothesis_id,
        "since": since.isoformat(),
        "until_exclusive": until_exclusive.isoformat(),
        "exchange": exchange,
        "market_type": market_type,
        "capture_version": capture_version,
        "directions": sorted(directions),
        "control_boundary_policy_version": control_boundary_policy_version,
    }


def _encode_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _encode_path(path: ExactPricePath | None) -> dict[str, Any]:
    if path is None:
        return {"present": False, "unresolved_reason": MISSING_PATH_RESULT_REASON}
    payload = asdict(path)
    for field in ("trigger_at", "entry_at", "first_up_25_at", "first_down_25_at"):
        payload[field] = _encode_datetime(payload[field])
    payload["present"] = True
    payload["unresolved_reason"] = path.unresolved_reason
    return payload


def _decode_path(payload: dict[str, Any]) -> ExactPricePath | None:
    if not payload["present"]:
        return None
    return ExactPricePath(
        request_id=payload["request_id"],
        symbol=payload["symbol"],
        trigger_at=datetime.fromisoformat(payload["trigger_at"]),
        entry_at=datetime.fromisoformat(payload["entry_at"]),
        entry_price=payload["entry_price"],
        observed_minutes=payload["observed_minutes"],
        max_high=payload["max_high"],
        min_low=payload["min_low"],
        first_up_25_at=(
            datetime.fromisoformat(payload["first_up_25_at"]) if payload["first_up_25_at"] else None
        ),
        first_down_25_at=(
            datetime.fromisoformat(payload["first_down_25_at"])
            if payload["first_down_25_at"]
            else None
        ),
    )


def _encode_request(request: PathRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "symbol": request.symbol,
        "trigger_at": request.trigger_at.isoformat(),
        "entry_at": request.entry_at.isoformat(),
    }


def _decode_request(payload: dict[str, Any]) -> PathRequest:
    return PathRequest(
        request_id=payload["request_id"],
        symbol=payload["symbol"],
        trigger_at=datetime.fromisoformat(payload["trigger_at"]),
        entry_at=datetime.fromisoformat(payload["entry_at"]),
    )


def build_rows(
    *,
    episodes: Sequence[OutcomeSignalEpisode],
    signal_paths: dict[str, ExactPricePath],
    controls_by_episode: dict[int, tuple[PathRequest, ...]],
    control_paths: dict[str, ExactPricePath],
) -> list[dict[str, Any]]:
    """Pure -- no I/O. One row per episode, carrying its own signal path
    AND the full ordered list of its own control requests/paths (never
    only the already-selected control -- see module docstring)."""
    ordered = sorted(episodes, key=lambda item: (item.trigger_at, item.episode_id))
    rows: list[dict[str, Any]] = []
    for episode in ordered:
        signal_path = signal_paths.get(signal_request(episode).request_id)
        control_requests = controls_by_episode.get(episode.episode_id, ())
        rows.append(
            {
                ROW_ID_FIELD: episode.signal_id,
                "episode_id": episode.episode_id,
                "source": episode.source,
                "exchange": episode.exchange,
                "symbol": episode.symbol,
                "direction": episode.direction,
                "trigger_at": episode.trigger_at.isoformat(),
                "entry_at": episode.entry_at.isoformat(),
                "signal_value": episode.signal_value,
                "signal_path": _encode_path(signal_path),
                "control_requests": [_encode_request(request) for request in control_requests],
                "control_paths": [
                    _encode_path(control_paths.get(request.request_id))
                    for request in control_requests
                ],
            }
        )
    return rows


def _cohort_key_hash(cohort: dict[str, Any]) -> str:
    payload = json.dumps(cohort, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _cohort_lock_path(cohort: dict[str, Any], *, directory: str | Path | None = None) -> Path:
    root = artifact_dir(directory)
    key_hash = _cohort_key_hash(cohort)
    return root / _COHORT_LOCK_SUBDIR / key_hash[:2] / f"{key_hash}.json"


def read_authoritative_fingerprint(
    cohort: dict[str, Any], *, directory: str | Path | None = None
) -> str | None:
    """None if this cohort has never had a successful freeze."""
    path = _cohort_lock_path(cohort, directory=directory)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchDatasetArtifactCorruptError(
            f"cohort lock at {path} is unreadable or not valid JSON: {exc}"
        ) from exc
    fingerprint = payload.get("authoritative_fingerprint")
    if not isinstance(fingerprint, str):
        raise ResearchDatasetArtifactCorruptError(
            f"cohort lock at {path} has no valid authoritative_fingerprint"
        )
    return fingerprint


def claim_authoritative_fingerprint(
    cohort: dict[str, Any], fingerprint: str, *, directory: str | Path | None = None
) -> str:
    """Atomic create-if-absent (`O_CREAT | O_EXCL`) -- no read-then-write
    race between two concurrent freeze attempts for the same cohort.
    Returns the fingerprint that IS now authoritative for this cohort:
    `fingerprint` itself if this call created the lock or an earlier one
    already recorded the exact same value; raises `CohortDriftDetectedError`
    if a DIFFERENT value was already recorded."""
    path = _cohort_lock_path(cohort, directory=directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"cohort": cohort, "authoritative_fingerprint": fingerprint}, sort_keys=True, indent=2
    )
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        existing = read_authoritative_fingerprint(cohort, directory=directory)
        if existing != fingerprint:
            raise CohortDriftDetectedError(
                f"cohort_key={_cohort_key_hash(cohort)}: authoritative fingerprint is "
                f"{existing!r}, but this freeze computed {fingerprint!r} for the SAME "
                f"cohort ({json.dumps(cohort, sort_keys=True)}). The underlying data this "
                "cohort depends on appears to have changed since the first successful "
                "freeze (a late backfill/correction is the realistic cause) -- "
                "investigate before treating either fingerprint as current; do not "
                "silently prefer the newer one."
            ) from None
        return existing
    else:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        return fingerprint


def freeze(
    *,
    cohort: dict[str, Any],
    rows: list[dict[str, Any]],
    code_revision: str,
    working_tree_dirty: bool,
    extra: dict[str, Any] | None = None,
    directory: str | Path | None = None,
) -> DatasetArtifactManifest:
    """Writes the content-addressed artifact, then claims (or verifies
    against) this cohort's own authoritative-fingerprint lock. Raises
    `ResearchDatasetArtifactWriteError` if the artifact itself could not be
    durably written, or `CohortDriftDetectedError` if this run's own
    content differs from an already-claimed authoritative fingerprint for
    the SAME cohort -- either way, never returns a manifest that is not
    genuinely the one-and-only frozen result for this cohort."""
    outcome, manifest = write_dataset_artifact(
        dataset_name=DATASET_NAME,
        dataset_version=DATASET_VERSION,
        schema_version=SCHEMA_VERSION,
        rows=rows,
        row_id_field=ROW_ID_FIELD,
        row_order=ROW_ORDER,
        cohort=cohort,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        extra=extra,
        directory=directory,
    )
    if outcome not in (ArtifactWriteOutcome.CREATED, ArtifactWriteOutcome.ALREADY_EXISTS):
        raise ResearchDatasetArtifactWriteError(
            f"failed to persist the {DATASET_NAME} artifact: {outcome.value}"
        )
    assert manifest is not None
    claim_authoritative_fingerprint(cohort, manifest.fingerprint, directory=directory)
    return manifest


def read(
    fingerprint: str, *, directory: str | Path | None = None
) -> tuple[
    DatasetArtifactManifest,
    tuple[OutcomeSignalEpisode, ...],
    dict[str, ExactPricePath],
    dict[int, tuple[PathRequest, ...]],
    dict[str, ExactPricePath],
]:
    """Raises `ResearchDatasetArtifactCorruptError` (via
    `read_dataset_artifact`) on any integrity failure, or
    `WrongDatasetArtifactError` if `fingerprint` resolves to a real
    artifact that is not this dataset. Returns `(manifest, episodes,
    signal_paths, controls_by_episode, control_paths)` -- exactly the
    shape `select_matched_pairs`/`build_direction_results` already
    consume, so the evaluate path can call them unchanged."""
    manifest, raw_rows = read_dataset_artifact(fingerprint, directory=directory)
    if (
        manifest.dataset_name != DATASET_NAME
        or manifest.dataset_version != DATASET_VERSION
        or manifest.schema_version != SCHEMA_VERSION
    ):
        raise WrongDatasetArtifactError(
            f"fingerprint {fingerprint} resolves to "
            f"{manifest.dataset_name}/{manifest.dataset_version}/{manifest.schema_version}, "
            f"not {DATASET_NAME}/{DATASET_VERSION}/{SCHEMA_VERSION}"
        )

    episodes: list[OutcomeSignalEpisode] = []
    signal_paths: dict[str, ExactPricePath] = {}
    controls_by_episode: dict[int, tuple[PathRequest, ...]] = {}
    control_paths: dict[str, ExactPricePath] = {}
    for row in raw_rows:
        episode = OutcomeSignalEpisode(
            episode_id=row["episode_id"],
            signal_id=row[ROW_ID_FIELD],
            source=row["source"],
            exchange=row["exchange"],
            symbol=row["symbol"],
            direction=row["direction"],
            trigger_at=datetime.fromisoformat(row["trigger_at"]),
            entry_at=datetime.fromisoformat(row["entry_at"]),
            signal_value=row["signal_value"],
        )
        episodes.append(episode)
        signal_path = _decode_path(row["signal_path"])
        if signal_path is not None:
            signal_paths[signal_request(episode).request_id] = signal_path
        requests = tuple(_decode_request(item) for item in row["control_requests"])
        controls_by_episode[episode.episode_id] = requests
        for request, path_payload in zip(requests, row["control_paths"], strict=True):
            control_path = _decode_path(path_payload)
            if control_path is not None:
                control_paths[request.request_id] = control_path

    ordered_episodes = tuple(sorted(episodes, key=lambda item: (item.trigger_at, item.episode_id)))
    return manifest, ordered_episodes, signal_paths, controls_by_episode, control_paths
