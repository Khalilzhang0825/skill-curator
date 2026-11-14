from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CURATOR_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "skill-curator"
    / "scripts"
    / "curator.py"
)


class TestCuratorSearchTagsAndHealthCli(unittest.TestCase):
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

    def init_vault(self, global_cap: int = 8, claude_sync: str = "auto", stale_days: int = 30) -> None:
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
                claude_sync,
                "--stale-days",
                str(stale_days),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def create_skill_dir(
        self,
        parent: Path,
        slug: str,
        description: str = "A well-scoped skill for workflow automation.",
        body_lines: int = 5,
        include_name: bool = True,
    ) -> Path:
        skill_dir = parent / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        fm = ["---"]
        if include_name:
            fm.append(f"name: {slug}")
        fm.append(f'description: "{description}"')
        fm.append("---")
        body = "\n".join(f"Step {i}" for i in range(1, body_lines + 1))
        (skill_dir / "SKILL.md").write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
        return skill_dir

    def store_skill(
        self,
        slug: str,
        description: str,
        keywords: str = "",
        tags: str = "",
        source_url: str = "",
    ) -> None:
        src = self.create_skill_dir(self.root / "incoming", slug, description=description)
        proc = self.run_cli(
            [
                "store",
                "--source-dir",
                str(src),
                "--keywords",
                keywords,
                "--tags",
                tags,
                "--source-url",
                source_url,
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_search_ranks_english_and_cjk_matches_respects_index_edits_and_filters(self) -> None:
        self.init_vault()
        self.store_skill(
            "wechat-formatter",
            description="微信公众号文章排版与封面设计工具。",
            keywords="wechat,markdown,layout",
            tags="wechat-writing,content",
        )
        self.store_skill(
            "sql-migrator",
            description="Plans safe zero-downtime Postgres schema migrations.",
            keywords="postgres,migrations,database",
            tags="backend",
        )
        self.store_skill(
            "wechat-draft",
            description="Drafts viral tech newsletters and headlines.",
            keywords="writing,newsletter",
            tags="wechat-writing",
        )
        self.store_skill(
            "docker-debug",
            description="Inspects container logs and network namespaces.",
            keywords="containers,networking",
            tags="devops",
        )

        # Manually edit INDEX.md to add a keyword & Scene Tag on sql-migrator.
        index_path = self.vault_dir / "INDEX.md"
        index_text = index_path.read_text(encoding="utf-8")
        index_text = index_text.replace(
            "| sql-migrator | postgres, migrations, database | backend |",
            "| sql-migrator | postgres, migrations, database, flyway | backend, data-ops |",
        )
        index_path.write_text(index_text, encoding="utf-8")

        # 1) Search English exact/keyword match (including manual INDEX.md keyword 'flyway').
        p_flyway = self.run_cli(
            ["search", "flyway", "--config", str(self.config_path)]
        )
        self.assertEqual(p_flyway.returncode, 0, msg=p_flyway.stderr)
        out_flyway = json.loads(p_flyway.stdout)
        self.assertEqual([r["name"] for r in out_flyway["results"]], ["sql-migrator"])

        # 2) Search Chinese CJK phrase matching bigram/token in description.
        p_cjk = self.run_cli(
            ["search", "公众号排版", "--config", str(self.config_path)]
        )
        self.assertEqual(p_cjk.returncode, 0, msg=p_cjk.stderr)
        out_cjk = json.loads(p_cjk.stdout)
        self.assertGreaterEqual(len(out_cjk["results"]), 1)
        self.assertEqual(out_cjk["results"][0]["name"], "wechat-formatter")
        names_cjk = {r["name"] for r in out_cjk["results"]}
        self.assertNotIn("docker-debug", names_cjk)

        # 3) Search exact name vs description ranks exact name highest + --limit.
        p_wechat = self.run_cli(
            ["search", "wechat", "--limit", "2", "--config", str(self.config_path)]
        )
        self.assertEqual(p_wechat.returncode, 0, msg=p_wechat.stderr)
        out_wechat = json.loads(p_wechat.stdout)
        self.assertEqual(len(out_wechat["results"]), 2)
        self.assertEqual(
            {r["name"] for r in out_wechat["results"]},
            {"wechat-draft", "wechat-formatter"},
        )

        # 4) Filter by --tag without query (including manual INDEX.md tag 'data-ops').
        p_tag = self.run_cli(
            ["search", "--tag", "data-ops", "--config", str(self.config_path)]
        )
        self.assertEqual(p_tag.returncode, 0, msg=p_tag.stderr)
        out_tag = json.loads(p_tag.stdout)
        self.assertEqual([r["name"] for r in out_tag["results"]], ["sql-migrator"])

        # 5) Filter by --tag combined with query.
        p_tag_q = self.run_cli(
            [
                "search",
                "newsletter",
                "--tag",
                "wechat-writing",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(p_tag_q.returncode, 0, msg=p_tag_q.stderr)
        out_tag_q = json.loads(p_tag_q.stdout)
        self.assertEqual([r["name"] for r in out_tag_q["results"]], ["wechat-draft"])

    def test_mount_tag_and_mount_with_tag_flag_batch_mounts_project(self) -> None:
        self.init_vault(claude_sync="auto")
        self.store_skill(
            "wechat-formatter",
            description="Formats articles for publishing.",
            tags="wechat-writing,content",
            source_url="https://github.com/acme/wechat-formatter",
        )
        self.store_skill(
            "wechat-title",
            description="Generates crisp titles for articles.",
            tags="wechat-writing",
            source_url="https://github.com/acme/wechat-title",
        )
        self.store_skill(
            "rust-profiler",
            description="Profiles Rust binaries with flamegraphs.",
            tags="systems",
        )

        # Mark project as Claude Code enabled so auto sync mirrors into .claude/skills/.
        (self.project_dir / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")

        # 1) mount-tag subcommand.
        proc = self.run_cli(
            [
                "mount-tag",
                "wechat-writing",
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(
            sorted(out["mounted_skills"]),
            ["wechat-formatter", "wechat-title"],
        )
        self.assertTrue(out["claude_mirrored"])

        for slug in ("wechat-formatter", "wechat-title"):
            agents_dest = self.project_dir / ".agents" / "skills" / slug
            claude_dest = self.project_dir / ".claude" / "skills" / slug
            self.assertTrue((agents_dest / "SKILL.md").is_file())
            self.assertFalse(agents_dest.is_symlink())
            self.assertTrue((claude_dest / "SKILL.md").is_file())
            meta = json.loads((agents_dest / ".curator-meta.json").read_text(encoding="utf-8"))
            self.assertTrue(meta["mounted_at"].endswith("Z"))
            self.assertEqual(meta["target"], "project")
            self.assertIn("mounted_from_vault", meta)

        self.assertFalse((self.project_dir / ".agents" / "skills" / "rust-profiler").exists())

        # 2) mount --tag <tag> with --claude-sync never into fresh project.
        proj2 = self.root / "project2"
        proj2.mkdir()
        (proj2 / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
        proc2 = self.run_cli(
            [
                "mount",
                "--tag",
                "wechat-writing",
                "--project-dir",
                str(proj2),
                "--claude-sync",
                "never",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(proc2.returncode, 0, msg=proc2.stderr)
        self.assertTrue((proj2 / ".agents" / "skills" / "wechat-formatter" / "SKILL.md").is_file())
        self.assertFalse((proj2 / ".claude" / "skills" / "wechat-formatter").exists())

        # 3) Unknown tag fails with TAG_EMPTY_OR_NOT_FOUND.
        proc_empty = self.run_cli(
            [
                "mount-tag",
                "nonexistent-tag",
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc_empty.returncode, 0)
        err_empty = json.loads(proc_empty.stderr)
        self.assertEqual(err_empty["error_code"], "TAG_EMPTY_OR_NOT_FOUND")

        # 4) Batch --tag mount rejects --target global.
        proc_global = self.run_cli(
            [
                "mount",
                "--tag",
                "wechat-writing",
                "--target",
                "global",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(proc_global.returncode, 0)

    def test_demote_from_project_and_global_unblocks_cap_and_protects_bundles(self) -> None:
        self.init_vault(global_cap=2, claude_sync="always")

        # Store and mount a skill to project (mirrored to .agents and .claude).
        self.store_skill(
            "proj-skill",
            description="Project-specific helper for data exports.",
            keywords="csv,export",
            tags="reporting",
            source_url="https://github.com/acme/proj-skill",
        )
        self.assertEqual(
            self.run_cli(
                [
                    "mount",
                    "proj-skill",
                    "--target",
                    "project",
                    "--project-dir",
                    str(self.project_dir),
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )
        self.assertTrue((self.project_dir / ".agents" / "skills" / "proj-skill").is_dir())
        self.assertTrue((self.project_dir / ".claude" / "skills" / "proj-skill").is_dir())

        # Demote from project -> removes both .agents & .claude copies, preserves Vault metadata without mount fields.
        demote_proj = self.run_cli(
            [
                "demote",
                "proj-skill",
                "--from",
                "project",
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(demote_proj.returncode, 0, msg=demote_proj.stderr)
        self.assertFalse((self.project_dir / ".agents" / "skills" / "proj-skill").exists())
        self.assertFalse((self.project_dir / ".claude" / "skills" / "proj-skill").exists())

        vault_meta = json.loads(
            (self.vault_dir / "skills" / "proj-skill" / ".curator-meta.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(vault_meta["scene_tags"], ["reporting"])
        self.assertEqual(vault_meta["keywords"], ["csv", "export"])
        self.assertEqual(vault_meta["source_url"], "https://github.com/acme/proj-skill")
        self.assertNotIn("mounted_at", vault_meta)
        self.assertNotIn("mounted_from_vault", vault_meta)
        self.assertNotIn("target", vault_meta)

        # Pre-existing Global Core skills (not in Vault yet) filling cap 2/2.
        self.create_skill_dir(
            self.global_dir_1,
            "legacy-global",
            description="Legacy global workflow skill not yet archived.",
        )
        self.create_skill_dir(
            self.global_dir_2,
            "legacy-global",
            description="Legacy global workflow skill not yet archived.",
        )
        self.create_skill_dir(
            self.global_dir_1,
            "second-global",
            description="Second global skill.",
        )

        # Store third skill and confirm global mount is blocked at cap (2/2).
        self.store_skill("new-global", description="New candidate global skill.")
        blocked = self.run_cli(
            ["mount", "new-global", "--target", "global", "--config", str(self.config_path)]
        )
        self.assertNotEqual(blocked.returncode, 0)

        # Demote legacy-global from global -> stores in Vault + INDEX.md, removes from BOTH global dirs!
        demote_global = self.run_cli(
            [
                "demote",
                "legacy-global",
                "--from",
                "global",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(demote_global.returncode, 0, msg=demote_global.stderr)
        self.assertFalse((self.global_dir_1 / "legacy-global").exists())
        self.assertFalse((self.global_dir_2 / "legacy-global").exists())
        self.assertTrue((self.vault_dir / "skills" / "legacy-global" / "SKILL.md").is_file())
        self.assertIn("legacy-global", (self.vault_dir / "INDEX.md").read_text(encoding="utf-8"))

        # Now Global Core occupancy is 1/2, so mounting new-global succeeds!
        unblocked = self.run_cli(
            ["mount", "new-global", "--target", "global", "--config", str(self.config_path)]
        )
        self.assertEqual(unblocked.returncode, 0, msg=unblocked.stderr)

        # Refuse demoting skill-curator, plugin bundles, or non-existent mounts.
        self.create_skill_dir(self.global_dir_1, "skill-curator", description="Curator itself.")
        self.assertNotEqual(
            self.run_cli(
                ["demote", "skill-curator", "--from", "global", "--config", str(self.config_path)]
            ).returncode,
            0,
        )
        bundle = self.create_skill_dir(self.global_dir_1, "ego-bundle", description="Plugin.")
        (bundle / "plugin.json").write_text('{"name": "ego-bundle"}\n', encoding="utf-8")
        self.assertNotEqual(
            self.run_cli(
                ["demote", "ego-bundle", "--from", "global", "--config", str(self.config_path)]
            ).returncode,
            0,
        )
        self.assertNotEqual(
            self.run_cli(
                ["demote", "ghost-skill", "--from", "project", "--project-dir", str(self.project_dir), "--config", str(self.config_path)]
            ).returncode,
            0,
        )

    def test_health_audit_detects_cap_duplicates_bloat_stale_and_healthy_state(self) -> None:
        self.init_vault(global_cap=1, stale_days=30)

        # 1) Clean healthy state first: 1 fresh global skill + 1 fresh distinct project skill.
        self.store_skill(
            "fresh-global",
            description="Handles local certificate trust and TLS verification.",
        )
        self.store_skill(
            "fresh-project",
            description="Generates GraphQL resolver unit tests and mocks.",
        )
        self.assertEqual(
            self.run_cli(
                ["mount", "fresh-global", "--target", "global", "--config", str(self.config_path)]
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                [
                    "mount",
                    "fresh-project",
                    "--target",
                    "project",
                    "--project-dir",
                    str(self.project_dir),
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )

        clean_proc = self.run_cli(
            [
                "health",
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(clean_proc.returncode, 0, msg=clean_proc.stderr)
        clean = json.loads(clean_proc.stdout)
        self.assertTrue(clean["ok"])
        self.assertTrue(clean["healthy"])
        self.assertEqual(clean["cap_violations"], [])
        self.assertEqual(clean["duplicates_and_overlaps"], [])
        self.assertEqual(clean["bloat_and_quality"], [])
        self.assertEqual(clean["stale_skills"], [])
        self.assertEqual(clean["untracked_mounts"], [])

        # 2) Introduce violations:
        #    - Force mount a bloated (>500 lines) overlapping skill into global -> cap 2 > 1 + bloat + overlap.
        bloated_src = self.create_skill_dir(
            self.root / "incoming",
            "bloated-global",
            description="Generates GraphQL resolver unit tests and schema mocks.",
            body_lines=505,
        )
        self.assertEqual(
            self.run_cli(
                ["store", "--source-dir", str(bloated_src), "--config", str(self.config_path)]
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                [
                    "mount",
                    "bloated-global",
                    "--target",
                    "global",
                    "--force",
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )

        #    - Mount fresh-global into project as well -> cross-tier duplicate.
        self.assertEqual(
            self.run_cli(
                [
                    "mount",
                    "fresh-global",
                    "--target",
                    "project",
                    "--project-dir",
                    str(self.project_dir),
                    "--config",
                    str(self.config_path),
                ]
            ).returncode,
            0,
        )

        #    - Make fresh-project stale (mounted_at 45 days ago).
        old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        proj_meta_path = (
            self.project_dir / ".agents" / "skills" / "fresh-project" / ".curator-meta.json"
        )
        proj_meta = json.loads(proj_meta_path.read_text(encoding="utf-8"))
        proj_meta["mounted_at"] = old_ts
        proj_meta_path.write_text(json.dumps(proj_meta, indent=2), encoding="utf-8")

        #    - Add an untracked mount without mounted_at (must NOT false-positive as stale).
        self.create_skill_dir(
            self.project_dir / ".agents" / "skills",
            "manual-untracked",
            description="Manually placed skill without curator metadata.",
        )

        audit_proc = self.run_cli(
            [
                "health",
                "--project-dir",
                str(self.project_dir),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(audit_proc.returncode, 0, msg=audit_proc.stderr)
        audit = json.loads(audit_proc.stdout)
        self.assertFalse(audit["healthy"])
        self.assertEqual(len(audit["cap_violations"]), 1)
        self.assertEqual(audit["cap_violations"][0]["count"], 2)
        self.assertEqual(audit["cap_violations"][0]["cap"], 1)

        dup_types = {
            (d["type"], tuple(sorted(d.get("skills", [d.get("name", "")]))))
            for d in audit["duplicates_and_overlaps"]
        }
        self.assertIn(("cross_tier_duplicate", ("fresh-global",)), dup_types)
        self.assertIn(("keyword_overlap", ("bloated-global", "fresh-project")), dup_types)

        bloat_names = {b["name"]: b["issues"] for b in audit["bloat_and_quality"]}
        self.assertIn("bloated-global", bloat_names)
        self.assertIn("instruction_bloat", bloat_names["bloated-global"])

        stale_names = [s["name"] for s in audit["stale_skills"]]
        self.assertEqual(stale_names, ["fresh-project"])

        untracked_names = [
            (u["name"], u["reason"]) for u in audit["untracked_mounts"]
        ]
        self.assertIn(("manual-untracked", "missing_mounted_at"), untracked_names)

        # 3) Override --stale-days 60 -> fresh-project (45 days old) is no longer flagged stale.
        audit_60 = json.loads(
            self.run_cli(
                [
                    "health",
                    "--project-dir",
                    str(self.project_dir),
                    "--stale-days",
                    "60",
                    "--config",
                    str(self.config_path),
                ]
            ).stdout
        )
        self.assertEqual(audit_60["stale_skills"], [])


if __name__ == "__main__":
    unittest.main()
