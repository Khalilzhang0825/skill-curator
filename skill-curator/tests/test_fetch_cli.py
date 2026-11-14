from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "skill-curator"
    / "scripts"
)
CURATOR_SCRIPT = SCRIPTS_DIR / "curator.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class TestCuratorFetchCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.dest_dir = self.root / "staged"
        self.dest_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        from common import rmtree_safe

        rmtree_safe(self.root, ignore_errors=True)
        try:
            self._tmpdir.cleanup()
        except OSError:
            rmtree_safe(self.root, ignore_errors=True)

    def run_cli(
        self,
        args: list[str],
        env_overrides: dict[str, str] | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("SKILL_CURATOR_CONFIG", None)
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [sys.executable, str(CURATOR_SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )

    def _git(self, cwd: Path, *args: str) -> None:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Test Author",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test Author",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            }
        )
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, msg=f"git {args} failed: {proc.stderr}")

    def create_bare_repo(
        self,
        name: str,
        files: dict[str, str],
        symlinks: dict[str, str] | None = None,
        branch: str = "main",
        extra_branches: dict[str, dict[str, str]] | None = None,
    ) -> Path:
        work = self.root / f"work-{name}"
        bare = self.root / f"{name}.git"
        work.mkdir(parents=True, exist_ok=True)
        self._git(work, "init", "-b", branch)

        for rel, content in files.items():
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        if symlinks:
            for rel, link_target in symlinks.items():
                target = work / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.symlink(link_target, target)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 1314:
                        self.skipTest("Windows symlink creation requires Developer Mode or Administrator privileges")
                    raise

        self._git(work, "add", "-A")
        self._git(work, "commit", "-m", "initial")

        if extra_branches:
            for br_name, br_files in extra_branches.items():
                self._git(work, "checkout", "-b", br_name)
                for rel, content in br_files.items():
                    target = work / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                self._git(work, "add", "-A")
                self._git(work, "commit", "-m", f"branch {br_name}")
            self._git(work, "checkout", branch)

        subprocess.run(
            ["git", "clone", "--bare", str(work), str(bare)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return bare

    def test_fetch_single_skill_bare_git_repo(self) -> None:
        bare = self.create_bare_repo(
            "root-skill",
            files={
                "SKILL.md": (
                    "---\n"
                    "description: Root-level single skill.\n"
                    "---\n\n"
                    "# Root Skill\n"
                ),
                "references/notes.md": "Helpful notes.\n",
            },
        )
        proc = self.run_cli(
            ["fetch", str(bare), "--dest-dir", str(self.dest_dir)]
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "fetch")
        self.assertEqual(payload["status"], "fetched")
        self.assertFalse(payload["requires_selection"])
        self.assertEqual(payload["skill"]["name"], "root-skill")

        staged = Path(payload["skill"]["staged_dir"])
        self.assertEqual(payload["staged_dir"], payload["skill"]["staged_dir"])
        self.assertEqual(staged.resolve(), (self.dest_dir / "root-skill").resolve())
        self.assertTrue((staged / "SKILL.md").is_file())
        self.assertTrue((staged / "references" / "notes.md").is_file())
        self.assertFalse((staged / ".git").exists())

    def test_fetch_multi_skill_bare_git_repo_returns_inventory_then_selects(self) -> None:
        bare = self.create_bare_repo(
            "multi-skills",
            files={
                "skills/alpha-skill/SKILL.md": (
                    "---\nname: alpha-skill\ndescription: Alpha helper.\n---\n# Alpha\n"
                ),
                "skills/beta-skill/SKILL.md": (
                    "---\nname: beta-skill\ndescription: Beta helper.\n---\n# Beta\n"
                ),
                "skills/beta-skill/extra.txt": "beta extra",
            },
        )

        # Without --skill: returns inventory and does NOT stage files.
        inv_proc = self.run_cli(
            ["fetch", bare.as_uri(), "--dest-dir", str(self.dest_dir)]
        )
        self.assertEqual(inv_proc.returncode, 0, msg=inv_proc.stderr)
        inv = json.loads(inv_proc.stdout)
        self.assertTrue(inv["ok"])
        self.assertEqual(inv["status"], "multiple_skills_found")
        self.assertTrue(inv["requires_selection"])
        names = {item["name"]: item for item in inv["skills"]}
        self.assertEqual(set(names.keys()), {"alpha-skill", "beta-skill"})
        self.assertEqual(names["alpha-skill"]["rel_path"], "skills/alpha-skill")
        self.assertEqual(names["beta-skill"]["description"], "Beta helper.")
        self.assertEqual(list(self.dest_dir.iterdir()), [])

        # With --skill beta-skill: stages only beta-skill.
        sel_proc = self.run_cli(
            [
                "fetch",
                bare.as_uri(),
                "--skill",
                "beta-skill",
                "--dest-dir",
                str(self.dest_dir),
            ]
        )
        self.assertEqual(sel_proc.returncode, 0, msg=sel_proc.stderr)
        sel = json.loads(sel_proc.stdout)
        self.assertEqual(sel["status"], "fetched")
        self.assertEqual(sel["skill"]["name"], "beta-skill")
        self.assertTrue((self.dest_dir / "beta-skill" / "extra.txt").is_file())
        self.assertFalse((self.dest_dir / "alpha-skill").exists())

        # Also verify selection by rel_path.
        rel_dest = self.root / "staged-by-rel"
        rel_proc = self.run_cli(
            [
                "fetch",
                bare.as_uri(),
                "--skill",
                "skills/alpha-skill",
                "--dest-dir",
                str(rel_dest),
            ]
        )
        self.assertEqual(rel_proc.returncode, 0, msg=rel_proc.stderr)
        rel_out = json.loads(rel_proc.stdout)
        self.assertEqual(rel_out["skill"]["name"], "alpha-skill")
        self.assertTrue((rel_dest / "alpha-skill" / "SKILL.md").is_file())

        # With --all: stages every discovered skill in one clone.
        all_dest = self.root / "staged-all"
        all_proc = self.run_cli(
            [
                "fetch",
                bare.as_uri(),
                "--all",
                "--dest-dir",
                str(all_dest),
            ]
        )
        self.assertEqual(all_proc.returncode, 0, msg=all_proc.stderr)
        all_out = json.loads(all_proc.stdout)
        self.assertEqual(all_out["status"], "fetched")
        self.assertFalse(all_out["requires_selection"])
        self.assertEqual(
            [s["name"] for s in all_out["skills"]],
            ["alpha-skill", "beta-skill"],
        )
        self.assertTrue((all_dest / "alpha-skill" / "SKILL.md").is_file())
        self.assertTrue((all_dest / "beta-skill" / "extra.txt").is_file())

        # Combining --all and --skill is rejected cleanly.
        bad_combo = self.run_cli(
            ["fetch", bare.as_uri(), "--all", "--skill", "alpha-skill"]
        )
        self.assertNotEqual(bad_combo.returncode, 0)

    def test_fetch_subpath_ref_and_github_tree_url_parsing(self) -> None:
        acme_dir = self.root / "acme"
        acme_dir.mkdir(parents=True, exist_ok=True)
        bare = self.create_bare_repo(
            "agent-skills",
            files={
                "skills/old-skill/SKILL.md": (
                    "---\nname: old-skill\ndescription: Main branch skill.\n---\n"
                )
            },
            extra_branches={
                "feature-v2": {
                    "packs/frontend/ui-polish/SKILL.md": (
                        "---\nname: ui-polish\ndescription: Polish UI layouts.\n---\n"
                    ),
                    "packs/backend/db-tune/SKILL.md": (
                        "---\nname: db-tune\ndescription: Tune queries.\n---\n"
                    ),
                }
            },
        )
        # Move bare repo under <root>/acme/agent-skills.git so file://<root> acts as GitHub base URL.
        target_bare = acme_dir / "agent-skills.git"
        bare.rename(target_bare)

        # 1) Fetch using --ref and --subpath directly against bare repo.
        proc = self.run_cli(
            [
                "fetch",
                str(target_bare),
                "--ref",
                "feature-v2",
                "--subpath",
                "packs/frontend",
                "--dest-dir",
                str(self.dest_dir),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "fetched")
        self.assertEqual(payload["skill"]["name"], "ui-polish")
        self.assertTrue((self.dest_dir / "ui-polish" / "SKILL.md").is_file())
        self.assertFalse((self.dest_dir / "db-tune").exists())

        # 2) Fetch via GitHub /tree/<ref>/<subpath> URL using SKILL_CURATOR_GITHUB_BASE_URL in subprocess.
        tree_dest = self.root / "staged-tree"
        tree_proc = self.run_cli(
            [
                "fetch",
                "https://github.com/acme/agent-skills/tree/feature-v2/packs/backend",
                "--dest-dir",
                str(tree_dest),
            ],
            env_overrides={"SKILL_CURATOR_GITHUB_BASE_URL": self.root.as_uri()},
        )
        self.assertEqual(tree_proc.returncode, 0, msg=tree_proc.stderr)
        tree_out = json.loads(tree_proc.stdout)
        self.assertEqual(tree_out["status"], "fetched")
        self.assertEqual(tree_out["skill"]["name"], "db-tune")
        self.assertTrue((tree_dest / "db-tune" / "SKILL.md").is_file())
        self.assertFalse((tree_dest / "ui-polish").exists())

    def test_fetch_normalizes_uppercase_and_non_kebab_names(self) -> None:
        # 1) Repo named Claude-Skills with omitted frontmatter name -> normalized slug claude-skills.
        bare_upper = self.create_bare_repo(
            "Claude-Skills",
            files={
                "SKILL.md": (
                    "---\n"
                    "description: Uppercase repo name fallback.\n"
                    "---\n# Claude Skills\n"
                )
            },
        )
        proc_upper = self.run_cli(["fetch", str(bare_upper), "--dest-dir", str(self.dest_dir)])
        self.assertEqual(proc_upper.returncode, 0, msg=proc_upper.stderr)
        out_upper = json.loads(proc_upper.stdout)
        self.assertEqual(out_upper["skill"]["name"], "claude-skills")
        self.assertTrue((self.dest_dir / "claude-skills" / "SKILL.md").is_file())

        # 2) Non-kebab frontmatter name (Bad_Name) -> fetch stages into bad-name without crashing.
        bare_bad = self.create_bare_repo(
            "bad-repo",
            files={
                "SKILL.md": (
                    "---\n"
                    "name: Bad_Name\n"
                    "description: Skill with non-kebab frontmatter name.\n"
                    "---\n# Bad\n"
                )
            },
        )
        proc_bad = self.run_cli(["fetch", str(bare_bad), "--dest-dir", str(self.dest_dir)])
        self.assertEqual(proc_bad.returncode, 0, msg=proc_bad.stderr)
        out_bad = json.loads(proc_bad.stdout)
        self.assertEqual(out_bad["skill"]["name"], "bad-name")
        self.assertTrue((self.dest_dir / "bad-name" / "SKILL.md").is_file())

    def test_fetch_single_file_blob_url_rewrite_and_curl_http_server(self) -> None:
        http_root = self.root / "http_srv"
        skill_file = http_root / "acme" / "repo" / "main" / "skills" / "http-skill" / "SKILL.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(
            "---\n"
            "description: Fetched over HTTP curl.\n"
            "---\n\n"
            "# HTTP Skill\n",
            encoding="utf-8",
        )

        handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(http_root), **kwargs)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # Test 1: GitHub /blob/ URL rewritten via SKILL_CURATOR_RAW_BASE_URL -> real curl fetch.
            blob_url = "https://github.com/acme/repo/blob/main/skills/http-skill/SKILL.md"
            proc_blob = self.run_cli(
                ["fetch", blob_url, "--dest-dir", str(self.dest_dir)],
                env_overrides={"SKILL_CURATOR_RAW_BASE_URL": f"http://127.0.0.1:{port}"},
            )
            self.assertEqual(proc_blob.returncode, 0, msg=proc_blob.stderr)
            out_blob = json.loads(proc_blob.stdout)
            self.assertEqual(out_blob["status"], "fetched")
            self.assertEqual(out_blob["skill"]["name"], "http-skill")
            self.assertEqual(
                out_blob["raw_url"],
                f"http://127.0.0.1:{port}/acme/repo/main/skills/http-skill/SKILL.md",
            )
            self.assertTrue((self.dest_dir / "http-skill" / "SKILL.md").is_file())

            # Test 2: Direct raw HTTP URL with omitted --dest-dir creates temp staging dir.
            direct_url = f"http://127.0.0.1:{port}/acme/repo/main/skills/http-skill/SKILL.md"
            proc_direct = self.run_cli(["fetch", direct_url])
            self.assertEqual(proc_direct.returncode, 0, msg=proc_direct.stderr)
            out_direct = json.loads(proc_direct.stdout)
            staged_auto = Path(out_direct["skill"]["staged_dir"])
            try:
                self.assertTrue((staged_auto / "SKILL.md").is_file())
            finally:
                import shutil

                shutil.rmtree(staged_auto.parent, ignore_errors=True)
        finally:
            server.shutdown()
            server.server_close()

    def test_fetch_timeout_fails_fast_on_hanging_socket(self) -> None:
        # Listen on TCP socket and hold accepted connections open without sending bytes.
        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv_sock.bind(("127.0.0.1", 0))
        srv_sock.listen(4)
        port = srv_sock.getsockname()[1]
        held: list[socket.socket] = []
        stop_flag = threading.Event()

        def accept_loop() -> None:
            srv_sock.settimeout(0.2)
            while not stop_flag.is_set():
                try:
                    conn, _ = srv_sock.accept()
                    held.append(conn)
                except socket.timeout:
                    continue
                except OSError:
                    break

        t = threading.Thread(target=accept_loop, daemon=True)
        t.start()
        try:
            start = time.monotonic()
            proc_http = self.run_cli(
                [
                    "fetch",
                    f"http://127.0.0.1:{port}/hang/SKILL.md",
                    "--timeout",
                    "1",
                    "--dest-dir",
                    str(self.dest_dir),
                ],
                timeout=6,
            )
            elapsed = time.monotonic() - start
            self.assertNotEqual(proc_http.returncode, 0)
            self.assertLess(elapsed, 4.5)
            self.assertIn("timed out", (proc_http.stderr + proc_http.stdout).lower())

            start_git = time.monotonic()
            proc_git = self.run_cli(
                [
                    "fetch",
                    f"http://127.0.0.1:{port}/acme/hang-repo.git",
                    "--timeout",
                    "1",
                    "--dest-dir",
                    str(self.dest_dir),
                ],
                timeout=6,
            )
            elapsed_git = time.monotonic() - start_git
            self.assertNotEqual(proc_git.returncode, 0)
            self.assertLess(elapsed_git, 4.5)
            self.assertIn("timed out", (proc_git.stderr + proc_git.stdout).lower())
        finally:
            stop_flag.set()
            for c in held:
                try:
                    c.close()
                except OSError:
                    pass
            srv_sock.close()

    def test_fetch_rejects_symlink_escaping_skill_dir(self) -> None:
        outside_file = self.root / "host-secret.txt"
        outside_file.write_text("secret", encoding="utf-8")

        # 1) Symlink pointing outside the entire repository.
        bare_out = self.create_bare_repo(
            "escape-outside",
            files={
                "SKILL.md": "---\nname: escape-outside\ndescription: Bad skill.\n---\n",
            },
            symlinks={"leak.txt": str(outside_file)},
        )
        proc_out = self.run_cli(["fetch", str(bare_out), "--dest-dir", str(self.dest_dir)])
        self.assertNotEqual(proc_out.returncode, 0)
        self.assertIn("outside", (proc_out.stderr + proc_out.stdout).lower())
        self.assertFalse((self.dest_dir / "escape-outside").exists())

        # 2) Symlink pointing inside the same monorepo, but escaping the skill directory.
        bare_mono = self.create_bare_repo(
            "escape-monorepo",
            files={
                "other-folder/secret.txt": "monorepo-secret",
                "skills/sneaky/SKILL.md": (
                    "---\nname: sneaky\ndescription: Escapes to sibling.\n---\n"
                ),
            },
            symlinks={"skills/sneaky/sibling-leak.txt": "../../other-folder/secret.txt"},
        )
        proc_mono = self.run_cli(["fetch", str(bare_mono), "--dest-dir", str(self.dest_dir)])
        self.assertNotEqual(proc_mono.returncode, 0)
        self.assertIn("outside", (proc_mono.stderr + proc_mono.stdout).lower())
        self.assertFalse((self.dest_dir / "sneaky").exists())

    def test_fetch_missing_skill_md_or_unknown_selection_fails(self) -> None:
        bare_empty = self.create_bare_repo("no-skills", files={"README.md": "Just a readme."})
        proc_empty = self.run_cli(["fetch", str(bare_empty), "--dest-dir", str(self.dest_dir)])
        self.assertNotEqual(proc_empty.returncode, 0)
        self.assertIn("skill.md", (proc_empty.stderr + proc_empty.stdout).lower())

        bare_multi = self.create_bare_repo(
            "two-skills",
            files={
                "skills/one/SKILL.md": "---\nname: one\ndescription: One.\n---\n",
                "skills/two/SKILL.md": "---\nname: two\ndescription: Two.\n---\n",
            },
        )
        proc_unknown = self.run_cli(
            [
                "fetch",
                str(bare_multi),
                "--skill",
                "does-not-exist",
                "--dest-dir",
                str(self.dest_dir),
            ]
        )
        self.assertNotEqual(proc_unknown.returncode, 0)
        self.assertIn("does-not-exist", (proc_unknown.stderr + proc_unknown.stdout).lower())


if __name__ == "__main__":
    unittest.main()
