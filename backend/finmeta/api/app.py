"""FastAPI app factory for Finmeta (SPEC §11, §12).

REST under ``/api``; OpenAPI at ``/api/docs``. Serves the built UI from
``backend/finmeta/web`` (if present) at ``/``.
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from finmeta import __version__
from finmeta.api.state import AppContext
from finmeta.api.routes_core import router as core_router
from finmeta.api.routes_candidates import router as cand_router
from finmeta.api.routes_runs import router as runs_router
from finmeta.api.routes_config import router as config_router
from finmeta.api.routes_auth import router as auth_router
from finmeta.api.routes_settings import router as settings_router
from finmeta.api.routes_logo_packs import router as logo_packs_router
from finmeta.api.routes_profiles import router as profiles_router
from finmeta.api.routes_image_packs import router as image_packs_router
from finmeta.api.routes_collections import router as collections_router
from finmeta.api.routes_webhooks import router as webhooks_router
from finmeta.api.routes_scans import router as scans_router
from finmeta.api.routes_activity import router as activity_router
from finmeta.api.routes_backup import router as backup_router

# Where the built UI lives relative to this package (D1: resolved, not hardcoded
# to an absolute path).
_WEB_DIR_CANDIDATES = [
    # The Vite build lands in backend/finmeta/web (see /work/ui/vite.config.ts).
    Path(__file__).resolve().parent.parent / "web",
    # Fallback for in-package web assets.
    Path(__file__).resolve().parent / "web",
]


def _web_dir() -> Optional[Path]:
    for d in _WEB_DIR_CANDIDATES:
        if d.is_dir() and any(d.iterdir()):
            return d
    return None


#: Routes reachable without a session, by exact path.
_PUBLIC_EXACT_PATHS = frozenset({
    "/api/health", "/api/auth/login", "/api/auth/setup", "/api/auth/status",
})

#: ``/api/webhooks/jellyfin/<token>`` (Jellyfin's webhook plugin can't log
#: in) is the one public route with a variable path -- its own token
#: (validated inside the route, constant-time, against a stored hash) is
#: the gate instead of a session cookie. Matched structurally (exactly one
#: opaque path segment after the fixed prefix) rather than by excluding the
#: sibling management routes' names: audit 2026-11-11 "A3" found the
#: previous version excluded "status"/"regenerate" by name but not
#: "disable" (harmless today only because that route happens to have no
#: trailing segment at all, `DELETE /api/webhooks/jellyfin`) -- a default
#: that fails OPEN for any name someone forgets to add to the exclusion
#: list. This version fails closed: a path has to look like exactly one
#: token segment to be public at all, and even then one of the three
#: reserved words below (matching a sibling route's own path segment,
#: should one ever be added at this depth) still refuses it.
_WEBHOOK_TOKEN_RE = re.compile(r"^/api/webhooks/jellyfin/[^/]+$")
_WEBHOOK_RESERVED_WORDS = frozenset({"status", "regenerate", "disable"})


def _is_public(path: str) -> bool:
    """Routes reachable without a session (SPEC S9 §A)."""
    if path in _PUBLIC_EXACT_PATHS:
        return True
    m = _WEBHOOK_TOKEN_RE.match(path)
    if m is not None:
        token = path.rsplit("/", 1)[-1]
        return token not in _WEBHOOK_RESERVED_WORDS
    return False


#: Sent on every response (CLAUDE.md §5 "Sikkerhets-headere"). The UI is
#: fully self-contained -- its own bundle, images as data: URIs, same-origin
#: /api/proxy for everything remote -- so a strict CSP costs nothing here and
#: removes the whole class of injected-remote-script bugs. 'unsafe-inline' is
#: needed for style only (components set inline styles); script-src stays
#: 'self'. frame-ancestors/X-Frame-Options also hardens the SameSite=Strict
#: cookie against clickjacking, which is the CSRF defence this app relies on.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _install_middleware(app: FastAPI) -> None:
    """Enforce auth on /api (except public routes), set security headers, and
    redact secrets from every JSON response (errors, job logs, plan JSON) —
    single chokepoint (D2)."""
    from finmeta.api.auth import COOKIE_NAME, auth_enabled

    # NOTE: order matters and is the reverse of registration -- Starlette
    # builds the stack so the LAST-registered middleware is the OUTERMOST.
    # The auth middleware short-circuits with its own JSONResponse, so the
    # header middleware has to wrap it (i.e. be registered after it) or 401
    # responses go out with no security headers at all.
    @app.middleware("http")
    async def finmeta_auth_and_redaction(request: Request, call_next):
        ctx = request.app.state.context
        path = request.url.path
        is_api = path.startswith("/api")
        if is_api and auth_enabled() and not _is_public(path):
            if not ctx.sessions.valid(request.cookies.get(COOKIE_NAME)):
                return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        response = await call_next(request)
        ctype = response.headers.get("content-type", "")
        if is_api and "application/json" in ctype:
            try:
                # bytearray, not `body += chunk` on bytes: the latter copies
                # the whole accumulator per chunk (O(n^2)), which on a big
                # library listing meant hundreds of MB of pointless copying.
                buf = bytearray()
                async for chunk in response.body_iterator:
                    buf += chunk
                body = bytes(buf)
            except Exception:  # unreadable body: pass through
                return response
            if body:
                try:
                    red = ctx.redact(body.decode("utf-8")).encode("utf-8")
                except Exception:
                    red = body
                headers = {k: v for k, v in response.headers.items()
                           if k.lower() != "content-length"}
                return Response(content=red, status_code=response.status_code,
                                headers=headers, media_type="application/json")
        return response

    # Registered last => outermost, so it also decorates the 401 above.
    @app.middleware("http")
    async def finmeta_security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response


def create_app(context: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        context.start()
        try:
            yield
        finally:
            context.shutdown()

    app = FastAPI(
        title="Finmeta API", version=__version__, lifespan=lifespan,
        docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
        # Audit 2026-11-11 "D10": FastAPI's Swagger-UI OAuth2 redirect
        # helper, registered by default at /docs/oauth2-redirect -- the
        # only route this app ever had outside /api, so the auth
        # middleware above (gated on path.startswith("/api")) never saw
        # it. There is no OAuth2 flow configured here and the route
        # serves no data; None disables it from being registered at all.
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.context = context
    _install_middleware(app)

    app.include_router(core_router)
    app.include_router(cand_router)
    app.include_router(runs_router)
    app.include_router(config_router)
    app.include_router(auth_router)
    app.include_router(settings_router)
    app.include_router(logo_packs_router)
    app.include_router(profiles_router)
    app.include_router(image_packs_router)
    app.include_router(collections_router)
    app.include_router(webhooks_router)
    app.include_router(scans_router)
    app.include_router(activity_router)
    app.include_router(backup_router)

    # Serve the UI last so /api/* and /api/docs win.
    web = _web_dir()
    if web is not None:
        app.mount("/", StaticFiles(directory=str(web), html=True), name="ui")

    return app


def create_app_from_path(config_path: str, **kw) -> FastAPI:
    """Convenience: build a context (with defaults) and the app around it."""
    ctx = AppContext(config_path, **kw)
    return create_app(ctx)


__all__ = ["create_app", "create_app_from_path"]
