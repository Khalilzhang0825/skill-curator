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

from common import rmtree_safe


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
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
        timeout=20,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc


class TestGitCloneAndSymlinkDiscovery(unittest.TestCase):
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
        rmtree_safe(self.root, ignore_errors=True)
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
            timeout=20,
        )

    def _symlink_or_skip(self, target: Path, link_path: Path) -> None:
        link_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(str(target), str(link_path), target_is_directory=target.is_dir())
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Symlink privilege (SeCreateSymbolicLinkPrivilege) not enabled on Windows.")
            raise

    def write_skill(
        self,
        parent: Path,
        slug: str,
        *,
        fm_name: str | None = None,
        description: str = "Structured engineering workflow helper skill.",
        body: str = "Follow deterministic engineering steps.\n",
        frontmatter_extra: str = "",
    ) -> Path:
        skill_dir = parent / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        name_val = slug if fm_name is None else fm_name
        name_line = f"name: {name_val}\n" if name_val else ""
        content = (
            "---\n"
            f"{name_line}"
            f'description: "{description}"\n'
            f"{frontmatter_extra}"
            "---\n\n"
            f"# {slug}\n\n"
            f"{body}"
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return skill_dir

    def test_direct_nested_git_clone_discovery_and_remediate(self) -> None:
        agents = self.home / ".agents" / "skills"
        agents.mkdir(parents=True, exist_ok=True)

        # Create upstream repo and clone directly into ~/.agents/skills/mattpocock-clone
        upstream = self.root / "upstream_mattpocock"
        upstream.mkdir(parents=True, exist_ok=True)
        _git(upstream, "init")
        suite = [
            "grill-me",
            "prd-to-plan",
            "tdd",
            "ubiquitous-language",
            "write-a-prd",
            "write-a-skill",
        ]
        for slug in suite:
            self.write_skill(upstream / "skills", slug, description=f"Matt Pocock suite skill {slug}.")
        # Noise inside SKIP_DISCOVERY_DIRS must not be discovered as skills
        self.write_skill(upstream / "tests" / "fixture-skill", "ignored-fixture", description="Fixture only.")
        _git(upstream, "add", ".")
        _git(upstream, "commit", "-m", "initial suite")
        _git(upstream, "remote", "add", "origin", "git@github.com:mattpocock/skills.git")

        cloned = agents / "mattpocock-clone"
        _git(agents, "clone", str(upstream), str(cloned))
        # Set origin remote on cloned repo to GitHub SSH URL (as if cloned directly from GitHub)
        _git(cloned, "remote", "set-url", "origin", "git@github.com:mattpocock/skills.git")

        # Also add a tracked 3-skill global suite + 2 standalone skills so we can test cap-demoting a clone
        for lone in ("alpha-lone", "beta-lone", "gamma-lone", "zeta-lone"):
            self.write_skill(agents, lone, description=f"Standalone utility {lone}.")

        scan_proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(scan_proc.returncode, 0, msg=scan_proc.stderr)
        out = json.loads(scan_proc.stdout)

        by_name = {s["name"]: s for s in out["skills"]}
        self.assertNotIn("ignored-fixture", by_name)
        for slug in suite:
            self.assertIn(slug, by_name)
            self.assertEqual(by_name[slug]["source_repo"], "github.com/mattpocock/skills")
            self.assertEqual(by_name[slug]["source_url"], "https://github.com/mattpocock/skills")
            self.assertEqual(by_name[slug]["source_repo_signal"], "git-origin")
            self.assertIn("mattpocock-skills", by_name[slug]["suggested_scene_tags"])

        rec = out["recommendations"]
        keep_names = {x["name"] for x in rec["keep_and_adopt"]}
        self.assertTrue(set(suite).issubset(keep_names), msg=keep_names)
        self.assertEqual(len(keep_names), 8)
        self.assertFalse(rec["soft_cap_exceeded"])

        # Init & run remediate for a tracked suite + demoting the 6 cloned skills (--global-cap 5 after adding higher-priority tracked suite)
        self.assertEqual(
            self.run_cli(
                ["init", "--vault", str(self.vault_dir), "--discover-global-dirs", "--home-dir", str(self.home)]
            ).returncode,
            0,
        )
        rem_plan = {
            "adopt": [item for item in rec["keep_and_adopt"] if item["name"] not in suite],
            "demote": [
                {
                    "name": slug,
                    "keywords": by_name[slug]["suggested_keywords"],
                    "scene_tags": by_name[slug]["suggested_scene_tags"],
                    "source_url": by_name[slug]["source_url"],
                }
                for slug in suite
            ],
        }
        rem_proc = self.run_cli(
            ["remediate", "--plan", "-", "--home-dir", str(self.home), "--config", str(self.config_path)],
            stdin_text=json.dumps(rem_plan),
        )
        self.assertEqual(rem_proc.returncode, 0, msg=rem_proc.stderr)

        # Vault holds all 6 skills with canonical GitHub URL & no .git; nested skill dirs & empty skills/ pruned; .git kept
        index_text = (self.vault_dir / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/mattpocock/skills", index_text)
        self.assertIn("mattpocock-skills", index_text)
        for slug in suite:
            self.assertTrue((self.vault_dir / "skills" / slug / "SKILL.md").is_file())
            self.assertFalse((self.vault_dir / "skills" / slug / ".git").exists())
            self.assertFalse((cloned / "skills" / slug).exists())
        self.assertFalse((cloned / "skills").exists())
        self.assertTrue((cloned / ".git").is_dir())

    def test_symlinked_external_clone_worktree_home_dotfiles_and_npx_symlinks(self) -> None:
        agents = self.home / ".agents" / "skills"
        claude = self.home / ".claude" / "skills"
        agents.mkdir(parents=True, exist_ok=True)
        claude.mkdir(parents=True, exist_ok=True)

        # Fake $HOME/.git dotfiles repo that MUST NEVER be attributed to harness skills
        _git(self.home, "init")
        _git(self.home, "remote", "add", "origin", "git@github.com:user/dotfiles.git")

        # External repo 1: per-skill symlinks + npx-style cross-harness symlink
        meta_repo = self.root / "external_repos" / "meta-suite"
        meta_repo.mkdir(parents=True, exist_ok=True)
        _git(meta_repo, "init")
        for slug in ("meta-arch", "meta-docs", "meta-qa"):
            self.write_skill(meta_repo / "skills", slug, description=f"Meta engineering skill {slug}.")
        _git(meta_repo, "add", ".")
        _git(meta_repo, "commit", "-m", "meta suite")
        _git(meta_repo, "remote", "add", "origin", "https://github.com/meta/companion-skills.git")

        for slug in ("meta-arch", "meta-docs", "meta-qa"):
            self._symlink_or_skip(meta_repo / "skills" / slug, agents / slug)
        # Cross-harness npx-style symlink in ~/.claude/skills/meta-arch -> ~/.agents/skills/meta-arch
        self._symlink_or_skip(agents / "meta-arch", claude / "meta-arch")

        # External repo 2: git worktree (.git file with gitdir: + commondir) whole-repo symlink
        wt_main = self.root / "external_repos" / "wt-origin"
        wt_main.mkdir(parents=True, exist_ok=True)
        _git(wt_main, "init")
        self.write_skill(wt_main / "skills", "wt-alpha", description="Worktree companion alpha.")
        self.write_skill(wt_main / "skills", "wt-beta", description="Worktree companion beta.")
        _git(wt_main, "add", ".")
        _git(wt_main, "commit", "-m", "worktree base")
        _git(wt_main, "remote", "add", "origin", "https://github.com/acme/worktree-tools.git")
        wt_checkout = self.root / "external_repos" / "wt-checkout"
        _git(wt_main, "worktree", "add", str(wt_checkout), "-b", "feature-wt")
        self._symlink_or_skip(wt_checkout, agents / "wt-suite")

        # Unrelated top-level skill (no provenance; must NOT inherit $HOME/.git dotfiles!)
        self.write_skill(agents, "dotfile-neighbor", description="Standalone neighbor helper.")

        scan_proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(scan_proc.returncode, 0, msg=scan_proc.stderr)
        out = json.loads(scan_proc.stdout)
        by_name = {s["name"]: s for s in out["skills"]}

        self.assertIsNone(by_name["dotfile-neighbor"]["source_repo"])
        for slug in ("meta-arch", "meta-docs", "meta-qa"):
            self.assertEqual(by_name[slug]["source_repo"], "github.com/meta/companion-skills")
            self.assertEqual(by_name[slug]["source_repo_signal"], "git-origin")
        for slug in ("wt-alpha", "wt-beta"):
            self.assertEqual(by_name[slug]["source_repo"], "github.com/acme/worktree-tools")
            self.assertEqual(by_name[slug]["source_repo_signal"], "git-origin")

        # Init Vault and adopt meta-arch: must archive to Vault WITHOUT dirtying meta_repo
        self.assertEqual(
            self.run_cli(
                ["init", "--vault", str(self.vault_dir), "--discover-global-dirs", "--home-dir", str(self.home)]
            ).returncode,
            0,
        )
        adopt_proc = self.run_cli(
            [
                "adopt",
                "meta-arch",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(adopt_proc.returncode, 0, msg=adopt_proc.stderr)
        adopt_out = json.loads(adopt_proc.stdout)
        self.assertTrue(adopt_out["skipped_symlink_stamps"])
        self.assertFalse((meta_repo / "skills" / "meta-arch" / ".curator-meta.json").exists())
        status_proc = _git(meta_repo, "status", "--porcelain")
        self.assertEqual(status_proc.stdout.strip(), "")
        vault_meta = json.loads((self.vault_dir / "skills" / "meta-arch" / ".curator-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(vault_meta["source_url"], "https://github.com/meta/companion-skills")

        # Re-scan confirms symlinked adopted skill is recognized as tracked=True via Vault
        scan2 = json.loads(
            self.run_cli(["scan", "--home-dir", str(self.home), "--config", str(self.config_path)]).stdout
        )
        by_name2 = {s["name"]: s for s in scan2["skills"]}
        self.assertTrue(by_name2["meta-arch"]["tracked"])

        # Partial single-skill demote inside whole-repo symlink container MUST fail with SYMLINKED_REPO_CONTAINER
        partial_demote = self.run_cli(
            [
                "demote",
                "wt-alpha",
                "--from",
                "global",
                "--discover-global-dirs",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(partial_demote.returncode, 0)
        partial_err = json.loads(partial_demote.stderr)
        self.assertEqual(partial_err["error_code"], "SYMLINKED_REPO_CONTAINER")

        partial_dry = self.run_cli(
            [
                "remediate",
                "--dry-run",
                "--demote",
                "wt-alpha",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertNotEqual(partial_dry.returncode, 0)
        self.assertEqual(json.loads(partial_dry.stderr)["error_code"], "SYMLINKED_REPO_CONTAINER")
        self.assertTrue((agents / "wt-suite").is_symlink())
        self.assertTrue((wt_checkout / "skills" / "wt-alpha" / "SKILL.md").is_file())

        # Full-container remediate demoting both wt-alpha and wt-beta unlinks wt-suite and leaves external worktree untouched
        rem_wt = self.run_cli(
            [
                "remediate",
                "--demote",
                "wt-alpha",
                "--demote",
                "wt-beta",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(rem_wt.returncode, 0, msg=rem_wt.stderr)
        self.assertFalse(os.path.lexists(str(agents / "wt-suite")))
        self.assertTrue((wt_checkout / "skills" / "wt-alpha" / "SKILL.md").is_file())
        self.assertTrue((wt_checkout / "skills" / "wt-beta" / "SKILL.md").is_file())

        # Demote per-skill symlink + cross-harness npx symlink: unlinks both symlinks, preserves external meta_repo
        dem_meta = self.run_cli(
            [
                "demote",
                "meta-arch",
                "--from",
                "global",
                "--discover-global-dirs",
                "--home-dir",
                str(self.home),
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(dem_meta.returncode, 0, msg=dem_meta.stderr)
        self.assertFalse(os.path.lexists(str(agents / "meta-arch")))
        self.assertFalse(os.path.lexists(str(claude / "meta-arch")))
        self.assertTrue((meta_repo / "skills" / "meta-arch" / "SKILL.md").is_file())

    def test_unzipped_container_dir_with_and_without_package_json_and_hybrid_dedup(self) -> None:
        agents = self.home / ".agents" / "skills"
        # Container 1: unzipped GitHub archive with package.json repository
        sepia_root = agents / "sepia-main"
        sepia_root.mkdir(parents=True, exist_ok=True)
        (sepia_root / "package.json").write_text(
            json.dumps({"name": "sepia", "repository": {"type": "git", "url": "git+https://github.com/Nanako0129/sepia.git"}}),
            encoding="utf-8",
        )
        for slug in ("sepia-write", "sepia-review", "sepia-refactor"):
            self.write_skill(sepia_root / "skills", slug, description=f"Sepia prose helper {slug}.")

        # Container 2: unzipped folder WITHOUT .git and WITHOUT package.json
        pack_root = agents / "custom-pack-main" / "skills"
        for slug in ("pack-one", "pack-two"):
            self.write_skill(pack_root, slug, description=f"Custom pack skill {slug}.")

        # Hybrid depth-1 repo: root SKILL.md + skills/hybrid-repo/SKILL.md + skills/hybrid-helper/SKILL.md
        hybrid = self.write_skill(agents, "hybrid-repo", description="Root hybrid repo skill.")
        self.write_skill(hybrid / "skills", "hybrid-repo", description="Dedicated hybrid repo skill.")
        self.write_skill(hybrid / "skills", "hybrid-helper", description="Hybrid companion helper.")

        # Standalone skill
        self.write_skill(agents, "unrelated-one", description="Unrelated standalone utility.")

        scan_proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(scan_proc.returncode, 0, msg=scan_proc.stderr)
        out = json.loads(scan_proc.stdout)
        by_name = {s["name"]: s for s in out["skills"]}

        self.assertEqual(by_name["sepia-write"]["source_repo"], "github.com/nanako0129/sepia")
        self.assertEqual(by_name["sepia-write"]["source_repo_signal"], "package-json")
        self.assertEqual(by_name["pack-one"]["source_repo"], "container:custom-pack-main")
        self.assertEqual(by_name["pack-one"]["source_repo_signal"], "container-dir")
        self.assertEqual(by_name["pack-two"]["source_repo"], "container:custom-pack-main")
        self.assertIsNone(by_name["unrelated-one"]["source_repo"])

        # Hybrid repo deduplicated cleanly (1 entry for hybrid-repo pointing to skills/hybrid-repo, plus hybrid-helper)
        self.assertEqual(len(by_name["hybrid-repo"]["locations"]), 1)
        self.assertTrue(by_name["hybrid-repo"]["locations"][0]["path"].endswith(os.path.join("hybrid-repo", "skills", "hybrid-repo")))
        self.assertEqual(by_name["hybrid-helper"]["source_repo"], "container:hybrid-repo")

    def test_single_skill_git_clone_excludes_dot_git_everywhere(self) -> None:
        agents = self.home / ".agents" / "skills"
        skill_dir = self.write_skill(
            agents,
            "solo-git-skill",
            description="Single-skill git clone with remote origin.",
        )
        _git(skill_dir, "init")
        _git(skill_dir, "remote", "add", "origin", "https://github.com/acme/solo-git-skill.git")
        _git(skill_dir, "add", ".")
        _git(skill_dir, "commit", "-m", "solo skill")

        scan_proc = self.run_cli(["scan", "--home-dir", str(self.home)])
        self.assertEqual(scan_proc.returncode, 0, msg=scan_proc.stderr)
        out = json.loads(scan_proc.stdout)
        by_name = {s["name"]: s for s in out["skills"]}

        # .git/hooks/*.sample must NOT trigger false executable_scripts_present
        self.assertEqual(by_name["solo-git-skill"]["scripts_detected"], [])
        self.assertEqual(by_name["solo-git-skill"]["source_repo"], "github.com/acme/solo-git-skill")
        self.assertEqual(by_name["solo-git-skill"]["source_repo_signal"], "git-origin")

        self.assertEqual(
            self.run_cli(
                ["init", "--vault", str(self.vault_dir), "--discover-global-dirs", "--home-dir", str(self.home)]
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                ["adopt", "solo-git-skill", "--home-dir", str(self.home), "--config", str(self.config_path)]
            ).returncode,
            0,
        )
        self.assertFalse((self.vault_dir / "skills" / "solo-git-skill" / ".git").exists())
        vault_meta = json.loads((self.vault_dir / "skills" / "solo-git-skill" / ".curator-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(vault_meta["source_url"], "https://github.com/acme/solo-git-skill")

        dem_proc = self.run_cli(
            [
                "demote",
                "solo-git-skill",
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
        self.assertFalse(skill_dir.exists())
        self.assertFalse((self.vault_dir / "skills" / "solo-git-skill" / ".git").exists())


if __name__ == "__main__":
    unittest.main()
