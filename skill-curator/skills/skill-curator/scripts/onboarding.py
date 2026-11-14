"""Post-install local skill discovery, diagnostics, adoption, and batch remediation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from auditor import (
    STOPWORDS,
    assess_skill_quality,
    compute_keyword_overlap,
    detect_scripts,
    extract_tokens,
    is_plugin_bundle_dir,
)
from common import (
    STANDARD_HARNESS_SPECS,
    CuratorError,
    copy_tree_clean,
    parse_frontmatter,
    read_index_rows,
    resolve_home_dir,
    rewrite_frontmatter_description,
    tree_content_digest,
    upsert_index_row,
    utc_now_iso,
    validate_skill_name,
    validate_tree_containment,
)
from lifecycle import (
    _merge_str_lists,
    _read_json_dict,
    demote_skill,
    ensure_no_diverged_duplicates,
)

SCENE_TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "wechat-writing": ("wechat", "weixin", "公众号", "排版", "article"),
    "documents": ("pdf", "docx", "xlsx", "pptx", "markdown", "ocr", "document"),
    "frontend": ("react", "vue", "css", "tailwind", "ui", "ux", "nextjs", "frontend"),
    "backend-db": ("sql", "postgres", "mysql", "database", "api", "graphql", "backend"),
    "devops": ("docker", "kubernetes", "k8s", "terraform", "deploy", "ci", "cd", "pipeline"),
    "testing": ("test", "testing", "qa", "playwright", "selenium", "coverage", "lint"),
    "security": ("security", "auth", "oauth", "license", "audit", "vulnerability"),
    "architecture": ("diagram", "mermaid", "architecture", "架构", "设计"),
}


def discover_harness_directories(
    *,
    home_dir: Path | None = None,
    configured_dirs: list[str] | None = None,
    scan_dirs: list[str] | None = None,
    project_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return existing skill parent directories across standard harnesses, config, and overrides."""
    resolved_home = resolve_home_dir(home_dir)
    configured_resolved = {
        str(Path(os.path.expanduser(d)).resolve()) for d in (configured_dirs or [])
    }

    seen_paths: dict[str, dict[str, Any]] = {}

    def _register(path_obj: Path, harness: str, tier: str) -> None:
        if not path_obj.is_dir():
            return
        resolved_str = str(path_obj.resolve())
        if resolved_str in seen_paths:
            return
        seen_paths[resolved_str] = {
            "path": resolved_str,
            "harness": harness,
            "tier": tier,
            "configured_in_global_core": resolved_str in configured_resolved,
        }

    for harness, parts in STANDARD_HARNESS_SPECS:
        _register(resolved_home.joinpath(*parts), harness, "global")

    for raw_cfg_dir in configured_dirs or []:
        _register(Path(os.path.expanduser(raw_cfg_dir)).resolve(), "custom-global", "global")

    for raw_scan_dir in scan_dirs or []:
        _register(Path(os.path.expanduser(raw_scan_dir)).resolve(), "custom-scan", "global")

    if project_dir is not None:
        proj_resolved = project_dir.resolve()
        _register(proj_resolved / ".agents" / "skills", "project-agents", "project")
        _register(proj_resolved / ".claude" / "skills", "project-claude", "project")

    return list(seen_paths.values())


def discover_standard_global_dirs(home_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return existing standard harness global skill directories with unmanaged counts."""
    entries = discover_harness_directories(home_dir=home_dir)
    results: list[dict[str, Any]] = []
    for item in entries:
        parent = Path(item["path"])
        skill_count = 0
        excluded: list[str] = []
        for child in sorted(parent.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.name == "skill-curator":
                continue
            if is_plugin_bundle_dir(child):
                excluded.append(child.name)
                continue
            if (child / "SKILL.md").is_file():
                skill_count += 1
        results.append(
            {
                "harness": item["harness"],
                "path": item["path"],
                "skill_count": skill_count,
                "excluded_bundles": excluded,
            }
        )
    return results


def suggest_keywords_and_tags(
    name: str,
    description: str,
    existing_keywords: list[str] | None = None,
    existing_tags: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Infer concise search keywords and Scene Tags from skill metadata."""
    keywords: list[str] = list(existing_keywords or [])
    raw_tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", f"{name} {description}".lower())
    for tok in raw_tokens:
        if tok in STOPWORDS or len(tok) < 2:
            continue
        if tok not in keywords:
            keywords.append(tok)
        if len(keywords) >= 6:
            break
    if not keywords:
        keywords = [name]

    tags: list[str] = list(existing_tags or [])
    haystack = f"{name} {description}".lower()
    for tag, needles in SCENE_TAG_KEYWORDS.items():
        if any(needle in haystack for needle in needles):
            if tag not in tags:
                tags.append(tag)
        if len(tags) >= 3:
            break
    if not tags:
        tags = ["general"]
    return keywords, tags


def _store_to_vault_preserving_source(
    *,
    vault_dir: Path,
    source_dir: Path,
    skill_name: str,
    description: str,
    keywords: list[str],
    scene_tags: list[str],
    source_url: str,
    override_description: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Copy source_dir into Vault, optionally override Vault description only, update INDEX.md."""
    vault_skills_dir = vault_dir / "skills"
    vault_skills_dir.mkdir(parents=True, exist_ok=True)
    vault_skill_dir = vault_skills_dir / skill_name

    existing_meta = _read_json_dict(vault_skill_dir / ".curator-meta.json")
    index_row = read_index_rows(vault_dir / "INDEX.md").get(skill_name, {})

    merged_keywords = _merge_str_lists(
        keywords,
        existing_meta.get("keywords", []) if isinstance(existing_meta.get("keywords"), list) else [],
        index_row.get("keywords", []),
    )
    merged_tags = _merge_str_lists(
        scene_tags,
        existing_meta.get("scene_tags", [])
        if isinstance(existing_meta.get("scene_tags"), list)
        else [],
        index_row.get("scene_tags", []),
    )
    resolved_url = (
        source_url
        or str(existing_meta.get("source_url") or "")
        or str(index_row.get("source_url") or "")
    )
    final_desc = override_description if override_description is not None else description
    stored_at = str(existing_meta.get("stored_at") or utc_now_iso())

    copy_tree_clean(source_dir, vault_skill_dir)
    if override_description is not None:
        rewrite_frontmatter_description(vault_skill_dir / "SKILL.md", skill_name, final_desc)

    vault_meta = {
        "name": skill_name,
        "description": final_desc,
        "keywords": merged_keywords,
        "scene_tags": merged_tags,
        "source_url": resolved_url,
        "stored_at": stored_at,
    }
    (vault_skill_dir / ".curator-meta.json").write_text(
        json.dumps(vault_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    upsert_index_row(
        index_path=vault_dir / "INDEX.md",
        name=skill_name,
        keywords=merged_keywords,
        scene_tags=merged_tags,
        source_url=resolved_url,
        rel_path=f"skills/{skill_name}",
    )
    return vault_skill_dir, vault_meta


def scan_local_skills(
    *,
    cfg: dict[str, Any] | None = None,
    config_loaded: bool = False,
    home_dir: Path | None = None,
    scan_dirs: list[str] | None = None,
    project_dir: Path | None = None,
    global_cap_override: int | None = None,
) -> dict[str, Any]:
    """Scan local agent harness directories and emit diagnostics and remediation plan."""
    global_cap = global_cap_override or (int(cfg["global_cap"]) if cfg else 8)
    vault_path = Path(cfg["vault_path"]) if cfg and cfg.get("vault_path") else None
    vault_skills_dir = (vault_path / "skills") if vault_path else None
    index_rows = (
        read_index_rows(vault_path / "INDEX.md")
        if vault_path and (vault_path / "INDEX.md").is_file()
        else {}
    )

    dir_specs = discover_harness_directories(
        home_dir=home_dir,
        configured_dirs=cfg.get("global_core_dirs") if cfg else None,
        scan_dirs=scan_dirs,
        project_dir=project_dir,
    )

    discovered_dirs: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, Any]] = {}

    for dspec in dir_specs:
        parent = Path(dspec["path"])
        excluded_bundles: list[str] = []
        skill_count = 0

        for entry in sorted(parent.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            if entry.name == "skill-curator":
                continue
            if is_plugin_bundle_dir(entry):
                excluded_bundles.append(entry.name)
                continue
            if not (entry / "SKILL.md").is_file():
                continue

            skill_count += 1
            containment_error = None
            try:
                validate_tree_containment(entry)
            except CuratorError as exc:
                containment_error = str(exc)

            quality = assess_skill_quality(entry)
            scripts_detected = detect_scripts(entry)
            digest = tree_content_digest(entry)
            meta = _read_json_dict(entry / ".curator-meta.json")
            tracked = bool(meta.get("mounted_at") or meta.get("mounted_from_vault"))
            skill_name = entry.name

            loc = {
                "tier": dspec["tier"],
                "harness": dspec["harness"],
                "path": str(entry),
                "digest": digest,
                "tracked": tracked,
                "scripts_detected": scripts_detected,
                "quality_issues": list(quality["issues"]),
                "line_count": quality["instruction_check"]["line_count"],
                "containment_error": containment_error,
            }

            bucket = grouped.get(skill_name)
            if bucket is None:
                in_vault = bool(
                    vault_skills_dir and (vault_skills_dir / skill_name / "SKILL.md").is_file()
                )
                row = index_rows.get(skill_name, {})
                sug_kw, sug_tags = suggest_keywords_and_tags(
                    skill_name,
                    quality["description"],
                    existing_keywords=row.get("keywords"),
                    existing_tags=row.get("scene_tags"),
                )
                bucket = {
                    "name": skill_name,
                    "description": quality["description"],
                    "tiers": set(),
                    "locations": [],
                    "in_vault": in_vault,
                    "tracked": False,
                    "quality": quality,
                    "scripts_detected": list(scripts_detected),
                    "suggested_keywords": sug_kw,
                    "suggested_scene_tags": sug_tags,
                    "_tokens": extract_tokens(f"{skill_name} {quality['description']}"),
                }
                grouped[skill_name] = bucket

            bucket["tiers"].add(dspec["tier"])
            bucket["locations"].append(loc)
            if tracked:
                bucket["tracked"] = True
            for sfile in scripts_detected:
                if sfile not in bucket["scripts_detected"]:
                    bucket["scripts_detected"].append(sfile)

        discovered_dirs.append(
            {
                "path": dspec["path"],
                "harness": dspec["harness"],
                "tier": dspec["tier"],
                "configured_in_global_core": dspec["configured_in_global_core"],
                "skill_count": skill_count,
                "excluded_bundles": excluded_bundles,
            }
        )

    skills_out: list[dict[str, Any]] = []
    cross_harness_duplicates: list[dict[str, Any]] = []
    cross_tier_duplicates: list[dict[str, Any]] = []
    bloat_and_quality: list[dict[str, Any]] = []
    scripts_present: list[dict[str, Any]] = []
    untracked_skills: list[str] = []
    global_names: list[str] = []

    for skill_name in sorted(grouped.keys()):
        b = grouped[skill_name]
        locs = b["locations"]
        digests = {loc["digest"] for loc in locs}
        content_identical = len(digests) <= 1

        global_locs = [loc for loc in locs if loc["tier"] == "global"]
        project_locs = [loc for loc in locs if loc["tier"] == "project"]
        if global_locs:
            global_names.append(skill_name)

        if len(global_locs) > 1:
            g_digests = {loc["digest"] for loc in global_locs}
            cross_harness_duplicates.append(
                {
                    "name": skill_name,
                    "locations": [loc["path"] for loc in global_locs],
                    "harnesses": [loc["harness"] for loc in global_locs],
                    "content_identical": len(g_digests) <= 1,
                }
            )

        if global_locs and project_locs:
            cross_tier_duplicates.append(
                {
                    "name": skill_name,
                    "global_locations": [loc["path"] for loc in global_locs],
                    "project_locations": [loc["path"] for loc in project_locs],
                    "content_identical": content_identical,
                }
            )

        q_issues = b["quality"]["issues"]
        if q_issues:
            bloat_and_quality.append(
                {
                    "name": skill_name,
                    "issues": q_issues,
                    "line_count": b["quality"]["instruction_check"]["line_count"],
                }
            )
        if b["scripts_detected"]:
            scripts_present.append(
                {"name": skill_name, "scripts": sorted(b["scripts_detected"])}
            )
        if not b["tracked"] or (vault_skills_dir is not None and not b["in_vault"]):
            untracked_skills.append(skill_name)

        tiers_sorted = sorted(b["tiers"])
        tier_label = "global+project" if len(tiers_sorted) > 1 else tiers_sorted[0]
        skills_out.append(
            {
                "name": skill_name,
                "tier": tier_label,
                "locations": [
                    {
                        "tier": loc["tier"],
                        "harness": loc["harness"],
                        "path": loc["path"],
                        "tracked": loc["tracked"],
                    }
                    for loc in locs
                ],
                "content_identical": content_identical,
                "in_vault": b["in_vault"],
                "tracked": b["tracked"],
                "quality": b["quality"],
                "scripts_detected": sorted(b["scripts_detected"]),
                "suggested_keywords": b["suggested_keywords"],
                "suggested_scene_tags": b["suggested_scene_tags"],
            }
        )

    keyword_overlaps: list[dict[str, Any]] = []
    names_sorted = sorted(grouped.keys())
    for idx, name_a in enumerate(names_sorted):
        for name_b in names_sorted[idx + 1 :]:
            overlap = compute_keyword_overlap(grouped[name_a]["_tokens"], grouped[name_b]["_tokens"])
            if overlap is not None:
                shared, jaccard = overlap
                keyword_overlaps.append(
                    {
                        "skills": [name_a, name_b],
                        "shared_keywords": shared,
                        "jaccard": jaccard,
                    }
                )

    cap_violations: list[dict[str, Any]] = []
    if len(global_names) > global_cap:
        cap_violations.append(
            {
                "count": len(global_names),
                "cap": global_cap,
                "excess": len(global_names) - global_cap,
                "skills": global_names,
            }
        )

    # Recommendations: keep <= global_cap clean global skills; archive bloated or excess skills.
    keep_and_adopt: list[dict[str, Any]] = []
    demote_to_vault: list[dict[str, Any]] = []
    review_warnings: list[dict[str, Any]] = []

    clean_global: list[str] = []
    for name in global_names:
        b = grouped[name]
        issues = b["quality"]["issues"]
        reasons: list[str] = []
        g_locs = [loc for loc in b["locations"] if loc["tier"] == "global"]
        if len({loc["digest"] for loc in g_locs}) > 1:
            reasons.append("diverged_duplicate")
        if b["scripts_detected"]:
            reasons.append("executable_scripts_present")
        reasons.extend(issues)
        if reasons:
            review_warnings.append(
                {
                    "name": name,
                    "reasons": list(dict.fromkeys(reasons)),
                    "locations": [loc["path"] for loc in g_locs],
                }
            )

        if "instruction_bloat" in issues:
            demote_to_vault.append(
                {
                    "name": name,
                    "reason": "instruction_bloat",
                    "suggested_keywords": b["suggested_keywords"],
                    "suggested_scene_tags": b["suggested_scene_tags"],
                    "locations": [loc["path"] for loc in g_locs],
                }
            )
        else:
            clean_global.append(name)

    # Prefer tracked / warning-free skills first inside the Global Core cap.
    clean_global.sort(
        key=lambda n: (
            0 if grouped[n]["tracked"] else 1,
            1 if grouped[n]["quality"]["issues"] else 0,
            n,
        )
    )
    for name in clean_global[:global_cap]:
        b = grouped[name]
        keep_and_adopt.append(
            {
                "name": name,
                "suggested_keywords": b["suggested_keywords"],
                "suggested_scene_tags": b["suggested_scene_tags"],
                "locations": [loc["path"] for loc in b["locations"] if loc["tier"] == "global"],
            }
        )
    for name in clean_global[global_cap:]:
        b = grouped[name]
        demote_to_vault.append(
            {
                "name": name,
                "reason": "over_global_cap",
                "suggested_keywords": b["suggested_keywords"],
                "suggested_scene_tags": b["suggested_scene_tags"],
                "locations": [loc["path"] for loc in b["locations"] if loc["tier"] == "global"],
            }
        )

    return {
        "ok": True,
        "action": "scan",
        "config_loaded": config_loaded,
        "global_cap": global_cap,
        "discovered_dirs": discovered_dirs,
        "skills": skills_out,
        "diagnosis": {
            "cap_violations": cap_violations,
            "cross_harness_duplicates": cross_harness_duplicates,
            "cross_tier_duplicates": cross_tier_duplicates,
            "keyword_overlaps": keyword_overlaps,
            "bloat_and_quality": bloat_and_quality,
            "scripts_present": scripts_present,
            "untracked_skills": untracked_skills,
        },
        "recommendations": {
            "keep_and_adopt": keep_and_adopt,
            "demote_to_vault": demote_to_vault,
            "review_warnings": review_warnings,
        },
    }


def _find_active_skill_paths(
    *,
    skill_name: str,
    tier: str,
    cfg: dict[str, Any],
    home_dir: Path | None,
    scan_dirs: list[str] | None,
    project_dir: Path | None,
) -> list[Path]:
    """Locate active mount directories matching skill_name in the requested tier."""
    if tier == "project":
        proj = project_dir if project_dir is not None else Path.cwd().resolve()
        parents = [proj / ".agents" / "skills", proj / ".claude" / "skills"]
    else:
        dir_specs = discover_harness_directories(
            home_dir=home_dir,
            configured_dirs=cfg.get("global_core_dirs"),
            scan_dirs=scan_dirs,
        )
        parents = [Path(d["path"]) for d in dir_specs if d["tier"] == "global"]

    found: list[Path] = []
    for parent in parents:
        cand = parent / skill_name
        if cand.exists() or cand.is_symlink():
            if is_plugin_bundle_dir(cand):
                raise CuratorError(f"Refusing to adopt/modify external plugin bundle '{cand.name}'.")
            found.append(cand)
    return found


def adopt_skill(
    *,
    cfg: dict[str, Any],
    raw_name: str | None = None,
    source_dir: Path | None = None,
    tier: str = "global",
    project_dir: Path | None = None,
    home_dir: Path | None = None,
    scan_dirs: list[str] | None = None,
    keywords: list[str] | None = None,
    scene_tags: list[str] | None = None,
    source_url: str = "",
    override_description: str | None = None,
) -> dict[str, Any]:
    """Back up kept local skill(s) into Skill Vault and stamp provenance on active copies."""
    if source_dir is not None:
        resolved_src = Path(os.path.expanduser(str(source_dir))).resolve()
        if resolved_src.name == "skill-curator" or is_plugin_bundle_dir(resolved_src):
            raise CuratorError(f"Refusing to adopt protected skill or bundle '{resolved_src.name}'.")
        validate_tree_containment(resolved_src)
        fm = parse_frontmatter(resolved_src / "SKILL.md")
        skill_name = validate_skill_name(raw_name or fm.get("name") or resolved_src.name)
        active_paths = [resolved_src]
    elif raw_name:
        if raw_name.strip() == "skill-curator":
            raise CuratorError("Refusing to adopt 'skill-curator' itself.")
        skill_name = validate_skill_name(raw_name)
        active_paths = _find_active_skill_paths(
            skill_name=skill_name,
            tier=tier,
            cfg=cfg,
            home_dir=home_dir,
            scan_dirs=scan_dirs,
            project_dir=project_dir,
        )
        if not active_paths:
            raise CuratorError(f"Skill '{skill_name}' was not found in active {tier} directories.")
        resolved_src = next((p for p in active_paths if (p / "SKILL.md").is_file()), active_paths[0])
        validate_tree_containment(resolved_src)
        fm = parse_frontmatter(resolved_src / "SKILL.md")
    else:
        raise CuratorError("Specify a skill <name> or --source-dir for adopt.")

    description = fm.get("description", "")
    sug_kw, sug_tags = suggest_keywords_and_tags(skill_name, description)
    final_keywords = keywords if keywords else sug_kw
    final_tags = scene_tags if scene_tags else sug_tags

    vault_dir = Path(cfg["vault_path"])
    vault_skill_dir, vault_meta = _store_to_vault_preserving_source(
        vault_dir=vault_dir,
        source_dir=resolved_src,
        skill_name=skill_name,
        description=description,
        keywords=final_keywords,
        scene_tags=final_tags,
        source_url=source_url,
        override_description=override_description,
    )

    mounted_at = utc_now_iso()
    stamped_paths: list[str] = []
    for active in active_paths:
        existing_active_meta = _read_json_dict(active / ".curator-meta.json")
        active_meta = {
            **vault_meta,
            "stored_at": str(existing_active_meta.get("stored_at") or vault_meta["stored_at"]),
            "mounted_at": mounted_at,
            "mounted_from_vault": str(vault_skill_dir),
            "target": tier,
        }
        (active / ".curator-meta.json").write_text(
            json.dumps(active_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        stamped_paths.append(str(active))

    return {
        "ok": True,
        "action": "adopt",
        "name": skill_name,
        "tier": tier,
        "vault_skill_dir": str(vault_skill_dir),
        "stamped_paths": stamped_paths,
        "keywords": vault_meta["keywords"],
        "scene_tags": vault_meta["scene_tags"],
    }


def _normalize_plan_items(raw_items: Any) -> list[dict[str, Any]]:
    """Normalize string or dict items from remediate --plan JSON."""
    if not raw_items:
        return []
    if not isinstance(raw_items, list):
        raise CuratorError("Plan entries for 'adopt'/'demote' must be lists.")
    normalized: list[dict[str, Any]] = []
    for entry in raw_items:
        if isinstance(entry, str):
            normalized.append({"name": entry})
        elif isinstance(entry, dict) and (entry.get("name") or entry.get("primary_path")):
            item = dict(entry)
            if not item.get("keywords") and item.get("suggested_keywords"):
                item["keywords"] = item["suggested_keywords"]
            if not item.get("scene_tags") and item.get("suggested_scene_tags"):
                item["scene_tags"] = item["suggested_scene_tags"]
            normalized.append(item)
        else:
            raise CuratorError(f"Invalid remediate plan entry: {entry!r}")
    return normalized


def parse_remediate_plan_payload(
    plan_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract (adopt_items, demote_items) from explicit, actions, or scan plan schemas."""
    adopt_items = _normalize_plan_items(plan_data.get("adopt"))
    demote_items = _normalize_plan_items(plan_data.get("demote"))

    actions_raw = plan_data.get("actions")
    if actions_raw:
        if not isinstance(actions_raw, list):
            raise CuratorError("Plan 'actions' must be a list.")
        for raw_entry in actions_raw:
            if not isinstance(raw_entry, dict):
                raise CuratorError(f"Invalid actions plan entry: {raw_entry!r}")
            act = str(raw_entry.get("action") or "").strip().lower()
            if act == "adopt":
                adopt_items.extend(_normalize_plan_items([raw_entry]))
            elif act == "demote":
                demote_items.extend(_normalize_plan_items([raw_entry]))
            else:
                raise CuratorError(
                    f"Unsupported action '{act}' in plan 'actions' (expected 'adopt' or 'demote')."
                )

    rec_source = plan_data.get("recommendations") if isinstance(plan_data.get("recommendations"), dict) else plan_data
    adopt_items.extend(_normalize_plan_items(rec_source.get("keep_and_adopt")))
    demote_items.extend(_normalize_plan_items(rec_source.get("demote_to_vault")))
    return adopt_items, demote_items


def remediate_plan(
    *,
    cfg: dict[str, Any],
    adopt_items: list[dict[str, Any]],
    demote_items: list[dict[str, Any]],
    home_dir: Path | None = None,
    scan_dirs: list[str] | None = None,
    project_dir: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Validate and execute user-confirmed adopt/demote remediation plan."""
    if not adopt_items and not demote_items:
        raise CuratorError("Specify --adopt, --demote, or --plan entries for remediate.")

    discovered_global = [
        d["path"]
        for d in discover_harness_directories(
            home_dir=home_dir,
            configured_dirs=cfg.get("global_core_dirs"),
            scan_dirs=scan_dirs,
        )
        if d["tier"] == "global"
    ]

    # Pre-validate all items before performing any destructive operations.
    planned_adopts: list[dict[str, Any]] = []
    for item in adopt_items:
        name = str(item.get("name") or "").strip()
        if name == "skill-curator":
            raise CuratorError("Refusing to remediate 'skill-curator' itself.")
        tier = str(item.get("tier") or "global")
        primary = Path(os.path.expanduser(str(item["primary_path"]))).resolve() if item.get("primary_path") else None
        if primary is None:
            found = _find_active_skill_paths(
                skill_name=validate_skill_name(name),
                tier=tier,
                cfg=cfg,
                home_dir=home_dir,
                scan_dirs=scan_dirs,
                project_dir=project_dir,
            )
            if not found:
                raise CuratorError(f"Adopt target '{name}' was not found in {tier} directories.")
            src_for_meta = next((p for p in found if (p / "SKILL.md").is_file()), found[0])
        else:
            src_for_meta = primary
            name = name or src_for_meta.name
        fm = parse_frontmatter(src_for_meta / "SKILL.md")
        sug_kw, sug_tags = suggest_keywords_and_tags(validate_skill_name(name), fm.get("description", ""))
        planned_adopts.append(
            {
                "name": validate_skill_name(name),
                "tier": tier,
                "source_dir": primary,
                "keywords": item.get("keywords") or sug_kw,
                "scene_tags": item.get("scene_tags") or sug_tags,
                "override_description": item.get("override_description"),
                "path": str(src_for_meta),
            }
        )

    planned_demotes: list[dict[str, Any]] = []
    for item in demote_items:
        name = str(item.get("name") or "").strip()
        if name == "skill-curator":
            raise CuratorError("Refusing to demote 'skill-curator' itself.")
        skill_name = validate_skill_name(name)
        tier = str(item.get("tier") or "global")
        found = _find_active_skill_paths(
            skill_name=skill_name,
            tier=tier,
            cfg=cfg,
            home_dir=home_dir,
            scan_dirs=scan_dirs,
            project_dir=project_dir,
        )
        if not found:
            raise CuratorError(f"Demote target '{skill_name}' was not found in {tier} directories.")
        entry_force = bool(force or item.get("force"))
        ensure_no_diverged_duplicates(found, skill_name, entry_force)
        primary = next((p for p in found if (p / "SKILL.md").is_file()), found[0])
        fm = parse_frontmatter(primary / "SKILL.md")
        sug_kw, sug_tags = suggest_keywords_and_tags(skill_name, fm.get("description", ""))
        planned_demotes.append(
            {
                "name": skill_name,
                "tier": tier,
                "keywords": item.get("keywords") or sug_kw,
                "scene_tags": item.get("scene_tags") or sug_tags,
                "paths": [str(p) for p in found],
                "force": entry_force,
            }
        )

    if dry_run:
        return {
            "ok": True,
            "action": "remediate",
            "dry_run": True,
            "adopt": planned_adopts,
            "demote": planned_demotes,
        }

    adopted_results = [
        adopt_skill(
            cfg=cfg,
            raw_name=entry["name"],
            source_dir=entry["source_dir"],
            tier=entry["tier"],
            project_dir=project_dir,
            home_dir=home_dir,
            scan_dirs=scan_dirs,
            keywords=entry["keywords"],
            scene_tags=entry["scene_tags"],
            override_description=entry["override_description"],
        )
        for entry in planned_adopts
    ]

    demoted_results = [
        demote_skill(
            cfg=cfg,
            raw_name=entry["name"],
            source_tier=entry["tier"],
            project_dir=project_dir,
            keywords=entry["keywords"],
            scene_tags=entry["scene_tags"],
            extra_global_dirs=discovered_global,
            home_dir=home_dir,
            discover_global_dirs=True,
            force=bool(entry["force"]),
        )
        for entry in planned_demotes
    ]

    return {
        "ok": True,
        "action": "remediate",
        "dry_run": False,
        "adopted": adopted_results,
        "demoted": demoted_results,
    }
