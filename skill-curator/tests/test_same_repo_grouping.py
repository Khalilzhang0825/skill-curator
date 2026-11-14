from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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


class TestSameRepoSkillGrouping(unittest.TestCase):
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
        description: str = "Structured workflow helper skill for engineering tasks.",
        body: str = "Follow deterministic engineering steps.\n",
        frontmatter_extra: str = "",
        curator_meta: dict[str, object] | None = None,
    ) -> Path:
        skill_dir = parent / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f"name: {slug}\n"
            f'description: "{description}"\n'
            f"{frontmatter_extra}"
            "---\n\n"
            f"# {slug}\n\n"
            f"{body}"
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        if curator_meta is not None:
            (skill_dir / ".curator-meta.json").write_text(
                json.dumps(curator_meta, indent=2) + "\n", encoding="utf-8"
            )
        return skill_dir

    def test_normalize_repo_source_variants(self) -> None:
        import onboarding

        cases = [
            (
                "https://github.com/mattpocock/skills/tree/main/skills/grill-me",
                ("github.com/mattpocock/skills", "https://github.com/mattpocock/skills"),
            ),
            (
                "https://skills.sh/mattpocock/skills/grill-me",
                ("github.com/mattpocock/skills", "https://github.com/mattpocock/skills"),
            ),
            (
                "https://raw.githubusercontent.com/mattpocock/skills/main/grill-me/SKILL.md",
                ("github.com/mattpocock/skills", "https://github.com/mattpocock/skills"),
            ),
            (
                "git@github.com:mattpocock/skills.git",
                ("github.com/mattpocock/skills", "https://github.com/mattpocock/skills"),
            ),
            (
                "ssh://git@gitlab.com/Acme/My-Skills.git",
                ("gitlab.com/acme/my-skills", "https://gitlab.com/Acme/My-Skills"),
            ),
            (
                "https://gitlab.com/acme/companion-skills/-/tree/main/skills/one",
                ("gitlab.com/acme/companion-skills", "https://gitlab.com/acme/companion-skills"),
            ),
            (
                "mattpocock/skills",
                ("github.com/mattpocock/skills", "https://github.com/mattpocock/skills"),
            ),
            (
                "mattpocock/skills/tdd",
                ("github.com/mattpocock/skills", "https://github.com/mattpocock/skills"),
            ),
        ]
        for raw, expected in cases:
            self.assertEqual(onboarding.normalize_repo_source(raw), expected, msg=raw)

    def test_mattpocock_and_meta_multi_repo_packing_never_splits_groups(self) -> None:
        agents = self.home / ".agents" / "skills"
        claude = self.home / ".claude" / "skills"

        mattpocock_skills = [
            "grill-me",
            "prd-to-plan",
            "tdd",
            "ubiquitous-language",
            "write-a-prd",
            "write-a-skill",
        ]
        for slug in mattpocock_skills:
            self.write_skill(agents, slug, description=f"Matt Pocock workflow skill {slug}.")

        # Global npx skills lockfile attributes the 6 mattpocock/skills entries
        (self.home / ".agents" / ".skill-lock.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "skills": {
                        slug: {
                            "source": "mattpocock/skills",
                            "sourceType": "github",
                            "skillPath": f"skills/{slug}/SKILL.md",
                        }
                        for slug in mattpocock_skills
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # Second cohesive repo with 4 companion skills attributed via .curator-meta.json subpaths
        meta_skills = ["meta-arch", "meta-docs", "meta-qa", "meta-review"]
        for slug in meta_skills:
            self.write_skill(
                claude,
                slug,
                description=f"Meta engineering companion skill {slug}.",
                curator_meta={
                    "name": slug,
                    "source_url": f"https://github.com/meta/companion-skills/tree/main/skills/{slug}",
                },
            )

        # Standalone singletons
        for slug in ("alpha-lint", "beta-doc", "zeta-ui"):
            self.write_skill(agents, slug, description=f"Standalone utility {slug}.")

        proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        rec = out["recommendations"]

        keep_names = {item["name"]: item for item in rec["keep_and_adopt"]}
        demote_names = {item["name"]: item for item in rec["demote_to_vault"]}

        # All 6 mattpocock/skills stay together in keep_and_adopt
        self.assertTrue(set(mattpocock_skills).issubset(keep_names.keys()), msg=keep_names.keys())
        for slug in mattpocock_skills:
            self.assertEqual(keep_names[slug]["source_repo"], "github.com/mattpocock/skills")
            self.assertEqual(keep_names[slug]["source_url"], "https://github.com/mattpocock/skills")

        # All 4 meta/companion-skills stay together in demote_to_vault (8-slot cap cannot fit 6 + 4)
        self.assertTrue(set(meta_skills).issubset(demote_names.keys()), msg=demote_names.keys())
        for slug in meta_skills:
            self.assertEqual(demote_names[slug]["reason"], "over_global_cap_repo_group")
            self.assertEqual(demote_names[slug]["source_repo"], "github.com/meta/companion-skills")

        # Remaining 2 slots filled by singletons (6 + 2 = 8), soft_cap_exceeded is False
        self.assertEqual(len(rec["keep_and_adopt"]), 8)
        self.assertFalse(rec["soft_cap_exceeded"])
        self.assertEqual(rec["recommended_global_cap"], 8)

        # Zero repo group has members split across keep_and_adopt and demote_to_vault
        groups_by_repo = {g["source_repo"]: g for g in out["repo_groups"]}
        self.assertIn("github.com/mattpocock/skills", groups_by_repo)
        self.assertIn("github.com/meta/companion-skills", groups_by_repo)
        self.assertEqual(groups_by_repo["github.com/mattpocock/skills"]["suite_scene_tag"], "mattpocock-skills")
        self.assertEqual(groups_by_repo["github.com/mattpocock/skills"]["recommended_disposition"], "keep_and_adopt")
        self.assertEqual(groups_by_repo["github.com/meta/companion-skills"]["recommended_disposition"], "demote_to_vault")

        for g in out["repo_groups"]:
            members = set(g["global_skills"])
            in_keep = bool(members & set(keep_names.keys()))
            in_demote = bool(members & set(demote_names.keys()))
            self.assertFalse(in_keep and in_demote, msg=f"Group split: {g}")

        # Verify shared suite_scene_tag appears in suggested_scene_tags
        by_skill = {s["name"]: s for s in out["skills"]}
        self.assertIn("mattpocock-skills", by_skill["grill-me"]["suggested_scene_tags"])

    def test_oversized_cohesive_suite_stays_intact_and_remediate_propagates_source_url(self) -> None:
        agents = self.home / ".agents" / "skills"
        suite = [
            "epic-01",
            "epic-02",
            "epic-03",
            "epic-04",
            "epic-05",
            "epic-06",
            "epic-07",
            "epic-08",
            "epic-09",
            "epic-10",
        ]
        for slug in suite:
            self.write_skill(agents, slug, description=f"Cohesive suite skill {slug}.")

        (self.home / ".agents" / ".skill-lock.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "skills": {
                        slug: {"source": "mattpocock/skills", "sourceType": "github"}
                        for slug in suite
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.write_skill(agents, "extra-lone", description="Unrelated standalone utility.")

        scan_proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(scan_proc.returncode, 0, msg=scan_proc.stderr)
        out = json.loads(scan_proc.stdout)
        rec = out["recommendations"]

        keep_names = [item["name"] for item in rec["keep_and_adopt"]]
        demote_names = [item["name"] for item in rec["demote_to_vault"]]
        self.assertEqual(sorted(keep_names), suite)
        self.assertEqual(demote_names, ["extra-lone"])
        self.assertTrue(rec["soft_cap_exceeded"])
        self.assertEqual(rec["recommended_global_cap"], 10)

        # Remediate using the scan output directly and verify provenance + scene tags propagate
        self.assertEqual(
            self.run_cli(
                ["init", "--vault", str(self.vault_dir), "--discover-global-dirs", "--home-dir", str(self.home)]
            ).returncode,
            0,
        )
        rem_proc = self.run_cli(
            ["remediate", "--plan", "-", "--home-dir", str(self.home), "--config", str(self.config_path)],
            stdin_text=json.dumps(out),
        )
        self.assertEqual(rem_proc.returncode, 0, msg=rem_proc.stderr)
        index_text = (self.vault_dir / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/mattpocock/skills", index_text)
        self.assertIn("mattpocock-skills", index_text)
        self.assertFalse((agents / "extra-lone").exists())
        epic_meta = json.loads((self.vault_dir / "skills" / "epic-01" / ".curator-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(epic_meta["source_url"], "https://github.com/mattpocock/skills")

        # Also test standalone demote propagates lockfile source_url even when active meta lacked source_url
        self.write_skill(agents, "lock-demote", description="Installed via npx skills add.")
        lock_file = json.loads((self.home / ".agents" / ".skill-lock.json").read_text(encoding="utf-8"))
        lock_file["skills"]["lock-demote"] = {"source": "acme/demoted-tools", "sourceType": "github"}
        (self.home / ".agents" / ".skill-lock.json").write_text(json.dumps(lock_file), encoding="utf-8")
        dem_proc = self.run_cli(
            [
                "demote",
                "lock-demote",
                "--from",
                "global",
                "--discover-global-dirs",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(dem_proc.returncode, 0, msg=dem_proc.stderr)
        dem_meta = json.loads((self.vault_dir / "skills" / "lock-demote" / ".curator-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(dem_meta["source_url"], "https://github.com/acme/demoted-tools")

    def test_cross_reference_fallback_without_lockfile_avoids_prose_false_positives(self) -> None:
        agents = self.home / ".agents" / "skills"
        # 5 manually copied companion skills with NO lockfile and NO .curator-meta.json
        self.write_skill(
            agents,
            "grill-me",
            body="Interview user relentlessly. Pairs with /write-a-prd and `ubiquitous-language`.\n",
        )
        self.write_skill(
            agents,
            "write-a-prd",
            body="Run /grill-me first, then hand off to /prd-to-plan.\n",
        )
        self.write_skill(
            agents,
            "prd-to-plan",
            body="Convert PRD into slices using `tdd` skill loops.\n",
        )
        self.write_skill(
            agents,
            "tdd",
            body="Red-green-refactor cycle after skills/prd-to-plan.\n",
        )
        self.write_skill(
            agents,
            "ubiquitous-language",
            body="Maintain domain glossary referenced by write-a-prd.\n",
        )

        # Unrelated singletons with generic English words like "test", "plan", "grill", "review"
        for idx in range(6):
            self.write_skill(
                agents,
                f"alpha-{chr(97 + idx)}",
                body="This skill helps test and plan code changes and grill edge cases in a standard workflow.\n",
            )

        # Also a 4-letter generic word skill named `plan` should NOT false-match prose "plan code changes"
        self.write_skill(agents, "plan", body="General planning helper.\n")

        proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)

        companion = {"grill-me", "prd-to-plan", "tdd", "ubiquitous-language", "write-a-prd"}
        matching_groups = [g for g in out["repo_groups"] if companion & set(g["skills"])]
        self.assertEqual(len(matching_groups), 1, msg=out["repo_groups"])
        grp = matching_groups[0]
        self.assertEqual(set(grp["skills"]), companion)
        self.assertTrue(grp["source_repo"].startswith("inferred-suite:"))
        self.assertEqual(grp["signal"], "cross-reference")
        self.assertEqual(grp["recommended_disposition"], "keep_and_adopt")

        # Verify `plan` and `alpha-*` were NOT pulled into the companion suite
        by_name = {s["name"]: s for s in out["skills"]}
        self.assertIsNone(by_name["alpha-a"]["source_repo"])
        self.assertIsNone(by_name["plan"]["source_repo"])

    def test_conflicting_explicit_repos_are_never_merged_even_with_cross_reference(self) -> None:
        agents = self.home / ".agents" / "skills"
        self.write_skill(
            agents,
            "repo-a-skill",
            body="After running this, optionally invoke /repo-b-skill.\n",
            frontmatter_extra="metadata:\n  source: acme/repo-a\n",
        )
        self.write_skill(
            agents,
            "repo-b-skill",
            body="Standalone helper in repo B.\n",
            frontmatter_extra="source_url: https://github.com/other/repo-b\n",
        )

        proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        by_name = {s["name"]: s for s in out["skills"]}
        self.assertEqual(by_name["repo-a-skill"]["source_repo"], "github.com/acme/repo-a")
        self.assertEqual(by_name["repo-b-skill"]["source_repo"], "github.com/other/repo-b")
        self.assertEqual(out["repo_groups"], [])

    def test_tracked_singleton_plus_oversized_suite_with_short_description_keeps_suite_intact(self) -> None:
        agents = self.home / ".agents" / "skills"
        self.write_skill(
            agents,
            "my-core",
            description="Already tracked core workflow helper.",
            curator_meta={
                "name": "my-core",
                "mounted_at": "2026-11-10T00:00:00Z",
                "mounted_from_vault": str(self.vault_dir / "skills" / "my-core"),
            },
        )
        suite = [f"suite-{idx:02d}" for idx in range(1, 11)]
        for idx, slug in enumerate(suite):
            desc = "TDD loop." if idx == 0 else f"Cohesive companion workflow {slug}."
            self.write_skill(agents, slug, description=desc)

        (self.home / ".agents" / "skills-lock.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "skills": {
                        slug: {"repository": "mattpocock/skills"}
                        for slug in suite
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        for lone in ("alpha-lone", "beta-lone"):
            self.write_skill(agents, lone, description=f"Untracked standalone utility {lone}.")

        proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        rec = out["recommendations"]

        keep_names = {item["name"] for item in rec["keep_and_adopt"]}
        demote_names = {item["name"] for item in rec["demote_to_vault"]}
        self.assertEqual(keep_names, {"my-core", *suite})
        self.assertEqual(demote_names, {"alpha-lone", "beta-lone"})
        self.assertTrue(rec["soft_cap_exceeded"])
        self.assertEqual(rec["recommended_global_cap"], 11)
        self.assertEqual(len(rec["repo_groups"]), 1)
        self.assertEqual(rec["repo_groups"][0]["bloated_skills"], [])
        self.assertEqual(rec["repo_groups"], out["repo_groups"])
        by_name = {s["name"]: s for s in out["skills"]}
        self.assertEqual(by_name["suite-01"]["source_repo_signal"], "skills-lock")


if __name__ == "__main__":
    unittest.main()
