"""Immutable serialization for the complete early_momentum evidence input."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .early_momentum_net_evidence import (
    EpisodeRow,
    ExitLiquidityRow,
    LegacyContextRow,
    RawDataset,
    TradeRow,
)
from .research_dataset_artifact import (
    ArtifactWriteOutcome,
    DatasetArtifactManifest,
    read_dataset_artifact,
    write_dataset_artifact,
)

DATASET_NAME = "early_momentum_net_evidence"
DATASET_VERSION = "v1"
SCHEMA_VERSION = "early_momentum_raw_dataset_v1"
ROW_ID_FIELD = "artifact_row_id"
ROW_ORDER = "kind ascending, then durable episode/trade/strategy identity ascending"

_EPISODE_DATETIMES = ("armed_at", "expires_at", "claimed_at", "claim_expires_at")
_TRADE_DATETIMES = ("entry_at", "exit_at")


class WrongDatasetArtifactError(ValueError):
    pass


def _encode_dataclass(row: Any, *, datetime_fields: tuple[str, ...]) -> dict[str, Any]:
    payload = asdict(row)
    for name in datetime_fields:
        value = payload[name]
        payload[name] = value.isoformat() if value is not None else None
    for name, value in tuple(payload.items()):
        if isinstance(value, bytes):
            payload[name] = value.hex()
    return payload


def _decode_datetimes(payload: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    result = dict(payload)
    for name in names:
        value = result[name]
        result[name] = datetime.fromisoformat(value) if value is not None else None
    return result


def _artifact_rows(
    dataset: RawDataset, legacy_context: tuple[LegacyContextRow, ...]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for episode in dataset.episodes:
        rows.append(
            {
                ROW_ID_FIELD: f"episode:{episode.episode_id}",
                "kind": "episode",
                "payload": _encode_dataclass(episode, datetime_fields=_EPISODE_DATETIMES),
            }
        )
    for trade in dataset.trades:
        rows.append(
            {
                ROW_ID_FIELD: f"trade:{trade.trade_id}",
                "kind": "trade",
                "payload": _encode_dataclass(trade, datetime_fields=_TRADE_DATETIMES),
            }
        )
    for observation in dataset.exit_liquidity:
        rows.append(
            {
                ROW_ID_FIELD: f"exit_liquidity:{observation.trade_id}",
                "kind": "exit_liquidity",
                "payload": asdict(observation),
            }
        )
    for legacy_row in legacy_context:
        rows.append(
            {
                ROW_ID_FIELD: f"legacy:{legacy_row.setup_context_strategy}",
                "kind": "legacy_context",
                "payload": asdict(legacy_row),
            }
        )
    return sorted(rows, key=lambda row: str(row[ROW_ID_FIELD]))


def freeze(
    dataset: RawDataset,
    legacy_context: tuple[LegacyContextRow, ...],
    *,
    registration: dict[str, Any],
    code_revision: str,
    working_tree_dirty: bool,
    directory: str | None = None,
) -> tuple[ArtifactWriteOutcome, DatasetArtifactManifest | None]:
    # Registration is part of `cohort`, rather than only `extra`, because
    # generic artifact fingerprints deliberately do not commit `extra`.
    # This matters most for an empty checkpoint: two different strategy or
    # runtime-policy registrations with the same timestamps and zero rows
    # must never collapse onto one fingerprint.
    return write_dataset_artifact(
        dataset_name=DATASET_NAME,
        dataset_version=DATASET_VERSION,
        schema_version=SCHEMA_VERSION,
        rows=_artifact_rows(dataset, legacy_context),
        row_id_field=ROW_ID_FIELD,
        row_order=ROW_ORDER,
        cohort={
            "start": dataset.cohort_start.isoformat(),
            "end": dataset.cohort_end.isoformat(),
            "registration": registration,
        },
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        extra={"db_snapshot_at": dataset.db_snapshot_at.isoformat()},
        allow_empty=True,
        directory=directory,
    )


def read(
    fingerprint: str, *, directory: str | None = None
) -> tuple[
    DatasetArtifactManifest,
    RawDataset,
    tuple[LegacyContextRow, ...],
    dict[str, Any],
]:
    manifest, rows = read_dataset_artifact(fingerprint, directory=directory)
    if (
        manifest.dataset_name != DATASET_NAME
        or manifest.dataset_version != DATASET_VERSION
        or manifest.schema_version != SCHEMA_VERSION
        or manifest.row_id_field != ROW_ID_FIELD
    ):
        raise WrongDatasetArtifactError(
            f"artifact {fingerprint} is not {DATASET_NAME}@{DATASET_VERSION}/{SCHEMA_VERSION}"
        )
    episodes: list[EpisodeRow] = []
    trades: list[TradeRow] = []
    exits: list[ExitLiquidityRow] = []
    legacy: list[LegacyContextRow] = []
    for row in rows:
        kind = row.get("kind")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise WrongDatasetArtifactError(f"artifact row has invalid payload: {row!r}")
        if kind == "episode":
            decoded = _decode_datetimes(payload, _EPISODE_DATETIMES)
            decoded["contract_sha256"] = bytes.fromhex(decoded["contract_sha256"])
            episodes.append(EpisodeRow(**decoded))
        elif kind == "trade":
            trades.append(TradeRow(**_decode_datetimes(payload, _TRADE_DATETIMES)))
        elif kind == "exit_liquidity":
            exits.append(ExitLiquidityRow(**payload))
        elif kind == "legacy_context":
            legacy.append(LegacyContextRow(**payload))
        else:
            raise WrongDatasetArtifactError(f"artifact row has unknown kind: {kind!r}")
    registration = manifest.cohort.get("registration")
    if not isinstance(registration, dict):
        raise WrongDatasetArtifactError("artifact is missing its cohort registration")
    dataset = RawDataset(
        cohort_start=datetime.fromisoformat(manifest.cohort["start"]),
        cohort_end=datetime.fromisoformat(manifest.cohort["end"]),
        db_snapshot_at=datetime.fromisoformat(manifest.extra["db_snapshot_at"]),
        episodes=tuple(episodes),
        trades=tuple(trades),
        exit_liquidity=tuple(exits),
    )
    return manifest, dataset, tuple(legacy), registration


__all__ = [
    "DATASET_NAME",
    "DATASET_VERSION",
    "SCHEMA_VERSION",
    "WrongDatasetArtifactError",
    "freeze",
    "read",
]
