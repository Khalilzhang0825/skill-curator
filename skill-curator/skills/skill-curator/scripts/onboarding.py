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
    discover_skills_in_dir,
    extract_frontmatter_repo_source,
    extract_markdown_body,
    find_enclosing_symlink_container,
    has_symlink_component,
    inspect_ancestor_repo_provenance,
    is_plugin_bundle_dir,
    normalize_repo_source,
    normalize_slug,
    parse_frontmatter,
    read_index_rows,
    read_skills_lock_sources,
    resolve_home_dir,
    rewrite_frontmatter_description,
    rmtree_safe,
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
        discovered, excluded = discover_skills_in_dir(parent)
        results.append(
            {
                "harness": item["harness"],
                "path": item["path"],
                "skill_count": len(discovered),
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


GENERIC_SKILL_REPO_NAMES = frozenset(
    {
        "skills",
        "agent-skills",
        "claude-skills",
        "ai-skills",
        "codex-skills",
        "cursor-skills",
        "prompts",
    }
)
_CROSSREF_CONTEXT_RE = re.compile(r"\b(?:skill|skills|invoke|run|use)\b", re.IGNORECASE)


def suite_scene_tag_for_repo(source_repo: str) -> str:
    """Derive deterministic Suite Scene Tag from a normalized source_repo identifier."""
    if source_repo.startswith("inferred-suite:"):
        anchor = normalize_slug(source_repo.split(":", 1)[1]) or "companion"
        return f"{anchor}-suite"
    if source_repo.startswith("container:"):
        return normalize_slug(source_repo.split(":", 1)[1]) or "companion-suite"
    if source_repo.startswith("local-git:"):
        parts = source_repo.split(":")
        return normalize_slug(parts[1]) if len(parts) > 1 else "local-suite"
    if source_repo.startswith("file://"):
        tail = normalize_slug(Path(source_repo[7:]).name) or "local"
        return tail if tail.endswith("-suite") else f"{tail}-suite"
    parts = [p for p in source_repo.split("/") if p]
    if len(parts) >= 3:
        owner, repo = parts[-2], parts[-1]
        if repo.lower() in GENERIC_SKILL_REPO_NAMES:
            return normalize_slug(f"{owner}-{repo}") or "companion-suite"
        return normalize_slug(repo) or "companion-suite"
    return normalize_slug(parts[-1]) if parts else "companion-suite"


def _body_mentions_skill(body: str, target_slug: str) -> bool:
    """Return True if markdown body intentionally references target_slug as a companion skill."""
    if not body or not target_slug:
        return False
    esc = re.escape(target_slug)
    slash_or_path = re.compile(
        rf"(?:(?<![A-Za-z0-9_:/.-])/|(?<![A-Za-z0-9_:/.-])(?:skills|\.\.)/){esc}(?![A-Za-z0-9_/-])",
        re.IGNORECASE,
    )
    if slash_or_path.search(body):
        return True
    backtick_re = re.compile(rf"`{esc}`", re.IGNORECASE)
    if "-" in target_slug:
        if backtick_re.search(body):
            return True
        if len(target_slug) >= 6:
            slug_re = re.compile(rf"(?<![A-Za-z0-9_/-]){esc}(?![A-Za-z0-9_/-])", re.IGNORECASE)
            if slug_re.search(body):
                return True
    else:
        for match in backtick_re.finditer(body):
            window = body[max(0, match.start() - 32) : min(len(body), match.end() + 32)]
            if _CROSSREF_CONTEXT_RE.search(window):
                return True
    return False


def _attribute_repos_for_scan(
    grouped: dict[str, dict[str, Any]],
    *,
    vault_skills_dir: Path | None,
    index_rows: dict[str, dict[str, Any]],
    lock_sources: dict[str, tuple[str, str]],
    home_dir: Path | None = None,
) -> None:
    """Resolve 8-signal repo attribution across discovered skills without merging conflicting repos."""
    for skill_name, bucket in grouped.items():
        source_repo: str | None = None
        source_url = ""
        signal: str | None = None

        anc_signals: dict[str, tuple[str, str]] = {}
        for entry_path, parent_path in bucket.get("_ancestor_candidates", []):
            found_anc = inspect_ancestor_repo_provenance(
                entry_path,
                skill_name=skill_name,
                harness_parent=parent_path,
                home_dir=home_dir,
            )
            for k, v in found_anc.items():
                anc_signals.setdefault(k, v)

        # Signal 1: .curator-meta.json on active copies
        for cand_url in bucket.get("_meta_urls", []):
            repo_key, canonical_url = normalize_repo_source(cand_url)
            if repo_key:
                source_repo, source_url, signal = repo_key, canonical_url, "curator-meta"
                break

        # Signal 2: Vault INDEX.md / Vault .curator-meta.json
        if not source_repo:
            vault_meta = (
                _read_json_dict(vault_skills_dir / skill_name / ".curator-meta.json")
                if vault_skills_dir is not None
                else {}
            )
            row = index_rows.get(skill_name, {})
            for cand_url in (row.get("source_url", ""), vault_meta.get("source_url", "")):
                if isinstance(cand_url, str) and cand_url.strip():
                    repo_key, canonical_url = normalize_repo_source(cand_url)
                    if repo_key:
                        source_repo, source_url, signal = repo_key, canonical_url, "vault-index"
                        break

        # Signal 3: npx skills lockfile (.agents/.skill-lock.json or ancestor lockfile)
        if not source_repo:
            if skill_name in lock_sources:
                repo_key, canonical_url = lock_sources[skill_name]
                source_repo, source_url, signal = repo_key, canonical_url, "skills-lock"
            elif "skills-lock" in anc_signals:
                repo_key, canonical_url = anc_signals["skills-lock"]
                source_repo, source_url, signal = repo_key, canonical_url, "skills-lock"

        # Signal 4: enclosing .git/config or worktree/submodule origin remote
        if not source_repo and "git-origin" in anc_signals:
            repo_key, canonical_url = anc_signals["git-origin"]
            source_repo, source_url, signal = repo_key, canonical_url, "git-origin"

        # Signal 5: SKILL.md YAML frontmatter
        if not source_repo and bucket.get("_primary_skill_md"):
            fm_url = extract_frontmatter_repo_source(Path(bucket["_primary_skill_md"]))
            if fm_url:
                repo_key, canonical_url = normalize_repo_source(fm_url)
                if repo_key:
                    source_repo, source_url, signal = repo_key, canonical_url, "frontmatter"

        # Signal 6: ancestor package.json repository
        if not source_repo and "package-json" in anc_signals:
            repo_key, canonical_url = anc_signals["package-json"]
            source_repo, source_url, signal = repo_key, canonical_url, "package-json"

        bucket["source_repo"] = source_repo
        bucket["source_url"] = source_url
        bucket["attribution_signal"] = signal
        bucket["_git_local"] = anc_signals.get("git-local")

    # Signal 7a: local .git repo without remote URL
    for bucket in grouped.values():
        if bucket["source_repo"] is None and bucket.get("_git_local"):
            repo_key, canonical_url = bucket["_git_local"]
            bucket["source_repo"] = repo_key
            bucket["source_url"] = canonical_url
            bucket["attribution_signal"] = "git-local"

    # Signal 7b: shared nested top_container (unzipped archive / bundle dir without .git)
    containers_map: dict[str, tuple[Path, list[str]]] = {}
    for skill_name, bucket in grouped.items():
        for container_path in bucket.get("_containers", []):
            if container_path is None:
                continue
            ckey = os.path.normcase(os.path.normpath(os.path.abspath(str(container_path))))
            entry = containers_map.setdefault(ckey, (container_path, []))
            if skill_name not in entry[1]:
                entry[1].append(skill_name)

    for container_path, c_members in containers_map.values():
        explicit_repos = {
            (grouped[m]["source_repo"], grouped[m]["source_url"])
            for m in c_members
            if grouped[m]["source_repo"] and grouped[m]["attribution_signal"] != "git-local"
        }
        if len(explicit_repos) == 1:
            inferred_repo, inferred_url = next(iter(explicit_repos))
            for m in c_members:
                if not grouped[m]["source_repo"] or grouped[m]["attribution_signal"] == "git-local":
                    grouped[m]["source_repo"] = inferred_repo
                    grouped[m]["source_url"] = inferred_url
                    grouped[m]["attribution_signal"] = "container-dir"
        elif len(explicit_repos) == 0:
            unattr = [m for m in c_members if not grouped[m]["source_repo"]]
            if len(unattr) >= 2:
                c_repo = f"container:{normalize_slug(container_path.name) or 'suite'}"
                for m in unattr:
                    grouped[m]["source_repo"] = c_repo
                    grouped[m]["source_url"] = ""
                    grouped[m]["attribution_signal"] = "container-dir"

    # Signal 8: SKILL.md cross-reference connected components (only for unattributed skills)
    names_sorted = sorted(grouped.keys())
    bodies: dict[str, str] = {}
    for name in names_sorted:
        p = grouped[name].get("_primary_skill_md")
        bodies[name] = extract_markdown_body(Path(p)) if p else ""

    adj: dict[str, set[str]] = {name: set() for name in names_sorted}
    for src_name in names_sorted:
        body = bodies[src_name]
        if not body:
            continue
        for dst_name in names_sorted:
            if src_name == dst_name:
                continue
            if _body_mentions_skill(body, dst_name):
                adj[src_name].add(dst_name)
                adj[dst_name].add(src_name)

    visited: set[str] = set()
    for start in names_sorted:
        if grouped[start]["source_repo"] is not None or start in visited:
            continue
        component: list[str] = []
        touching_attributed: dict[str, str] = {}
        stack = [start]
        visited.add(start)
        while stack:
            cur = stack.pop()
            component.append(cur)
            for nb in adj[cur]:
                nb_repo = grouped[nb]["source_repo"]
                if nb_repo is not None:
                    touching_attributed[nb_repo] = grouped[nb]["source_url"]
                elif nb not in visited:
                    visited.add(nb)
                    stack.append(nb)

        if len(touching_attributed) == 1:
            inferred_repo, inferred_url = next(iter(touching_attributed.items()))
            for member in component:
                grouped[member]["source_repo"] = inferred_repo
                grouped[member]["source_url"] = inferred_url
                grouped[member]["attribution_signal"] = "cross-reference"
        elif len(component) >= 2:
            anchor = sorted(component)[0]
            inferred_repo = f"inferred-suite:{anchor}"
            for member in component:
                grouped[member]["source_repo"] = inferred_repo
                grouped[member]["source_url"] = ""
                grouped[member]["attribution_signal"] = "cross-reference"


def _resolve_source_url_for_action(
    *,
    explicit_url: str = "",
    skill_name: str,
    skill_md_path: Path | None,
    lock_sources: dict[str, tuple[str, str]],
    skill_dir: Path | None = None,
    harness_parent: Path | None = None,
    home_dir: Path | None = None,
) -> str:
    """Resolve and normalize canonical source_url for adopt/demote/remediate."""
    if explicit_url and explicit_url.strip():
        _, canonical = normalize_repo_source(explicit_url.strip())
        return canonical or explicit_url.strip()
    if skill_name in lock_sources:
        return lock_sources[skill_name][1]
    target_dir = skill_dir or (skill_md_path.parent if skill_md_path is not None else None)
    anc_signals: dict[str, tuple[str, str]] = {}
    if target_dir is not None and target_dir.exists():
        anc_signals = inspect_ancestor_repo_provenance(
            target_dir,
            skill_name=skill_name,
            harness_parent=harness_parent,
            home_dir=home_dir,
        )
    if "skills-lock" in anc_signals:
        return anc_signals["skills-lock"][1]
    if "git-origin" in anc_signals:
        return anc_signals["git-origin"][1]
    if skill_md_path is not None and skill_md_path.is_file():
        fm_url = extract_frontmatter_repo_source(skill_md_path)
        if fm_url:
            return fm_url
    if "package-json" in anc_signals:
        return anc_signals["package-json"][1]
    return ""


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
    lock_sources = read_skills_lock_sources(home_dir=home_dir, project_dir=project_dir)

    discovered_dirs: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, Any]] = {}

    for dspec in dir_specs:
        parent = Path(dspec["path"])
        entries, excluded_bundles = discover_skills_in_dir(parent)
        skill_count = 0

        for ent in entries:
            entry = ent["path"]
            skill_name = ent["name"]
            top_container = ent["top_container"]
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
            in_vault = bool(
                vault_skills_dir and (vault_skills_dir / skill_name / "SKILL.md").is_file()
            )
            is_symlinked = has_symlink_component(entry, parent)
            tracked = bool(
                meta.get("mounted_at")
                or meta.get("mounted_from_vault")
                or (is_symlinked and in_vault)
            )

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
                    "_meta_urls": [],
                    "_primary_skill_md": str(entry / "SKILL.md"),
                    "_ancestor_candidates": [],
                    "_containers": [],
                }
                grouped[skill_name] = bucket

            bucket["_ancestor_candidates"].append((entry, parent))
            if top_container is not None:
                bucket["_containers"].append(top_container)
            raw_meta_url = meta.get("source_url")
            if isinstance(raw_meta_url, str) and raw_meta_url.strip():
                bucket["_meta_urls"].append(raw_meta_url.strip())
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

    _attribute_repos_for_scan(
        grouped,
        vault_skills_dir=vault_skills_dir,
        index_rows=index_rows,
        lock_sources=lock_sources,
        home_dir=home_dir,
    )
    repo_members: dict[str, list[str]] = {}
    for sname in sorted(grouped.keys()):
        repo = grouped[sname].get("source_repo")
        if repo:
            repo_members.setdefault(repo, []).append(sname)
    for repo, members in repo_members.items():
        if len(members) >= 2:
            suite_tag = suite_scene_tag_for_repo(repo)
            for member in members:
                existing_tags = grouped[member]["suggested_scene_tags"]
                updated_tags = [suite_tag] + [
                    t for t in existing_tags if t != suite_tag and t != "general"
                ]
                grouped[member]["suggested_scene_tags"] = updated_tags[:4]

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
                "source_repo": b["source_repo"],
                "source_url": b["source_url"],
                "source_repo_signal": b["attribution_signal"],
                "attribution_signal": b["attribution_signal"],
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
                    "source_repo": b["source_repo"],
                    "source_url": b["source_url"],
                }
            )
        else:
            clean_global.append(name)

    # Group clean global skills atomically by source_repo so companion suites stay together.
    grouped_clean_repos: dict[str, list[str]] = {}
    atomic_units: list[list[str]] = []
    for name in clean_global:
        repo = grouped[name].get("source_repo")
        if repo:
            grouped_clean_repos.setdefault(repo, []).append(name)
        else:
            atomic_units.append([name])
    for members in grouped_clean_repos.values():
        atomic_units.append(sorted(members))

    # Prefer tracked units first, then cohesive suites (>=2), then warning-free, then larger groups.
    atomic_units.sort(
        key=lambda members: (
            0 if any(grouped[n]["tracked"] for n in members) else 1,
            0 if len(members) >= 2 else 1,
            1 if any(grouped[n]["quality"]["issues"] for n in members) else 0,
            -len(members),
            sorted(members)[0],
        )
    )

    used_slots = 0
    keep_units: list[list[str]] = []
    demote_units: list[list[str]] = []
    has_kept_oversized_suite = False
    soft_cap_exceeded = False
    for members in atomic_units:
        ordered_members = sorted(members)
        any_tracked = any(grouped[n]["tracked"] for n in ordered_members)
        if (
            (not soft_cap_exceeded and used_slots + len(ordered_members) <= global_cap)
            or any_tracked
            or (len(ordered_members) > global_cap and not has_kept_oversized_suite)
        ):
            keep_units.append(ordered_members)
            used_slots += len(ordered_members)
            if len(ordered_members) > global_cap:
                has_kept_oversized_suite = True
            soft_cap_exceeded = used_slots > global_cap
        else:
            demote_units.append(ordered_members)

    for members in keep_units:
        for name in members:
            b = grouped[name]
            keep_and_adopt.append(
                {
                    "name": name,
                    "suggested_keywords": b["suggested_keywords"],
                    "suggested_scene_tags": b["suggested_scene_tags"],
                    "locations": [loc["path"] for loc in b["locations"] if loc["tier"] == "global"],
                    "source_repo": b["source_repo"],
                    "source_url": b["source_url"],
                }
            )
    for members in demote_units:
        demote_reason = "over_global_cap_repo_group" if len(members) > 1 else "over_global_cap"
        for name in members:
            b = grouped[name]
            demote_to_vault.append(
                {
                    "name": name,
                    "reason": demote_reason,
                    "suggested_keywords": b["suggested_keywords"],
                    "suggested_scene_tags": b["suggested_scene_tags"],
                    "locations": [loc["path"] for loc in b["locations"] if loc["tier"] == "global"],
                    "source_repo": b["source_repo"],
                    "source_url": b["source_url"],
                }
            )

    keep_name_set = {item["name"] for item in keep_and_adopt}
    demote_name_set = {item["name"] for item in demote_to_vault}
    repo_groups: list[dict[str, Any]] = []
    for repo in sorted(repo_members.keys()):
        members = repo_members[repo]
        if len(members) < 2:
            continue
        g_members = [
            n for n in members if any(loc["tier"] == "global" for loc in grouped[n]["locations"])
        ]
        p_members = [
            n for n in members if any(loc["tier"] == "project" for loc in grouped[n]["locations"])
        ]
        if g_members:
            if all(n in keep_name_set for n in g_members):
                disposition = "keep_and_adopt"
            elif all(n in demote_name_set for n in g_members):
                disposition = "demote_to_vault"
            else:
                disposition = "mixed"
        else:
            disposition = "project_only"
        bloated_members = [
            n for n in members if "instruction_bloat" in grouped[n]["quality"]["issues"]
        ]
        repo_groups.append(
            {
                "source_repo": repo,
                "source_url": grouped[members[0]]["source_url"],
                "signal": grouped[members[0]]["attribution_signal"],
                "suite_scene_tag": suite_scene_tag_for_repo(repo),
                "skills": members,
                "global_skills": g_members,
                "project_skills": p_members,
                "bloated_skills": bloated_members,
                "recommended_disposition": disposition,
            }
        )

    soft_cap_exceeded = len(keep_and_adopt) > global_cap
    recommended_global_cap = max(global_cap, len(keep_and_adopt))

    return {
        "ok": True,
        "action": "scan",
        "config_loaded": config_loaded,
        "global_cap": global_cap,
        "discovered_dirs": discovered_dirs,
        "skills": skills_out,
        "repo_groups": repo_groups,
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
            "repo_groups": repo_groups,
            "soft_cap_exceeded": soft_cap_exceeded,
            "recommended_global_cap": recommended_global_cap,
        },
    }


def _find_active_skill_entries(
    *,
    skill_name: str,
    tier: str,
    cfg: dict[str, Any],
    home_dir: Path | None,
    scan_dirs: list[str] | None,
    project_dir: Path | None,
) -> list[tuple[Path, Path]]:
    """Locate active (skill_path, harness_parent) entries matching skill_name."""
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

    found: list[tuple[Path, Path]] = []
    seen_paths: set[str] = set()
    for parent in parents:
        cand = parent / skill_name
        if (cand.exists() or os.path.islink(str(cand))) and is_plugin_bundle_dir(cand):
            raise CuratorError(f"Refusing to adopt/modify external plugin bundle '{cand.name}'.")
        entries, _ = discover_skills_in_dir(parent)
        for ent in entries:
            if ent["name"] != skill_name:
                continue
            epath = ent["path"]
            if is_plugin_bundle_dir(epath):
                raise CuratorError(f"Refusing to adopt/modify external plugin bundle '{epath.name}'.")
            ekey = os.path.normcase(os.path.normpath(os.path.abspath(str(epath))))
            if ekey not in seen_paths:
                seen_paths.add(ekey)
                found.append((epath, parent))
    return found


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
    return [
        p
        for p, _ in _find_active_skill_entries(
            skill_name=skill_name,
            tier=tier,
            cfg=cfg,
            home_dir=home_dir,
            scan_dirs=scan_dirs,
            project_dir=project_dir,
        )
    ]


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
        resolved_src = Path(os.path.expanduser(str(source_dir)))
        if resolved_src.name == "skill-curator" or is_plugin_bundle_dir(resolved_src):
            raise CuratorError(f"Refusing to adopt protected skill or bundle '{resolved_src.name}'.")
        validate_tree_containment(resolved_src)
        fm = parse_frontmatter(resolved_src / "SKILL.md")
        skill_name = validate_skill_name(raw_name or fm.get("name") or resolved_src.name)
        active_pairs: list[tuple[Path, Path | None]] = [(resolved_src, None)]
        primary_parent: Path | None = None
    elif raw_name:
        if raw_name.strip() == "skill-curator":
            raise CuratorError("Refusing to adopt 'skill-curator' itself.")
        skill_name = validate_skill_name(raw_name)
        found_pairs = _find_active_skill_entries(
            skill_name=skill_name,
            tier=tier,
            cfg=cfg,
            home_dir=home_dir,
            scan_dirs=scan_dirs,
            project_dir=project_dir,
        )
        if not found_pairs:
            raise CuratorError(f"Skill '{skill_name}' was not found in active {tier} directories.")
        resolved_src, primary_parent = next(
            ((p, par) for p, par in found_pairs if (p / "SKILL.md").is_file()),
            found_pairs[0],
        )
        validate_tree_containment(resolved_src)
        fm = parse_frontmatter(resolved_src / "SKILL.md")
        active_pairs = [(p, par) for p, par in found_pairs]
    else:
        raise CuratorError("Specify a skill <name> or --source-dir for adopt.")

    description = fm.get("description", "")
    sug_kw, sug_tags = suggest_keywords_and_tags(skill_name, description)
    final_keywords = keywords if keywords else sug_kw
    final_tags = scene_tags if scene_tags else sug_tags
    lock_sources = read_skills_lock_sources(home_dir=home_dir, project_dir=project_dir)
    resolved_source_url = _resolve_source_url_for_action(
        explicit_url=source_url,
        skill_name=skill_name,
        skill_md_path=resolved_src / "SKILL.md",
        lock_sources=lock_sources,
        skill_dir=resolved_src,
        harness_parent=primary_parent,
        home_dir=home_dir,
    )

    vault_dir = Path(cfg["vault_path"])
    vault_skill_dir, vault_meta = _store_to_vault_preserving_source(
        vault_dir=vault_dir,
        source_dir=resolved_src,
        skill_name=skill_name,
        description=description,
        keywords=final_keywords,
        scene_tags=final_tags,
        source_url=resolved_source_url,
        override_description=override_description,
    )

    mounted_at = utc_now_iso()
    stamped_paths: list[str] = []
    skipped_symlink_stamps: list[str] = []
    for active, parent in active_pairs:
        if has_symlink_component(active, parent):
            skipped_symlink_stamps.append(str(active))
            continue
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
        "skipped_symlink_stamps": skipped_symlink_stamps,
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
    lock_sources = read_skills_lock_sources(home_dir=home_dir, project_dir=project_dir)

    # Pre-validate all items before performing any destructive operations.
    planned_adopts: list[dict[str, Any]] = []
    for item in adopt_items:
        name = str(item.get("name") or "").strip()
        if name == "skill-curator":
            raise CuratorError("Refusing to remediate 'skill-curator' itself.")
        tier = str(item.get("tier") or "global")
        primary = Path(os.path.expanduser(str(item["primary_path"]))) if item.get("primary_path") else None
        primary_parent: Path | None = None
        if primary is None:
            found_pairs = _find_active_skill_entries(
                skill_name=validate_skill_name(name),
                tier=tier,
                cfg=cfg,
                home_dir=home_dir,
                scan_dirs=scan_dirs,
                project_dir=project_dir,
            )
            if not found_pairs:
                raise CuratorError(f"Adopt target '{name}' was not found in {tier} directories.")
            src_for_meta, primary_parent = next(
                ((p, par) for p, par in found_pairs if (p / "SKILL.md").is_file()),
                found_pairs[0],
            )
        else:
            src_for_meta = primary
            name = name or src_for_meta.name
        skill_slug = validate_skill_name(name)
        fm = parse_frontmatter(src_for_meta / "SKILL.md")
        sug_kw, sug_tags = suggest_keywords_and_tags(skill_slug, fm.get("description", ""))
        resolved_url = _resolve_source_url_for_action(
            explicit_url=str(item.get("source_url") or ""),
            skill_name=skill_slug,
            skill_md_path=src_for_meta / "SKILL.md",
            lock_sources=lock_sources,
            skill_dir=src_for_meta,
            harness_parent=primary_parent,
            home_dir=home_dir,
        )
        planned_adopts.append(
            {
                "name": skill_slug,
                "tier": tier,
                "source_dir": primary,
                "keywords": item.get("keywords") or sug_kw,
                "scene_tags": item.get("scene_tags") or sug_tags,
                "source_url": resolved_url,
                "override_description": item.get("override_description"),
                "path": str(src_for_meta),
            }
        )

    planned_demotes: list[dict[str, Any]] = []
    demote_pairs_by_skill: list[tuple[str, list[tuple[Path, Path]]]] = []
    for item in demote_items:
        name = str(item.get("name") or "").strip()
        if name == "skill-curator":
            raise CuratorError("Refusing to demote 'skill-curator' itself.")
        skill_name = validate_skill_name(name)
        tier = str(item.get("tier") or "global")
        found_pairs = _find_active_skill_entries(
            skill_name=skill_name,
            tier=tier,
            cfg=cfg,
            home_dir=home_dir,
            scan_dirs=scan_dirs,
            project_dir=project_dir,
        )
        if not found_pairs:
            raise CuratorError(f"Demote target '{skill_name}' was not found in {tier} directories.")
        found = [p for p, _ in found_pairs]
        entry_force = bool(force or item.get("force"))
        ensure_no_diverged_duplicates(found, skill_name, entry_force)
        primary, demote_parent = next(
            ((p, par) for p, par in found_pairs if (p / "SKILL.md").is_file()),
            found_pairs[0],
        )
        fm = parse_frontmatter(primary / "SKILL.md")
        sug_kw, sug_tags = suggest_keywords_and_tags(skill_name, fm.get("description", ""))
        resolved_url = _resolve_source_url_for_action(
            explicit_url=str(item.get("source_url") or ""),
            skill_name=skill_name,
            skill_md_path=primary / "SKILL.md",
            lock_sources=lock_sources,
            skill_dir=primary,
            harness_parent=demote_parent,
            home_dir=home_dir,
        )
        demote_pairs_by_skill.append((skill_name, found_pairs))
        planned_demotes.append(
            {
                "name": skill_name,
                "tier": tier,
                "keywords": item.get("keywords") or sug_kw,
                "scene_tags": item.get("scene_tags") or sug_tags,
                "source_url": resolved_url,
                "paths": [str(p) for p in found],
                "force": entry_force,
            }
        )

    batch_demote_names = {entry["name"] for entry in planned_demotes}
    for skill_name, found_pairs in demote_pairs_by_skill:
        for p, par in found_pairs:
            container = find_enclosing_symlink_container(p, par)
            if container is not None:
                siblings, _ = discover_skills_in_dir(container, max_depth=3)
                remaining = sorted({s["name"] for s in siblings if s["name"] not in batch_demote_names})
                if remaining:
                    raise CuratorError(
                        f"Skill '{skill_name}' resides inside whole-repo symlink '{container}' "
                        f"alongside active siblings {remaining}. Demote the full suite together or "
                        "replace the repo symlink with per-skill mounts.",
                        payload={
                            "error_code": "SYMLINKED_REPO_CONTAINER",
                            "name": skill_name,
                            "container": str(container),
                            "remaining_skills": remaining,
                        },
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
            source_url=entry["source_url"],
            override_description=entry["override_description"],
        )
        for entry in planned_adopts
    ]

    batch_demote_names = {entry["name"] for entry in planned_demotes}
    deferred_containers: set[Path] = set()
    demoted_results = [
        demote_skill(
            cfg=cfg,
            raw_name=entry["name"],
            source_tier=entry["tier"],
            project_dir=project_dir,
            keywords=entry["keywords"],
            scene_tags=entry["scene_tags"],
            source_url=entry["source_url"],
            extra_global_dirs=discovered_global,
            home_dir=home_dir,
            discover_global_dirs=True,
            force=bool(entry["force"]),
            batch_demote_names=batch_demote_names,
            deferred_symlink_containers=deferred_containers,
        )
        for entry in planned_demotes
    ]
    for container in sorted(deferred_containers):
        rmtree_safe(container)

    return {
        "ok": True,
        "action": "remediate",
        "dry_run": False,
        "adopted": adopted_results,
        "demoted": demoted_results,
    }
