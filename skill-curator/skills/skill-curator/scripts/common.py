"""Shared validation, frontmatter parsing, and safe filesystem helpers for Skill Curator."""

from __future__ import annotations

import hashlib
import json
import locale
import os
import re
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
INDEX_TITLE = "# Skill Vault Index"
INDEX_COLUMNS = "| Name | Keywords | Scene Tags | Source | Path |"
INDEX_SEPARATOR = "| --- | --- | --- | --- | --- |"
UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")

STANDARD_HARNESS_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("agents", (".agents", "skills")),
    ("claude", (".claude", "skills")),
    ("antigravity", (".gemini", "config", "skills")),
    ("antigravity-legacy", (".gemini", "antigravity", "skills")),
    ("codex", (".codex", "skills")),
    ("cursor", (".cursor", "skills")),
    ("opencode", (".opencode", "skills")),
    ("opencode", (".config", "opencode", "skills")),
)


def resolve_home_dir(home_dir: Path | None = None) -> Path:
    """Resolve user home directory from explicit override or OS environment."""
    if home_dir is not None:
        return Path(os.path.expanduser(str(home_dir))).resolve()
    if os.name == "nt":
        env_home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    else:
        env_home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if env_home:
        return Path(os.path.expanduser(env_home)).resolve()
    return Path.home().resolve()


def decode_raw_bytes(raw_bytes: bytes) -> str:
    """Decode raw stdin bytes trying UTF-8, OS preferred encoding, then GB18030."""
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        pass
    preferred = locale.getpreferredencoding(False) or ""
    candidates: list[str] = []
    for enc in (preferred, "gb18030"):
        if enc and enc.lower() not in ("utf-8", "utf8", "ascii") and enc.lower() not in [c.lower() for c in candidates]:
            candidates.append(enc)
    for enc in candidates:
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def tree_content_digest(skill_dir: Path) -> str:
    """Compute a deterministic SHA-256 digest of skill files excluding .curator-meta.json and .git."""
    digest = hashlib.sha256()
    resolved_root = Path(os.path.realpath(str(skill_dir)))
    for dirpath, dirnames, filenames in os.walk(str(resolved_root), followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for fname in sorted(filenames):
            if fname == ".curator-meta.json":
                continue
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(resolved_root).as_posix()
            digest.update(rel.encode("utf-8", errors="replace"))
            digest.update(b"\x00")
            if fpath.is_symlink():
                digest.update(b"L:" + os.readlink(str(fpath)).encode("utf-8", errors="replace"))
            else:
                with fpath.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        digest.update(chunk)
            digest.update(b"\x00")
    return digest.hexdigest()


class CuratorError(Exception):
    """Controlled CLI error reported as structured JSON."""

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


def normalize_slug(raw: str) -> str:
    """Normalize arbitrary text into a lowercase kebab-case slug."""
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_skill_name(name: str) -> str:
    """Ensure skill name follows kebab-case convention."""
    cleaned = name.strip()
    if not cleaned or not SKILL_NAME_RE.match(cleaned):
        raise CuratorError(
            f"Invalid skill name '{name}'. Expected kebab-case matching {SKILL_NAME_RE.pattern}."
        )
    return cleaned


def is_path_within(root_real: str, candidate_real: str) -> bool:
    """Return True if candidate_real is inside or equal to root_real."""
    root_norm = os.path.normcase(os.path.normpath(root_real))
    cand_norm = os.path.normcase(os.path.normpath(candidate_real))
    try:
        return os.path.commonpath([root_norm, cand_norm]) == root_norm
    except ValueError:
        return False


def validate_tree_containment(root_dir: Path) -> None:
    """Reject any symlink or path inside root_dir whose realpath escapes root_dir."""
    if not root_dir.exists() or not root_dir.is_dir():
        raise CuratorError(f"Directory does not exist: {root_dir}")

    root_real = os.path.realpath(str(root_dir))
    for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
        entries = [dirpath]
        entries.extend(os.path.join(dirpath, d) for d in dirnames)
        entries.extend(os.path.join(dirpath, f) for f in filenames)
        for entry in entries:
            entry_real = os.path.realpath(entry)
            if not is_path_within(root_real, entry_real):
                raise CuratorError(
                    f"Security error: path '{entry}' resolves outside skill root '{root_dir}'."
                )
            if os.path.islink(entry) and not os.path.exists(entry):
                raise CuratorError(f"Broken symlink inside skill directory: '{entry}'.")


def _make_writable(path: str) -> None:
    """Clear read-only bits on path without touching symlink targets."""
    if os.path.islink(path):
        return
    try:
        mode = stat.S_IWRITE | stat.S_IREAD
        if os.path.isdir(path):
            mode |= stat.S_IEXEC
        os.chmod(path, mode)
    except OSError:
        pass


def _handle_remove_readonly(func: Any, path: str, _exc: Any) -> None:
    """Clear read-only attributes (such as .git packfiles) and retry removal."""
    parent = os.path.dirname(path)
    if parent:
        _make_writable(parent)
    _make_writable(path)
    func(path)


def rmtree_safe(target: Path | str, *, ignore_errors: bool = False) -> None:
    """Remove files, directory symlinks/junctions, or read-only trees safely across OSes."""
    target_str = str(target)
    if not os.path.lexists(target_str):
        return
    try:
        if os.path.islink(target_str):
            try:
                os.unlink(target_str)
            except PermissionError:
                os.rmdir(target_str)
            return
        if os.path.isfile(target_str):
            _make_writable(target_str)
            os.unlink(target_str)
            return
        if sys.version_info >= (3, 12):
            shutil.rmtree(target_str, onexc=_handle_remove_readonly)
        else:
            shutil.rmtree(target_str, onerror=_handle_remove_readonly)
    except OSError:
        if not ignore_errors:
            raise


def copy_tree_clean(src: Path, dst: Path) -> None:
    """Validate src containment and copy to dst without symlinks or .git directories."""
    validate_tree_containment(src)
    src_real = os.path.realpath(str(src))
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_parent = tempfile.mkdtemp(dir=str(dst.parent), prefix=f".tmp-{dst.name}-")
    try:
        staged = Path(tmp_parent) / dst.name
        shutil.copytree(
            src_real,
            str(staged),
            symlinks=False,
            ignore=shutil.ignore_patterns(".git"),
        )
        if dst.exists() or os.path.islink(str(dst)):
            rmtree_safe(dst)
        staged.rename(dst)
    finally:
        if os.path.exists(tmp_parent):
            rmtree_safe(tmp_parent, ignore_errors=True)


def _unquote_yaml_scalar(value: str) -> str:
    """Strip matching single or double quotes from a YAML scalar."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        inner = value[1:-1]
        if value[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n")
        return inner
    return value


def _parse_block_scalar(
    fm_lines: list[str], start_idx: int, block_style: str
) -> tuple[str, int]:
    """Parse a YAML literal ('|') or folded ('>') block scalar starting at start_idx."""
    block_lines: list[str] = []
    idx = start_idx
    base_indent: int | None = None
    while idx < len(fm_lines):
        nxt = fm_lines[idx]
        if not nxt.strip():
            block_lines.append("")
            idx += 1
            continue
        indent = len(nxt) - len(nxt.lstrip(" "))
        if indent == 0:
            break
        if base_indent is None:
            base_indent = indent
        block_lines.append(nxt[base_indent:])
        idx += 1

    if block_style == "|":
        return "\n".join(block_lines).strip(), idx

    paragraphs: list[str] = []
    current: list[str] = []
    for bl in block_lines:
        if bl == "":
            if current:
                paragraphs.append(" ".join(current))
                current = []
        else:
            current.append(bl.strip())
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs).strip(), idx


def parse_frontmatter(skill_md_path: Path) -> dict[str, str]:
    """Parse top-level YAML frontmatter scalars from SKILL.md using stdlib."""
    if not skill_md_path.is_file():
        raise CuratorError(f"Missing required SKILL.md at '{skill_md_path}'.")

    text = skill_md_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise CuratorError(f"Unterminated YAML frontmatter in '{skill_md_path}'.")

    fm_lines = lines[1:end_idx]
    data: dict[str, str] = {}
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not match:
            i += 1
            continue
        key, raw_val = match.group(1), match.group(2).strip()
        if raw_val in ("|", ">", "|-", ">-"):
            data[key], i = _parse_block_scalar(fm_lines, i + 1, raw_val[0])
            continue

        parts = [_unquote_yaml_scalar(raw_val)] if raw_val else []
        i += 1
        while i < len(fm_lines):
            nxt = fm_lines[i]
            if not nxt.strip():
                break
            indent = len(nxt) - len(nxt.lstrip(" "))
            if indent == 0:
                break
            parts.append(nxt.strip())
            i += 1
        data[key] = " ".join(p for p in parts if p).strip()

    return data


_GITHUB_ALIAS_HOSTS = frozenset(
    {
        "github.com",
        "www.github.com",
        "skills.sh",
        "www.skills.sh",
        "raw.githubusercontent.com",
    }
)
_SHORTHAND_REPO_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/.*)?$")
_SCP_GIT_RE = re.compile(
    r"^[^@\s/]+@([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$"
)
_REPO_FM_KEYS = ("source_url", "source", "repository", "repo", "github", "origin", "homepage", "url")
_MD_LINK_RE = re.compile(r"^\[[^\]]*\]\(([^)]+)\)$")


def normalize_repo_source(raw: str) -> tuple[str | None, str]:
    """Normalize a repo URL, SSH remote, file URL, or owner/repo shorthand."""
    cleaned = (raw or "").strip()
    if cleaned.startswith("<") and cleaned.endswith(">") and len(cleaned) > 2:
        cleaned = cleaned[1:-1].strip()
    cleaned = _unquote_yaml_scalar(cleaned).strip()
    md_match = _MD_LINK_RE.match(cleaned)
    if md_match:
        cleaned = md_match.group(1).strip()
    if cleaned.lower().startswith("git+"):
        cleaned = cleaned[4:].strip()
    if not cleaned:
        return None, ""

    if cleaned.lower().startswith("file://"):
        raw_path = cleaned[7:]
        norm_path = os.path.normcase(os.path.normpath(raw_path)).replace("\\", "/")
        if norm_path.endswith(".git"):
            norm_path = norm_path[:-4]
        return (f"file://{norm_path}", cleaned) if norm_path else (None, "")

    scp_match = _SCP_GIT_RE.match(cleaned)
    if scp_match:
        host, owner, repo = scp_match.groups()
        repo_clean = repo[:-4] if repo.endswith(".git") else repo
        if owner and repo_clean:
            return (
                f"{host.lower()}/{owner.lower()}/{repo_clean.lower()}",
                f"https://{host}/{owner}/{repo_clean}",
            )

    if "://" in cleaned:
        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").lower()
        parts = [p for p in parsed.path.split("/") if p]
        if not host or len(parts) < 2:
            return None, ""
        owner, repo = parts[0], parts[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if not owner or not repo:
            return None, ""
        if host in _GITHUB_ALIAS_HOSTS:
            return (
                f"github.com/{owner.lower()}/{repo.lower()}",
                f"https://github.com/{owner}/{repo}",
            )
        if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or not re.match(r"^[A-Za-z0-9_.-]+$", repo):
            return None, ""
        if len(parts) == 2 and repo.lower().endswith((".html", ".htm", ".md", ".txt")):
            return None, ""
        canonical_host = parsed.hostname or host
        return (
            f"{host}/{owner.lower()}/{repo.lower()}",
            f"https://{canonical_host}/{owner}/{repo}",
        )

    if ":" not in cleaned and not cleaned.startswith((".", "/")):
        sh_match = _SHORTHAND_REPO_RE.match(cleaned)
        if sh_match:
            owner, repo = sh_match.groups()
            if repo.endswith(".git"):
                repo = repo[:-4]
            if owner and repo and owner not in (".", "..") and repo not in (".", ".."):
                return (
                    f"github.com/{owner.lower()}/{repo.lower()}",
                    f"https://github.com/{owner}/{repo}",
                )

    return None, ""


def extract_frontmatter_repo_source(skill_md_path: Path) -> str:
    """Extract normalized canonical repo URL from SKILL.md top-level or metadata: frontmatter."""
    if not skill_md_path.is_file():
        return ""
    text = decode_raw_bytes(skill_md_path.read_bytes()).lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return ""

    fm_lines = lines[1:end_idx]
    top_fields: dict[str, str] = {}
    meta_fields: dict[str, str] = {}
    in_metadata = False
    meta_indent = 0

    for line in fm_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        kv = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", stripped)
        if indent == 0:
            in_metadata = bool(kv and kv.group(1) == "metadata" and not kv.group(2).strip())
            meta_indent = 0
            if kv and kv.group(2).strip():
                top_fields[kv.group(1).lower()] = _unquote_yaml_scalar(kv.group(2).strip())
        elif in_metadata and kv:
            if meta_indent == 0:
                meta_indent = indent
            if indent == meta_indent and kv.group(2).strip():
                meta_fields[kv.group(1).lower()] = _unquote_yaml_scalar(kv.group(2).strip())

    for key in _REPO_FM_KEYS:
        for mapping in (top_fields, meta_fields):
            raw_val = mapping.get(key, "")
            if not raw_val:
                continue
            repo_key, canonical_url = normalize_repo_source(raw_val)
            if repo_key:
                return canonical_url
    return ""


def extract_markdown_body(skill_md_path: Path) -> str:
    """Return SKILL.md markdown body after any YAML frontmatter block."""
    if not skill_md_path.is_file():
        return ""
    text = decode_raw_bytes(skill_md_path.read_bytes()).lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1 :])
    return text


def read_skills_lock_sources(
    home_dir: Path | None = None,
    project_dir: Path | None = None,
) -> dict[str, tuple[str, str]]:
    """Read skill -> (repo_key, canonical_url) from global and explicit project lockfiles."""
    resolved_home = resolve_home_dir(home_dir)
    lock_paths = [
        resolved_home / ".agents" / ".skill-lock.json",
        resolved_home / ".agents" / "skills-lock.json",
    ]
    if project_dir is not None:
        resolved_proj = Path(os.path.expanduser(str(project_dir))).resolve()
        lock_paths.extend(
            [
                resolved_proj / ".agents" / ".skill-lock.json",
                resolved_proj / "skills-lock.json",
            ]
        )

    mapping: dict[str, tuple[str, str]] = {}
    for lock_path in lock_paths:
        if not lock_path.is_file():
            continue
        try:
            data = json.loads(decode_raw_bytes(lock_path.read_bytes()))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        skills_obj = data.get("skills")
        if not isinstance(skills_obj, dict):
            continue
        for slug, entry in skills_obj.items():
            if not isinstance(slug, str) or not isinstance(entry, dict):
                continue
            for field in ("sourceUrl", "source", "repository", "url"):
                raw_src = entry.get(field)
                if isinstance(raw_src, str) and raw_src.strip():
                    repo_key, canonical_url = normalize_repo_source(raw_src)
                    if repo_key:
                        mapping[slug] = (repo_key, canonical_url)
                        break
    return mapping


def rewrite_frontmatter_description(
    skill_md_path: Path, skill_name: str, new_description: str
) -> None:
    """Update description in SKILL.md frontmatter (creating frontmatter if absent)."""
    text = skill_md_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    quoted_desc = f"description: {json.dumps(new_description)}"

    if not lines or lines[0].strip() != "---":
        new_text = f"---\nname: {skill_name}\n{quoted_desc}\n---\n\n{text.lstrip()}"
        skill_md_path.write_text(new_text, encoding="utf-8")
        return

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise CuratorError(f"Unterminated YAML frontmatter in '{skill_md_path}'.")

    fm_lines = lines[1:end_idx]
    body_lines = lines[end_idx + 1 :]
    updated_fm: list[str] = []
    replaced = False
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if re.match(r"^description\s*:", line):
            if not replaced:
                updated_fm.append(quoted_desc)
                replaced = True
            i += 1
            while i < len(fm_lines):
                nxt = fm_lines[i]
                if not nxt.strip():
                    # Skip blank lines inside a block scalar if followed by indented lines.
                    if i + 1 < len(fm_lines) and (
                        len(fm_lines[i + 1]) - len(fm_lines[i + 1].lstrip(" "))
                    ) > 0:
                        i += 1
                        continue
                    break
                if (len(nxt) - len(nxt.lstrip(" "))) == 0:
                    break
                i += 1
            continue
        updated_fm.append(line)
        i += 1

    if not replaced:
        updated_fm.append(quoted_desc)

    rebuilt = ["---", *updated_fm, "---", *body_lines]
    skill_md_path.write_text("\n".join(rebuilt).rstrip("\n") + "\n", encoding="utf-8")


def split_csv(raw: str | None) -> list[str]:
    """Split comma-separated values into a deduplicated list preserving order."""
    if not raw:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def escape_md_cell(value: str) -> str:
    """Sanitize value for inclusion inside a Markdown table cell."""
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def split_md_row(stripped_line: str) -> list[str]:
    """Split a Markdown table row on unescaped pipe characters and unescape cells."""
    return [
        c.strip().replace("\\|", "|")
        for c in UNESCAPED_PIPE_RE.split(stripped_line[1:-1])
    ]


def ensure_index_file(index_path: Path) -> None:
    """Create INDEX.md with the canonical Vault Index header if missing."""
    if index_path.exists():
        return
    index_path.write_text(
        f"{INDEX_TITLE}\n\n{INDEX_COLUMNS}\n{INDEX_SEPARATOR}\n",
        encoding="utf-8",
    )


def read_index_rows(index_path: Path) -> dict[str, dict[str, Any]]:
    """Parse INDEX.md rows into a mapping of skill name -> parsed fields."""
    if not index_path.is_file():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for line in index_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = split_md_row(stripped)
        if not cells or cells[0] == "Name" or cells[0].startswith("---"):
            continue
        name = cells[0]
        keywords = split_csv(cells[1]) if len(cells) > 1 else []
        scene_tags = split_csv(cells[2]) if len(cells) > 2 else []
        source_url = cells[3] if len(cells) > 3 else ""
        rel_path = cells[4] if len(cells) > 4 else f"skills/{name}"
        rows[name] = {
            "name": name,
            "keywords": keywords,
            "scene_tags": scene_tags,
            "source_url": source_url,
            "path": rel_path,
        }
    return rows


def upsert_index_row(
    index_path: Path,
    name: str,
    keywords: list[str],
    scene_tags: list[str],
    source_url: str,
    rel_path: str,
) -> None:
    """Insert or update a single skill row in INDEX.md without duplicating rows."""
    ensure_index_file(index_path)
    existing_lines = index_path.read_text(encoding="utf-8").splitlines()

    row_line = (
        f"| {escape_md_cell(name)} "
        f"| {escape_md_cell(', '.join(keywords))} "
        f"| {escape_md_cell(', '.join(scene_tags))} "
        f"| {escape_md_cell(source_url)} "
        f"| {escape_md_cell(rel_path)} |"
    )

    updated_lines: list[str] = []
    replaced = False
    last_table_idx = -1

    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            last_table_idx = len(updated_lines)
            cells = split_md_row(stripped)
            if cells and cells[0] == name:
                if not replaced:
                    updated_lines.append(row_line)
                    replaced = True
                continue
        updated_lines.append(line)

    if not replaced:
        if last_table_idx >= 0:
            updated_lines.insert(last_table_idx + 1, row_line)
        else:
            updated_lines.extend(["", INDEX_COLUMNS, INDEX_SEPARATOR, row_line])

    index_path.write_text("\n".join(updated_lines).rstrip("\n") + "\n", encoding="utf-8")


SKIP_DISCOVERY_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        "tests",
        "test",
        "examples",
        "references",
        "assets",
        "scripts",
        ".claude-plugin",
        ".github",
    }
)


def is_plugin_bundle_dir(entry: Path) -> bool:
    """Return True if entry or its resolved target is an external plugin bundle."""
    candidates = [entry]
    try:
        resolved = entry.resolve()
        if resolved != entry:
            candidates.append(resolved)
    except OSError:
        pass
    return any(
        (cand / "plugin.json").exists()
        or (cand / ".claude-plugin" / "plugin.json").exists()
        or (cand / "gemini-extension.json").exists()
        for cand in candidates
    )


def _is_link_or_junction(path: Path) -> bool:
    """Return True if path is a symbolic link or Windows junction point."""
    p_str = str(path)
    if os.path.islink(p_str):
        return True
    is_junc = getattr(os.path, "isjunction", None)
    return bool(is_junc(p_str)) if is_junc is not None else False


def has_symlink_component(path: Path, stop_at: Path | None = None) -> bool:
    """Return True if path or any intermediate ancestor strictly below stop_at is a symlink."""
    if stop_at is None:
        return _is_link_or_junction(path)
    stop_norm = os.path.normcase(os.path.normpath(os.path.abspath(str(stop_at))))
    cur = path
    while True:
        cur_norm = os.path.normcase(os.path.normpath(os.path.abspath(str(cur))))
        if cur_norm == stop_norm:
            return False
        if _is_link_or_junction(cur):
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent


def find_enclosing_symlink_container(skill_path: Path, parent_dir: Path) -> Path | None:
    """Return any symlinked container directory between skill_path.parent and parent_dir."""
    stop_norm = os.path.normcase(os.path.normpath(os.path.abspath(str(parent_dir))))
    cur = skill_path.parent
    found: Path | None = None
    while True:
        cur_norm = os.path.normcase(os.path.normpath(os.path.abspath(str(cur))))
        if cur_norm == stop_norm:
            return found
        if _is_link_or_junction(cur):
            found = cur
        if cur.parent == cur:
            return found
        cur = cur.parent


def _resolve_discovered_skill_name(skill_dir: Path, depth: int) -> str:
    """Resolve canonical skill name for depth=1 vs nested cloned/symlinked skill dirs."""
    fm: dict[str, str] = {}
    try:
        fm = parse_frontmatter(skill_dir / "SKILL.md")
    except (CuratorError, OSError):
        pass
    fm_name = fm.get("name", "").strip()
    if depth == 1:
        if SKILL_NAME_RE.match(skill_dir.name):
            return skill_dir.name
        return normalize_slug(fm_name) or normalize_slug(skill_dir.name) or skill_dir.name
    if fm_name and SKILL_NAME_RE.match(fm_name):
        return fm_name
    return normalize_slug(skill_dir.name) or skill_dir.name


def discover_skills_in_dir(
    parent_dir: Path,
    *,
    max_depth: int = 4,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Discover standalone, nested cloned, and symlinked skills up to max_depth."""
    if not parent_dir.is_dir():
        return [], []

    raw_entries: list[dict[str, Any]] = []
    excluded_bundles: list[str] = []

    def _walk(
        cur_dir: Path,
        depth: int,
        top_container: Path | None,
        branch_reals: frozenset[str],
    ) -> None:
        try:
            children = sorted(cur_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for child in children:
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name == "skill-curator":
                continue
            if is_plugin_bundle_dir(child):
                if depth == 1:
                    excluded_bundles.append(child.name)
                continue
            try:
                child_norm = os.path.normcase(os.path.normpath(os.path.realpath(str(child))))
            except OSError:
                continue
            if child_norm in branch_reals:
                continue
            next_branch = branch_reals | {child_norm}
            cur_top = child if depth == 1 else top_container

            if (child / "SKILL.md").is_file():
                skill_name = _resolve_discovered_skill_name(child, depth)
                raw_entries.append(
                    {
                        "name": skill_name,
                        "path": child,
                        "depth": depth,
                        "top_container": cur_top if depth > 1 else None,
                        "_real": child_norm,
                        "_root_container": cur_top,
                    }
                )
                skills_sub = child / "skills"
                if (
                    depth == 1
                    and depth + 2 <= max_depth
                    and skills_sub.is_dir()
                    and not (skills_sub / "SKILL.md").is_file()
                ):
                    try:
                        sub_real = os.path.normcase(os.path.normpath(os.path.realpath(str(skills_sub))))
                        sub_children = list(skills_sub.iterdir())
                    except OSError:
                        sub_real = ""
                        sub_children = []
                    if sub_real and sub_real not in next_branch:
                        has_nested = any(
                            sub.is_dir() and (sub / "SKILL.md").is_file()
                            for sub in sub_children
                        )
                        if has_nested:
                            _walk(skills_sub, depth + 2, cur_top, next_branch | {sub_real})
                continue

            if child.name in SKIP_DISCOVERY_DIRS:
                continue
            if depth < max_depth:
                _walk(child, depth + 1, cur_top, next_branch)

    try:
        root_norm = os.path.normcase(os.path.normpath(os.path.realpath(str(parent_dir))))
    except OSError:
        root_norm = ""
    _walk(parent_dir, 1, None, frozenset({root_norm}) if root_norm else frozenset())

    nested_names_by_top: set[tuple[str, str]] = set()
    for ent in raw_entries:
        if ent["depth"] > 1 and ent["top_container"] is not None:
            top_key = os.path.normcase(os.path.normpath(str(ent["top_container"])))
            nested_names_by_top.add((top_key, ent["name"]))

    filtered: list[dict[str, Any]] = []
    for ent in raw_entries:
        if ent["depth"] == 1:
            path_key = os.path.normcase(os.path.normpath(str(ent["path"])))
            if (path_key, ent["name"]) in nested_names_by_top:
                continue
        filtered.append(ent)

    # Deduplicate same realpath entries within the same parent_dir (prefer shallower depth).
    filtered.sort(key=lambda e: (e["depth"], str(e["path"])))
    deduped: list[dict[str, Any]] = []
    seen_reals: set[str] = set()
    for ent in filtered:
        if ent["_real"] in seen_reals:
            continue
        seen_reals.add(ent["_real"])
        deduped.append(
            {
                "name": ent["name"],
                "path": ent["path"],
                "depth": ent["depth"],
                "top_container": ent["top_container"],
            }
        )
    deduped.sort(key=lambda e: (str(e["path"]), e["name"]))
    return deduped, sorted(set(excluded_bundles))


def _read_git_origin_url(git_entry: Path) -> str | None:
    """Read preferred remote URL from .git dir or .git gitdir file via stdlib."""
    config_candidates: list[Path] = []
    if git_entry.is_dir():
        config_candidates.append(git_entry / "config")
    elif git_entry.is_file():
        try:
            text = decode_raw_bytes(git_entry.read_bytes())
        except OSError:
            return None
        gitdir_raw = ""
        for line in text.splitlines():
            m = re.match(r"^\s*gitdir\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
            if m:
                gitdir_raw = m.group(1).strip()
                break
        if gitdir_raw:
            gd = Path(gitdir_raw)
            if not gd.is_absolute():
                gd = (git_entry.parent / gd).resolve()
            config_candidates.append(gd / "config")
            commondir_file = gd / "commondir"
            if commondir_file.is_file():
                try:
                    c_raw = decode_raw_bytes(commondir_file.read_bytes()).strip()
                except OSError:
                    c_raw = ""
                if c_raw:
                    c_path = Path(c_raw)
                    if not c_path.is_absolute():
                        c_path = (gd / c_path).resolve()
                    config_candidates.append(c_path / "config")

    remotes: dict[str, str] = {}
    for cfg_path in config_candidates:
        if not cfg_path.is_file():
            continue
        try:
            cfg_text = decode_raw_bytes(cfg_path.read_bytes())
        except OSError:
            continue
        current_remote: str | None = None
        for line in cfg_text.splitlines():
            sec = re.match(r'^\s*\[remote\s+"([^"]+)"\]\s*$', line)
            if sec:
                current_remote = sec.group(1)
                continue
            if line.lstrip().startswith("["):
                current_remote = None
                continue
            if current_remote is not None:
                um = re.match(r"^\s*url\s*=\s*(.+?)\s*$", line)
                if um:
                    remotes.setdefault(current_remote, um.group(1).strip())
        if remotes:
            break

    if not remotes:
        return None
    if "origin" in remotes:
        return remotes["origin"]
    if "upstream" in remotes:
        return remotes["upstream"]
    return next(iter(remotes.values()))


def _extract_repo_from_lockfile_data(lock_path: Path, skill_name: str) -> tuple[str, str] | None:
    """Look up a skill's normalized repo source from a single lockfile."""
    if not lock_path.is_file():
        return None
    try:
        data = json.loads(decode_raw_bytes(lock_path.read_bytes()))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    skills_obj = data.get("skills")
    if not isinstance(skills_obj, dict):
        return None
    entry = skills_obj.get(skill_name)
    if not isinstance(entry, dict):
        return None
    for field in ("sourceUrl", "source", "repository", "url"):
        raw_src = entry.get(field)
        if isinstance(raw_src, str) and raw_src.strip():
            repo_key, canonical_url = normalize_repo_source(raw_src)
            if repo_key:
                return repo_key, canonical_url
    return None


def inspect_ancestor_repo_provenance(
    skill_dir: Path,
    *,
    skill_name: str,
    harness_parent: Path | None = None,
    home_dir: Path | None = None,
) -> dict[str, tuple[str, str]]:
    """Safely collect bounded ancestor signals (skills-lock, git-origin, package-json, git-local)."""
    resolved_home = resolve_home_dir(home_dir)
    stop_norms = {
        os.path.normcase(os.path.normpath(os.path.realpath(str(resolved_home)))),
        os.path.normcase(os.path.normpath(os.path.realpath(tempfile.gettempdir()))),
    }
    parent_abs_norm = (
        os.path.normcase(os.path.normpath(os.path.abspath(str(harness_parent))))
        if harness_parent is not None
        else None
    )
    parent_real_norm = (
        os.path.normcase(os.path.normpath(os.path.realpath(str(harness_parent))))
        if harness_parent is not None
        else None
    )
    if parent_real_norm:
        stop_norms.add(parent_real_norm)

    ancestors: list[Path] = []
    seen_anc_norms: set[str] = set()

    def _add_anc(p: Path) -> None:
        norm = os.path.normcase(os.path.normpath(os.path.abspath(str(p))))
        if norm not in seen_anc_norms:
            seen_anc_norms.add(norm)
            ancestors.append(p)

    # 1. Lexical ancestors strictly below harness_parent
    if parent_abs_norm and is_path_within(parent_abs_norm, os.path.abspath(str(skill_dir))):
        cur = skill_dir
        for _ in range(8):
            cur_norm = os.path.normcase(os.path.normpath(os.path.abspath(str(cur))))
            if cur_norm == parent_abs_norm:
                break
            _add_anc(cur)
            if cur.parent == cur:
                break
            cur = cur.parent

    # 2. Resolved realpath ancestors when realpath escapes harness_parent (or harness_parent is None)
    skill_real = Path(os.path.realpath(str(skill_dir)))
    if not parent_real_norm or not is_path_within(parent_real_norm, str(skill_real)):
        cur = skill_real
        for _ in range(8):
            cur_norm = os.path.normcase(os.path.normpath(str(cur)))
            if cur_norm in stop_norms:
                break
            _add_anc(cur)
            if cur.parent == cur:
                break
            cur = cur.parent

    signals: dict[str, tuple[str, str]] = {}
    for anc in ancestors:
        if "skills-lock" not in signals:
            for lf in (
                anc / "skills-lock.json",
                anc / ".agents" / ".skill-lock.json",
                anc / ".agents" / "skills-lock.json",
            ):
                matched = _extract_repo_from_lockfile_data(lf, skill_name)
                if matched:
                    signals["skills-lock"] = matched
                    break

        git_entry = anc / ".git"
        if git_entry.exists():
            if "git-origin" not in signals:
                origin_url = _read_git_origin_url(git_entry)
                if origin_url:
                    repo_key, canonical_url = normalize_repo_source(origin_url)
                    if repo_key:
                        signals["git-origin"] = (repo_key, canonical_url)
            if "git-local" not in signals and "git-origin" not in signals:
                anc_real = os.path.realpath(str(anc))
                h8 = hashlib.sha256(anc_real.encode("utf-8", errors="replace")).hexdigest()[:8]
                signals["git-local"] = (f"local-git:{normalize_slug(anc.name) or 'repo'}:{h8}", "")

        pkg_json = anc / "package.json"
        if "package-json" not in signals and pkg_json.is_file():
            try:
                pdata = json.loads(decode_raw_bytes(pkg_json.read_bytes()))
            except (OSError, json.JSONDecodeError):
                pdata = None
            if isinstance(pdata, dict):
                raw_repo = pdata.get("repository")
                repo_str = (
                    raw_repo
                    if isinstance(raw_repo, str)
                    else (str(raw_repo.get("url") or "") if isinstance(raw_repo, dict) else "")
                )
                if repo_str.strip():
                    repo_key, canonical_url = normalize_repo_source(repo_str)
                    if repo_key:
                        signals["package-json"] = (repo_key, canonical_url)

    return signals
