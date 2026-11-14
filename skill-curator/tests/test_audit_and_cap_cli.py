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


class TestCuratorAuditAndCapCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.vault_dir = self.root / "vault"
        self.global_dir_1 = self.root / "global_one"
        self.global_dir_2 = self.root / "global_two"
        self.project_dir = self.root / "project"
        self.global_dir_1.mkdir(parents=True, exist_ok=True)
        self.global_dir_2.mkdir(parents=True, exist_ok=True)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.vault_dir / "config.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def run_cli(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("SKILL_CURATOR_CONFIG", None)
        return subprocess.run(
            [sys.executable, str(CURATOR_SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=15,
        )

    def init_vault(self, global_cap: int = 8) -> None:
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
                str(global_cap),
                "--claude-sync",
                "auto",
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def create_skill_dir(
        self,
        parent: Path,
        folder_name: str,
        frontmatter_name: str | None = None,
        description: str = "A well-scoped skill for testing audit behavior.",
        body_lines: int = 5,
        extra_files: dict[str, tuple[str, bool]] | None = None,
    ) -> Path:
        skill_dir = parent / folder_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        fm_lines = ["---"]
        if frontmatter_name is not None:
            fm_lines.append(f"name: {frontmatter_name}")
        fm_lines.append(f'description: "{description}"')
        fm_lines.append("---")
        body = "\n".join(f"Line {i}" for i in range(1, body_lines + 1))
        (skill_dir / "SKILL.md").write_text(
            "\n".join(fm_lines) + "\n\n" + body + "\n", encoding="utf-8"
        )
        if extra_files:
            for rel, (content, executable) in extra_files.items():
                target = skill_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                if executable:
                    target.chmod(0o755)
        return skill_dir

    def test_inspect_clean_skill_preserves_source_bytes_and_recommends_project_or_vault(
        self,
    ) -> None:
        self.init_vault()
        src = self.create_skill_dir(
            self.root / "incoming",
            "clean-helper",
            frontmatter_name="clean-helper",
            description="Helps format SQL migrations cleanly and safely.",
        )
        before_bytes = (src / "SKILL.md").read_bytes()

        # Without --project-dir: auto recommends vault (never defaults blindly to global).
        proc_no_proj = self.run_cli(
            ["inspect", "--source-dir", str(src), "--config", str(self.config_path)]
        )
        self.assertEqual(proc_no_proj.returncode, 0, msg=proc_no_proj.stderr)
        out_no_proj = json.loads(proc_no_proj.stdout)
        self.assertTrue(out_no_proj["ok"])
        self.assertEqual(out_no_proj["action"], "inspect")
        self.assertEqual(out_no_proj["candidate_name"], "clean-helper")
        self.assertTrue(out_no_proj["name_check"]["valid"])
        self.assertEqual(out_no_proj["description_check"]["issues"], [])
        self.assertEqual(out_no_proj["instruction_check"]["issues"], [])
        self.assertEqual(out_no_proj["scripts_detected"], [])
        self.assertEqual(out_no_proj["exact_collisions"], [])
        self.assertEqual(out_no_proj["recommended_tier"], "vault")
        self.assertEqual(out_no_proj["warnings"], [])

        # With --project-dir: auto recommends project.
        proc_proj = self.run_cli(
            [
                "inspect",
                "--source-dir",
                str(src),
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_proj.returncode, 0, msg=proc_proj.stderr)
        out_proj = json.loads(proc_proj.stdout)
        self.assertEqual(out_proj["recommended_tier"], "project")
        self.assertEqual((src / "SKILL.md").read_bytes(), before_bytes)

    def test_inspect_flags_bloat_vague_description_bad_names_and_scripts(self) -> None:
        self.init_vault()
        # 1) Bloated SKILL.md (>500 lines), bloated description (>1024 chars), invalid name, executable scripts
        long_desc = "x" * 1050
        src_bloat = self.create_skill_dir(
            self.root / "incoming",
            "bad-name",
            frontmatter_name="Bad_Name",
            description=long_desc,
            body_lines=510,
            extra_files={
                "scripts/run.sh": ("#!/bin/sh\necho hi\n", True),
                "tools/check.py": ("print('ok')\n", False),
                "bin/runner": ("#!/usr/bin/env bash\necho run\n", False),
            },
        )
        proc = self.run_cli(
            ["inspect", "--source-dir", str(src_bloat), "--config", str(self.config_path)]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        self.assertFalse(out["name_check"]["valid"])
        self.assertIn("invalid_skill_name", out["name_check"]["issues"])
        self.assertEqual(out["candidate_name"], "bad-name")
        self.assertIn("description_bloat", out["description_check"]["issues"])
        self.assertGreater(out["description_check"]["char_count"], 1024)
        self.assertIn("instruction_bloat", out["instruction_check"]["issues"])
        self.assertGreater(out["instruction_check"]["line_count"], 500)
        self.assertEqual(
            out["scripts_detected"],
            ["bin/runner", "scripts/run.sh", "tools/check.py"],
        )
        self.assertIn("executable_scripts_present", out["warnings"])

        # 2) Omitted frontmatter name + vague description (<15 chars)
        src_vague = self.create_skill_dir(
            self.root / "incoming",
            "claude-skills",
            frontmatter_name=None,
            description="Too short",
        )
        proc_vague = self.run_cli(
            ["inspect", "--source-dir", str(src_vague), "--config", str(self.config_path)]
        )
        self.assertEqual(proc_vague.returncode, 0, msg=proc_vague.stderr)
        out_vague = json.loads(proc_vague.stdout)
        self.assertFalse(out_vague["name_check"]["valid"])
        self.assertIn("missing_frontmatter_name", out_vague["name_check"]["issues"])
        self.assertIn("missing_or_vague_description", out_vague["description_check"]["issues"])

    def test_inspect_detects_exact_collisions_and_keyword_overlaps_across_tiers(self) -> None:
        self.init_vault()

        # Global tier skill with same name as candidate.
        self.create_skill_dir(
            self.global_dir_1,
            "api-tester",
            frontmatter_name="api-tester",
            description="Runs integration tests for REST endpoints.",
        )
        # Project tier skill with overlapping keywords.
        self.create_skill_dir(
            self.project_dir / ".agents" / "skills",
            "rest-verifier",
            frontmatter_name="rest-verifier",
            description="Automates schema validation and fuzzing for GraphQL endpoints and payloads.",
        )
        # Vault tier skill stored via CLI with overlapping keywords.
        vault_src = self.create_skill_dir(
            self.root / "incoming",
            "graphql-helper",
            frontmatter_name="graphql-helper",
            description="Schema validation, query linting, and endpoint diagnostics for GraphQL.",
        )
        self.assertEqual(
            self.run_cli(
                [
                    "store",
                    "--source-dir",
                    str(vault_src),
                    "--keywords",
                    "graphql,schema,validation",
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )

        # Candidate collides by name with global 'api-tester' and overlaps keywords with project + vault.
        candidate = self.create_skill_dir(
            self.root / "incoming",
            "api-tester",
            frontmatter_name="api-tester",
            description="Schema validation and endpoint diagnostics for GraphQL payloads.",
        )
        proc = self.run_cli(
            [
                "inspect",
                "--source-dir",
                str(candidate),
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)

        collision_tiers = {(c["tier"], c["name"]) for c in out["exact_collisions"]}
        self.assertIn(("global", "api-tester"), collision_tiers)

        overlap_targets = {(o["tier"], o["name"]) for o in out["keyword_overlaps"]}
        self.assertIn(("project", "rest-verifier"), overlap_targets)
        self.assertIn(("vault", "graphql-helper"), overlap_targets)

    def test_global_core_occupancy_deduplicates_and_excludes_curator_and_plugins(self) -> None:
        self.init_vault(global_cap=3)

        # Skill 'alpha' mirrored in BOTH global_dir_1 and global_dir_2 -> counts as 1.
        self.create_skill_dir(self.global_dir_1, "alpha", frontmatter_name="alpha")
        self.create_skill_dir(self.global_dir_2, "alpha", frontmatter_name="alpha")
        # Skill 'beta' in global_dir_2 -> counts as 2 total.
        self.create_skill_dir(self.global_dir_2, "beta", frontmatter_name="beta")

        # Excluded: skill-curator itself.
        self.create_skill_dir(self.global_dir_1, "skill-curator", frontmatter_name="skill-curator")

        # Excluded: external plugin bundles (plugin.json / .claude-plugin/plugin.json / gemini-extension.json).
        p1 = self.create_skill_dir(self.global_dir_1, "ego-plugin", frontmatter_name="ego-plugin")
        (p1 / "plugin.json").write_text('{"name": "ego-plugin"}\n', encoding="utf-8")

        p2 = self.create_skill_dir(self.global_dir_2, "claude-bundle", frontmatter_name="claude-bundle")
        (p2 / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (p2 / ".claude-plugin" / "plugin.json").write_text('{"name": "claude-bundle"}\n', encoding="utf-8")

        p3 = self.create_skill_dir(self.global_dir_1, "gemini-ext", frontmatter_name="gemini-ext")
        (p3 / "gemini-extension.json").write_text('{"name": "gemini-ext"}\n', encoding="utf-8")

        candidate = self.create_skill_dir(
            self.root / "incoming", "gamma", frontmatter_name="gamma"
        )
        proc = self.run_cli(
            [
                "inspect",
                "--source-dir",
                str(candidate),
                "--desired-tier",
                "global",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["global_core"]["count"], 2)
        self.assertEqual(out["global_core"]["cap"], 3)
        self.assertFalse(out["global_core"]["at_cap"])
        self.assertEqual(out["global_core"]["skills"], ["alpha", "beta"])
        self.assertFalse(out["global_cap_blocked"])
        self.assertEqual(out["recommended_tier"], "global")

    def test_global_cap_gate_blocks_new_mount_allows_existing_update_and_force(self) -> None:
        self.init_vault(global_cap=2)

        # Store 3 skills in Vault.
        for slug in ("skill-one", "skill-two", "skill-three"):
            sdir = self.create_skill_dir(self.root / "incoming", slug, frontmatter_name=slug)
            self.assertEqual(
                self.run_cli(
                    ["store", "--source-dir", str(sdir), "--config", str(self.config_path)]
                ).returncode,
                0,
            )

        # Mount first two into global -> fills cap (2/2).
        self.assertEqual(
            self.run_cli(
                ["mount", "skill-one", "--target", "global", "--config", str(self.config_path)]
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                ["mount", "skill-two", "--target", "global", "--config", str(self.config_path)]
            ).returncode,
            0,
        )

        # inspect for skill-three with --desired-tier global must report cap blocked + options.
        cand = self.create_skill_dir(
            self.root / "incoming2", "skill-three", frontmatter_name="skill-three"
        )
        insp = self.run_cli(
            [
                "inspect",
                "--source-dir",
                str(cand),
                "--project-dir",
                str(self.project_dir),
                "--desired-tier",
                "global",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(insp.returncode, 0, msg=insp.stderr)
        insp_out = json.loads(insp.stdout)
        self.assertTrue(insp_out["global_cap_blocked"])
        self.assertEqual(insp_out["recommended_tier"], "project")
        self.assertEqual(insp_out["demote_candidates"], ["skill-one", "skill-two"])
        self.assertIn("demote_existing_global_to_vault", insp_out["cap_resolution_options"])

        # Mounting skill-three to global without --force must exit 1 with GLOBAL_CAP_REACHED.
        blocked = self.run_cli(
            ["mount", "skill-three", "--target", "global", "--config", str(self.config_path)]
        )
        self.assertNotEqual(blocked.returncode, 0)
        err_payload = json.loads(blocked.stderr)
        self.assertFalse(err_payload["ok"])
        self.assertEqual(err_payload["error_code"], "GLOBAL_CAP_REACHED")
        self.assertEqual(err_payload["global_count"], 2)
        self.assertEqual(err_payload["global_cap"], 2)
        self.assertEqual(err_payload["demote_candidates"], ["skill-one", "skill-two"])
        self.assertFalse((self.global_dir_1 / "skill-three").exists())

        # Re-mounting an already-present Global Core skill ('skill-one') at cap must succeed!
        remount = self.run_cli(
            ["mount", "skill-one", "--target", "global", "--config", str(self.config_path)]
        )
        self.assertEqual(remount.returncode, 0, msg=remount.stderr)

        # Mounting skill-three with --force must succeed.
        forced = self.run_cli(
            [
                "mount",
                "skill-three",
                "--target",
                "global",
                "--force",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(forced.returncode, 0, msg=forced.stderr)
        self.assertTrue((self.global_dir_1 / "skill-three" / "SKILL.md").is_file())

    def test_store_override_description_updates_vault_only_and_preserves_source(self) -> None:
        self.init_vault()
        src = self.create_skill_dir(
            self.root / "incoming",
            "vague-skill",
            frontmatter_name="vague-skill",
            description="Too short",
        )
        before_bytes = (src / "SKILL.md").read_bytes()

        new_desc = "Generates OpenAPI v3 specifications from FastAPI route handlers."
        proc = self.run_cli(
            [
                "store",
                "--source-dir",
                str(src),
                "--override-description",
                new_desc,
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        # Source directory remains byte-for-byte untouched.
        self.assertEqual((src / "SKILL.md").read_bytes(), before_bytes)

        # Vault copy reflects the user-approved description in both SKILL.md and .curator-meta.json.
        vault_skill = self.vault_dir / "skills" / "vague-skill"
        vault_skill_md = (vault_skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(new_desc, vault_skill_md)
        self.assertNotIn("Too short", vault_skill_md)

        meta = json.loads((vault_skill / ".curator-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["description"], new_desc)


if __name__ == "__main__":
    unittest.main()
