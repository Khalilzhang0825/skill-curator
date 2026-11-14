#!/usr/bin/env python3
"""Build clean, reproducible distributable archives for Skill Curator."""

from __future__ import annotations

import hashlib
import json
import stat
import tarfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "skill-curator"
DIST_DIR = REPO_ROOT / "dist"
FIXED_MTIME = 1700000000  # Reproducible archive timestamps

EXCLUDE_PARTS = {"tests", "__pycache__", ".git", ".pytest_cache"}
EXCLUDE_NAMES = {".DS_Store"}
EXECUTABLE_SCRIPTS = {"curator.py", "hook_intercept.py", "run-hook.cmd", "curator.cmd"}


def collect_plugin_files(plugin_root: Path) -> list[Path]:
    """Return sorted runtime plugin files excluding tests and bytecode caches."""
    files: list[Path] = []
    for path in sorted(plugin_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(plugin_root)
        if any(part in EXCLUDE_PARTS for part in rel.parts):
            continue
        if path.name in EXCLUDE_NAMES or path.suffix in (".pyc", ".pyo"):
            continue
        files.append(path)
    return files


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archives() -> list[Path]:
    """Create .tar.gz, .zip, and SHA256SUMS.txt under dist/."""
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "0.1.0")
    stem = f"skill-curator-v{version}"

    files = collect_plugin_files(PLUGIN_DIR)
    if not files:
        raise RuntimeError(f"No plugin files found in {PLUGIN_DIR}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    for old_archive in DIST_DIR.glob("skill-curator-v*"):
        if old_archive.is_file():
            old_archive.unlink()
    tar_path = DIST_DIR / f"{stem}.tar.gz"
    zip_path = DIST_DIR / f"{stem}.zip"
    sums_path = DIST_DIR / "SHA256SUMS.txt"

    with tarfile.open(tar_path, "w:gz") as tf:
        for fpath in files:
            rel = fpath.relative_to(PLUGIN_DIR).as_posix()
            arcname = f"skill-curator/{rel}"
            info = tf.gettarinfo(str(fpath), arcname=arcname)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = FIXED_MTIME
            info.mode = 0o755 if fpath.name in EXECUTABLE_SCRIPTS else 0o644
            with fpath.open("rb") as fh:
                tf.addfile(info, fh)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fpath in files:
            rel = fpath.relative_to(PLUGIN_DIR).as_posix()
            arcname = f"skill-curator/{rel}"
            zinfo = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
            mode = 0o755 if fpath.name in EXECUTABLE_SCRIPTS else 0o644
            zinfo.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(zinfo, fpath.read_bytes())

    lines = [f"{sha256_file(p)}  {p.name}" for p in (tar_path, zip_path)]
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return [tar_path, zip_path, sums_path]


def main() -> int:
    outputs = build_archives()
    for out in outputs:
        print(f"{out.relative_to(REPO_ROOT)} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
