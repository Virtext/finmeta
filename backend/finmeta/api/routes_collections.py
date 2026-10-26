"""Preset collection catalog routes (SPEC S9f2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext
from finmeta.engine.collection_presets import KNOWN_PRESETS

router = APIRouter(prefix="/api/collection-presets", tags=["collections"])


@router.get("")
def list_collection_presets(ctx: AppContext = Depends(get_ctx)):
    """List the built-in preset collections (seasonal + chart) the operator
    can toggle on in ``Config.collection_presets``."""
    return {
        "presets": [
            {
                "id": p.id,
                "label": p.label,
                "description": p.description,
                "library_type": p.library_type,
                "kind": p.kind,
            }
            for p in KNOWN_PRESETS.values()
        ],
    }


__all__ = ["router"]
