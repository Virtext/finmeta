"""Auth routes: first-run setup, rate-limited login, logout, status, me."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from finmeta.api.auth import (
    COOKIE_NAME, MIN_PASSWORD_LENGTH, SESSION_TTL, auth_enabled, client_key,
    cookie_secure,
)
from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
def status(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Public: lets the client decide whether to show login vs first-run setup."""
    return {
        "auth_enabled": auth_enabled(),
        "has_password": ctx.auth.has_password(),
        "first_run": ctx.auth.first_run(),
        # True when auth.yml exists but is unparseable: the UI can say so
        # instead of offering a login that can never succeed.
        "config_unreadable": ctx.auth.unreadable,
    }


class PasswordIn(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)


@router.post("/setup")
def setup(body: PasswordIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not auth_enabled():
        raise HTTPException(status_code=400, detail="auth is disabled (FINMETA_AUTH=off)")
    if ctx.auth.unreadable:
        # Existing-but-unreadable auth.yml: never let this unauthenticated
        # route mint a fresh admin account over it (SECURITY_REVIEW "H2").
        raise HTTPException(
            status_code=409,
            detail="auth.yml exists but could not be read; fix or remove it on "
                   "the server before running setup")
    if ctx.auth.has_password():
        raise HTTPException(status_code=409, detail="a password is already set")
    ctx.auth.set_password(body.password)
    return {"ok": True, "has_password": True, "first_run": False}


class LoginIn(BaseModel):
    username: str = "admin"
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response,
          ctx: AppContext = Depends(get_ctx)) -> dict:
    if not auth_enabled():
        return {"ok": True, "auth_enabled": False}
    key = client_key(request)
    if ctx.login_limiter.blocked(key):
        raise HTTPException(status_code=429,
                            detail="too many failed attempts; try again later")
    if not ctx.auth.verify(body.password, username=body.username):
        ctx.login_limiter.record_failure(key)
        raise HTTPException(status_code=401, detail="invalid credentials")
    ctx.login_limiter.reset(key)
    token = ctx.sessions.create()
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict",
                        secure=cookie_secure(), max_age=SESSION_TTL, path="/")
    return {"ok": True, "username": ctx.auth.username}


@router.post("/logout")
def logout(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        ctx.sessions.destroy(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(ctx: AppContext = Depends(get_ctx)) -> dict:
    return {"username": ctx.auth.username}


class AccountIn(BaseModel):
    current_password: str
    new_username: Optional[str] = None
    new_password: Optional[str] = None


@router.post("/account")
def update_account(body: AccountIn, request: Request,
                   ctx: AppContext = Depends(get_ctx)) -> dict:
    """Change username and/or password. Requires the current password.

    A wrong current password returns 400 with a message (NOT 401, which would
    log the user out); validation errors are also 400. Rate-limited on the
    same per-client buckets as login (CLAUDE.md §5 asks for both routes): a
    session alone should not buy unlimited guesses at the password, which is
    what the re-auth check here is protecting."""
    key = client_key(request)
    if ctx.login_limiter.blocked(key):
        raise HTTPException(status_code=429,
                            detail="too many failed attempts; try again later")
    if not ctx.auth.verify(body.current_password):
        ctx.login_limiter.record_failure(key)
        raise HTTPException(status_code=400, detail="current password is incorrect")
    ctx.login_limiter.reset(key)
    new_username = body.new_username.strip() if body.new_username else None
    if new_username is not None and not new_username:
        raise HTTPException(status_code=400, detail="new username must not be empty")
    if body.new_password is not None and len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"new password must be at least {MIN_PASSWORD_LENGTH} characters")
    if new_username is not None:
        ctx.auth.set_username(new_username)
    if body.new_password is not None:
        ctx.auth.update_password(body.new_password)
    return {"ok": True, "username": ctx.auth.username}


__all__ = ["router"]
