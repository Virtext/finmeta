"""Secret descriptors + single-source secret resolution (S9).

Kept in the config package (NOT the API package) so non-API code — the sources
factory and the CLI — can resolve secrets without pulling in FastAPI. This is
the single source of truth for the known secret names, their env-var lookup,
and :func:`resolve_secret` (env var first, then the Settings store).
"""
from __future__ import annotations

import re

import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SecretSpec:
    name: str
    env_var: str
    short: str
    long: str


def secret_specs(cfg) -> List[SecretSpec]:
    """Known secret descriptors, resolved against the live config.

    The secret *name* is stable (used in secrets.yml / the API); the *env var*
    is configurable per config (``*_api_key_env``) and takes precedence.
    """
    out = [
        SecretSpec(
            name="jellyfin_api_key",
            env_var=cfg.server.api_key_env,
            short="Jellyfin API key",
            long=(
                "API key used to talk to the Jellyfin server. Set it via the environment "
                f"variable {cfg.server.api_key_env} (recommended) or in Settings. When the "
                "environment variable is set it takes precedence and the Settings field is "
                "locked. Never shown back — only a masked hint is."
            ),
        ),
        SecretSpec(
            name="tmdb_key",
            env_var=cfg.sources.tmdb.api_key_env,
            short="TMDb API key",
            long=(
                "TMDb API v3 key used for metadata and artwork. Set it via "
                f"{cfg.sources.tmdb.api_key_env} (recommended) or in Settings. Get a free key "
                "at https://www.themoviedb.org/settings/api. Rate limit is ~20 req/s."
            ),
        ),
        SecretSpec(
            name="fanart_key",
            env_var=cfg.sources.fanart.api_key_env,
            short="fanart.tv API key",
            long=(
                "fanart.tv API key for additional artwork. Set it via "
                f"{cfg.sources.fanart.api_key_env} or in Settings. Get a free key at "
                "https://www.fanart.tv/user/apikey. Optional."
            ),
        ),
        SecretSpec(
            name="omdb_key",
            env_var="OMDB_KEY",
            short="OMDb API key",
            long=(
                "OMDb API key for IMDb, Rotten Tomatoes and Metacritic ratings. "
                "Set it via the environment variable OMDB_KEY (recommended) or in "
                "Settings. Get a free key at https://www.omdbapi.com/apikey.aspx. "
                "Rate limit is 1000 requests/day; responses are cached (7 day TTL)."
            ),
        ),
        SecretSpec(
            name="mdblist_key",
            env_var=cfg.sources.mdblist.api_key_env,
            short="MDBList API key",
            long=(
                "MDBList API key for IMDb, Rotten Tomatoes (critic + audience), Metacritic, "
                f"Trakt, TMDb and Letterboxd ratings. Set it via {cfg.sources.mdblist.api_key_env} "
                "(recommended) or in Settings. Get a free key at https://api.mdblist.com. Free tier is "
                "1,000 requests/day; responses are cached (7 day TTL)."
            ),
        ),
    ]
    out.extend(_arr_instance_specs(cfg))
    # Cover instance keys already saved in the store whose config entry does
    # not exist yet (the form saves the secret BEFORE the instance is written
    # to config). Names come from the store, so only real stored names.
    if getattr(cfg, "secrets_store", None) is not None:
        known = {sp.name for sp in out}
        for name in cfg.secrets_store.names():
            if name not in known and is_arr_secret_name(name):
                kind = name.split("_", 1)[0]
                inst = name[len(kind) + 1:-len("_api_key")].replace("-", "_")
                out.append(SecretSpec(
                    name=name, env_var="",
                    short=f"{kind.capitalize()} API key ({inst})",
                    long=(f"API key for the {kind} instance `{inst}`. "
                          "Set it in Settings; it is sent as the `X-Api-Key` "
                          "header on every request."),
                ))
    return out


_ARR_SECRET_RE = re.compile(r"^(sonarr|radarr)_[a-z0-9][a-z0-9_-]*_api_key$")


def valid_api_key_ref(cfg, name: str) -> bool:
    """True when ``name`` names a secret that exists for this config.

    Known specs (fixed + per-instance) or the store-backed instance-key shape
    (``<kind>_<inst>_api_key``). Anything else is an invalid reference — e.g.
    an operator-typed string in ``media_apps.*[].api_key`` that was never saved
    as a secret. Used by the config view to flag instances that need re-saving.
    """
    if not name:
        return False
    known = {sp.name for sp in secret_specs(cfg)}
    if name in known:
        return True
    if is_arr_secret_name(name):
        store = getattr(cfg, "secrets_store", None)
        if store is not None and name in set(store.names()):
            return True
        # Not saved yet but well-formed (form saves the secret before the
        # instance is written to config) — treat as valid.
        return True
    return False


def is_arr_secret_name(name: str) -> bool:
    """True for the form-generated instance-key shape ``<kind>_<inst>_api_key``.

    The instance part is a UI slug: lowercase alnum with runs of other chars
    collapsed to ``_`` (so "sonarr-4k" -> ``sonarr_sonarr_4k_api_key``); dashes
    may still appear from pre-existing store names. Used both by :func:`secret_specs` (to
    cover instances whose config entry does not exist yet) and by the secrets
    API (write access before the instance is saved)."""
    return bool(_ARR_SECRET_RE.match(name))


def _arr_instance_specs(cfg) -> List[SecretSpec]:
    """One spec per Sonarr/Radarr instance (S9f1a): the secret named by the
    instance's ``api_key`` field. No fixed env var — set it in Settings."""
    out: List[SecretSpec] = []
    for kind in ("sonarr", "radarr"):
        for inst in getattr(cfg.media_apps, kind, []):
            if not inst.api_key:
                continue
            # S9d2-fix2 step 6a: only accept well-formed instance-key names —
            # an invalid reference (any other string) must not surface as a
            # secret; the instance is flagged in the config view instead.
            if not is_arr_secret_name(inst.api_key):
                continue
            out.append(SecretSpec(
                name=inst.api_key,
                env_var="",
                short=f"{kind.capitalize()} API key ({inst.name})",
                long=(f"API key for the {kind} instance `{inst.name}` "
                      f"({inst.url}). Set it in Settings; it is sent as the "
                      "`X-Api-Key` header on every request."),
            ))
    return out


def resolve_secret(config, secrets, name: str, env=None) -> Optional[str]:
    """Single source for resolving a secret value: env var first, then the store.

    ``config`` is a :class:`Config` (for the per-config env-var name), ``secrets``
    is a :class:`SecretsStore` (may be ``None`` for callers that only read env),
    and ``name`` is the stable secret name (e.g. ``jellyfin_api_key``). ``env``
    is an injectable environment mapping (defaults to ``os.environ``). Returns
    ``None`` if the secret is unknown or unset. This is the ONE function every
    secret consumer uses (make_jellyfin, make_tmdb, make_sources, run_service,
    the service testers) so a key saved via Settings takes effect without a
    restart.
    """
    env = os.environ if env is None else env
    spec = next((sp for sp in secret_specs(config) if sp.name == name), None)
    if spec is not None and spec.env_var:
        ev = env.get(spec.env_var)
        if ev:
            return ev
    # Fall back to the store directly: this also covers secrets that were
    # saved for an instance whose config entry no longer exists (e.g. a new
    # instance reusing an old secret name before it is saved).
    return secrets.get(name) if secrets is not None else None


__all__ = ["SecretSpec", "secret_specs", "resolve_secret"]
