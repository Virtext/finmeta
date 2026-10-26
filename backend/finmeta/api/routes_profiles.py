"""S9d2: library overlay profiles — profile YAML validation.

Endpoints (all under /api):
  POST /profiles/{name}/yaml   -> validate one profile doc in place

``GET /libraries/{name}/sample`` (plus its ``sample_item_ids`` helper and the
``SAMPLE_SIZE`` constant) lived here until 2026-10-22. It existed only to feed
the "New sample" preview strip in ``SamplePreview.tsx``; the operator had that
widget removed, which left the endpoint with no caller, and asked for it to go
too (docs/SECURITY_REVIEW.md "D1"). The one surviving use of the old constant
-- capping how many items ``POST /api/overlays/preview`` accepts -- moved to
``routes_settings.MAX_PREVIEW_ITEMS``, which is where that cap is enforced.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext

router = APIRouter(prefix="/api", tags=["profiles"])


class ProfileYamlIn(BaseModel):
    """Body of ``POST /api/profiles/{name}/yaml`` — one profile's raw YAML."""

    yaml: str = Field(min_length=1, description="YAML document for the single profile.")


@router.post("/profiles/{name}/yaml")
def profile_yaml(body: ProfileYamlIn, name: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Validate a profile's YAML (shape + known keys) WITHOUT saving it.

    The editor uses this to check the raw view live; saving still goes through
    PUT /api/config so last-good/undo semantics apply exactly once."""
    import yaml as _yaml
    try:
        doc = _yaml.safe_load(body.yaml)
    except _yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"invalid YAML: {exc}")
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise HTTPException(status_code=422, detail="profile must be a mapping")
    allowed = {"style", "format_rules", "elements"}
    unknown = set(doc) - allowed
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown profile keys: {sorted(unknown)} (allowed: {sorted(allowed)})")
    els = doc.get("elements")
    if els is not None and (not isinstance(els, list) or not all(isinstance(e, str) for e in els)):
        raise HTTPException(status_code=422, detail="profile `elements` must be a list of strings")
    fr = doc.get("format_rules")
    if fr is not None:
        if not isinstance(fr, dict):
            raise HTTPException(status_code=422, detail="profile `format_rules` must be a mapping")
        for cat, rules in fr.items():
            if cat not in ("resolution", "dynamic_range", "audio"):
                raise HTTPException(status_code=422, detail=f"unknown format_rules category {cat!r}")
            if not isinstance(rules, list):
                raise HTTPException(status_code=422, detail=f"format_rules[{cat!r}] must be a list")
    style = doc.get("style")
    if style is not None and not isinstance(style, dict):
        raise HTTPException(status_code=422, detail="profile `style` must be a mapping")
    return {"ok": True, "name": name}


__all__ = ["router"]
