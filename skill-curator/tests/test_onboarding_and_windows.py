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


class TestOnboardingDiscoveryAndRemediation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.home = self.root / "fake_home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.vault_dir = self.root / "vault"
        self.config_path = self.vault_dir / "config.json"
        self.project_dir = self.root / "project"
        self.project_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def run_cli(
        self,
        args: list[str],
        *,
        stdin_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("SKILL_CURATOR_CONFIG", None)
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        return subprocess.run(
            [sys.executable, str(CURATOR_SCRIPT), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=15,
        )

    def write_skill(
        self,
        parent: Path,
        slug: str,
        *,
        description: str = "Automates structured workflows cleanly and reliably.",
        body_lines: int = 12,
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        skill_dir = parent / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        filler = "\n".join(f"- Step {i}: perform deterministic check." for i in range(body_lines))
        content = (
            "---\n"
            f"name: {slug}\n"
            f'description: "{description}"\n'
            "---\n\n"
            f"# {slug}\n\n"
            f"{filler}\n"
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        if extra_files:
            for rel, body in extra_files.items():
                fp = skill_dir / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(body, encoding="utf-8")
        return skill_dir

    def test_scan_pre_init_discovers_harnesses_exclusions_and_diagnostics(self) -> None:
        # Populate 8 standard harness dirs + 1 project dir before `init` is ever run.
        agents = self.home / ".agents" / "skills"
        claude = self.home / ".claude" / "skills"
        gemini_cfg = self.home / ".gemini" / "config" / "skills"
        gemini_leg = self.home / ".gemini" / "antigravity" / "skills"
        codex = self.home / ".codex" / "skills"
        cursor = self.home / ".cursor" / "skills"
        opencode_dot = self.home / ".opencode" / "skills"
        opencode_cfg = self.home / ".config" / "opencode" / "skills"

        # Excluded self + plugin bundle
        self.write_skill(agents, "skill-curator", description="Self skill.")
        bundle = agents / "ego-browser"
        (bundle / "skills" / "sub").mkdir(parents=True, exist_ok=True)
        (bundle / "plugin.json").write_text('{"name": "ego-browser"}\n', encoding="utf-8")
        (bundle / "skills" / "sub" / "SKILL.md").write_text("---\nname: sub\ndescription: Sub.\n---\n", encoding="utf-8")

        # 10 unique skills across harnesses -> triggers global_cap (8) violation
        self.write_skill(agents, "core-one", description="Core planning workflow across repositories.")
        self.write_skill(agents, "core-two", description="Core code review assistant for pull requests.")
        self.write_skill(claude, "core-three", description="Generates clean changelog entries from diffs.")
        self.write_skill(gemini_cfg, "core-four", description="Formats API schemas into strict types.")
        self.write_skill(gemini_leg, "core-five", description="Runs deterministic SQL schema audits.")
        self.write_skill(codex, "core-six", description="Inspects Docker compose networks safely.")
        self.write_skill(cursor, "core-seven", description="Checks accessibility compliance on templates.")
        self.write_skill(opencode_dot, "core-eight", description="Validates OpenAPI specifications cleanly.")

        # Identical cross-harness duplicate
        self.write_skill(agents, "same-dup", description="Deterministic formatter for markdown tables.")
        self.write_skill(claude, "same-dup", description="Deterministic formatter for markdown tables.")

        # Diverged cross-harness duplicate
        self.write_skill(agents, "diff-dup", description="Manages deployment pipelines cleanly.")
        self.write_skill(cursor, "diff-dup", description="Manages deployment pipelines with extra hook.", extra_files={"extra.txt": "diverged"})

        # Bloated + vague + script risk
        self.write_skill(
            opencode_cfg,
            "bloated-pdf",
            description="Helper.",
            body_lines=520,
            extra_files={"scripts/convert.sh": "#!/bin/sh\necho hi\n"},
        )

        # Cross-tier duplicate in project workspace
        self.write_skill(self.project_dir / ".agents" / "skills", "core-one", description="Core planning workflow across repositories.")

        for cmd_name in ("scan", "diagnose"):
            proc = self.run_cli(
                [cmd_name, "--home-dir", str(self.home), "--project-dir", str(self.project_dir)]
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            out = json.loads(proc.stdout)
            self.assertTrue(out["ok"])
            self.assertFalse(out["config_loaded"])

            labels = {d["harness"] for d in out["discovered_dirs"]}
            self.assertTrue(
                {"agents", "claude", "antigravity", "antigravity-legacy", "codex", "cursor", "opencode"}.issubset(labels),
                msg=labels,
            )
            excluded = {b for d in out["discovered_dirs"] for b in d["excluded_bundles"]}
            self.assertIn("ego-browser", excluded)

            skill_names = {s["name"] for s in out["skills"]}
            self.assertNotIn("skill-curator", skill_names)
            self.assertNotIn("ego-browser", skill_names)

            diag = out["diagnosis"]
            self.assertTrue(diag["cap_violations"])
            same_dups = {d["name"]: d for d in diag["cross_harness_duplicates"]}
            self.assertTrue(same_dups["same-dup"]["content_identical"])
            self.assertFalse(same_dups["diff-dup"]["content_identical"])
            self.assertTrue(any(d["name"] == "core-one" for d in diag["cross_tier_duplicates"]))
            self.assertTrue(any(b["name"] == "bloated-pdf" for b in diag["bloat_and_quality"]))
            self.assertTrue(any(s["name"] == "bloated-pdf" for s in diag["scripts_present"]))
            self.assertIn("bloated-pdf", diag["untracked_skills"])

            rec = out["recommendations"]
            self.assertLessEqual(len(rec["keep_and_adopt"]), 8)
            demote_names = {d["name"] for d in rec["demote_to_vault"]}
            self.assertIn("bloated-pdf", demote_names)
            bloated_rec = next(d for d in rec["demote_to_vault"] if d["name"] == "bloated-pdf")
            self.assertTrue(bloated_rec["suggested_keywords"])
            self.assertTrue(bloated_rec["suggested_scene_tags"])
            warn_names = {w["name"] for w in rec["review_warnings"]}
            self.assertIn("diff-dup", warn_names)

        # Also verify `--home-dir` isolates default config lookup even when HOME has config.json
        (self.home / ".config" / "skill-curator").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "skill-curator" / "config.json").write_text(
            json.dumps({"vault_path": str(self.vault_dir)}), encoding="utf-8"
        )
        isolated_home = self.root / "isolated_clean_home"
        self.write_skill(isolated_home / ".claude" / "skills", "iso-skill")
        iso_scan = self.run_cli(["scan", "--home-dir", str(isolated_home)])
        self.assertEqual(iso_scan.returncode, 0, msg=iso_scan.stderr)
        iso_out = json.loads(iso_scan.stdout)
        self.assertFalse(iso_out["config_loaded"])

    def test_adopt_preserves_active_and_skill_md_and_vault_override_description(self) -> None:
        claude_dir = self.home / ".claude" / "skills"
        active_skill = self.write_skill(
            claude_dir,
            "clean-tool",
            description="Audits dependency licenses across repositories.",
        )
        orig_bytes = (active_skill / "SKILL.md").read_bytes()

        self.assertEqual(
            self.run_cli(["init", "--vault", str(self.vault_dir), "--global-dir", str(claude_dir)]).returncode,
            0,
        )

        proc = self.run_cli(
            [
                "adopt",
                "clean-tool",
                "--home-dir",
                str(self.home),
                "--keywords",
                "license,audit",
                "--tags",
                "security,compliance",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "adopt")
        self.assertTrue((self.vault_dir / "skills" / "clean-tool" / "SKILL.md").is_file())
        self.assertTrue(active_skill.is_dir())
        self.assertEqual((active_skill / "SKILL.md").read_bytes(), orig_bytes)
        meta = json.loads((active_skill / ".curator-meta.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["mounted_at"])
        self.assertEqual(meta["target"], "global")

        # Also test --source-dir + --tier project + --override-description (Vault-only mutation)
        proj_skill = self.write_skill(
            self.project_dir / ".agents" / "skills",
            "proj-linter",
            description="Original verbose description kept untouched locally.",
        )
        proj_orig_bytes = (proj_skill / "SKILL.md").read_bytes()
        proc_proj = self.run_cli(
            [
                "adopt",
                "--source-dir",
                str(proj_skill),
                "--tier",
                "project",
                "--project-dir",
                str(self.project_dir),
                "--override-description",
                "Concise Vault-only summary for linter.",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_proj.returncode, 0, msg=proc_proj.stderr)
        self.assertEqual((proj_skill / "SKILL.md").read_bytes(), proj_orig_bytes)
        vault_text = (self.vault_dir / "skills" / "proj-linter" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Concise Vault-only summary for linter.", vault_text)

    def test_enhanced_demote_multi_harness_and_diverged_duplicate_safety(self) -> None:
        agents = self.home / ".agents" / "skills"
        codex = self.home / ".codex" / "skills"
        cursor = self.home / ".cursor" / "skills"

        self.write_skill(agents, "dup-skill", description="Identical across agents and codex.")
        self.write_skill(codex, "dup-skill", description="Identical across agents and codex.")

        self.write_skill(agents, "forked-skill", description="Version A.")
        self.write_skill(cursor, "forked-skill", description="Version B.", extra_files={"diff.txt": "yes"})

        self.assertEqual(
            self.run_cli(["init", "--vault", str(self.vault_dir), "--global-dir", str(agents)]).returncode,
            0,
        )

        # Identical duplicates across discovered harnesses are cleanly archived and removed
        proc_dup = self.run_cli(
            [
                "demote",
                "dup-skill",
                "--from",
                "global",
                "--discover-global-dirs",
                "--home-dir",
                str(self.home),
                "--keywords",
                "sync,helper",
                "--tags",
                "infra",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_dup.returncode, 0, msg=proc_dup.stderr)
        self.assertTrue((self.vault_dir / "skills" / "dup-skill" / "SKILL.md").is_file())
        self.assertFalse((agents / "dup-skill").exists())
        self.assertFalse((codex / "dup-skill").exists())
        index_text = (self.vault_dir / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("infra", index_text)

        # Diverged duplicate refuses without --force
        proc_fail = self.run_cli(
            [
                "demote",
                "forked-skill",
                "--from",
                "global",
                "--discover-global-dirs",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc_fail.returncode, 0)
        self.assertIn("diverged", (proc_fail.stderr + proc_fail.stdout).lower())
        self.assertTrue((agents / "forked-skill").exists())
        self.assertTrue((cursor / "forked-skill").exists())

        # Succeeds with --force
        proc_force = self.run_cli(
            [
                "demote",
                "forked-skill",
                "--from",
                "global",
                "--discover-global-dirs",
                "--home-dir",
                str(self.home),
                "--force",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc_force.returncode, 0, msg=proc_force.stderr)
        self.assertFalse((agents / "forked-skill").exists())
        self.assertFalse((cursor / "forked-skill").exists())

        # Corrupted duplicate (one copy missing SKILL.md, extra file present) must refuse without --force
        self.write_skill(agents, "corrupt-dup", description="Healthy copy.")
        corrupt_copy = cursor / "corrupt-dup"
        corrupt_copy.mkdir(parents=True, exist_ok=True)
        (corrupt_copy / "notes.txt").write_text("stale partial dir", encoding="utf-8")
        proc_corrupt = self.run_cli(
            [
                "demote",
                "corrupt-dup",
                "--from",
                "global",
                "--discover-global-dirs",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc_corrupt.returncode, 0)
        self.assertEqual(json.loads(proc_corrupt.stderr)["error_code"], "DIVERGED_DUPLICATE")
        self.assertTrue((agents / "corrupt-dup").exists())

    def test_remediate_dry_run_flags_and_json_plan(self) -> None:
        agents = self.home / ".agents" / "skills"
        claude = self.home / ".claude" / "skills"
        self.write_skill(agents, "core-a", description="Core everyday code verification checklist.")
        self.write_skill(claude, "core-b", description="Helper.")
        self.write_skill(agents, "heavy-pdf", description="PDF batch converter and OCR workflow.", body_lines=510)
        self.write_skill(claude, "wechat-writer", description="Formats WeChat official account articles with crisp typography.")
        self.write_skill(agents, "diverged-dup", description="Copy A.")
        self.write_skill(claude, "diverged-dup", description="Copy B.", extra_files={"extra.md": "diff"})

        self.assertEqual(
            self.run_cli(
                ["init", "--vault", str(self.vault_dir), "--discover-global-dirs", "--home-dir", str(self.home)]
            ).returncode,
            0,
        )

        # 1) --dry-run mutates zero files
        dry = self.run_cli(
            [
                "remediate",
                "--dry-run",
                "--adopt",
                "core-a",
                "--demote",
                "heavy-pdf",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(dry.returncode, 0, msg=dry.stderr)
        dry_out = json.loads(dry.stdout)
        self.assertTrue(dry_out["dry_run"])
        self.assertEqual(list((self.vault_dir / "skills").iterdir()), [])
        self.assertTrue((agents / "heavy-pdf").exists())

        # 2) Diverged duplicate refuses without --force
        bad_div = self.run_cli(
            [
                "remediate",
                "--demote",
                "diverged-dup",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(bad_div.returncode, 0)
        self.assertTrue((agents / "diverged-dup").exists())

        # 3) Flag execution: adopts core-a (keeps active + backs up) and demotes heavy-pdf (Vault-first + removes)
        exec_proc = self.run_cli(
            [
                "remediate",
                "--adopt",
                "core-a",
                "--demote",
                "heavy-pdf",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(exec_proc.returncode, 0, msg=exec_proc.stderr)
        self.assertTrue((self.vault_dir / "skills" / "core-a" / "SKILL.md").is_file())
        self.assertTrue((agents / "core-a" / ".curator-meta.json").is_file())
        self.assertTrue((self.vault_dir / "skills" / "heavy-pdf" / "SKILL.md").is_file())
        self.assertFalse((agents / "heavy-pdf").exists())

        # 4) JSON --plan - execution with custom tags and override_description
        plan = {
            "adopt": [
                {
                    "name": "core-b",
                    "keywords": ["helper", "setup"],
                    "scene_tags": ["core"],
                    "override_description": "Refined concise setup workflow description.",
                }
            ],
            "demote": [
                {
                    "name": "wechat-writer",
                    "keywords": ["wechat", "writing"],
                    "scene_tags": ["wechat-writing"],
                }
            ],
        }
        plan_proc = self.run_cli(
            [
                "remediate",
                "--plan",
                "-",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ],
            stdin_text=json.dumps(plan),
        )
        self.assertEqual(plan_proc.returncode, 0, msg=plan_proc.stderr)
        # Active core-b SKILL.md stays unmodified ("Helper."), Vault gets refined description
        self.assertIn("Helper.", (claude / "core-b" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn(
            "Refined concise setup workflow description.",
            (self.vault_dir / "skills" / "core-b" / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((claude / "wechat-writer").exists())
        self.assertIn("wechat-writing", (self.vault_dir / "INDEX.md").read_text(encoding="utf-8"))

        # 5) JSON --plan - with {"actions": [...]} schema and per-item "force": true on diverged-dup
        actions_plan = {
            "actions": [
                {
                    "action": "demote",
                    "name": "diverged-dup",
                    "keywords": ["dup", "forced"],
                    "scene_tags": ["cleanup"],
                    "force": True,
                }
            ]
        }
        actions_proc = self.run_cli(
            ["remediate", "--plan", "-", "--home-dir", str(self.home), "--config", str(self.config_path)],
            stdin_text=json.dumps(actions_plan),
        )
        self.assertEqual(actions_proc.returncode, 0, msg=actions_proc.stderr)
        self.assertFalse((agents / "diverged-dup").exists())
        self.assertFalse((claude / "diverged-dup").exists())

        # 6) JSON --plan - accepting direct `scan` recommendations format (keep_and_adopt / demote_to_vault)
        self.write_skill(agents, "rec-keep", description="Clean core skill.")
        self.write_skill(claude, "rec-archive", description="Archivable stack skill.")
        rec_plan = {
            "recommendations": {
                "keep_and_adopt": [
                    {
                        "name": "rec-keep",
                        "suggested_keywords": ["clean", "core"],
                        "suggested_scene_tags": ["core"],
                    }
                ],
                "demote_to_vault": [
                    {
                        "name": "rec-archive",
                        "suggested_keywords": ["stack"],
                        "suggested_scene_tags": ["backend-db"],
                    }
                ],
            }
        }
        rec_proc = self.run_cli(
            ["remediate", "--plan", "-", "--home-dir", str(self.home), "--config", str(self.config_path)],
            stdin_text=json.dumps(rec_plan),
        )
        self.assertEqual(rec_proc.returncode, 0, msg=rec_proc.stderr)
        self.assertTrue((agents / "rec-keep" / ".curator-meta.json").is_file())
        self.assertFalse((claude / "rec-archive").exists())
        self.assertIn("backend-db", (self.vault_dir / "INDEX.md").read_text(encoding="utf-8"))

    def test_init_discover_global_dirs_reports_existing_harnesses(self) -> None:
        claude = self.home / ".claude" / "skills"
        codex = self.home / ".codex" / "skills"
        self.write_skill(claude, "alpha-tool")
        self.write_skill(codex, "beta-tool")

        # Default init remains untouched
        plain_init = self.run_cli(["init", "--vault", str(self.root / "plain_vault")])
        self.assertEqual(plain_init.returncode, 0, msg=plain_init.stderr)
        self.assertNotIn("discovered_global_dirs", json.loads(plain_init.stdout))

        # init --discover-global-dirs includes discovered existing harness dirs + skill counts
        disc_init = self.run_cli(
            [
                "init",
                "--vault",
                str(self.vault_dir),
                "--discover-global-dirs",
                "--home-dir",
                str(self.home),
            ]
        )
        self.assertEqual(disc_init.returncode, 0, msg=disc_init.stderr)
        out = json.loads(disc_init.stdout)
        by_harness = {d["harness"]: d for d in out["discovered_global_dirs"]}
        self.assertEqual(by_harness["claude"]["skill_count"], 1)
        self.assertEqual(by_harness["codex"]["skill_count"], 1)
        self.assertIn(str(claude.resolve()), out["config"]["global_core_dirs"])
        self.assertIn(str(codex.resolve()), out["config"]["global_core_dirs"])


if __name__ == "__main__":
    unittest.main()
