"""Pre-install quality/safety audit, cross-tier overlap scan, and Global Core cap checks."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from common import (
    SKILL_NAME_RE,
    normalize_slug,
    parse_frontmatter,
    read_index_rows,
    validate_tree_containment,
)

SCRIPT_EXTENSIONS = {
    ".sh",
    ".bash",
    ".zsh",
    ".py",
    ".js",
    ".ts",
    ".rb",
    ".pl",
    ".ps1",
    ".bat",
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "use",
    "using",
    "when",
    "with",
    "you",
    "your",
}

CAP_RESOLUTION_OPTIONS = [
    "demote_existing_global_to_vault",
    "mount_to_project",
    "store_in_vault_only",
]


def is_plugin_bundle_dir(entry: Path) -> bool:
    """Return True if entry looks like an external plugin bundle rather than a standalone skill."""
    return (
        (entry / "plugin.json").exists()
        or (entry / ".claude-plugin" / "plugin.json").exists()
        or (entry / "gemini-extension.json").exists()
    )


def scan_global_core(global_core_dirs: list[str], global_cap: int) -> dict[str, Any]:
    """Count deduplicated standalone skills across managed Global Core directories."""
    paths_by_name: dict[str, Path] = {}
    for raw_dir in global_core_dirs:
        gdir = Path(os.path.expanduser(raw_dir)).resolve()
        if not gdir.is_dir():
            continue
        for entry in sorted(gdir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            if entry.name == "skill-curator" or is_plugin_bundle_dir(entry):
                continue
            if not (entry / "SKILL.md").is_file():
                continue
            paths_by_name.setdefault(entry.name, entry)

    skills = sorted(paths_by_name.keys())
    count = len(skills)
    return {
        "count": count,
        "cap": global_cap,
        "at_cap": count >= global_cap,
        "skills": skills,
        "paths": paths_by_name,
    }


def _is_script_file(path: Path) -> bool:
    """Return True if path has a script extension, executable bit, or #! shebang."""
    if path.suffix.lower() in SCRIPT_EXTENSIONS:
        return True
    try:
        mode = os.stat(str(path), follow_symlinks=False).st_mode
        if mode & 0o111:
            return True
        with path.open("rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def detect_scripts(root_dir: Path) -> list[str]:
    """Return sorted relative POSIX paths of script files inside root_dir."""
    detected: list[str] = []
    for dirpath, dirnames, filenames in os.walk(str(root_dir), followlinks=False):
        dirnames.sort()
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if _is_script_file(fpath):
                detected.append(fpath.relative_to(root_dir).as_posix())
    detected.sort()
    return detected


def extract_tokens(text: str) -> set[str]:
    """Extract meaningful lowercase alphanumeric tokens and CJK runs/bigrams excluding stopwords."""
    tokens: set[str] = set()
    for tok in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text.lower()):
        if tok[0] >= "\u4e00":
            tokens.add(tok)
            if len(tok) >= 2:
                for idx in range(len(tok) - 1):
                    tokens.add(tok[idx : idx + 2])
        elif len(tok) >= 2 and tok not in STOPWORDS:
            tokens.add(tok)
    return tokens


def compute_keyword_overlap(
    tokens_a: set[str], tokens_b: set[str]
) -> tuple[list[str], float] | None:
    """Return (shared_keywords, jaccard) if two token sets meet the overlap threshold."""
    if not tokens_a or not tokens_b:
        return None
    shared = sorted(tokens_a & tokens_b)
    union = tokens_a | tokens_b
    jaccard = len(shared) / len(union) if union else 0.0
    if len(shared) >= 3 or jaccard >= 0.35:
        return shared, round(jaccard, 3)
    return None


def assess_skill_quality(skill_dir: Path) -> dict[str, Any]:
    """Evaluate SKILL.md naming, description, and instruction length checks."""
    skill_md_path = skill_dir / "SKILL.md"
    frontmatter = parse_frontmatter(skill_md_path)
    skill_md_text = skill_md_path.read_text(encoding="utf-8-sig")

    fm_name = frontmatter.get("name", "").strip()
    name_issues: list[str] = []
    if not fm_name:
        name_issues.append("missing_frontmatter_name")
        name_valid = False
    elif not SKILL_NAME_RE.match(fm_name):
        name_issues.append("invalid_skill_name")
        name_valid = False
    else:
        name_valid = True

    candidate_name = (
        fm_name
        if name_valid
        else (normalize_slug(fm_name or skill_dir.name) or skill_dir.name)
    )

    desc = frontmatter.get("description", "")
    desc_issues: list[str] = []
    if len(desc.strip()) < 15:
        desc_issues.append("missing_or_vague_description")
    if len(desc) > 1024:
        desc_issues.append("description_bloat")

    line_count = len(skill_md_text.splitlines())
    instr_issues: list[str] = []
    if line_count > 500:
        instr_issues.append("instruction_bloat")

    return {
        "frontmatter_name": fm_name,
        "candidate_name": candidate_name,
        "description": desc,
        "name_check": {
            "frontmatter_name": fm_name,
            "candidate_name": candidate_name,
            "valid": name_valid,
            "issues": name_issues,
        },
        "description_check": {
            "char_count": len(desc),
            "issues": desc_issues,
        },
        "instruction_check": {
            "line_count": line_count,
            "max_lines": 500,
            "issues": instr_issues,
        },
        "issues": [*name_issues, *desc_issues, *instr_issues],
    }


def _read_skill_summary(skill_dir: Path) -> tuple[str, list[str]]:
    """Read description and keywords for an existing skill directory."""
    description = ""
    keywords: list[str] = []
    meta_file = skill_dir / ".curator-meta.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                description = str(meta.get("description") or "")
                raw_kw = meta.get("keywords")
                if isinstance(raw_kw, list):
                    keywords = [str(k) for k in raw_kw if str(k).strip()]
        except (OSError, json.JSONDecodeError):
            pass

    if not description and (skill_dir / "SKILL.md").is_file():
        try:
            fm = parse_frontmatter(skill_dir / "SKILL.md")
            description = fm.get("description", "")
        except Exception:
            pass
    return description, keywords


def _collect_dir_skills(base_dir: Path, tier: str, seen: dict[str, dict[str, Any]]) -> None:
    """Collect standalone skills from a skill container directory."""
    if not base_dir.is_dir():
        return
    for entry in sorted(base_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if entry.name == "skill-curator" or is_plugin_bundle_dir(entry):
            continue
        if not (entry / "SKILL.md").is_file():
            continue
        if entry.name in seen:
            continue
        desc, kws = _read_skill_summary(entry)
        seen[entry.name] = {
            "tier": tier,
            "name": entry.name,
            "path": str(entry),
            "description": desc,
            "keywords": kws,
        }


def _load_index_keywords(index_path: Path) -> dict[str, list[str]]:
    """Parse skill keywords from Vault INDEX.md if present."""
    return {name: row["keywords"] for name, row in read_index_rows(index_path).items()}


def _collect_existing_skills(
    cfg: dict[str, Any],
    global_scan: dict[str, Any],
    project_dir: Path | None,
) -> list[dict[str, Any]]:
    """Gather existing skills across Global Core, Project Workspace, and Skill Vault."""
    records: list[dict[str, Any]] = []

    for name, path in global_scan["paths"].items():
        desc, kws = _read_skill_summary(path)
        records.append(
            {
                "tier": "global",
                "name": name,
                "path": str(path),
                "description": desc,
                "keywords": kws,
            }
        )

    if project_dir is not None:
        proj_seen: dict[str, dict[str, Any]] = {}
        _collect_dir_skills(project_dir / ".agents" / "skills", "project", proj_seen)
        _collect_dir_skills(project_dir / ".claude" / "skills", "project", proj_seen)
        records.extend(proj_seen.values())

    vault_dir = Path(cfg["vault_path"])
    vault_seen: dict[str, dict[str, Any]] = {}
    _collect_dir_skills(vault_dir / "skills", "vault", vault_seen)
    index_kws = _load_index_keywords(vault_dir / "INDEX.md")
    for name, rec in vault_seen.items():
        if name in index_kws and index_kws[name]:
            merged = list(dict.fromkeys([*rec["keywords"], *index_kws[name]]))
            rec["keywords"] = merged
    records.extend(vault_seen.values())

    return records


def inspect_skill(
    source_dir: Path,
    cfg: dict[str, Any],
    project_dir: Path | None = None,
    desired_tier: str = "auto",
) -> dict[str, Any]:
    """Audit a candidate skill without modifying source_dir and recommend placement."""
    validate_tree_containment(source_dir)
    quality = assess_skill_quality(source_dir)
    fm_name = quality["frontmatter_name"]
    candidate_name = quality["candidate_name"]
    desc = quality["description"]
    name_check = quality["name_check"]
    desc_check = quality["description_check"]
    instr_check = quality["instruction_check"]

    scripts_detected = detect_scripts(source_dir)

    global_scan = scan_global_core(cfg["global_core_dirs"], cfg["global_cap"])
    existing = _collect_existing_skills(cfg, global_scan, project_dir)

    cand_names = {candidate_name}
    if fm_name:
        cand_names.add(fm_name)
    cand_tokens = extract_tokens(f"{candidate_name} {fm_name} {desc}")

    exact_collisions: list[dict[str, str]] = []
    keyword_overlaps: list[dict[str, Any]] = []

    for rec in existing:
        if rec["name"] in cand_names:
            exact_collisions.append(
                {"tier": rec["tier"], "name": rec["name"], "path": rec["path"]}
            )
            continue
        exist_tokens = extract_tokens(
            f"{rec['name']} {rec['description']} {' '.join(rec['keywords'])}"
        )
        overlap = compute_keyword_overlap(cand_tokens, exist_tokens)
        if overlap is not None:
            shared, jaccard = overlap
            keyword_overlaps.append(
                {
                    "tier": rec["tier"],
                    "name": rec["name"],
                    "shared_keywords": shared,
                    "jaccard": jaccard,
                }
            )

    project_collision = any(c["tier"] == "project" for c in exact_collisions)
    global_cap_blocked = False
    cap_resolution_options: list[str] = []
    demote_candidates: list[str] = []

    if desired_tier == "auto":
        recommended_tier = (
            "project" if (project_dir is not None and not project_collision) else "vault"
        )
    elif desired_tier == "global":
        if global_scan["at_cap"] and candidate_name not in global_scan["skills"]:
            global_cap_blocked = True
            recommended_tier = (
                "project" if (project_dir is not None and not project_collision) else "vault"
            )
            cap_resolution_options = list(CAP_RESOLUTION_OPTIONS)
            demote_candidates = list(global_scan["skills"])
        else:
            recommended_tier = "global"
    else:
        recommended_tier = desired_tier

    warnings: list[str] = []
    warnings.extend(quality["issues"])
    if scripts_detected:
        warnings.append("executable_scripts_present")
    if exact_collisions:
        warnings.append("exact_collision")
    if keyword_overlaps:
        warnings.append("keyword_overlap")
    if global_cap_blocked:
        warnings.append("global_cap_reached")

    return {
        "ok": True,
        "action": "inspect",
        "source_dir": str(source_dir),
        "candidate_name": candidate_name,
        "name_check": name_check,
        "description_check": desc_check,
        "instruction_check": instr_check,
        "scripts_detected": scripts_detected,
        "exact_collisions": exact_collisions,
        "keyword_overlaps": keyword_overlaps,
        "global_core": {
            "count": global_scan["count"],
            "cap": global_scan["cap"],
            "at_cap": global_scan["at_cap"],
            "skills": global_scan["skills"],
        },
        "desired_tier": desired_tier,
        "recommended_tier": recommended_tier,
        "global_cap_blocked": global_cap_blocked,
        "cap_resolution_options": cap_resolution_options,
        "demote_candidates": demote_candidates,
        "warnings": warnings,
    }
