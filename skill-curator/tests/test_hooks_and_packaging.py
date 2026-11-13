from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "skills" / "skill-curator" / "scripts"
HOOK_SCRIPT = SCRIPTS_DIR / "hook_intercept.py"
CURATOR_SCRIPT = SCRIPTS_DIR / "curator.py"


class TestHooksAndPackaging(unittest.TestCase):
    def run_hook(self, raw_stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=raw_stdin,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def assert_antigravity_triggered(self, user_input: str, invocation_num: int | None = None) -> None:
        payload: dict[str, object] = {"lastUserInput": user_input}
        if invocation_num is not None:
            payload["invocationNum"] = invocation_num
        proc = self.run_hook(json.dumps(payload))
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\ninput={user_input}")
        out = json.loads(proc.stdout)
        self.assertIn("injectSteps", out, msg=f"Expected trigger for: {user_input}")
        steps = out["injectSteps"]
        self.assertEqual(len(steps), 1)
        msg = steps[0]["systemMessage"]["systemMessage"]
        self.assertIn("skill-curator", msg.lower())
        self.assertLess(len(msg.split()), 120)

    def assert_antigravity_ignored(self, user_input: str, invocation_num: int | None = None) -> None:
        payload: dict[str, object] = {"lastUserInput": user_input}
        if invocation_num is not None:
            payload["invocationNum"] = invocation_num
        proc = self.run_hook(json.dumps(payload))
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\ninput={user_input}")
        self.assertEqual(json.loads(proc.stdout), {}, msg=f"Expected ignore for: {user_input}")

    def assert_claude_triggered(self, prompt: str) -> None:
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
        proc = self.run_hook(json.dumps(payload))
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nprompt={prompt}")
        out = json.loads(proc.stdout)
        self.assertIn("hookSpecificOutput", out, msg=f"Expected trigger for: {prompt}")
        hook_out = out["hookSpecificOutput"]
        self.assertEqual(hook_out["hookEventName"], "UserPromptSubmit")
        ctx = hook_out["additionalContext"]
        self.assertIn("skill-curator", ctx.lower())
        self.assertLess(len(ctx.split()), 120)

    def assert_claude_ignored(self, prompt: str) -> None:
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
        proc = self.run_hook(json.dumps(payload))
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nprompt={prompt}")
        self.assertEqual(json.loads(proc.stdout), {}, msg=f"Expected ignore for: {prompt}")

    def test_antigravity_preinvocation_triggers_and_ignores(self) -> None:
        # Triggers
        self.assert_antigravity_triggered("https://github.com/vercel-labs/agent-skills")
        self.assert_antigravity_triggered(
            "帮我看看这个 https://github.com/anthropics/skills/tree/main/skills/pdf"
        )
        self.assert_antigravity_triggered(
            "Install https://github.com/acme/skills/blob/main/pdf-helper/SKILL.md"
        )
        self.assert_antigravity_triggered("Check https://skills.sh/vercel-labs/agent-skills")
        self.assert_antigravity_triggered("Run npx skills add owner/repo")
        self.assert_antigravity_triggered(
            "帮我把 wechat-writing 场景标签的 skills 挂载到当前项目"
        )
        self.assert_antigravity_triggered("Search my Skill Vault for database migration skills")
        self.assert_antigravity_triggered("给我的全局 skills 做个健康检查")
        self.assert_antigravity_triggered("帮我装个画架构图的 skill")
        self.assert_antigravity_triggered("https://github.com/vercel-labs/agent-skills", invocation_num=0)

        # CJK text stuck directly after URL without whitespace should not pollute URL
        stick_proc = self.run_hook(
            json.dumps({"lastUserInput": "https://github.com/Nanako0129/sepia帮我装一下这个 Skills"})
        )
        self.assertEqual(stick_proc.returncode, 0)
        stick_msg = json.loads(stick_proc.stdout)["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Detected URL(s): https://github.com/Nanako0129/sepia.", stick_msg)

        # Markdown-wrapped URL strips trailing punctuation cleanly
        md_proc = self.run_hook(
            json.dumps({"lastUserInput": "Check [sepia](https://github.com/Nanako0129/sepia)."})
        )
        self.assertEqual(md_proc.returncode, 0)
        md_msg = json.loads(md_proc.stdout)["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Detected URL(s): https://github.com/Nanako0129/sepia.", md_msg)

        # Non-triggers
        self.assert_antigravity_ignored("Refactor JWT middleware in auth.py")
        self.assert_antigravity_ignored("帮我修复 Header.tsx 的样式对齐问题")
        self.assert_antigravity_ignored("帮我把这段 skill 解析逻辑封装成函数")
        self.assert_antigravity_ignored("把这段配置封装成函数")
        self.assert_antigravity_ignored(
            "Fix https://github.com/acme/webapp/issues/42 and check "
            "https://github.com/acme/webapp/blob/main/src/app.py"
        )
        # Continuation step guard (Antigravity invocationNum is 0-indexed: >= 1 is continuation)
        self.assert_antigravity_ignored(
            "Install https://github.com/vercel-labs/agent-skills",
            invocation_num=1,
        )
        self.assert_antigravity_ignored(
            "Install https://github.com/vercel-labs/agent-skills",
            invocation_num=2,
        )

    def test_claude_user_prompt_submit_triggers_and_ignores(self) -> None:
        # Triggers
        self.assert_claude_triggered("https://github.com/vercel-labs/agent-skills")
        self.assert_claude_triggered(
            "Inspect https://github.com/anthropics/skills/tree/main/skills/pdf"
        )
        self.assert_claude_triggered(
            "Install https://github.com/acme/skills/blob/main/pdf-helper/SKILL.md"
        )
        self.assert_claude_triggered("帮我把 wechat-writing 场景标签的 skills 挂载到当前项目")
        self.assert_claude_triggered("Search my Skill Vault for database migration skills")

        # Non-triggers
        self.assert_claude_ignored("Refactor JWT middleware in auth.py")
        self.assert_claude_ignored("帮我修复 Header.tsx 的样式对齐问题")
        self.assert_claude_ignored(
            "Fix https://github.com/acme/webapp/issues/42 and check "
            "https://github.com/acme/webapp/blob/main/src/app.py"
        )

    def test_hook_fail_open_on_empty_and_malformed_stdin(self) -> None:
        for raw in ("", "   \n", "{bad json", "null", "[]"):
            proc = self.run_hook(raw)
            self.assertEqual(proc.returncode, 0, msg=f"Failed on stdin={raw!r}: {proc.stderr}")
            self.assertEqual(json.loads(proc.stdout), {})

    def test_plugin_manifests_shell_hooks_docs_and_dogfood_inspect(self) -> None:
        # 1) Manifests
        agy_manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(agy_manifest["name"], "skill-curator")
        self.assertLessEqual(
            set(agy_manifest.keys()),
            {
                "name",
                "displayName",
                "version",
                "description",
                "suggestedPrompts",
                "logo",
                "author",
                "homepage",
                "repository",
                "disabled",
            },
        )
        if "logo" in agy_manifest:
            self.assertTrue((PLUGIN_ROOT / agy_manifest["logo"]).is_file())

        claude_manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(claude_manifest["name"], "skill-curator")

        # 2) Antigravity hooks.json + shell execution from plugin root
        agy_hooks = json.loads((PLUGIN_ROOT / "hooks.json").read_text(encoding="utf-8"))
        agy_cmd = agy_hooks["skill-curator-intercept"]["PreInvocation"][0]["command"]
        self.assertEqual(
            agy_cmd, "python3 ./skills/skill-curator/scripts/hook_intercept.py"
        )
        agy_sh = subprocess.run(
            ["sh", "-c", agy_cmd],
            cwd=str(PLUGIN_ROOT),
            input=json.dumps({"lastUserInput": "https://github.com/vercel-labs/agent-skills"}),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(agy_sh.returncode, 0, msg=agy_sh.stderr)
        self.assertIn("injectSteps", json.loads(agy_sh.stdout))

        # 3) Claude Code hooks/hooks.json + shell execution with CLAUDE_PLUGIN_ROOT
        claude_hooks = json.loads(
            (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        claude_cmd = claude_hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", claude_cmd)
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        claude_sh = subprocess.run(
            ["sh", "-c", claude_cmd],
            input=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Install https://github.com/acme/skills/blob/main/pdf/SKILL.md",
                }
            ),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(claude_sh.returncode, 0, msg=claude_sh.stderr)
        self.assertIn("hookSpecificOutput", json.loads(claude_sh.stdout))

        # 4) Rules, adapters, and reference docs exist & size constraints hold
        agents_rules = PLUGIN_ROOT / "rules" / "AGENTS.md"
        self.assertTrue(agents_rules.is_file())
        self.assertGreater(agents_rules.stat().st_size, 0)
        self.assertLess(agents_rules.stat().st_size, 24 * 1024)

        for rel in (
            "CLAUDE.md",
            "README.md",
            "skills/skill-curator/SKILL.md",
            "skills/skill-curator/references/config-schema.md",
            "skills/skill-curator/references/vault-index-template.md",
        ):
            p = PLUGIN_ROOT / rel
            self.assertTrue(p.is_file(), msg=f"Missing required file: {rel}")
            self.assertGreater(p.stat().st_size, 0, msg=f"Empty file: {rel}")

        # 5) Dogfood self-audit of skill-curator/skills/skill-curator
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            vault = tmp_root / "vault"
            init_proc = subprocess.run(
                [sys.executable, str(CURATOR_SCRIPT), "init", "--vault", str(vault)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(init_proc.returncode, 0, msg=init_proc.stderr)

            insp_proc = subprocess.run(
                [
                    sys.executable,
                    str(CURATOR_SCRIPT),
                    "inspect",
                    "--source-dir",
                    str(PLUGIN_ROOT / "skills" / "skill-curator"),
                    "--config",
                    str(vault / "config.json"),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(insp_proc.returncode, 0, msg=insp_proc.stderr)
            insp = json.loads(insp_proc.stdout)
            self.assertTrue(insp["name_check"]["valid"])
            self.assertEqual(insp["description_check"]["issues"], [])
            self.assertEqual(insp["instruction_check"]["issues"], [])
            self.assertLessEqual(insp["instruction_check"]["line_count"], 500)

            # 6) CONFIG_NOT_FOUND when no --config, no SKILL_CURATOR_CONFIG, and no default file in HOME
            iso_env = os.environ.copy()
            iso_env.pop("SKILL_CURATOR_CONFIG", None)
            iso_env["HOME"] = str(tmp_root / "clean-home")
            Path(iso_env["HOME"]).mkdir()
            no_cfg = subprocess.run(
                [sys.executable, str(CURATOR_SCRIPT), "health"],
                capture_output=True,
                text=True,
                env=iso_env,
                timeout=10,
            )
            self.assertNotEqual(no_cfg.returncode, 0)
            self.assertEqual(json.loads(no_cfg.stderr)["error_code"], "CONFIG_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
