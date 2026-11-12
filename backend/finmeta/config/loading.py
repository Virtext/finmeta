"""Load and validate config/config.yml into a Config model."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

from finmeta.config.models import Config


class ConfigError(ValueError):
    """Raised for any config problem (parse, validation, missing env)."""


def resolve_env(name: str) -> str:
    """Return the value of environment variable ``name``.

    Raises a ConfigError that names the missing variable; never prints values.
    """
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"environment variable {name} is not set")
    return value


def load_config(path) -> Config:
    """Read and validate a YAML config file into a Config model.

    Audit 2026-11-11 "B7": a missing, deleted or otherwise unreadable
    config.yml (the far end of a torn write, or any other filesystem
    surprise) used to raise a bare ``OSError``/``FileNotFoundError`` --
    correct in the sense that nothing papers over a real problem, but a
    raw traceback where the caller (``AppContext.__init__``, which has no
    handling for this at all) gets no actionable message naming the path.
    Wrapped in the same ``ConfigError`` every other config problem here
    already raises.
    """
    p = Path(path)
    try:
        raw_text = p.read_text()
    except OSError as exc:
        raise ConfigError(f"could not read config file {p}: {exc}") from exc
    try:
        data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if data is None:
        data: Dict[str, Any] = {}
    if not isinstance(data, dict):
        raise ConfigError(f"top-level config must be a mapping, got {type(data).__name__}")
    try:
        return Config(**data)
    except Exception as exc:  # pydantic.ValidationError or nested ValueError
        raise ConfigError(f"invalid config: {exc}") from exc


__all__ = ["ConfigError", "load_config", "resolve_env"]
