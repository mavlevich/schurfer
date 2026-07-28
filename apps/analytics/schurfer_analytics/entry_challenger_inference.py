"""Compatibility facade for entry-family formal inference."""

from __future__ import annotations

from .challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerEpisode,
    ChallengerFormalResult,
    ChallengerInference,
    ClusterConcentration,
    InferenceReadiness,
    InferenceSettings,
    PairedInference,
    StrategyInference,
    build_challenger_inference,
)

ENTRY_INFERENCE_VERSION = "entry_challenger_formal_inference_v1"

InferenceEpisode = ChallengerEpisode
EntryChallengerInference = ChallengerInference


def build_entry_challenger_inference(
    episodes: tuple[InferenceEpisode, ...],
    variant_keys: tuple[str, ...],
    *,
    settings: InferenceSettings = DEFAULT_INFERENCE_SETTINGS,
) -> EntryChallengerInference:
    """Preserve the registered entry-family inference contract."""
    return build_challenger_inference(
        episodes,
        variant_keys,
        settings=settings,
        inference_version=ENTRY_INFERENCE_VERSION,
    )


__all__ = [
    "DEFAULT_INFERENCE_SETTINGS",
    "ENTRY_INFERENCE_VERSION",
    "ChallengerFormalResult",
    "ClusterConcentration",
    "EntryChallengerInference",
    "InferenceEpisode",
    "InferenceReadiness",
    "InferenceSettings",
    "PairedInference",
    "StrategyInference",
    "build_entry_challenger_inference",
]
