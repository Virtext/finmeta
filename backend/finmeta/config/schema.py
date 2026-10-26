"""Export / verify the JSON Schema for the config model."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from finmeta.config.models import Config


def current_schema() -> Dict[str, Any]:
    return Config.model_json_schema()


def export_schema(path) -> Path:
    """Write the JSON Schema for Config to ``path`` (deterministic order)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    schema = current_schema()
    p.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    return p


def schema_is_current(path) -> bool:
    """True if the schema file on disk matches the live model schema."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        on_disk: Dict[str, Any] = json.loads(p.read_text())
    except json.JSONDecodeError:
        return False
    return on_disk == current_schema()


__all__ = ["current_schema", "export_schema", "schema_is_current"]
