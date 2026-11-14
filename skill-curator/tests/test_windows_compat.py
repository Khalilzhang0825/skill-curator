from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "skills" / "skill-curator" / "scripts"
HOOK_PY = SCRIPTS_DIR / "hook_intercept.py"
CURATOR_PY = SCRIPTS_DIR / "curator.py"
RUN_HOOK_CMD = PLUGIN_ROOT / "hooks" / "run-hook.cmd"
CURATOR_CMD = SCRIPTS_DIR / "curator.cmd"
PACKAGE_SCRIPT = REPO_ROOT / "scripts" / "package.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestWindowsAndCrossOsCompat(unittest.TestCase):
    def test_gitattributes_and_polyglot_launchers_ascii_lf(self) -> None:
        gitattributes = REPO_ROOT / ".gitattributes"
        self.assertTrue(gitattributes.is_file(), msg="Missing .gitattributes at repo root")
        attr_text = gitattributes.read_text(encoding="utf-8")
        for ext in ("*.cmd", "*.py", "*.sh", "*.json", "*.md"):
            self.assertIn(ext, attr_text)
            self.assertIn("eol=lf", attr_text)

        for launcher in (RUN_HOOK_CMD, CURATOR_CMD):
            self.assertTrue(launcher.is_file(), msg=f"Missing launcher: {launcher}")
            raw = launcher.read_bytes()
            self.assertNotIn(b"\r", raw, msg=f"CRLF byte found in {launcher}")
            raw.decode("ascii")  # pure ASCII only
            self.assertTrue(raw.startswith(b": << 'CMDBLOCK'\n"))

    def test_polyglot_launchers_fallback_from_broken_python3_and_stripped_exec_bit(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX sh + fake bin stub test runs on Unix shells")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub_bin = tmp_path / "stub_bin"
            stub_bin.mkdir()

            # Simulate broken WindowsApps python3 / dead pyenv shim emitting noise and exiting 9009 % 256 (or 127)
            fake_py3 = stub_bin / "python3"
            fake_py3.write_text(
                "#!/bin/sh\necho 'Microsoft Store stub error' >&2\nexit 127\n",
                encoding="utf-8",
            )
            fake_py3.chmod(0o755)

            # Working `python` wrapper delegating to sys.executable
            real_py = stub_bin / "python"
            real_py.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
                encoding="utf-8",
            )
            real_py.chmod(0o755)

            # Also verify fallback to `sh ...` works even if `+x` bit is stripped (e.g. from unzip)
            plugin_copy = tmp_path / "plugin copy"
            shutil.copytree(str(PLUGIN_ROOT), str(plugin_copy), ignore=shutil.ignore_patterns("__pycache__"))
            noexec_hook = plugin_copy / "hooks" / "run-hook.cmd"
            noexec_curator = plugin_copy / "skills" / "skill-curator" / "scripts" / "curator.cmd"
            noexec_hook.chmod(0o644)
            noexec_curator.chmod(0o644)

            env = os.environ.copy()
            env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"

            # 0) With +x intact and broken python3 stub: stderr is completely empty
            agy_hooks = json.loads((plugin_copy / "hooks.json").read_text(encoding="utf-8"))
            agy_cmd = agy_hooks["skill-curator-intercept"]["PreInvocation"][0]["command"]
            noexec_hook.chmod(0o755)
            exec_proc = subprocess.run(
                ["sh", "-c", agy_cmd],
                cwd=str(plugin_copy),
                input=json.dumps({"lastUserInput": "https://github.com/vercel-labs/agent-skills"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=10,
            )
            self.assertEqual(exec_proc.returncode, 0, msg=exec_proc.stderr)
            self.assertEqual(exec_proc.stderr, "")
            self.assertIn("injectSteps", json.loads(exec_proc.stdout))

            # 1) Hook command string from hooks.json with stripped +x still succeeds via `|| sh` fallback
            noexec_hook.chmod(0o644)
            hook_proc = subprocess.run(
                ["sh", "-c", agy_cmd],
                cwd=str(plugin_copy),
                input=json.dumps({"lastUserInput": "https://github.com/vercel-labs/agent-skills"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=10,
            )
            self.assertEqual(hook_proc.returncode, 0, msg=hook_proc.stderr)
            self.assertIn("injectSteps", json.loads(hook_proc.stdout))

            # 2) Claude hooks/hooks.json with spaces in CLAUDE_PLUGIN_ROOT and failing python3 (with +x intact)
            noexec_hook.chmod(0o755)
            claude_hooks = json.loads((plugin_copy / "hooks" / "hooks.json").read_text(encoding="utf-8"))
            claude_cmd = claude_hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            env["CLAUDE_PLUGIN_ROOT"] = str(plugin_copy)
            claude_proc = subprocess.run(
                ["sh", "-c", claude_cmd],
                input=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "https://github.com/vercel-labs/agent-skills",
                    }
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=10,
            )
            self.assertEqual(claude_proc.returncode, 0, msg=claude_proc.stderr)
            self.assertEqual(claude_proc.stderr, "")
            self.assertIn("hookSpecificOutput", json.loads(claude_proc.stdout))

            # 3) curator.cmd runs cleanly via `sh` with failing python3 stub -> falls back to `python`
            vault = tmp_path / "vault"
            cur_proc = subprocess.run(
                ["sh", str(noexec_curator), "init", "--vault", str(vault)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=10,
            )
            self.assertEqual(cur_proc.returncode, 0, msg=cur_proc.stderr)
            self.assertTrue(json.loads(cur_proc.stdout)["ok"])
            self.assertTrue((vault / "config.json").is_file())

    def test_utf8_stdio_and_git_under_ascii_c_locale(self) -> None:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)

        # Hook handles Chinese UTF-8 prompt under LC_ALL=C without crashing or dropping match
        cjk_prompt = "https://github.com/Nanako0129/sepia 帮我装一下这个 Skills"
        hook_proc = subprocess.run(
            [sys.executable, str(HOOK_PY)],
            input=json.dumps({"lastUserInput": cjk_prompt}, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(hook_proc.returncode, 0, msg=hook_proc.stderr.decode("utf-8", "replace"))
        hook_out = json.loads(hook_proc.stdout.decode("utf-8"))
        self.assertIn("injectSteps", hook_out)

        # Hook handles raw GBK / CP936 bytes emitted by Windows cmd.exe `chcp 936`
        gbk_bytes = json.dumps(
            {"lastUserInput": "扫描并整理我本地已有的 skills"}, ensure_ascii=False
        ).encode("gb18030")
        gbk_proc = subprocess.run(
            [sys.executable, str(HOOK_PY)],
            input=gbk_bytes,
            capture_output=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(gbk_proc.returncode, 0, msg=gbk_proc.stderr.decode("utf-8", "replace"))
        gbk_proc.stdout.decode("ascii")  # ensure_ascii=True keeps stdout safe under chcp 936
        self.assertIn("injectSteps", json.loads(gbk_proc.stdout.decode("utf-8")))

        # CLI store/search + git fetch with Chinese commit/description under LC_ALL=C
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            work = root / "work-中文"
            bare = root / "repo-中文.git"
            work.mkdir()
            git_env = {
                **env,
                "GIT_AUTHOR_NAME": "测试用户",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "测试用户",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            }
            for cmd in (
                ["git", "init", "-b", "main"],
            ):
                subprocess.run(cmd, cwd=str(work), check=True, capture_output=True, env=git_env)
            (work / "SKILL.md").write_text(
                "---\nname: cjk-helper\ndescription: 中文文档排版助手与架构图生成器。\n---\n\n# CJK Helper\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "-A"], cwd=str(work), check=True, capture_output=True, env=git_env)
            subprocess.run(
                ["git", "commit", "-m", "初始化中文技能"],
                cwd=str(work),
                check=True,
                capture_output=True,
                env=git_env,
            )
            subprocess.run(
                ["git", "clone", "--bare", str(work), str(bare)],
                check=True,
                capture_output=True,
                env=git_env,
            )

            dest = root / "staged"
            fetch_proc = subprocess.run(
                [sys.executable, str(CURATOR_PY), "fetch", bare.as_uri(), "--dest-dir", str(dest)],
                capture_output=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(fetch_proc.returncode, 0, msg=fetch_proc.stderr.decode("utf-8", "replace"))
            fetch_out = json.loads(fetch_proc.stdout.decode("utf-8"))
            self.assertEqual(fetch_out["skill"]["name"], "cjk-helper")

            subprocess.run(
                [sys.executable, str(CURATOR_PY), "init", "--vault", str(vault)],
                check=True,
                capture_output=True,
                env=env,
            )
            store_proc = subprocess.run(
                [
                    sys.executable,
                    str(CURATOR_PY),
                    "store",
                    "--source-dir",
                    str(dest / "cjk-helper"),
                    "--tags",
                    "中文排版",
                    "--config",
                    str(vault / "config.json"),
                ],
                capture_output=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(store_proc.returncode, 0, msg=store_proc.stderr.decode("utf-8", "replace"))
            search_proc = subprocess.run(
                [sys.executable, str(CURATOR_PY), "search", "中文排版", "--config", str(vault / "config.json")],
                capture_output=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(search_proc.returncode, 0, msg=search_proc.stderr.decode("utf-8", "replace"))
            self.assertIn("中文排版", search_proc.stdout.decode("utf-8"))

    def test_git_env_cross_platform_and_tool_not_found(self) -> None:
        import fetcher

        git_env = fetcher._git_env()
        self.assertEqual(git_env.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(git_env.get("GCM_INTERACTIVE"), "never")
        self.assertEqual(git_env.get("GIT_ASKPASS"), "")
        self.assertEqual(git_env.get("SSH_ASKPASS"), "")
        self.assertEqual(git_env.get("SSH_ASKPASS_REQUIRE"), "never")
        self.assertNotIn("/bin/echo", git_env.values())

        with mock.patch("fetcher.subprocess.run", side_effect=FileNotFoundError("git")):
            with self.assertRaises(fetcher.CuratorError) as ctx:
                fetcher._run_git(["--version"], timeout=5)
            self.assertEqual(ctx.exception.payload.get("error_code"), "TOOL_NOT_FOUND")
            self.assertEqual(ctx.exception.payload.get("tool"), "git")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("fetcher.subprocess.run", side_effect=FileNotFoundError("curl")):
                with self.assertRaises(fetcher.CuratorError) as ctx2:
                    fetcher._fetch_single_file("https://example.com/SKILL.md", Path(tmp), timeout=5)
                self.assertEqual(ctx2.exception.payload.get("error_code"), "TOOL_NOT_FOUND")
                self.assertEqual(ctx2.exception.payload.get("tool"), "curl")

    def test_rmtree_safe_removes_readonly_git_packfiles_and_demote(self) -> None:
        import common

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack_dir = root / "repo" / ".git" / "objects" / "pack"
            pack_dir.mkdir(parents=True)
            pack_file = pack_dir / "pack-123456.pack"
            pack_file.write_bytes(b"PACKDATA")
            pack_file.chmod(stat.S_IREAD)
            pack_dir.chmod(stat.S_IREAD | stat.S_IXUSR)

            common.rmtree_safe(root / "repo")
            self.assertFalse((root / "repo").exists())

            # Also verify demote cleans up skills containing read-only files cleanly
            vault = root / "vault"
            gdir = root / "global_skills"
            gdir.mkdir()
            src = root / "incoming" / "ro-skill"
            src.mkdir(parents=True)
            (src / "SKILL.md").write_text("---\nname: ro-skill\ndescription: Read-only test skill.\n---\n", encoding="utf-8")
            (src / "data.bin").write_bytes(b"123")

            subprocess.run(
                [sys.executable, str(CURATOR_PY), "init", "--vault", str(vault), "--global-dir", str(gdir)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [sys.executable, str(CURATOR_PY), "store", "--source-dir", str(src), "--config", str(vault / "config.json")],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [sys.executable, str(CURATOR_PY), "mount", "ro-skill", "--target", "global", "--config", str(vault / "config.json")],
                check=True,
                capture_output=True,
            )
            (gdir / "ro-skill" / "data.bin").chmod(stat.S_IREAD)
            demote_proc = subprocess.run(
                [sys.executable, str(CURATOR_PY), "demote", "ro-skill", "--from", "global", "--config", str(vault / "config.json")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(demote_proc.returncode, 0, msg=demote_proc.stderr)
            self.assertFalse((gdir / "ro-skill").exists())

    def test_cmd_script_extension_detected_and_path_normcase(self) -> None:
        import auditor
        import common

        self.assertIn(".cmd", auditor.SCRIPT_EXTENSIONS)
        self.assertTrue(common.is_path_within(os.path.normcase("/tmp/root"), os.path.normcase("/tmp/root/sub/file.txt")))

        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "cmd-skill"
            (skill_dir / "scripts").mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: cmd-skill\ndescription: Automates build pipelines cleanly.\n---\n\n# Body\n",
                encoding="utf-8",
            )
            (skill_dir / "scripts" / "run.cmd").write_text("@echo off\necho hi\n", encoding="utf-8")
            cfg = {"vault_path": str(Path(tmp) / "vault"), "global_core_dirs": [], "global_cap": 8}
            res = auditor.inspect_skill(skill_dir, cfg)
            self.assertIn("scripts/run.cmd", res["scripts_detected"])

    def test_package_py_lists_cmd_launchers_executable(self) -> None:
        pkg_text = PACKAGE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("run-hook.cmd", pkg_text)
        self.assertIn("curator.cmd", pkg_text)


if __name__ == "__main__":
    unittest.main()
