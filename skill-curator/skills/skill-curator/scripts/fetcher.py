"""Fetch skills from GitHub repositories, tree subpaths, single-file URLs, and local Git repos."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from common import (
    CuratorError,
    copy_tree_clean,
    is_path_within,
    normalize_slug,
    parse_frontmatter,
    validate_skill_name,
)

GITHUB_BLOB_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$"
)
GITHUB_TREE_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.*))?$"
)
GITHUB_REPO_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
DEFAULT_RAW_BASE_URL = "https://raw.githubusercontent.com"
DEFAULT_GITHUB_BASE_URL = "https://github.com"


def _repo_slug(name: str) -> str:
    """Derive a clean slug from a repository name or path segment."""
    cleaned = name.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned


def parse_source_url(
    raw_source: str,
    ref_override: str | None = None,
    subpath_override: str | None = None,
    raw_base_url: str | None = None,
) -> dict[str, Any]:
    """Classify and normalize a GitHub URL, raw SKILL.md URL, or Git repository path."""
    source = raw_source.strip()
    if not source:
        raise CuratorError("Fetch source URL or path must not be empty.")

    # Strip query/fragment when matching GitHub web URL patterns.
    parsed = urlparse(source)
    clean_web = source.split("#", 1)[0].split("?", 1)[0]

    blob_match = GITHUB_BLOB_RE.match(clean_web)
    if blob_match:
        owner, repo, ref, file_path = blob_match.groups()
        posix_file = PurePosixPath(file_path)
        if posix_file.name.lower() != "skill.md":
            raise CuratorError(
                f"GitHub /blob/ URL must point to a SKILL.md file: '{source}'."
            )
        raw_base = (
            raw_base_url
            or os.environ.get("SKILL_CURATOR_RAW_BASE_URL")
            or DEFAULT_RAW_BASE_URL
        ).rstrip("/")
        raw_url = f"{raw_base}/{owner}/{repo}/{ref}/{file_path}"
        parent = posix_file.parent
        rel_path = "." if str(parent) == "." else str(parent)
        fallback_name = _repo_slug(repo) if rel_path == "." else parent.name
        return {
            "kind": "single_file",
            "source_url": source,
            "raw_url": raw_url,
            "fallback_name": fallback_name,
            "rel_path": rel_path,
        }

    tree_match = GITHUB_TREE_RE.match(clean_web)
    if tree_match:
        owner, repo, url_ref, url_subpath = tree_match.groups()
        repo_clean = _repo_slug(repo)
        gh_base = (
            os.environ.get("SKILL_CURATOR_GITHUB_BASE_URL") or DEFAULT_GITHUB_BASE_URL
        ).rstrip("/")
        return {
            "kind": "git",
            "source_url": source,
            "clone_url": f"{gh_base}/{owner}/{repo_clean}.git",
            "ref": ref_override if ref_override is not None else url_ref,
            "subpath": (
                subpath_override.strip("/")
                if subpath_override is not None
                else (url_subpath or "").strip("/")
            ),
            "repo_slug": repo_clean,
        }

    if parsed.scheme in ("http", "https", "file") and PurePosixPath(parsed.path).name.lower() == "skill.md":
        posix_path = PurePosixPath(parsed.path)
        parent_name = posix_path.parent.name
        return {
            "kind": "single_file",
            "source_url": source,
            "raw_url": source,
            "fallback_name": parent_name if parent_name else "",
            "rel_path": ".",
        }

    repo_match = GITHUB_REPO_RE.match(clean_web)
    if repo_match:
        owner, repo = repo_match.groups()
        repo_clean = _repo_slug(repo)
        gh_base = (
            os.environ.get("SKILL_CURATOR_GITHUB_BASE_URL") or DEFAULT_GITHUB_BASE_URL
        ).rstrip("/")
        return {
            "kind": "git",
            "source_url": source,
            "clone_url": f"{gh_base}/{owner}/{repo_clean}.git",
            "ref": ref_override,
            "subpath": (subpath_override or "").strip("/"),
            "repo_slug": repo_clean,
        }

    if parsed.scheme == "file":
        slug = _repo_slug(PurePosixPath(parsed.path).name)
        clone_url = source
    elif parsed.scheme in ("http", "https", "ssh", "git") or source.startswith("git@"):
        slug = _repo_slug(PurePosixPath(parsed.path).name)
        clone_url = source
    else:
        local_path = Path(os.path.expanduser(source)).resolve()
        if not local_path.exists():
            raise CuratorError(f"Source path does not exist: '{source}'.")
        slug = _repo_slug(local_path.name)
        clone_url = str(local_path)

    return {
        "kind": "git",
        "source_url": source,
        "clone_url": clone_url,
        "ref": ref_override,
        "subpath": (subpath_override or "").strip("/"),
        "repo_slug": slug,
    }


def _git_env() -> dict[str, str]:
    """Build non-interactive environment for git subprocesses."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/echo"
    return env


def _run_git(args: list[str], timeout: float, label: str) -> subprocess.CompletedProcess[str]:
    """Execute a git subprocess with hang and auth-prompt guards."""
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CuratorError(f"Git {label} timed out after {timeout:g}s.") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise CuratorError(f"Git {label} failed: {detail}")
    return proc


def _clone_repo(clone_url: str, dest: Path, ref: str | None, timeout: float) -> None:
    """Clone clone_url into dest and check out ref if requested."""
    deadline = time.monotonic() + timeout
    if not ref:
        _run_git(["clone", "--depth", "1", clone_url, str(dest)], timeout, "clone")
        return

    try:
        _run_git(
            ["clone", "--depth", "1", "--branch", ref, clone_url, str(dest)],
            timeout,
            "clone",
        )
    except CuratorError as exc:
        msg = str(exc).lower()
        if "timed out" in msg:
            raise
        if dest.exists():
            shutil.rmtree(str(dest), ignore_errors=True)
        remaining = max(0.5, deadline - time.monotonic())
        _run_git(["clone", clone_url, str(dest)], remaining, "clone")
        remaining = max(0.5, deadline - time.monotonic())
        _run_git(["-C", str(dest), "checkout", ref], remaining, f"checkout '{ref}'")


def _resolve_dest_parent(dest_dir: str | None) -> Path:
    """Return destination parent directory, creating a temp staging dir if omitted."""
    if dest_dir:
        parent = Path(os.path.expanduser(dest_dir)).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        return parent
    return Path(tempfile.mkdtemp(prefix="skill-curator-fetch-")).resolve()


def _fetch_single_file(spec: dict[str, Any], dest_dir: str | None, timeout: float) -> dict[str, Any]:
    """Download a single SKILL.md via curl and stage it cleanly."""
    raw_url = spec["raw_url"]
    max_time = str(max(1, int(round(timeout))))
    try:
        proc = subprocess.run(
            ["curl", "-fsSL", "--max-time", max_time, raw_url],
            capture_output=True,
            timeout=timeout + 1.5,
        )
    except subprocess.TimeoutExpired as exc:
        raise CuratorError(f"Fetch timed out after {timeout:g}s for '{raw_url}'.") from exc

    if proc.returncode != 0:
        err_text = proc.stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode == 28 or "timed out" in err_text.lower():
            raise CuratorError(f"Fetch timed out after {timeout:g}s for '{raw_url}'.")
        raise CuratorError(f"curl failed ({proc.returncode}) fetching '{raw_url}': {err_text}")

    with tempfile.TemporaryDirectory(prefix="skill-curator-file-") as tmp_dir:
        tmp_skill_dir = Path(tmp_dir) / "skill"
        tmp_skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md_path = tmp_skill_dir / "SKILL.md"
        skill_md_path.write_bytes(proc.stdout)

        fm = parse_frontmatter(skill_md_path)
        raw_name = fm.get("name") or spec["fallback_name"]
        skill_name = validate_skill_name(normalize_slug(raw_name) or normalize_slug(spec["fallback_name"]))
        description = fm.get("description", "")

        dest_parent = _resolve_dest_parent(dest_dir)
        staged_dir = dest_parent / skill_name
        copy_tree_clean(tmp_skill_dir, staged_dir)

    return {
        "ok": True,
        "action": "fetch",
        "status": "fetched",
        "requires_selection": False,
        "source_url": spec["source_url"],
        "raw_url": raw_url,
        "staged_dir": str(staged_dir),
        "skill": {
            "name": skill_name,
            "description": description,
            "rel_path": spec["rel_path"],
            "staged_dir": str(staged_dir),
        },
    }


def _discover_skills(
    clone_root: Path, search_root: Path, repo_slug: str
) -> list[dict[str, Any]]:
    """Find all SKILL.md files under search_root (excluding .git)."""
    clone_real = os.path.realpath(str(clone_root))
    found: list[dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(str(search_root), followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        if "SKILL.md" not in filenames:
            continue
        skill_dir = Path(dirpath)
        skill_md = skill_dir / "SKILL.md"
        if not is_path_within(clone_real, os.path.realpath(str(skill_dir))) or not is_path_within(
            clone_real, os.path.realpath(str(skill_md))
        ):
            raise CuratorError(
                f"Security error: skill path '{skill_dir}' resolves outside repository root."
            )
        fm = parse_frontmatter(skill_md)
        rel = skill_dir.relative_to(clone_root).as_posix()
        fallback = repo_slug if rel == "." else skill_dir.name
        raw_name = fm.get("name") or fallback
        name = normalize_slug(raw_name) or normalize_slug(fallback)
        found.append(
            {
                "name": name,
                "description": fm.get("description", ""),
                "rel_path": rel,
                "skill_dir": skill_dir,
            }
        )

    found.sort(key=lambda item: (item["rel_path"], item["name"]))
    return found


def _select_skill(
    discovered: list[dict[str, Any]], selector: str, subpath: str
) -> dict[str, Any]:
    """Select one skill by name or relative path from discovered skills."""
    normalized = selector.strip().strip("/")
    matches = [
        item
        for item in discovered
        if item["name"] == normalized
        or item["rel_path"] == normalized
        or (subpath and item["rel_path"] == f"{subpath}/{normalized}")
    ]
    if not matches:
        available = ", ".join(f"{item['name']} ({item['rel_path']})" for item in discovered)
        raise CuratorError(
            f"Skill '{selector}' not found in fetched repository. Available: {available}"
        )
    if len(matches) > 1:
        rels = ", ".join(item["rel_path"] for item in matches)
        raise CuratorError(
            f"Skill selector '{selector}' matched multiple entries ({rels}); specify rel_path."
        )
    return matches[0]


def fetch_source(
    source: str,
    dest_dir: str | None = None,
    skill_selector: str | None = None,
    ref: str | None = None,
    subpath: str | None = None,
    timeout: float = 30.0,
    fetch_all: bool = False,
) -> dict[str, Any]:
    """Fetch a skill, multi-skill inventory, or batch of skills from a URL or Git repo."""
    if timeout <= 0:
        raise CuratorError(f"Invalid --timeout '{timeout}'. Must be > 0.")
    if fetch_all and skill_selector:
        raise CuratorError("Specify either --all or --skill, not both.")

    spec = parse_source_url(source, ref_override=ref, subpath_override=subpath)
    if spec["kind"] == "single_file":
        return _fetch_single_file(spec, dest_dir=dest_dir, timeout=timeout)

    with tempfile.TemporaryDirectory(prefix="skill-curator-clone-") as tmp_dir:
        clone_root = Path(tmp_dir) / "repo"
        _clone_repo(spec["clone_url"], clone_root, ref=spec["ref"], timeout=timeout)
        shutil.rmtree(str(clone_root / ".git"), ignore_errors=True)

        search_root = clone_root
        sub = spec["subpath"]
        if sub:
            candidate = clone_root / sub
            if not is_path_within(
                os.path.realpath(str(clone_root)), os.path.realpath(str(candidate))
            ):
                raise CuratorError(
                    f"Security error: subpath '{sub}' escapes repository root."
                )
            if not candidate.is_dir():
                raise CuratorError(f"Subpath '{sub}' not found in repository.")
            search_root = candidate

        discovered = _discover_skills(clone_root, search_root, spec["repo_slug"])
        if not discovered:
            raise CuratorError(f"No SKILL.md found in '{source}'.")

        if fetch_all:
            dest_parent = _resolve_dest_parent(dest_dir)
            staged_skills: list[dict[str, Any]] = []
            seen_names: set[str] = set()
            for item in discovered:
                skill_name = validate_skill_name(item["name"])
                if skill_name in seen_names:
                    raise CuratorError(
                        f"Duplicate normalized skill name '{skill_name}' in repository; use --skill <rel_path>."
                    )
                seen_names.add(skill_name)
                staged_dir = dest_parent / skill_name
                copy_tree_clean(item["skill_dir"], staged_dir)
                staged_skills.append(
                    {
                        "name": skill_name,
                        "description": item["description"],
                        "rel_path": item["rel_path"],
                        "staged_dir": str(staged_dir),
                    }
                )
            return {
                "ok": True,
                "action": "fetch",
                "status": "fetched",
                "requires_selection": False,
                "source_url": spec["source_url"],
                "staged_parent": str(dest_parent),
                "skills": staged_skills,
            }

        if len(discovered) > 1 and not skill_selector:
            return {
                "ok": True,
                "action": "fetch",
                "status": "multiple_skills_found",
                "requires_selection": True,
                "source_url": spec["source_url"],
                "skills": [
                    {
                        "name": item["name"],
                        "description": item["description"],
                        "rel_path": item["rel_path"],
                    }
                    for item in discovered
                ],
            }

        selected = (
            _select_skill(discovered, skill_selector, sub)
            if skill_selector
            else discovered[0]
        )
        skill_name = validate_skill_name(selected["name"])
        dest_parent = _resolve_dest_parent(dest_dir)
        staged_dir = dest_parent / skill_name
        copy_tree_clean(selected["skill_dir"], staged_dir)

        return {
            "ok": True,
            "action": "fetch",
            "status": "fetched",
            "requires_selection": False,
            "source_url": spec["source_url"],
            "staged_dir": str(staged_dir),
            "skill": {
                "name": skill_name,
                "description": selected["description"],
                "rel_path": selected["rel_path"],
                "staged_dir": str(staged_dir),
            },
        }
