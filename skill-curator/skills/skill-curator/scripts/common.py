"""Shared validation, frontmatter parsing, and safe filesystem helpers for Skill Curator."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
INDEX_TITLE = "# Skill Vault Index"
INDEX_COLUMNS = "| Name | Keywords | Scene Tags | Source | Path |"
INDEX_SEPARATOR = "| --- | --- | --- | --- | --- |"
UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


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
    try:
        return os.path.commonpath([root_real, candidate_real]) == root_real
    except ValueError:
        return False


def validate_tree_containment(root_dir: Path) -> None:
    """Reject any symlink or path inside root_dir whose realpath escapes root_dir."""
    if not root_dir.exists() or not root_dir.is_dir():
        raise CuratorError(f"Directory does not exist: {root_dir}")

    root_real = os.path.realpath(str(root_dir))
    for dirpath, dirnames, filenames in os.walk(str(root_dir), followlinks=False):
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


def copy_tree_clean(src: Path, dst: Path) -> None:
    """Validate src containment and copy to dst without creating symlinks."""
    validate_tree_containment(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_parent = tempfile.mkdtemp(dir=str(dst.parent), prefix=f".tmp-{dst.name}-")
    try:
        staged = Path(tmp_parent) / dst.name
        shutil.copytree(str(src), str(staged), symlinks=False)
        if dst.exists():
            shutil.rmtree(str(dst))
        staged.rename(dst)
    finally:
        if os.path.exists(tmp_parent):
            shutil.rmtree(tmp_parent, ignore_errors=True)


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
