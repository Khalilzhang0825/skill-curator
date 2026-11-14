"""Vault search, Scene Tag batch mounting, demotion, and deterministic health audit."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auditor import (
    CAP_RESOLUTION_OPTIONS,
    assess_skill_quality,
    compute_keyword_overlap,
    extract_tokens,
    is_plugin_bundle_dir,
    scan_global_core,
)
from common import (
    STANDARD_HARNESS_SPECS,
    CuratorError,
    copy_tree_clean,
    parse_frontmatter,
    read_index_rows,
    resolve_home_dir,
    rmtree_safe,
    tree_content_digest,
    upsert_index_row,
    utc_now_iso,
    validate_skill_name,
    validate_tree_containment,
)


def _should_mirror_claude(project_dir: Path, mode: str) -> bool:
    """Return True if a project mount should be mirrored into .claude/skills/."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return (project_dir / ".claude").exists() or (project_dir / "CLAUDE.md").exists()


def _read_json_dict(path: Path) -> dict[str, Any]:
    """Read a JSON file as dict if present, raising CuratorError on corrupt JSON."""
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError as exc:
        raise CuratorError(f"Invalid metadata in '{path}': {exc}") from exc


def _merge_str_lists(*lists: list[Any]) -> list[str]:
    """Merge string lists preserving order and removing duplicates/blanks."""
    merged: list[str] = []
    seen: set[str] = set()
    for seq in lists:
        for item in seq:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                merged.append(s)
    return merged


def load_vault_catalog(vault_dir: Path) -> list[dict[str, Any]]:
    """Load all stored Vault skills merging INDEX.md cells with .curator-meta.json."""
    skills_dir = vault_dir / "skills"
    if not skills_dir.is_dir():
        return []

    index_rows = read_index_rows(vault_dir / "INDEX.md")
    catalog: list[dict[str, Any]] = []

    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or not (entry / "SKILL.md").is_file():
            continue
        meta = _read_json_dict(entry / ".curator-meta.json")
        fm = parse_frontmatter(entry / "SKILL.md")
        row = index_rows.get(entry.name, {})

        meta_kws = meta.get("keywords") if isinstance(meta.get("keywords"), list) else []
        meta_tags = meta.get("scene_tags") if isinstance(meta.get("scene_tags"), list) else []
        idx_kws = row.get("keywords", [])
        idx_tags = row.get("scene_tags", [])

        keywords = _merge_str_lists(meta_kws, idx_kws)
        scene_tags = _merge_str_lists(meta_tags, idx_tags)
        source_url = str(row.get("source_url") or meta.get("source_url") or "")
        description = str(meta.get("description") or fm.get("description") or "")

        catalog.append(
            {
                "name": entry.name,
                "description": description,
                "keywords": keywords,
                "scene_tags": scene_tags,
                "source_url": source_url,
                "stored_at": str(meta.get("stored_at") or ""),
                "path": f"skills/{entry.name}",
                "skill_dir": entry,
            }
        )
    return catalog


def mount_vault_skill(
    cfg: dict[str, Any],
    raw_name: str,
    target: str,
    project_dir: Path | None = None,
    claude_sync_override: str | None = None,
    force: bool = False,
    merged_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy-mount a single skill from the Skill Vault into Project Workspace or Global Core."""
    skill_name = validate_skill_name(raw_name)
    vault_dir = Path(cfg["vault_path"])
    vault_skill_dir = vault_dir / "skills" / skill_name
    if not vault_skill_dir.is_dir():
        raise CuratorError(f"Skill '{skill_name}' not found in Skill Vault '{vault_dir}'.")

    validate_tree_containment(vault_skill_dir)
    if not (vault_skill_dir / "SKILL.md").is_file():
        raise CuratorError(f"Vault skill '{skill_name}' is missing SKILL.md.")

    if merged_meta is None:
        raw_meta = _read_json_dict(vault_skill_dir / ".curator-meta.json")
        fm = parse_frontmatter(vault_skill_dir / "SKILL.md")
        row = read_index_rows(vault_dir / "INDEX.md").get(skill_name, {})

        meta_kws = raw_meta.get("keywords") if isinstance(raw_meta.get("keywords"), list) else []
        meta_tags = (
            raw_meta.get("scene_tags") if isinstance(raw_meta.get("scene_tags"), list) else []
        )
        vault_meta = {
            "name": skill_name,
            "description": str(raw_meta.get("description") or fm.get("description") or ""),
            "keywords": _merge_str_lists(meta_kws, row.get("keywords", [])),
            "scene_tags": _merge_str_lists(meta_tags, row.get("scene_tags", [])),
            "source_url": str(row.get("source_url") or raw_meta.get("source_url") or ""),
            "stored_at": str(raw_meta.get("stored_at") or ""),
        }
    else:
        vault_meta = merged_meta

    mounted_at = utc_now_iso()
    mount_meta = {
        "name": skill_name,
        "description": vault_meta.get("description", ""),
        "keywords": vault_meta.get("keywords", []),
        "scene_tags": vault_meta.get("scene_tags", []),
        "source_url": vault_meta.get("source_url", ""),
        "stored_at": vault_meta.get("stored_at", ""),
        "mounted_at": mounted_at,
        "mounted_from_vault": str(vault_skill_dir.resolve()),
        "target": target,
    }

    dest_dirs: list[Path] = []
    claude_mirrored = False

    if target == "project":
        resolved_project = project_dir if project_dir is not None else Path.cwd().resolve()
        dest_dirs.append(resolved_project / ".agents" / "skills" / skill_name)
        effective_sync = claude_sync_override or cfg["claude_sync"]
        if _should_mirror_claude(resolved_project, effective_sync):
            dest_dirs.append(resolved_project / ".claude" / "skills" / skill_name)
            claude_mirrored = True
    else:
        occupancy = scan_global_core(cfg["global_core_dirs"], cfg["global_cap"])
        if occupancy["at_cap"] and skill_name not in occupancy["skills"] and not force:
            raise CuratorError(
                f"Global Core cap reached ({occupancy['count']}/{occupancy['cap']}). "
                "Demote an existing Global Core skill to the Skill Vault, mount to a Project Workspace, "
                "keep in the Skill Vault, or pass --force.",
                payload={
                    "error_code": "GLOBAL_CAP_REACHED",
                    "global_count": occupancy["count"],
                    "global_cap": occupancy["cap"],
                    "demote_candidates": occupancy["skills"],
                    "options": list(CAP_RESOLUTION_OPTIONS),
                },
            )
        for raw_gdir in cfg["global_core_dirs"]:
            gdir = Path(os.path.expanduser(raw_gdir)).resolve()
            dest_dirs.append(gdir / skill_name)

    mounted_paths: list[str] = []
    for dest in dest_dirs:
        copy_tree_clean(vault_skill_dir, dest)
        (dest / ".curator-meta.json").write_text(
            json.dumps(mount_meta, indent=2) + "\n", encoding="utf-8"
        )
        mounted_paths.append(str(dest))

    return {
        "ok": True,
        "action": "mount",
        "name": skill_name,
        "target": target,
        "mounted_paths": mounted_paths,
        "claude_mirrored": claude_mirrored,
        "mounted_at": mounted_at,
    }


def search_vault(
    cfg: dict[str, Any],
    query: str = "",
    tag: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Search Skill Vault catalog by keyword, description, or Scene Tag."""
    if limit is not None and limit < 1:
        raise CuratorError(f"Invalid --limit '{limit}'. Must be >= 1.")

    vault_dir = Path(cfg["vault_path"])
    catalog = load_vault_catalog(vault_dir)

    tag_clean = (tag or "").strip()
    if tag_clean:
        tag_lower = tag_clean.lower()
        catalog = [
            item
            for item in catalog
            if tag_lower in [t.lower() for t in item["scene_tags"]]
        ]

    q_clean = (query or "").strip()
    scored: list[tuple[int, dict[str, Any]]] = []
    if not q_clean:
        for item in catalog:
            scored.append((1, item))
        scored.sort(key=lambda pair: pair[1]["name"])
    else:
        q_lower = q_clean.lower()
        q_tokens = extract_tokens(q_clean)
        for item in catalog:
            score = 0
            name_lower = item["name"].lower()
            if q_lower == name_lower:
                score += 100
            elif q_lower in name_lower:
                score += 50

            kws_lower = [k.lower() for k in item["keywords"]]
            tags_lower = [t.lower() for t in item["scene_tags"]]
            if q_lower in kws_lower or q_lower in tags_lower:
                score += 40
            elif any(q_lower in k for k in kws_lower) or any(
                q_lower in t for t in tags_lower
            ):
                score += 25

            if q_lower in item["description"].lower():
                score += 20

            item_tokens = extract_tokens(
                f"{item['name']} {' '.join(item['keywords'])} "
                f"{' '.join(item['scene_tags'])} {item['description']}"
            )
            overlap = q_tokens & item_tokens
            if overlap:
                score += 10 * len(overlap)

            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))

    if limit is not None:
        scored = scored[:limit]

    results = [
        {
            "name": item["name"],
            "description": item["description"],
            "keywords": item["keywords"],
            "scene_tags": item["scene_tags"],
            "source_url": item["source_url"],
            "path": item["path"],
            "score": score,
        }
        for score, item in scored
    ]

    return {
        "ok": True,
        "action": "search",
        "query": q_clean,
        "tag": tag_clean or None,
        "count": len(results),
        "results": results,
    }


def mount_by_tag(
    cfg: dict[str, Any],
    tag: str,
    project_dir: Path | None = None,
    claude_sync_override: str | None = None,
) -> dict[str, Any]:
    """Batch copy-mount all Vault skills sharing a Scene Tag into a Project Workspace."""
    tag_clean = (tag or "").strip()
    vault_dir = Path(cfg["vault_path"])
    catalog = load_vault_catalog(vault_dir)
    matching = [
        item
        for item in catalog
        if tag_clean and tag_clean.lower() in [t.lower() for t in item["scene_tags"]]
    ]
    if not tag_clean or not matching:
        raise CuratorError(
            f"No Vault skills found matching Scene Tag '{tag}'.",
            payload={"error_code": "TAG_EMPTY_OR_NOT_FOUND", "tag": tag_clean},
        )

    mounted_skills: list[str] = []
    mounted_paths: list[str] = []
    claude_mirrored = False

    for item in matching:
        res = mount_vault_skill(
            cfg=cfg,
            raw_name=item["name"],
            target="project",
            project_dir=project_dir,
            claude_sync_override=claude_sync_override,
            merged_meta=item,
        )
        mounted_skills.append(item["name"])
        mounted_paths.extend(res["mounted_paths"])
        claude_mirrored = claude_mirrored or bool(res["claude_mirrored"])

    return {
        "ok": True,
        "action": "mount-tag",
        "tag": tag_clean,
        "target": "project",
        "mounted_skills": mounted_skills,
        "mounted_paths": mounted_paths,
        "claude_mirrored": claude_mirrored,
    }


def ensure_no_diverged_duplicates(
    existing: list[Path], skill_name: str, force: bool
) -> None:
    """Reject removing multiple copies if any are missing/non-dirs or differ in content."""
    if len(existing) <= 1 or force:
        return
    non_symlink_dirs = [p for p in existing if p.is_dir() and not p.is_symlink()]
    if len(non_symlink_dirs) != len(existing) or len(
        {tree_content_digest(p) for p in non_symlink_dirs}
    ) > 1:
        raise CuratorError(
            f"Diverged duplicates detected for '{skill_name}' across {[str(p) for p in existing]}. "
            "Inspect differences or pass --force to archive primary and remove all copies.",
            payload={
                "error_code": "DIVERGED_DUPLICATE",
                "name": skill_name,
                "paths": [str(p) for p in existing],
            },
        )


def demote_skill(
    cfg: dict[str, Any],
    raw_name: str,
    source_tier: str,
    project_dir: Path | None = None,
    *,
    keywords: list[str] | None = None,
    scene_tags: list[str] | None = None,
    extra_global_dirs: list[str] | None = None,
    home_dir: Path | None = None,
    discover_global_dirs: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Archive/update an active skill in Skill Vault and remove its active mount(s)."""
    cleaned_raw = raw_name.strip()
    if cleaned_raw == "skill-curator":
        raise CuratorError("Refusing to demote 'skill-curator' itself.")

    skill_name = validate_skill_name(cleaned_raw)

    if source_tier == "project":
        resolved_proj = project_dir if project_dir is not None else Path.cwd().resolve()
        parent_dirs = [
            resolved_proj / ".agents" / "skills",
            resolved_proj / ".claude" / "skills",
        ]
    else:
        raw_parents: list[str] = list(cfg["global_core_dirs"])
        if extra_global_dirs:
            raw_parents.extend(extra_global_dirs)
        if discover_global_dirs:
            resolved_home = resolve_home_dir(home_dir)
            for _, parts in STANDARD_HARNESS_SPECS:
                cand_parent = resolved_home.joinpath(*parts)
                if cand_parent.is_dir():
                    raw_parents.append(str(cand_parent))
        dedup_parents: list[Path] = []
        seen_parents: set[str] = set()
        for raw_dir in raw_parents:
            p = Path(os.path.expanduser(raw_dir)).resolve()
            if str(p) not in seen_parents:
                seen_parents.add(str(p))
                dedup_parents.append(p)
        parent_dirs = dedup_parents

    candidates = [p / skill_name for p in parent_dirs]
    existing = [p for p in candidates if p.exists() or p.is_symlink()]
    if not existing:
        raise CuratorError(
            f"Skill '{skill_name}' is not mounted in {source_tier}."
        )

    for path in existing:
        if is_plugin_bundle_dir(path):
            raise CuratorError(
                f"Refusing to demote external plugin bundle '{path.name}'."
            )

    ensure_no_diverged_duplicates(existing, skill_name, force)

    primary = next((p for p in existing if (p / "SKILL.md").is_file()), None)
    if primary is None:
        raise CuratorError(
            f"Mounted directory for '{skill_name}' is missing SKILL.md."
        )

    validate_tree_containment(primary)
    fm = parse_frontmatter(primary / "SKILL.md")
    mount_meta = _read_json_dict(primary / ".curator-meta.json")

    vault_dir = Path(cfg["vault_path"])
    vault_skills_dir = vault_dir / "skills"
    vault_skills_dir.mkdir(parents=True, exist_ok=True)
    vault_skill_dir = vault_skills_dir / skill_name

    existing_vault_meta = _read_json_dict(vault_skill_dir / ".curator-meta.json")
    index_row = read_index_rows(vault_dir / "INDEX.md").get(skill_name, {})

    keywords = _merge_str_lists(
        keywords or [],
        existing_vault_meta.get("keywords", [])
        if isinstance(existing_vault_meta.get("keywords"), list)
        else [],
        index_row.get("keywords", []),
        mount_meta.get("keywords", [])
        if isinstance(mount_meta.get("keywords"), list)
        else [],
    )
    scene_tags = _merge_str_lists(
        scene_tags or [],
        existing_vault_meta.get("scene_tags", [])
        if isinstance(existing_vault_meta.get("scene_tags"), list)
        else [],
        index_row.get("scene_tags", []),
        mount_meta.get("scene_tags", [])
        if isinstance(mount_meta.get("scene_tags"), list)
        else [],
    )
    source_url = str(
        existing_vault_meta.get("source_url")
        or index_row.get("source_url")
        or mount_meta.get("source_url")
        or ""
    )
    description = str(
        fm.get("description")
        or mount_meta.get("description")
        or existing_vault_meta.get("description")
        or ""
    )
    stored_at = str(
        existing_vault_meta.get("stored_at")
        or mount_meta.get("stored_at")
        or utc_now_iso()
    )

    copy_tree_clean(primary, vault_skill_dir)
    vault_meta = {
        "name": skill_name,
        "description": description,
        "keywords": keywords,
        "scene_tags": scene_tags,
        "source_url": source_url,
        "stored_at": stored_at,
    }
    (vault_skill_dir / ".curator-meta.json").write_text(
        json.dumps(vault_meta, indent=2) + "\n", encoding="utf-8"
    )
    upsert_index_row(
        index_path=vault_dir / "INDEX.md",
        name=skill_name,
        keywords=keywords,
        scene_tags=scene_tags,
        source_url=source_url,
        rel_path=f"skills/{skill_name}",
    )

    removed_paths: list[str] = []
    for path in existing:
        rmtree_safe(path)
        removed_paths.append(str(path))

    return {
        "ok": True,
        "action": "demote",
        "name": skill_name,
        "from": source_tier,
        "vault_skill_dir": str(vault_skill_dir),
        "removed_paths": removed_paths,
    }


def _parse_iso_utc(raw_ts: str) -> datetime | None:
    """Parse ISO-8601 timestamp string into timezone-aware UTC datetime."""
    cleaned = raw_ts.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _collect_project_paths(project_dir: Path | None) -> dict[str, Path]:
    """Collect deduplicated active standalone skills from a Project Workspace."""
    if project_dir is None:
        return {}
    found: dict[str, Path] = {}
    for container in (
        project_dir / ".agents" / "skills",
        project_dir / ".claude" / "skills",
    ):
        if not container.is_dir():
            continue
        for entry in sorted(container.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            if entry.name == "skill-curator" or is_plugin_bundle_dir(entry):
                continue
            if not (entry / "SKILL.md").is_file():
                continue
            found.setdefault(entry.name, entry)
    return found


def health_audit(
    cfg: dict[str, Any],
    project_dir: Path | None = None,
    stale_days_override: int | None = None,
) -> dict[str, Any]:
    """Audit Global Core and Project Workspace for cap, overlaps, bloat, and staleness."""
    stale_days = (
        stale_days_override if stale_days_override is not None else cfg["stale_days"]
    )
    if stale_days < 1:
        raise CuratorError(f"Invalid stale_days '{stale_days}'. Must be >= 1.")

    global_scan = scan_global_core(cfg["global_core_dirs"], cfg["global_cap"])
    global_paths: dict[str, Path] = global_scan["paths"]
    project_paths = _collect_project_paths(project_dir)

    cap_violations: list[dict[str, Any]] = []
    if global_scan["count"] > cfg["global_cap"]:
        cap_violations.append(
            {
                "tier": "global",
                "count": global_scan["count"],
                "cap": cfg["global_cap"],
                "excess": global_scan["count"] - cfg["global_cap"],
                "skills": global_scan["skills"],
            }
        )

    active_records: list[dict[str, Any]] = []
    for tier, mapping in (("global", global_paths), ("project", project_paths)):
        for name, path in sorted(mapping.items()):
            meta = _read_json_dict(path / ".curator-meta.json")
            quality = assess_skill_quality(path)
            kws = meta.get("keywords") if isinstance(meta.get("keywords"), list) else []
            tokens = extract_tokens(
                f"{name} {quality['description']} {' '.join(str(k) for k in kws)}"
            )
            active_records.append(
                {
                    "tier": tier,
                    "name": name,
                    "path": path,
                    "meta": meta,
                    "quality": quality,
                    "tokens": tokens,
                }
            )

    duplicates_and_overlaps: list[dict[str, Any]] = []
    for name in sorted(set(global_paths.keys()) & set(project_paths.keys())):
        duplicates_and_overlaps.append(
            {
                "type": "cross_tier_duplicate",
                "name": name,
                "tiers": ["global", "project"],
                "paths": [str(global_paths[name]), str(project_paths[name])],
            }
        )

    for i in range(len(active_records)):
        rec_a = active_records[i]
        for j in range(i + 1, len(active_records)):
            rec_b = active_records[j]
            if rec_a["name"] == rec_b["name"]:
                continue
            overlap = compute_keyword_overlap(rec_a["tokens"], rec_b["tokens"])
            if overlap is not None:
                shared, jaccard = overlap
                duplicates_and_overlaps.append(
                    {
                        "type": "keyword_overlap",
                        "skills": [rec_a["name"], rec_b["name"]],
                        "tiers": [rec_a["tier"], rec_b["tier"]],
                        "shared_keywords": shared,
                        "jaccard": jaccard,
                    }
                )

    bloat_and_quality: list[dict[str, Any]] = []
    stale_skills: list[dict[str, Any]] = []
    untracked_mounts: list[dict[str, Any]] = []
    now_utc = datetime.now(timezone.utc)

    for rec in active_records:
        q = rec["quality"]
        if q["issues"]:
            bloat_and_quality.append(
                {
                    "tier": rec["tier"],
                    "name": rec["name"],
                    "path": str(rec["path"]),
                    "issues": q["issues"],
                    "line_count": q["instruction_check"]["line_count"],
                    "description_chars": q["description_check"]["char_count"],
                }
            )

        mounted_raw = str(rec["meta"].get("mounted_at") or "").strip()
        if not mounted_raw:
            untracked_mounts.append(
                {
                    "tier": rec["tier"],
                    "name": rec["name"],
                    "path": str(rec["path"]),
                    "reason": "missing_mounted_at",
                }
            )
            continue

        mounted_dt = _parse_iso_utc(mounted_raw)
        if mounted_dt is None:
            untracked_mounts.append(
                {
                    "tier": rec["tier"],
                    "name": rec["name"],
                    "path": str(rec["path"]),
                    "reason": "invalid_mounted_at",
                }
            )
            continue

        age_days = (now_utc - mounted_dt).total_seconds() / 86400.0
        if age_days > stale_days:
            stale_skills.append(
                {
                    "tier": rec["tier"],
                    "name": rec["name"],
                    "path": str(rec["path"]),
                    "mounted_at": mounted_raw,
                    "age_days": round(age_days, 2),
                    "stale_days": stale_days,
                }
            )

    healthy = not (
        cap_violations or duplicates_and_overlaps or bloat_and_quality or stale_skills
    )

    return {
        "ok": True,
        "action": "health",
        "healthy": healthy,
        "stale_days": stale_days,
        "global_core": {
            "count": global_scan["count"],
            "cap": cfg["global_cap"],
            "skills": global_scan["skills"],
        },
        "project_skills": sorted(project_paths.keys()),
        "cap_violations": cap_violations,
        "duplicates_and_overlaps": duplicates_and_overlaps,
        "bloat_and_quality": bloat_and_quality,
        "stale_skills": stale_skills,
        "untracked_mounts": untracked_mounts,
    }
