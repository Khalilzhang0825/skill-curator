#!/usr/bin/env python3
"""Skill Curator CLI: initialize Skill Vault, fetch, inspect, search, mount, demote, and audit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from auditor import inspect_skill
from common import (
    CuratorError,
    copy_tree_clean,
    decode_raw_bytes,
    ensure_index_file,
    parse_frontmatter,
    resolve_home_dir,
    rewrite_frontmatter_description,
    split_csv,
    upsert_index_row,
    utc_now_iso,
    validate_skill_name,
    validate_tree_containment,
)
from fetcher import fetch_source
from lifecycle import (
    demote_skill,
    health_audit,
    mount_by_tag,
    mount_vault_skill,
    search_vault,
)
from onboarding import (
    adopt_skill,
    discover_standard_global_dirs,
    parse_remediate_plan_payload,
    remediate_plan,
    scan_local_skills,
)

VALID_CLAUDE_SYNC = ("auto", "always", "never")
DEFAULT_GLOBAL_CORE_DIRS = ["~/.agents/skills"]
DEFAULT_GLOBAL_CAP = 8
DEFAULT_CLAUDE_SYNC = "auto"
DEFAULT_STALE_DAYS = 30


DEFAULT_CONFIG_PATH = Path("~/.config/skill-curator/config.json")


def _validate_positive_int(value: int, field_name: str) -> int:
    """Validate positive integer configuration values (>= 1)."""
    if value < 1:
        raise CuratorError(f"Invalid {field_name} '{value}'. Must be >= 1.")
    return value


def resolve_config_path(args: argparse.Namespace) -> Path:
    """Resolve config.json path from CLI flags, env var, or existing default config."""
    raw = getattr(args, "config_sub", None) or getattr(args, "config_top", None)
    if not raw:
        raw = os.environ.get("SKILL_CURATOR_CONFIG")
    if raw:
        return Path(os.path.expanduser(raw)).resolve()
    home_override = _optional_path(getattr(args, "home_dir", None))
    default_cfg = resolve_home_dir(home_override) / ".config" / "skill-curator" / "config.json"
    if default_cfg.is_file():
        return default_cfg.resolve()
    raise CuratorError(
        "Configuration not found. Run 'init' or pass --config <path> / SKILL_CURATOR_CONFIG.",
        payload={"error_code": "CONFIG_NOT_FOUND"},
    )


def load_config(config_path: Path) -> dict[str, Any]:
    """Load and validate config.json, filling defaults for optional fields."""
    if not config_path.is_file():
        raise CuratorError(f"Config file not found: '{config_path}'. Run 'init' first.")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CuratorError(f"Invalid JSON in config '{config_path}': {exc}") from exc

    if not isinstance(raw, dict) or not raw.get("vault_path"):
        raise CuratorError(f"Config '{config_path}' must define 'vault_path'.")

    claude_sync = raw.get("claude_sync", DEFAULT_CLAUDE_SYNC)
    if claude_sync not in VALID_CLAUDE_SYNC:
        raise CuratorError(
            f"Invalid claude_sync '{claude_sync}'. Expected one of {VALID_CLAUDE_SYNC}."
        )

    global_cap = _validate_positive_int(
        int(raw.get("global_cap", DEFAULT_GLOBAL_CAP)), "global_cap"
    )
    stale_days = _validate_positive_int(
        int(raw.get("stale_days", DEFAULT_STALE_DAYS)), "stale_days"
    )
    global_dirs = raw.get("global_core_dirs", DEFAULT_GLOBAL_CORE_DIRS)
    if not isinstance(global_dirs, list) or not all(isinstance(d, str) for d in global_dirs):
        raise CuratorError("Config 'global_core_dirs' must be a list of strings.")

    return {
        "vault_path": str(Path(os.path.expanduser(raw["vault_path"])).resolve()),
        "global_core_dirs": global_dirs,
        "global_cap": global_cap,
        "claude_sync": claude_sync,
        "stale_days": stale_days,
    }


def _optional_path(raw: str | None) -> Path | None:
    """Resolve an optional CLI path argument."""
    return Path(os.path.expanduser(raw)).resolve() if raw else None


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    """Initialize a Skill Vault directory, config.json, and INDEX.md idempotently."""
    vault_dir = Path(os.path.expanduser(args.vault)).resolve()
    skills_dir = vault_dir / "skills"
    config_path = (
        Path(os.path.expanduser(args.config_sub or args.config_top)).resolve()
        if (args.config_sub or args.config_top)
        else vault_dir / "config.json"
    )
    index_path = vault_dir / "INDEX.md"

    existing_cfg: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_cfg = loaded
        except json.JSONDecodeError as exc:
            raise CuratorError(f"Existing config '{config_path}' is invalid JSON: {exc}") from exc

    global_dirs = (
        args.global_dir
        if args.global_dir is not None
        else existing_cfg.get("global_core_dirs", list(DEFAULT_GLOBAL_CORE_DIRS))
    )
    global_cap = _validate_positive_int(
        args.global_cap
        if args.global_cap is not None
        else int(existing_cfg.get("global_cap", DEFAULT_GLOBAL_CAP)),
        "global_cap",
    )
    claude_sync = (
        args.claude_sync
        if args.claude_sync is not None
        else existing_cfg.get("claude_sync", DEFAULT_CLAUDE_SYNC)
    )
    stale_days = _validate_positive_int(
        args.stale_days
        if args.stale_days is not None
        else int(existing_cfg.get("stale_days", DEFAULT_STALE_DAYS)),
        "stale_days",
    )

    discovered_dirs: list[dict[str, Any]] | None = None
    if getattr(args, "discover_global_dirs", False):
        discovered_dirs = discover_standard_global_dirs(_optional_path(getattr(args, "home_dir", None)))
        discovered_paths = [entry["path"] for entry in discovered_dirs]
        if discovered_paths:
            merged_dirs: list[str] = []
            seen_dirs: set[str] = set()
            base_dirs = args.global_dir if args.global_dir is not None else existing_cfg.get("global_core_dirs", [])
            for raw_d in [*base_dirs, *discovered_paths]:
                resolved_d = str(Path(os.path.expanduser(raw_d)).resolve())
                if resolved_d not in seen_dirs:
                    seen_dirs.add(resolved_d)
                    merged_dirs.append(raw_d if raw_d in base_dirs else resolved_d)
            global_dirs = merged_dirs

    skills_dir.mkdir(parents=True, exist_ok=True)
    ensure_index_file(index_path)

    cfg_payload = {
        "vault_path": str(vault_dir),
        "global_core_dirs": global_dirs,
        "global_cap": global_cap,
        "claude_sync": claude_sync,
        "stale_days": stale_days,
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg_payload, indent=2) + "\n", encoding="utf-8")

    response: dict[str, Any] = {
        "ok": True,
        "action": "init",
        "vault_path": str(vault_dir),
        "skills_dir": str(skills_dir),
        "config_path": str(config_path),
        "index_path": str(index_path),
        "config": cfg_payload,
    }
    if discovered_dirs is not None:
        response["discovered_global_dirs"] = discovered_dirs
    return response


def cmd_store(args: argparse.Namespace) -> dict[str, Any]:
    """Validate and copy a skill directory into the Skill Vault and update INDEX.md."""
    cfg = load_config(resolve_config_path(args))

    source_dir = Path(os.path.expanduser(args.source_dir)).resolve()
    validate_tree_containment(source_dir)

    skill_md_path = source_dir / "SKILL.md"
    frontmatter = parse_frontmatter(skill_md_path)

    raw_name = args.name or frontmatter.get("name") or source_dir.name
    skill_name = validate_skill_name(raw_name)
    description = (
        args.override_description
        if args.override_description is not None
        else frontmatter.get("description", "")
    )

    keywords = split_csv(args.keywords)
    scene_tags = split_csv(args.tags)
    source_url = (args.source_url or "").strip()

    vault_dir = Path(cfg["vault_path"])
    vault_skills_dir = vault_dir / "skills"
    vault_skills_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = vault_skills_dir / skill_name

    copy_tree_clean(source_dir, dest_dir)
    if args.override_description is not None:
        rewrite_frontmatter_description(dest_dir / "SKILL.md", skill_name, description)

    meta = {
        "name": skill_name,
        "description": description,
        "keywords": keywords,
        "scene_tags": scene_tags,
        "source_url": source_url,
        "stored_at": utc_now_iso(),
    }
    meta_path = dest_dir / ".curator-meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    index_path = vault_dir / "INDEX.md"
    upsert_index_row(
        index_path=index_path,
        name=skill_name,
        keywords=keywords,
        scene_tags=scene_tags,
        source_url=source_url,
        rel_path=f"skills/{skill_name}",
    )

    return {
        "ok": True,
        "action": "store",
        "name": skill_name,
        "vault_skill_dir": str(dest_dir),
        "meta_path": str(meta_path),
        "index_path": str(index_path),
    }


def cmd_mount(args: argparse.Namespace) -> dict[str, Any]:
    """Copy-mount a Vault skill (or Scene Tag group via --tag) to a target tier."""
    cfg = load_config(resolve_config_path(args))

    if args.tag:
        if args.name:
            raise CuratorError("Specify either a skill <name> or --tag <tag>, not both.")
        if args.target == "global":
            raise CuratorError("Batch --tag mounting is supported only for --target project.")
        return mount_by_tag(
            cfg=cfg,
            tag=args.tag,
            project_dir=_optional_path(args.project_dir),
            claude_sync_override=args.claude_sync,
        )

    if not args.name:
        raise CuratorError("Skill <name> is required unless --tag <tag> is specified.")
    if not args.target:
        raise CuratorError("--target project|global is required when mounting a skill.")

    return mount_vault_skill(
        cfg=cfg,
        raw_name=args.name,
        target=args.target,
        project_dir=_optional_path(args.project_dir),
        claude_sync_override=args.claude_sync,
        force=args.force,
    )


def cmd_mount_tag(args: argparse.Namespace) -> dict[str, Any]:
    """Batch copy-mount all Vault skills matching a Scene Tag into Project Workspace."""
    cfg = load_config(resolve_config_path(args))
    return mount_by_tag(
        cfg=cfg,
        tag=args.tag,
        project_dir=_optional_path(args.project_dir),
        claude_sync_override=args.claude_sync,
    )


def cmd_fetch(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch a GitHub or local Git skill source into a clean staging directory."""
    if args.config_sub or args.config_top:
        load_config(resolve_config_path(args))
    return fetch_source(
        source=args.source,
        dest_dir=args.dest_dir,
        skill_selector=args.skill,
        ref=args.ref,
        subpath=args.subpath,
        timeout=args.timeout,
        fetch_all=args.all,
    )


def cmd_inspect(args: argparse.Namespace) -> dict[str, Any]:
    """Inspect a candidate skill directory for quality, safety, collisions, and cap."""
    cfg = load_config(resolve_config_path(args))
    source_dir = Path(os.path.expanduser(args.source_dir)).resolve()
    return inspect_skill(
        source_dir=source_dir,
        cfg=cfg,
        project_dir=_optional_path(args.project_dir),
        desired_tier=args.desired_tier,
    )


def cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    """Search Skill Vault INDEX.md and metadata by keyword, description, or Scene Tag."""
    cfg = load_config(resolve_config_path(args))
    return search_vault(
        cfg=cfg,
        query=args.query or "",
        tag=args.tag,
        limit=args.limit,
    )


def _load_optional_config(args: argparse.Namespace) -> tuple[dict[str, Any] | None, bool]:
    """Load config if present or explicitly requested; return (None, False) before init."""
    explicit = getattr(args, "config_sub", None) or getattr(args, "config_top", None) or os.environ.get("SKILL_CURATOR_CONFIG")
    if explicit:
        return load_config(Path(os.path.expanduser(explicit)).resolve()), True
    home_override = _optional_path(getattr(args, "home_dir", None))
    default_cfg = resolve_home_dir(home_override) / ".config" / "skill-curator" / "config.json"
    if default_cfg.is_file():
        return load_config(default_cfg.resolve()), True
    return None, False


def cmd_scan(args: argparse.Namespace) -> dict[str, Any]:
    """Scan local agent harness directories and generate diagnostics & remediation plan."""
    cfg, config_loaded = _load_optional_config(args)
    cap_override = (
        _validate_positive_int(args.global_cap, "global_cap")
        if args.global_cap is not None
        else None
    )
    return scan_local_skills(
        cfg=cfg,
        config_loaded=config_loaded,
        home_dir=_optional_path(args.home_dir),
        scan_dirs=args.scan_dir,
        project_dir=_optional_path(args.project_dir),
        global_cap_override=cap_override,
    )


def cmd_adopt(args: argparse.Namespace) -> dict[str, Any]:
    """Back up active skill(s) to Skill Vault and stamp active provenance metadata."""
    cfg = load_config(resolve_config_path(args))
    return adopt_skill(
        cfg=cfg,
        raw_name=args.name,
        source_dir=_optional_path(args.source_dir),
        tier=args.tier,
        project_dir=_optional_path(args.project_dir),
        home_dir=_optional_path(args.home_dir),
        scan_dirs=args.scan_dir,
        keywords=split_csv(args.keywords),
        scene_tags=split_csv(args.tags),
        source_url=(args.source_url or "").strip(),
        override_description=args.override_description,
    )


def cmd_remediate(args: argparse.Namespace) -> dict[str, Any]:
    """Execute user-confirmed onboarding/cleanup plan in one command."""
    cfg = load_config(resolve_config_path(args))
    adopt_items: list[dict[str, Any]] = []
    demote_items: list[dict[str, Any]] = []

    if args.plan:
        if args.plan == "-":
            if hasattr(sys.stdin, "buffer"):
                raw_plan_text = decode_raw_bytes(sys.stdin.buffer.read())
            else:
                raw_plan_text = sys.stdin.read()
        else:
            plan_path = Path(os.path.expanduser(args.plan)).resolve()
            if not plan_path.is_file():
                raise CuratorError(f"Plan file not found: '{plan_path}'.")
            raw_plan_text = plan_path.read_text(encoding="utf-8")
        try:
            plan_data = json.loads(raw_plan_text)
        except json.JSONDecodeError as exc:
            raise CuratorError(f"Invalid JSON plan: {exc}") from exc
        if not isinstance(plan_data, dict):
            raise CuratorError("Remediation JSON plan must be an object.")
        parsed_adopt, parsed_demote = parse_remediate_plan_payload(plan_data)
        adopt_items.extend(parsed_adopt)
        demote_items.extend(parsed_demote)

    for name in args.adopt or []:
        adopt_items.append({"name": name})
    for name in args.demote or []:
        demote_items.append({"name": name})

    return remediate_plan(
        cfg=cfg,
        adopt_items=adopt_items,
        demote_items=demote_items,
        home_dir=_optional_path(args.home_dir),
        scan_dirs=args.scan_dir,
        project_dir=_optional_path(args.project_dir),
        dry_run=bool(args.dry_run),
        force=bool(args.force),
    )


def cmd_demote(args: argparse.Namespace) -> dict[str, Any]:
    """Demote an active skill from Global Core or Project Workspace back to Skill Vault."""
    cfg = load_config(resolve_config_path(args))
    return demote_skill(
        cfg=cfg,
        raw_name=args.name,
        source_tier=args.source_tier,
        project_dir=_optional_path(args.project_dir),
        keywords=split_csv(args.keywords) if args.keywords else None,
        scene_tags=split_csv(args.tags) if args.tags else None,
        extra_global_dirs=args.extra_global_dir,
        home_dir=_optional_path(args.home_dir),
        discover_global_dirs=bool(args.discover_global_dirs),
        force=bool(args.force),
    )


def cmd_health(args: argparse.Namespace) -> dict[str, Any]:
    """Run deterministic health audit across Global Core and Project Workspace."""
    cfg = load_config(resolve_config_path(args))
    return health_audit(
        cfg=cfg,
        project_dir=_optional_path(args.project_dir),
        stale_days_override=args.stale_days,
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser."""
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument(
        "--config",
        dest="config_sub",
        help="Path to Skill Curator config.json (overrides SKILL_CURATOR_CONFIG).",
    )

    parser = argparse.ArgumentParser(
        prog="curator.py",
        description="Cross-agent Skill Curator CLI.",
    )
    parser.add_argument(
        "--config",
        dest="config_top",
        help="Path to Skill Curator config.json (overrides SKILL_CURATOR_CONFIG).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_p = subparsers.add_parser("init", parents=[config_parent], help="Initialize Skill Vault.")
    init_p.add_argument("--vault", required=True, help="Skill Vault directory path.")
    init_p.add_argument(
        "--global-dir",
        action="append",
        default=None,
        help="Managed Global Core skill directory (repeatable).",
    )
    init_p.add_argument("--global-cap", type=int, default=None, help="Global Core soft cap.")
    init_p.add_argument(
        "--claude-sync",
        choices=VALID_CLAUDE_SYNC,
        default=None,
        help="Claude Code mirroring mode for project mounts.",
    )
    init_p.add_argument("--stale-days", type=int, default=None, help="Stale threshold in days.")
    init_p.add_argument(
        "--discover-global-dirs",
        action="store_true",
        help="Discover existing agent harness skill directories and include them in global_core_dirs.",
    )
    init_p.add_argument("--home-dir", default=None, help="Override home directory for discovery.")
    init_p.set_defaults(func=cmd_init)

    store_p = subparsers.add_parser(
        "store", parents=[config_parent], help="Store a skill in the Skill Vault."
    )
    store_p.add_argument("--source-dir", required=True, help="Path to source skill directory.")
    store_p.add_argument("--name", default=None, help="Override kebab-case skill name.")
    store_p.add_argument("--keywords", default="", help="Comma-separated capability keywords.")
    store_p.add_argument("--tags", default="", help="Comma-separated Scene Tags.")
    store_p.add_argument("--source-url", default="", help="Origin URL for provenance.")
    store_p.add_argument(
        "--override-description",
        default=None,
        help="Replace description in Vault copy after explicit user confirmation.",
    )
    store_p.set_defaults(func=cmd_store)

    mount_p = subparsers.add_parser(
        "mount", parents=[config_parent], help="Copy-mount a Vault skill or Scene Tag to a tier."
    )
    mount_p.add_argument("name", nargs="?", default=None, help="Skill name in the Skill Vault.")
    mount_p.add_argument(
        "--tag", default=None, help="Batch-mount all Vault skills matching this Scene Tag."
    )
    mount_p.add_argument(
        "--target",
        required=False,
        default=None,
        choices=("project", "global"),
        help="Mount destination tier.",
    )
    mount_p.add_argument("--project-dir", default=None, help="Target Project Workspace path.")
    mount_p.add_argument(
        "--claude-sync",
        choices=VALID_CLAUDE_SYNC,
        default=None,
        help="Override claude_sync setting for this project mount.",
    )
    mount_p.add_argument(
        "--force",
        action="store_true",
        help="Override Global Core soft cap gate when mounting to global.",
    )
    mount_p.set_defaults(func=cmd_mount)

    mount_tag_p = subparsers.add_parser(
        "mount-tag",
        parents=[config_parent],
        help="Batch copy-mount all Vault skills matching a Scene Tag into a Project Workspace.",
    )
    mount_tag_p.add_argument("tag", help="Scene Tag to batch-mount.")
    mount_tag_p.add_argument("--project-dir", default=None, help="Target Project Workspace path.")
    mount_tag_p.add_argument(
        "--claude-sync",
        choices=VALID_CLAUDE_SYNC,
        default=None,
        help="Override claude_sync setting for these project mounts.",
    )
    mount_tag_p.set_defaults(func=cmd_mount_tag)

    fetch_p = subparsers.add_parser(
        "fetch", parents=[config_parent], help="Fetch skill(s) from GitHub URL or Git repo."
    )
    fetch_p.add_argument("source", help="GitHub URL, raw SKILL.md URL, or Git repository path.")
    fetch_p.add_argument("--dest-dir", default=None, help="Parent staging directory.")
    fetch_p.add_argument("--skill", default=None, help="Skill name or rel_path to select.")
    fetch_p.add_argument(
        "--all",
        action="store_true",
        help="Stage all discovered skills from a multi-skill repository in one clone.",
    )
    fetch_p.add_argument("--ref", default=None, help="Optional Git branch, tag, or commit.")
    fetch_p.add_argument("--subpath", default=None, help="Optional repository subpath.")
    fetch_p.add_argument(
        "--timeout", type=float, default=30.0, help="Subprocess timeout in seconds."
    )
    fetch_p.set_defaults(func=cmd_fetch)

    inspect_p = subparsers.add_parser(
        "inspect",
        parents=[config_parent],
        help="Audit candidate skill quality, collisions, and Global Core cap.",
    )
    inspect_p.add_argument("--source-dir", required=True, help="Path to candidate skill directory.")
    inspect_p.add_argument("--project-dir", default=None, help="Active Project Workspace path.")
    inspect_p.add_argument(
        "--desired-tier",
        choices=("auto", "global", "project", "vault"),
        default="auto",
        help="Desired placement tier for cap evaluation.",
    )
    inspect_p.set_defaults(func=cmd_inspect)

    search_p = subparsers.add_parser(
        "search", parents=[config_parent], help="Search the Skill Vault Index."
    )
    search_p.add_argument("query", nargs="?", default="", help="Keyword or capability query.")
    search_p.add_argument("--tag", default=None, help="Filter by Scene Tag.")
    search_p.add_argument("--limit", type=int, default=None, help="Maximum results to return.")
    search_p.set_defaults(func=cmd_search)

    demote_p = subparsers.add_parser(
        "demote",
        parents=[config_parent],
        help="Demote an active skill to the Skill Vault and remove active mounts.",
    )
    demote_p.add_argument("name", help="Skill name to demote.")
    demote_p.add_argument(
        "--from",
        dest="source_tier",
        required=True,
        choices=("global", "project"),
        help="Active tier to demote from.",
    )
    demote_p.add_argument("--project-dir", default=None, help="Project Workspace path.")
    demote_p.add_argument("--keywords", default="", help="Comma-separated keywords to store.")
    demote_p.add_argument("--tags", default="", help="Comma-separated Scene Tags to store.")
    demote_p.add_argument("--home-dir", default=None, help="Override home directory for discovery.")
    demote_p.add_argument(
        "--extra-global-dir",
        action="append",
        default=None,
        help="Additional global skill directory to demote from (repeatable).",
    )
    demote_p.add_argument(
        "--discover-global-dirs",
        action="store_true",
        help="Also scan standard agent harness directories when archiving/removing.",
    )
    demote_p.add_argument(
        "--force",
        action="store_true",
        help="Allow archiving primary and removing diverged multi-harness duplicates.",
    )
    demote_p.set_defaults(func=cmd_demote)

    scan_p = subparsers.add_parser(
        "scan",
        aliases=["diagnose"],
        parents=[config_parent],
        help="Discover local skills across harnesses, diagnose issues, and recommend remediation.",
    )
    scan_p.add_argument("--home-dir", default=None, help="Override home directory for discovery.")
    scan_p.add_argument(
        "--scan-dir",
        action="append",
        default=None,
        help="Additional skill directory to include in scan (repeatable).",
    )
    scan_p.add_argument("--project-dir", default=None, help="Project Workspace path.")
    scan_p.add_argument("--global-cap", type=int, default=None, help="Override Global Core soft cap.")
    scan_p.set_defaults(func=cmd_scan)

    adopt_p = subparsers.add_parser(
        "adopt",
        parents=[config_parent],
        help="Back up kept active skill(s) into Skill Vault and stamp provenance without removing.",
    )
    adopt_p.add_argument("name", nargs="?", default=None, help="Active skill name to adopt.")
    adopt_p.add_argument("--source-dir", default=None, help="Explicit active skill directory path.")
    adopt_p.add_argument(
        "--tier",
        choices=("global", "project"),
        default="global",
        help="Active tier of the kept skill.",
    )
    adopt_p.add_argument("--project-dir", default=None, help="Project Workspace path.")
    adopt_p.add_argument("--home-dir", default=None, help="Override home directory for discovery.")
    adopt_p.add_argument(
        "--scan-dir",
        action="append",
        default=None,
        help="Additional skill directory to search (repeatable).",
    )
    adopt_p.add_argument("--keywords", default="", help="Comma-separated capability keywords.")
    adopt_p.add_argument("--tags", default="", help="Comma-separated Scene Tags.")
    adopt_p.add_argument("--source-url", default="", help="Origin URL for provenance.")
    adopt_p.add_argument(
        "--override-description",
        default=None,
        help="Replace description in Vault copy only after explicit user confirmation.",
    )
    adopt_p.set_defaults(func=cmd_adopt)

    rem_p = subparsers.add_parser(
        "remediate",
        parents=[config_parent],
        help="Execute a user-confirmed local skill adoption and Vault-archival plan.",
    )
    rem_p.add_argument(
        "--adopt",
        action="append",
        default=None,
        help="Skill name to keep active and adopt into Vault (repeatable).",
    )
    rem_p.add_argument(
        "--demote",
        action="append",
        default=None,
        help="Skill name to archive into Vault and remove from active mounts (repeatable).",
    )
    rem_p.add_argument(
        "--plan",
        default=None,
        help="Path to JSON remediation plan file (or '-' for stdin).",
    )
    rem_p.add_argument("--home-dir", default=None, help="Override home directory for discovery.")
    rem_p.add_argument(
        "--scan-dir",
        action="append",
        default=None,
        help="Additional skill directory to include (repeatable).",
    )
    rem_p.add_argument("--project-dir", default=None, help="Project Workspace path.")
    rem_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview planned adopt/demote actions without writing or deleting files.",
    )
    rem_p.add_argument(
        "--force",
        action="store_true",
        help="Allow demoting diverged duplicates across multiple harnesses.",
    )
    rem_p.set_defaults(func=cmd_remediate)

    health_p = subparsers.add_parser(
        "health",
        parents=[config_parent],
        help="Audit active Global Core and Project Workspace mounts.",
    )
    health_p.add_argument("--project-dir", default=None, help="Active Project Workspace path.")
    health_p.add_argument(
        "--stale-days", type=int, default=None, help="Override stale age threshold in days."
    )
    health_p.set_defaults(func=cmd_health)

    return parser


def _ensure_utf8_stdio() -> None:
    """Reconfigure standard streams to UTF-8 to avoid CP936/CP1252 locale crashes."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except CuratorError as exc:
        err_body: dict[str, Any] = {"ok": False, "error": str(exc)}
        if exc.payload:
            err_body.update(exc.payload)
        sys.stderr.write(json.dumps(err_body, ensure_ascii=False) + "\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
