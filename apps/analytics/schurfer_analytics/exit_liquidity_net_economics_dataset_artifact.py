"""`freeze()`/`read()`: turns `NetEconomicsRow` query results into a frozen
`research_dataset_artifact.py` artifact, and back.

Deliberately its own dataset (`exit_liquidity_net_economics_v1`), not
`exit_liquidity_calibration`'s -- see `exit_liquidity_net_economics.py`'s
own module docstring and `docs/research/exit-liquidity-adjusted-net-
economics-v1.md` for why. `read()` verifies `dataset_name`/
`dataset_version`/`schema_version`/`row_id_field` before trusting a
fingerprint's rows belong to this consumer, for the same reason
`exit_liquidity_calibration_dataset_artifact.py` does (colleague review,
2026-08-25, on the sibling module).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .exit_liquidity_calibration_report import ExitLiquidityFilters
from .exit_liquidity_net_economics import NetEconomicsRow
from .research_dataset_artifact import (
    ArtifactWriteOutcome,
    DatasetArtifactManifest,
    read_dataset_artifact,
    write_dataset_artifact,
)

DATASET_NAME = "exit_liquidity_net_economics"
DATASET_VERSION = "v1"
SCHEMA_VERSION = "net_economics_row_v1"
ROW_ID_FIELD = "trade_id"

# Matches exit_liquidity_net_economics_repository.py's own
# `.order_by(Trade.exit_at, Trade.id)`.
ROW_ORDER = "exit_at ascending, then trade_id ascending (repository's own ORDER BY)"

_DATETIME_FIELDS = ("entry_at", "exit_at", "observed_at")


class WrongDatasetArtifactError(ValueError):
    """Raised by `read()` when the artifact at a given fingerprint does not
    identify itself as an `exit_liquidity_net_economics` dataset of the
    version/schema this module knows how to decode."""


def _row_to_dict(row: NetEconomicsRow) -> dict[str, Any]:
    payload = asdict(row)
    for field_name in _DATETIME_FIELDS:
        value = payload[field_name]
        payload[field_name] = value.isoformat() if value is not None else None
    return payload


def _row_from_dict(payload: dict[str, Any]) -> NetEconomicsRow:
    kwargs = dict(payload)
    for field_name in _DATETIME_FIELDS:
        value = kwargs[field_name]
        kwargs[field_name] = datetime.fromisoformat(value) if value is not None else None
    return NetEconomicsRow(**kwargs)


def freeze(
    rows: tuple[NetEconomicsRow, ...],
    filters: ExitLiquidityFilters,
    *,
    code_revision: str,
    working_tree_dirty: bool,
    directory: str | None = None,
) -> tuple[ArtifactWriteOutcome, DatasetArtifactManifest | None]:
    """Freezes exactly the rows the caller already fetched -- never queries
    the database itself, so the frozen cohort matches exactly what the
    caller saw."""
    return write_dataset_artifact(
        dataset_name=DATASET_NAME,
        dataset_version=DATASET_VERSION,
        schema_version=SCHEMA_VERSION,
        rows=[_row_to_dict(row) for row in rows],
        row_id_field=ROW_ID_FIELD,
        row_order=ROW_ORDER,
        cohort={"since": filters.since.isoformat(), "until": filters.until.isoformat()},
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        directory=directory,
    )


def read(
    fingerprint: str, *, directory: str | None = None
) -> tuple[DatasetArtifactManifest, ExitLiquidityFilters, tuple[NetEconomicsRow, ...]]:
    """Raises `WrongDatasetArtifactError` if the artifact does not identify
    itself as one this module knows how to decode."""
    manifest, row_dicts = read_dataset_artifact(fingerprint, directory=directory)
    if (
        manifest.dataset_name != DATASET_NAME
        or manifest.dataset_version != DATASET_VERSION
        or manifest.schema_version != SCHEMA_VERSION
        or manifest.row_id_field != ROW_ID_FIELD
    ):
        raise WrongDatasetArtifactError(
            f"artifact {fingerprint} is "
            f"{manifest.dataset_name}@{manifest.dataset_version} "
            f"(schema={manifest.schema_version!r}, row_id_field={manifest.row_id_field!r}), "
            f"expected {DATASET_NAME}@{DATASET_VERSION} "
            f"(schema={SCHEMA_VERSION!r}, row_id_field={ROW_ID_FIELD!r})"
        )
    filters = ExitLiquidityFilters(
        since=datetime.fromisoformat(manifest.cohort["since"]),
        until=datetime.fromisoformat(manifest.cohort["until"]),
    )
    return manifest, filters, tuple(_row_from_dict(d) for d in row_dicts)


__all__ = [
    "DATASET_NAME",
    "DATASET_VERSION",
    "ROW_ID_FIELD",
    "SCHEMA_VERSION",
    "WrongDatasetArtifactError",
    "freeze",
    "read",
]
