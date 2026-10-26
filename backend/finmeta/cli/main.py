"""finmeta CLI (SPEC §10, §11).

Commands:
  plan   Compute a WritePlan, print summary, write plan JSON.
  apply  Apply the most recent (or given) plan to disk + Jellyfin.
  undo   Undo a previous run (restore backups, delete created files).
  runs   List recorded runs (from SQLite job log).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("finmeta.cli")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _load_creds_env(cfg) -> Optional[str]:
    """Read the Jellyfin API key from the environment variable named in config."""
    env_name = cfg.server.api_key_env
    value = os.environ.get(env_name)
    if value is None or value == "":
        logger.error("environment variable %s is not set", env_name)
        return None
    return value


def _make_jellyfin(cfg, token: str):
    """Create an authenticated JellyfinClient."""
    from finmeta.jellyfin_client import JellyfinClient
    srv = cfg.server
    jf = JellyfinClient(srv.url, token=token, timeout=srv.timeout,
                        base_path=srv.base_path, verify_ssl=srv.verify_ssl)
    users = jf._request("GET", "/Users") or []
    user_id = users[0]["Id"] if users else None
    if user_id:
        jf.user_id = user_id
    return jf


def _make_tmdb(cfg):
    """Create a TmdbClient from config."""
    from finmeta.config.loading import resolve_env
    from finmeta.sources.tmdb import TmdbClient
    key = resolve_env(cfg.sources.tmdb.api_key_env)
    return TmdbClient(api_key=key, cache_path="config/cache.db")


def _make_sources(cfg):
    """Build the sources dict from config."""
    from finmeta.sources.factory import build_sources
    return build_sources(cfg, cache_path="config/cache.db")


def _config_dir(config_path: str) -> Path:
    return Path(config_path).parent


def cmd_plan(args: argparse.Namespace) -> int:
    """Execute the 'plan' subcommand."""
    from finmeta.config.loading import load_config, ConfigError
    from finmeta.engine.plan_builder import build_plan
    from finmeta.engine.diff import diff_to_text

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    token = _load_creds_env(cfg)
    if token is None:
        return 1

    jf = _make_jellyfin(cfg, token)
    tmdb = _make_tmdb(cfg)
    sources = _make_sources(cfg)

    try:
        plan = build_plan(
            cfg, jf, tmdb, sources,
            library=args.library, item_id=args.item,
        )
    except Exception as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 1
    finally:
        jf.close()
        tmdb.close()

    text = diff_to_text(plan)
    print(text)

    config_dir = _config_dir(args.config)
    runs_dir = config_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    plan_path = runs_dir / f"plan-{ts}.json"
    plan_path.write_text(plan.to_json() + "\n")
    print(f"\nPlan written to: {plan_path}")

    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Execute the 'apply' subcommand."""
    from finmeta.config.loading import load_config, ConfigError
    from finmeta.engine.executor import DryRunRefused, apply_plan
    from finmeta.engine.plan import WritePlan

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    token = _load_creds_env(cfg)
    if token is None:
        return 1

    # Load the plan
    if args.plan:
        plan_path = Path(args.plan)
    else:
        # Find the most recent plan
        config_dir = _config_dir(args.config)
        plans_dir = config_dir / "runs"
        plans = sorted(plans_dir.glob("plan-*.json"), reverse=True)
        if not plans:
            print("no plan found; run 'finmeta plan' first", file=sys.stderr)
            return 1
        plan_path = plans[0]

    plan = WritePlan.from_json(plan_path.read_text())
    run_id = f"run-{int(time.time())}"

    jf = _make_jellyfin(cfg, token)
    tmdb = _make_tmdb(cfg)

    config_dir = _config_dir(args.config)
    db_path = config_dir / "jobs.db"

    try:
        result = apply_plan(
            plan, run_id, cfg, jf, tmdb,
            config_dir=config_dir, db_path=db_path,
            plan_path=str(plan_path),
        )
    except DryRunRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        jf.close()
        tmdb.close()

    print(f"run_id: {result.run_id}")
    print(f"files written:   {result.files_written}")
    print(f"files unchanged: {result.files_unchanged}")
    print(f"api calls:       {result.api_calls}")
    if result.errors:
        print(f"errors ({len(result.errors)}):", file=sys.stderr)
        for e in result.errors:
            print(f"  {e}", file=sys.stderr)
    return 0 if not result.errors else 1


def cmd_undo(args: argparse.Namespace) -> int:
    """Execute the 'undo' subcommand."""
    from finmeta.engine.runs import undo, load_manifest

    config_dir = Path(args.config).parent
    try:
        manifest = load_manifest(args.run_id, config_dir=config_dir)
    except FileNotFoundError:
        print(f"no manifest found for run {args.run_id!r}", file=sys.stderr)
        return 1

    result = undo(args.run_id, config_dir=config_dir)
    print(f"restored: {len(result.restored)}")
    for p in result.restored:
        print(f"  + {p}")
    print(f"deleted:  {len(result.deleted)}")
    for p in result.deleted:
        print(f"  - {p}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    """Execute the 'runs' subcommand."""
    from finmeta.engine.jobs import list_runs

    config_dir = Path(args.config).parent
    db_path = config_dir / "jobs.db"

    if not db_path.is_file():
        print("no runs recorded yet")
        return 0

    runs = list_runs(db_path)
    if not runs:
        print("no runs recorded yet")
        return 0

    for r in runs:
        status = r["status"]
        icon = {"ok": "✓", "partial": "⚠", "running": "…", "error": "✗"}.get(status, "?")
        print(f"{icon} {r['run_id']}  {status:8s}  "
              f"written={r['files_written']} unchanged={r['files_unchanged']} "
              f"api={r['api_calls']}  started={r['started_at']}")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="finmeta", description="Finmeta — Jellyfin metadata manager")
    sub = parser.add_subparsers(dest="command")

    # plan
    p_plan = sub.add_parser("plan", help="Compute a WritePlan and print a summary")
    p_plan.add_argument("--config", required=True, help="Path to config.yml")
    p_plan.add_argument("--library", help="Library name (e.g. Movies)")
    p_plan.add_argument("--item", help="Jellyfin item ID")
    p_plan.add_argument("-v", "--verbose", action="store_true")

    # apply
    p_apply = sub.add_parser("apply", help="Apply the most recent plan")
    p_apply.add_argument("--config", required=True, help="Path to config.yml")
    p_apply.add_argument("--plan", help="Path to a specific plan JSON (default: most recent)")
    p_apply.add_argument("-v", "--verbose", action="store_true")

    # undo
    p_undo = sub.add_parser("undo", help="Undo a previous run")
    p_undo.add_argument("run_id", help="Run ID to undo")
    p_undo.add_argument("--config", required=True, help="Path to config.yml")
    p_undo.add_argument("-v", "--verbose", action="store_true")

    # runs
    p_runs = sub.add_parser("runs", help="List recorded runs")
    p_runs.add_argument("--config", required=True, help="Path to config.yml")
    p_runs.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))

    if args.command == "plan":
        return cmd_plan(args)
    elif args.command == "apply":
        return cmd_apply(args)
    elif args.command == "undo":
        return cmd_undo(args)
    elif args.command == "runs":
        return cmd_runs(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
