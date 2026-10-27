"""Settings live in config/*.yaml; code reads them here. Environment variables only choose files and endpoints."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml


def repo_root() -> Path:
    env = os.environ.get("LIRA_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "config" / "review.yaml").exists():
            return p
    return Path.cwd()


def config_dir() -> Path:
    return Path(os.environ.get("LIRA_CONFIG_DIR", repo_root() / "config"))


def data_dir() -> Path:
    return Path(os.environ.get("LIRA_DATA_DIR", repo_root() / "data"))


def models_dir() -> Path:
    return Path(os.environ.get("LIRA_MODELS_DIR", repo_root() / "models"))


def reports_dir() -> Path:
    return Path(os.environ.get("LIRA_REPORTS_DIR", repo_root() / "reports"))


@cache
def load(name: str) -> dict[str, Any]:
    path = config_dir() / f"{name}.yaml"
    data = yaml.safe_load(path.read_text())
    override = os.environ.get(f"LIRA_{name.upper()}_OVERRIDE")
    if override:
        _merge(data, yaml.safe_load(override))
    return data


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def model_mode() -> str:
    """ "simulated" (default: tests, CI, demo) or "live" (Bedrock / Anthropic with credentials)."""
    return os.environ.get("LIRA_MODEL", "simulated")
