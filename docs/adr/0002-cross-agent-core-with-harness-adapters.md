---
status: accepted
---

# Cross-agent core with harness-specific adapters

We ship the Skill Curator as a portable `AGENTS.md` + `agentskills.io`-compliant `SKILL.md` + zero-heavy-dependency (`git`, `curl`, `python3`) scripts core, paired with native plugin/hook adapters for Antigravity (`plugin.json`, `hooks.json`) and Claude Code (`.claude-plugin/`, `CLAUDE.md`). This gives universal compatibility across coding agents while delivering deterministic `PreInvocation` link-interception reminders in harnesses that support lifecycle hooks.
