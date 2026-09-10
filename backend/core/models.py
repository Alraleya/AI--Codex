"""Supported Codex production models for the workbench."""

from typing import Dict, Tuple


DEFAULT_MODEL = "gpt-5.6-luna"
MODEL_OPTIONS: Tuple[str, ...] = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
MODEL_LABELS: Dict[str, str] = {
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
}


def validate_model(model: str) -> str:
    if model not in MODEL_OPTIONS:
        raise ValueError("unsupported Codex model: %s" % model)
    return model
