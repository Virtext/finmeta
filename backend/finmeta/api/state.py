"""Application state for the Finmeta HTTP API.

Holds the (hot-reloadable) config, the job/override stores, a plan cache, the
single-run slot, and the dependency-injection points (plan builder, candidate
provider) so the API is unit-testable without a live server.
"""
from __future__ import annotations

import re as _re

import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml

from finmeta.config.loading import ConfigError, load_config, resolve_env
from finmeta.config.models import Config
from finmeta.engine.plan import WritePlan
from finmeta.jellyfin_client import JellyfinClient, PathMapper
from finmeta.sources.factory import build_sources
from finmeta.sources.tmdb import TmdbClient

from finmeta.api.overrides import OverrideStore
from finmeta.api.secrets import SecretsStore, secret_specs
from finmeta.config.secretspec import resolve_secret as _resolve_secret
from finmeta.api.auth import AuthStore, SessionManager, LoginRateLimiter

DEFAULT_CONFIG_PATH = "config/config.yml"


@dataclass
class _PlanCache:
    plan: WritePlan
    generated_at: str = ""


class AppContext:
    """Everything a request handler needs, plus the lifecycle knobs."""

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG_PATH,
        *,
        config_dir: Optional[str] = None,
        db_path: Optional[str] = None,
        overrides_path: Optional[str] = None,
        plan_builder: Optional[Callable] = None,
        candidates_provider: Optional[Callable] = None,
        service_testers: Optional[Dict[str, Callable]] = None,
    ):
        from finmeta.engine.plan_builder import build_plan
        self._config_path = Path(config_path)
        self._config_dir = Path(config_dir) if config_dir else self._config_path.parent
        self._db_path = Path(db_path) if db_path else self._config_dir / "jobs.db"
        self._overrides_path = (Path(overrides_path) if overrides_path
                                else self._config_dir / "overrides.db")
        self._plan_builder = plan_builder or build_plan
        self._candidates_provider = candidates_provider

        self._cfg: Config = load_config(self._config_path)
        self._gen = 0
        self._cfg_lock = threading.RLock()
        self._claim_lock = threading.Lock()
        self._active_run: Optional[str] = None
        self._active_run_scope: Optional[Dict[str, Optional[str]]] = None
        # Jellyfin webhook (ItemAdded): items that arrived while a run was
        # already active, drained one at a time as each run finishes (see
        # run_service._worker) -- never dropped, never piled into one giant
        # run. A dedup set so a burst of duplicate webhook calls for the same
        # item (Jellyfin can fire more than one per import) only queues once.
        self._webhook_queue: List[str] = []
        self._webhook_queued_set: set = set()
        self._cache: Dict[tuple, _PlanCache] = {}
        self._cache_lock = threading.Lock()
        self.overrides = OverrideStore(self._overrides_path)
        secrets_path = self._config_dir / "secrets.yml"
        self.secrets = SecretsStore(
            secrets_path,
            secret_key=os.environ.get("FINMETA_SECRET_KEY"),
        )
        self.auth = AuthStore(self._config_dir)
        # secretspec.secret_specs consults the store for already-saved instance
        # keys whose config entry does not exist yet (S9d2-fix2 step 0a).
        self.secrets_store = self.secrets
        self.sessions = SessionManager()
        self.login_limiter = LoginRateLimiter()
        self.service_testers = service_testers
        self._config_error: Optional[str] = None
        self.scheduler = None

    # -- config -------------------------------------------------------------
    @property
    def config(self) -> Config:
        return self._cfg

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def proxy_roots(self, cfg: Optional[Config] = None) -> List[Path]:
        """Directories a LOCAL candidate ref may be served from by the proxy.

        Deliberately an allowlist of the places artwork legitimately lives --
        the local asset source, Finmeta's own artwork-lifecycle directories,
        and the media roots from ``path_mappings`` -- and NOT ``config_dir``
        itself, which also holds ``secrets.yml`` / ``auth.yml`` /
        ``config.yml``. See ``api.proxy`` for why the ref cannot be trusted.
        """
        cfg = cfg or self.config
        roots: List[Path] = []
        local = getattr(getattr(cfg, "sources", None), "local", None)
        if local is not None and getattr(local, "path", None):
            roots.append(Path(local.path))
        for sub in ("assets", "backups", "trash", "cache"):
            roots.append(self._config_dir / sub)
        for m in getattr(cfg, "path_mappings", []) or []:
            if getattr(m, "local", None):
                roots.append(Path(m.local))
        return roots

    @property
    def gen(self) -> int:
        return self._gen

    @property
    def config_error(self) -> Optional[str]:
        return self._config_error

    def current_secret_values(self) -> List[str]:
        """All live secret values (UI store + env overrides) for redaction."""
        vals = list(self.secrets.values())
        for spec in secret_specs(self._cfg):
            ev = os.environ.get(spec.env_var)
            if ev:
                vals.append(ev)
        return vals

    def redact(self, text):
        """Scrub known secret values out of ``text`` (D2)."""
        from finmeta.api.secrets import Redactor
        return Redactor(self.current_secret_values()).redact(text)

    def last_good_path(self) -> Path:
        return self._config_dir / "config.last-good.yml"

    def config_text(self) -> str:
        return self._config_path.read_text()

    def _apply_config(self, cfg: Config) -> None:
        self._cfg = cfg
        self._gen += 1
        self._config_error = None
        with self._cache_lock:
            self._cache.clear()

    def reload(self) -> tuple:
        """Re-read config from disk. Returns (ok, error_message).

        On failure the previously-loaded config is kept.
        """
        try:
            cfg = load_config(self._config_path)
        except ConfigError as exc:
            self._config_error = str(exc)
            return (False, str(exc))
        with self._cfg_lock:
            self._apply_config(cfg)
        self.reschedule()
        return (True, None)

    def put_config(self, yaml_text: str) -> None:
        """Validate ``yaml_text``, then write it and reload.

        Raises :class:`ConfigError` (no write) if validation fails. On success
        the previous config is saved as ``config.last-good.yml`` so it can be
        restored via ``POST /api/config/undo``.
        """
        import yaml as _yaml
        try:
            data = _yaml.safe_load(yaml_text)
        except _yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML: {exc}") from exc
        # S9d2-fix2 step B: redacted secret values come back in the raw YAML as
        # the fixed marker string (or empty). Restore the STORED value for those
        # fields so a GET -> PUT round-trip never loses or corrupts them; the
        # marker itself is never persisted. Re-serialize from the restored dict.
        if isinstance(data, dict):
            data = self._restore_redacted_secrets(data)
            # S9d4c Fase A step 5: GET /api/config's `parsed` annotates each
            # media_apps instance with api_key_valid (S9f1b step 4) for display
            # only -- strip it here so a GET -> edit -> PUT round trip that
            # (accidentally or not) carries it along never 400s on an unknown
            # field, and it's never written to disk.
            data = self._strip_readonly_media_app_fields(data)
            # Re-serialize from the restored dict so the stored values — not the
            # marker — are what gets written to disk.
            yaml_text = _yaml.safe_dump(data, sort_keys=True, default_flow_style=False)
        try:
            cfg = Config(**(data or {}))
        except Exception as exc:
            raise ConfigError(f"invalid config: {exc}") from exc
        # Save the current (previous) config as last-good before overwriting.
        self.last_good_path().write_text(self._config_path.read_text())
        with self._cfg_lock:
            self._apply_config(cfg)
        # Write AFTER applying so a failed write can never leave memory and disk
        # out of sync (and the marker is never persisted).
        self._config_path.write_text(yaml_text if yaml_text.endswith("\n") else yaml_text + "\n")
        self.reschedule()

    @staticmethod
    def _secret_name_fields(data):
        """Yield (container, key) for every media_apps api_key field in ``data``."""
        apps = data.get("media_apps") if isinstance(data, dict) else None
        if not isinstance(apps, dict):
            return
        for kind in ("sonarr", "radarr"):
            for inst in apps.get(kind) or []:
                if isinstance(inst, dict) and "api_key" in inst:
                    yield inst, "api_key"

    @staticmethod
    def _strip_readonly_media_app_fields(data):
        """Remove server-computed fields (``api_key_valid``) GET /api/config
        annotates media_apps instances with -- never valid input, must never
        be persisted (S9d4c Fase A step 5)."""
        apps = data.get("media_apps") if isinstance(data, dict) else None
        if not isinstance(apps, dict):
            return data
        for kind in ("sonarr", "radarr"):
            for inst in apps.get(kind) or []:
                if isinstance(inst, dict):
                    inst.pop("api_key_valid", None)
        return data

    def _restore_redacted_secrets(self, data):
        """Replace marker/empty values in secret-named fields with the stored value.

        A field carrying the fixed redaction marker (or empty) means "the live
        response redacted whatever was here" — restore the instance's PREVIOUS
        secret name so a GET -> PUT round-trip keeps it. A real secret NAME that
        already exists in the store is kept as-is; a marker with no previous
        name degrades to empty (never the marker itself)."""
        marker = "__FINMETA_SECRET__"
        for container, key in self._secret_name_fields(data):
            val = container.get(key)
            if not isinstance(val, str):
                continue
            prev_name = self._prev_arr_secret_name(container.get("name"))
            if val == marker:
                # Restore a known secret NAME for this instance (one that exists
                # in the store). Never restore a raw value: config holds NAMES,
                # the store holds VALUES. If no known name exists, degrade to
                # empty — the operator re-enters the key in the UI.
                container[key] = self._known_secret_name_for(container.get("name")) or ""
            elif val == "":
                if prev_name and self.secrets.has(prev_name):
                    container[key] = prev_name  # blank edit with a live secret = keep it
            elif self.secrets is not None and self.secrets.has(val):
                pass  # real, known secret name — keep it
        return data

    def _prev_arr_secret_name(self, inst_name):
        for kind in ("sonarr", "radarr"):
            for inst in getattr(self._cfg.media_apps, kind, []):
                if inst.name == inst_name and inst.api_key:
                    return inst.api_key
        return None

    def _known_secret_name_for(self, inst_name):
        """A secret NAME (present in the store) associated with this instance.

        Prefers the instance's previous name; falls back to the form-generated
        ``<kind>_<inst>_api_key`` convention used by the Add/Edit form."""
        prev = self._prev_arr_secret_name(inst_name)
        if prev and self.secrets is not None and self.secrets.has(prev):
            return prev
        for kind in ("sonarr", "radarr"):
            name = f"{kind}_{_re.sub(r'[^a-z0-9]+', '_', str(inst_name).lower()).strip('_') or 'instance'}_api_key"
            if self.secrets is not None and self.secrets.has(name):
                return name
        return None

    def undo_config(self) -> bool:
        """Restore ``config.last-good.yml`` over the live config and reload."""
        lg = self.last_good_path()
        if not lg.is_file():
            return False
        self._config_path.write_text(lg.read_text())
        ok, _err = self.reload()
        return ok

    # -- secret resolution (single source for every consumer) ---------------
    def resolve_secret(self, name: str) -> Optional[str]:
        """Resolve a secret by name: env var first, then the Settings store.

        Reads the live store on every call, so a secret saved via Settings takes
        effect immediately without a restart.
        """
        return _resolve_secret(self._cfg, self.secrets, name)

    # -- client factories ---------------------------------------------------
    def make_jellyfin(self, cfg: Optional[Config] = None) -> JellyfinClient:
        cfg = cfg or self._cfg
        srv = cfg.server
        return JellyfinClient(
            srv.url, token=self.resolve_secret("jellyfin_api_key"),
            timeout=srv.timeout, base_path=srv.base_path, verify_ssl=srv.verify_ssl,
        )

    def make_tmdb(self, cfg: Optional[Config] = None) -> TmdbClient:
        cfg = cfg or self._cfg
        return TmdbClient(
            api_key=self.resolve_secret("tmdb_key") or "",
            cache_path=str(self._config_dir / "cache.db"),
        )

    def make_sources(self, cfg: Optional[Config] = None) -> Dict[str, object]:
        cfg = cfg or self._cfg
        return build_sources(cfg, cache_path=str(self._config_dir / "cache.db"),
                             secrets=self.secrets)

    def make_mapper(self, cfg: Optional[Config] = None) -> PathMapper:
        cfg = cfg or self._cfg
        return PathMapper.from_config(
            [{"jellyfin": m.jellyfin, "local": m.local} for m in cfg.path_mappings])

    # -- plan cache ---------------------------------------------------------
    def _plan_key(
        self, library: Optional[str], item_id: Optional[str],
        limit: Optional[int], offset: int,
    ) -> str:
        if item_id:
            return f"item:{item_id}"
        if library:
            # Paginated and full requests for the same library must not share
            # a cache entry -- a page request only builds that page's items.
            page = f":{limit}:{offset}" if limit is not None else ""
            return f"lib:{library}{page}"
        return "lib:*"

    def get_plan(
        self, library: Optional[str] = None, item_id: Optional[str] = None,
        limit: Optional[int] = None, offset: int = 0,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> WritePlan:
        """Return the WritePlan for a scope, building (and caching) if needed.

        ``limit``/``offset`` (single-library scope only) bound the expensive
        per-item work (TMDb/fanart/etc. lookups) to one page -- see
        ``plan_builder.build_plan`` -- so browsing a library with hundreds of
        titles doesn't block on building a plan for every one of them.
        ``progress_cb`` is forwarded straight to ``build_plan`` (never called
        at all on a cache hit, since no work happens then).
        """
        cfg = self._cfg
        key = self._plan_key(library, item_id, limit, offset)
        cache_key = (self._gen, key)
        with self._cache_lock:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit.plan
        jf = self.make_jellyfin(cfg)
        tmdb = self.make_tmdb(cfg)
        sources = self.make_sources(cfg)
        try:
            plan = self._plan_builder(
                cfg, jf, tmdb, sources,
                library=library, item_id=item_id, config_dir=self._config_dir,
                secrets=self.secrets, limit=limit, offset=offset,
                progress_cb=progress_cb,
            )
        finally:
            jf.close()
            try:
                tmdb.close()
            except Exception:
                pass
        plan = self._apply_overrides(plan)
        with self._cache_lock:
            self._cache[cache_key] = _PlanCache(plan=plan)
        return plan

    def _apply_overrides(self, plan: WritePlan) -> WritePlan:
        from finmeta.api.overrides import apply_overrides, apply_skips
        if not plan.items:
            return plan
        item_ids = [ip.item_id for ip in plan.items]
        ovs = self.overrides.all_by_item(item_ids)
        plan = apply_overrides(plan, ovs)
        skips = self.overrides.skipped_by_item(item_ids)  # S9d4b per-row skip
        return apply_skips(plan, skips)

    def invalidate_plans(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # -- candidate selection ------------------------------------------------
    def set_candidate(self, item_id: str, kind: str, ref: Optional[str],
                      source: Optional[str] = None) -> None:
        if ref:
            self.overrides.set(item_id, kind, ref, source)
        else:
            self.overrides.clear(item_id, kind)
        self.invalidate_plans()

    # -- single-run slot ----------------------------------------------------
    @property
    def active_run(self) -> Optional[str]:
        with self._claim_lock:
            return self._active_run

    @property
    def active_run_scope(self) -> Optional[Dict[str, Optional[str]]]:
        """``{action, library, item_id}`` for whatever ``active_run`` is, or
        ``None`` when no run is active -- lets a client that reconnects after
        a page reload (job status was otherwise only ever held in the
        frontend's own in-memory state, see STATE.md "job status lost on
        page reload") work out WHICH panel to resume polling into, not just
        that something is running."""
        with self._claim_lock:
            return dict(self._active_run_scope) if self._active_run_scope else None

    def claim_run(self, run_id: str, *, action: Optional[str] = None,
                  library: Optional[str] = None, item_id: Optional[str] = None) -> bool:
        with self._claim_lock:
            if self._active_run is not None:
                return False
            self._active_run = run_id
            self._active_run_scope = {"action": action, "library": library, "item_id": item_id}
            return True

    def release_run(self, run_id: str) -> None:
        with self._claim_lock:
            if self._active_run == run_id:
                self._active_run = None
                self._active_run_scope = None

    def queue_webhook_item(self, item_id: str) -> None:
        """A run was active when the webhook fired -- remember this item so
        run_service._worker can submit it once the active run finishes."""
        with self._claim_lock:
            if item_id not in self._webhook_queued_set:
                self._webhook_queued_set.add(item_id)
                self._webhook_queue.append(item_id)

    def pop_webhook_item(self) -> Optional[str]:
        with self._claim_lock:
            if not self._webhook_queue:
                return None
            item_id = self._webhook_queue.pop(0)
            self._webhook_queued_set.discard(item_id)
            return item_id

    # -- scheduler hook -----------------------------------------------------
    def reschedule(self) -> None:
        if self.scheduler is not None:
            from finmeta.api import scheduler as sched
            sched.reschedule(self)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        from finmeta.api import scheduler as sched
        sched.start(self)

    def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None


__all__ = ["AppContext", "DEFAULT_CONFIG_PATH"]
