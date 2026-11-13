from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CURATOR_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "skill-curator"
    / "scripts"
    / "curator.py"
)


class TestCuratorCliVaultAndMount(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.vault_dir = self.root / "vault"
        self.global_dir_1 = self.root / "global_one"
        self.global_dir_2 = self.root / "global_two"
        self.project_dir = self.root / "project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.vault_dir / "config.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def run_cli(
        self, args: list[str], env_overrides: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("SKILL_CURATOR_CONFIG", None)
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [sys.executable, str(CURATOR_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def create_sample_skill(
        self,
        parent_dir: Path,
        slug: str = "code-reviewer",
        description: str = "Reviews code for correctness and style.",
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        skill_dir = parent_dir / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = (
            "---\n"
            f"name: {slug}\n"
            f'description: "{description}"\n'
            "---\n\n"
            f"# {slug}\n\n"
            "Instructions body.\n"
        )
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        if extra_files:
            for rel, content in extra_files.items():
                target = skill_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        return skill_dir

    def init_vault(self, claude_sync: str = "auto") -> None:
        proc = self.run_cli(
            [
                "init",
                "--vault",
                str(self.vault_dir),
                "--global-dir",
                str(self.global_dir_1),
                "--global-dir",
                str(self.global_dir_2),
                "--global-cap",
                "8",
                "--claude-sync",
                claude_sync,
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")

    def test_init_creates_vault_and_is_idempotent(self) -> None:
        proc1 = self.run_cli(
            [
                "init",
                "--vault",
                str(self.vault_dir),
                "--global-dir",
                str(self.global_dir_1),
                "--global-cap",
                "6",
                "--claude-sync",
                "auto",
            ]
        )
        self.assertEqual(proc1.returncode, 0, msg=proc1.stderr)
        payload1 = json.loads(proc1.stdout)
        self.assertTrue(payload1["ok"])

        self.assertTrue((self.vault_dir / "skills").is_dir())
        self.assertTrue(self.config_path.is_file())
        index_path = self.vault_dir / "INDEX.md"
        self.assertTrue(index_path.is_file())

        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(Path(cfg["vault_path"]).resolve(), self.vault_dir.resolve())
        self.assertEqual(cfg["global_core_dirs"], [str(self.global_dir_1)])
        self.assertEqual(cfg["global_cap"], 6)
        self.assertEqual(cfg["claude_sync"], "auto")
        self.assertEqual(cfg["stale_days"], 30)

        index_text = index_path.read_text(encoding="utf-8")
        self.assertIn("| Name | Keywords | Scene Tags | Source | Path |", index_text)

        # Append a sentinel row and re-run init to verify idempotence.
        index_path.write_text(
            index_text.rstrip("\n")
            + "\n| existing-skill | test | core | https://example.com | skills/existing-skill |\n",
            encoding="utf-8",
        )
        proc2 = self.run_cli(["init", "--vault", str(self.vault_dir)])
        self.assertEqual(proc2.returncode, 0, msg=proc2.stderr)
        payload2 = json.loads(proc2.stdout)
        self.assertTrue(payload2["ok"])

        cfg_after = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg_after["global_cap"], 6)
        self.assertEqual(cfg_after["global_core_dirs"], [str(self.global_dir_1)])
        self.assertIn("existing-skill", index_path.read_text(encoding="utf-8"))

    def test_store_parses_frontmatter_writes_meta_and_upserts_index(self) -> None:
        self.init_vault()
        src_skill = self.create_sample_skill(
            self.root / "incoming",
            slug="python-linter",
            description="Lint Python modules with strict rules.",
            extra_files={"references/checklist.md": "- Check types\n"},
        )

        proc1 = self.run_cli(
            [
                "store",
                "--source-dir",
                str(src_skill),
                "--keywords",
                "lint,python",
                "--tags",
                "python,ci",
                "--source-url",
                "https://github.com/example/python-linter",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc1.returncode, 0, msg=proc1.stderr)
        out1 = json.loads(proc1.stdout)
        self.assertTrue(out1["ok"])

        vault_skill = self.vault_dir / "skills" / "python-linter"
        self.assertTrue((vault_skill / "SKILL.md").is_file())
        self.assertTrue((vault_skill / "references" / "checklist.md").is_file())

        meta = json.loads((vault_skill / ".curator-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["name"], "python-linter")
        self.assertEqual(meta["description"], "Lint Python modules with strict rules.")
        self.assertEqual(meta["keywords"], ["lint", "python"])
        self.assertEqual(meta["scene_tags"], ["python", "ci"])
        self.assertEqual(meta["source_url"], "https://github.com/example/python-linter")
        self.assertTrue(meta["stored_at"].endswith("Z"))

        index_text_1 = (self.vault_dir / "INDEX.md").read_text(encoding="utf-8")
        rows_1 = [line for line in index_text_1.splitlines() if line.startswith("| python-linter |")]
        self.assertEqual(len(rows_1), 1)
        self.assertIn("python, ci", rows_1[0])

        # Re-store with updated tags and keywords; INDEX.md must upsert without duplicating.
        proc2 = self.run_cli(
            [
                "store",
                "--source-dir",
                str(src_skill),
                "--keywords",
                "lint,style",
                "--tags",
                "python,quality",
                "--source-url",
                "https://github.com/example/python-linter",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc2.returncode, 0, msg=proc2.stderr)
        index_text_2 = (self.vault_dir / "INDEX.md").read_text(encoding="utf-8")
        rows_2 = [line for line in index_text_2.splitlines() if line.startswith("| python-linter |")]
        self.assertEqual(len(rows_2), 1)
        self.assertIn("python, quality", rows_2[0])
        self.assertIn("lint, style", rows_2[0])

    def test_store_supports_multiline_yaml_description(self) -> None:
        self.init_vault()
        skill_dir = self.root / "incoming" / "multi-line"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: multi-line\n"
            "description: >\n"
            "  First line of description\n"
            "  and second line.\n"
            "---\n\n"
            "# Multi-line\n",
            encoding="utf-8",
        )
        proc = self.run_cli(
            [
                "store",
                "--source-dir",
                str(skill_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        meta = json.loads(
            (self.vault_dir / "skills" / "multi-line" / ".curator-meta.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(meta["description"], "First line of description and second line.")

    def test_store_falls_back_to_source_dir_name_when_frontmatter_name_omitted(self) -> None:
        self.init_vault()
        skill_dir = self.root / "incoming" / "folder-named-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "description: Skill without explicit frontmatter name.\n"
            "---\n\n"
            "# Folder Named Skill\n",
            encoding="utf-8",
        )
        proc = self.run_cli(
            [
                "store",
                "--source-dir",
                str(skill_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["name"], "folder-named-skill")
        self.assertTrue((self.vault_dir / "skills" / "folder-named-skill" / "SKILL.md").is_file())

    def test_init_rejects_invalid_global_cap_and_stale_days(self) -> None:
        proc_cap = self.run_cli(
            ["init", "--vault", str(self.vault_dir), "--global-cap", "0"]
        )
        self.assertNotEqual(proc_cap.returncode, 0)
        self.assertIn("global_cap", proc_cap.stderr + proc_cap.stdout)

        proc_stale = self.run_cli(
            ["init", "--vault", str(self.vault_dir), "--stale-days", "0"]
        )
        self.assertNotEqual(proc_stale.returncode, 0)
        self.assertIn("stale_days", proc_stale.stderr + proc_stale.stdout)

    def test_store_index_upsert_preserves_escaped_pipes_in_cells(self) -> None:
        self.init_vault()
        skill_a = self.create_sample_skill(self.root / "incoming", slug="pipe-skill")
        skill_b = self.create_sample_skill(self.root / "incoming", slug="other-skill")

        proc_a1 = self.run_cli(
            [
                "store",
                "--source-dir",
                str(skill_a),
                "--keywords",
                "a|b,c",
                "--tags",
                "x|y",
                "--source-url",
                "https://example.com/?q=1|2",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_a1.returncode, 0, msg=proc_a1.stderr)
        self.assertEqual(
            self.run_cli(
                ["store", "--source-dir", str(skill_b), "--config", str(self.config_path)]
            ).returncode,
            0,
        )

        # Update pipe-skill; must replace its row cleanly without corrupting columns or other-skill.
        proc_a2 = self.run_cli(
            [
                "store",
                "--source-dir",
                str(skill_a),
                "--keywords",
                "a|b,updated",
                "--tags",
                "x|y,z",
                "--source-url",
                "https://example.com/?q=1|2",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_a2.returncode, 0, msg=proc_a2.stderr)

        index_lines = (self.vault_dir / "INDEX.md").read_text(encoding="utf-8").splitlines()
        pipe_rows = [ln for ln in index_lines if ln.startswith("| pipe-skill |")]
        other_rows = [ln for ln in index_lines if ln.startswith("| other-skill |")]
        self.assertEqual(len(pipe_rows), 1)
        self.assertEqual(len(other_rows), 1)
        self.assertIn("a\\|b, updated", pipe_rows[0])
        self.assertIn("x\\|y, z", pipe_rows[0])

    def test_store_rejects_missing_skill_md(self) -> None:
        self.init_vault()
        empty_dir = self.root / "empty-skill"
        empty_dir.mkdir(parents=True, exist_ok=True)
        (empty_dir / "README.md").write_text("Not a skill", encoding="utf-8")

        proc = self.run_cli(
            [
                "store",
                "--source-dir",
                str(empty_dir),
                "--name",
                "empty-skill",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SKILL.md", proc.stderr + proc.stdout)
        self.assertFalse((self.vault_dir / "skills" / "empty-skill").exists())

    def test_store_rejects_invalid_kebab_case_name(self) -> None:
        self.init_vault()
        bad_dir = self.create_sample_skill(self.root / "incoming", slug="Bad_Name")
        proc = self.run_cli(
            [
                "store",
                "--source-dir",
                str(bad_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.vault_dir / "skills" / "Bad_Name").exists())

    def test_store_rejects_symlink_escaping_source_dir(self) -> None:
        self.init_vault()
        secret_file = self.root / "secret.txt"
        secret_file.write_text("top-secret", encoding="utf-8")

        src_skill = self.create_sample_skill(self.root / "incoming", slug="sneaky-skill")
        os.symlink(secret_file, src_skill / "leaked.txt")

        proc = self.run_cli(
            [
                "store",
                "--source-dir",
                str(src_skill),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.vault_dir / "skills" / "sneaky-skill").exists())

        outside_dir = self.root / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "data.txt").write_text("secret-dir", encoding="utf-8")
        dir_escape_skill = self.create_sample_skill(self.root / "incoming", slug="dir-escape")
        os.symlink(outside_dir, dir_escape_skill / "refs")

        proc_dir = self.run_cli(
            [
                "store",
                "--source-dir",
                str(dir_escape_skill),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc_dir.returncode, 0)
        self.assertFalse((self.vault_dir / "skills" / "dir-escape").exists())

    def test_mount_project_copies_with_isolation_and_metadata(self) -> None:
        self.init_vault()
        src_skill = self.create_sample_skill(self.root / "incoming", slug="isolated-skill")
        self.assertEqual(
            self.run_cli(
                [
                    "store",
                    "--source-dir",
                    str(src_skill),
                    "--tags",
                    "backend,api",
                    "--source-url",
                    "https://github.com/example/isolated",
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )

        mount_proc = self.run_cli(
            [
                "mount",
                "isolated-skill",
                "--target",
                "project",
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(mount_proc.returncode, 0, msg=mount_proc.stderr)
        out = json.loads(mount_proc.stdout)
        self.assertTrue(out["ok"])

        mounted_dir = self.project_dir / ".agents" / "skills" / "isolated-skill"
        mounted_skill_md = mounted_dir / "SKILL.md"
        self.assertTrue(mounted_skill_md.is_file())
        self.assertFalse(mounted_dir.is_symlink())
        self.assertFalse(mounted_skill_md.is_symlink())

        mounted_meta = json.loads((mounted_dir / ".curator-meta.json").read_text(encoding="utf-8"))
        self.assertTrue(mounted_meta["mounted_at"].endswith("Z"))
        self.assertEqual(
            Path(mounted_meta["mounted_from_vault"]).resolve(),
            (self.vault_dir / "skills" / "isolated-skill").resolve(),
        )
        self.assertEqual(mounted_meta["source_url"], "https://github.com/example/isolated")
        self.assertEqual(mounted_meta["scene_tags"], ["backend", "api"])

        # Mutate mounted file and verify Vault copy remains untouched.
        vault_skill_md = self.vault_dir / "skills" / "isolated-skill" / "SKILL.md"
        original_vault_text = vault_skill_md.read_text(encoding="utf-8")
        mounted_skill_md.write_text("MUTATED LOCALLY", encoding="utf-8")
        self.assertEqual(vault_skill_md.read_text(encoding="utf-8"), original_vault_text)

    def test_mount_project_claude_sync_auto_always_never(self) -> None:
        self.init_vault(claude_sync="auto")
        src_skill = self.create_sample_skill(self.root / "incoming", slug="sync-skill")
        self.assertEqual(
            self.run_cli(
                [
                    "store",
                    "--source-dir",
                    str(src_skill),
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )

        # 1) auto without .claude or CLAUDE.md -> no mirror
        proj_plain = self.root / "proj-plain"
        proj_plain.mkdir()
        proc_plain = self.run_cli(
            [
                "mount",
                "sync-skill",
                "--target",
                "project",
                "--project-dir",
                str(proj_plain),
                "--claude-sync",
                "auto",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_plain.returncode, 0, msg=proc_plain.stderr)
        self.assertTrue((proj_plain / ".agents" / "skills" / "sync-skill" / "SKILL.md").is_file())
        self.assertFalse((proj_plain / ".claude" / "skills" / "sync-skill").exists())

        # 2) auto with CLAUDE.md -> mirrors to .claude/skills/
        proj_claude_md = self.root / "proj-claude-md"
        proj_claude_md.mkdir()
        (proj_claude_md / "CLAUDE.md").write_text("# Claude rules\n", encoding="utf-8")
        proc_claude_md = self.run_cli(
            [
                "mount",
                "sync-skill",
                "--target",
                "project",
                "--project-dir",
                str(proj_claude_md),
                "--claude-sync",
                "auto",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_claude_md.returncode, 0, msg=proc_claude_md.stderr)
        self.assertTrue(
            (proj_claude_md / ".claude" / "skills" / "sync-skill" / "SKILL.md").is_file()
        )
        self.assertTrue(
            (proj_claude_md / ".claude" / "skills" / "sync-skill" / ".curator-meta.json").is_file()
        )

        # 2b) auto with .claude dir (without CLAUDE.md) -> mirrors to .claude/skills/
        proj_claude_dir = self.root / "proj-claude-dir"
        (proj_claude_dir / ".claude").mkdir(parents=True)
        proc_claude_dir = self.run_cli(
            [
                "mount",
                "sync-skill",
                "--target",
                "project",
                "--project-dir",
                str(proj_claude_dir),
                "--claude-sync",
                "auto",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_claude_dir.returncode, 0, msg=proc_claude_dir.stderr)
        self.assertTrue(
            (proj_claude_dir / ".claude" / "skills" / "sync-skill" / "SKILL.md").is_file()
        )

        # 3) always without .claude or CLAUDE.md -> mirrors to .claude/skills/
        proj_always = self.root / "proj-always"
        proj_always.mkdir()
        proc_always = self.run_cli(
            [
                "mount",
                "sync-skill",
                "--target",
                "project",
                "--project-dir",
                str(proj_always),
                "--claude-sync",
                "always",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_always.returncode, 0, msg=proc_always.stderr)
        self.assertTrue((proj_always / ".claude" / "skills" / "sync-skill" / "SKILL.md").is_file())

        # 4) never even with .claude and CLAUDE.md -> no mirror
        proj_never = self.root / "proj-never"
        (proj_never / ".claude").mkdir(parents=True)
        (proj_never / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
        proc_never = self.run_cli(
            [
                "mount",
                "sync-skill",
                "--target",
                "project",
                "--project-dir",
                str(proj_never),
                "--claude-sync",
                "never",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_never.returncode, 0, msg=proc_never.stderr)
        self.assertTrue((proj_never / ".agents" / "skills" / "sync-skill" / "SKILL.md").is_file())
        self.assertFalse((proj_never / ".claude" / "skills" / "sync-skill").exists())

    def test_mount_global_copies_into_configured_global_dirs(self) -> None:
        self.init_vault()
        src_skill = self.create_sample_skill(self.root / "incoming", slug="global-helper")
        self.assertEqual(
            self.run_cli(
                [
                    "store",
                    "--source-dir",
                    str(src_skill),
                    "--tags",
                    "core",
                    "--source-url",
                    "https://github.com/example/global-helper",
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )

        proc = self.run_cli(
            [
                "mount",
                "global-helper",
                "--target",
                "global",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])

        for gdir in (self.global_dir_1, self.global_dir_2):
            dest = gdir / "global-helper"
            self.assertTrue((dest / "SKILL.md").is_file())
            self.assertFalse(dest.is_symlink())
            meta = json.loads((dest / ".curator-meta.json").read_text(encoding="utf-8"))
            self.assertTrue(meta["mounted_at"].endswith("Z"))
            self.assertEqual(meta["source_url"], "https://github.com/example/global-helper")
            self.assertEqual(meta["scene_tags"], ["core"])

    def test_mount_rejects_escaping_symlink_in_vault(self) -> None:
        self.init_vault()
        src_skill = self.create_sample_skill(self.root / "incoming", slug="tampered-skill")
        self.assertEqual(
            self.run_cli(
                [
                    "store",
                    "--source-dir",
                    str(src_skill),
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )

        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        os.symlink(outside, self.vault_dir / "skills" / "tampered-skill" / "escape.txt")

        proc = self.run_cli(
            [
                "mount",
                "tampered-skill",
                "--target",
                "project",
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.project_dir / ".agents" / "skills" / "tampered-skill").exists())

    def test_mount_missing_skill_fails(self) -> None:
        self.init_vault()
        proc = self.run_cli(
            [
                "mount",
                "non-existent",
                "--target",
                "project",
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not found", proc.stderr + proc.stdout)

    def test_config_via_env_var(self) -> None:
        self.init_vault()
        src_skill = self.create_sample_skill(self.root / "incoming", slug="env-skill")
        env = {"SKILL_CURATOR_CONFIG": str(self.config_path)}

        store_proc = self.run_cli(["store", "--source-dir", str(src_skill)], env_overrides=env)
        self.assertEqual(store_proc.returncode, 0, msg=store_proc.stderr)

        mount_proc = self.run_cli(
            ["mount", "env-skill", "--target", "project", "--project-dir", str(self.project_dir)],
            env_overrides=env,
        )
        self.assertEqual(mount_proc.returncode, 0, msg=mount_proc.stderr)
        self.assertTrue(
            (self.project_dir / ".agents" / "skills" / "env-skill" / "SKILL.md").is_file()
        )


if __name__ == "__main__":
    unittest.main()
