<div align="center">

<img src="./assets/logo.png" alt="Skill Curator logo" width="88" />

# Skill Curator (Plugin Bundle)

[Project README (English)](https://github.com/Khalilzhang0825/skill-curator/blob/main/README.md) | [中文完整文档](https://github.com/Khalilzhang0825/skill-curator/blob/main/README.zh-CN.md)

</div>

Cross-agent skill curation across three physical tiers: a capped **Global Core**, copy-mounted **Project Workspaces**, and an out-of-band **Skill Vault** indexed by `INDEX.md` with **Scene Tags**.

## Requirements

- Python 3.9+ (standard library only; zero PyPI/Node runtime dependencies)
- `git` and `curl`
- Optional: Node.js / `npx` if installing standalone skill via `npx skills add`

## Quick Start

1. Install via npm `skills` CLI (Codex / Cursor / OpenCode / Antigravity standalone skill) or extract this full plugin bundle:

```bash
# Option A: Official npx skills CLI (e.g. Codex)
npx skills add Khalilzhang0825/skill-curator -a codex -g --copy -y

# Option B: Initialize Skill Vault directly from this plugin bundle
python3 ./skills/skill-curator/scripts/curator.py init \
  --vault ~/.agents/skill-vault \
  --global-dir ~/.agents/skills \
  --global-dir ~/.gemini/config/skills \
  --config ~/.config/skill-curator/config.json
```

2. Use your agent normally in chat—paste any GitHub skill URL or ask to install, search, Scene-Tag mount, or audit skills—or invoke the CLI directly:

```bash
python3 ./skills/skill-curator/scripts/curator.py fetch "https://github.com/owner/repo" --all
python3 ./skills/skill-curator/scripts/curator.py inspect --source-dir /tmp/staged/my-skill --project-dir .
python3 ./skills/skill-curator/scripts/curator.py store --source-dir /tmp/staged/my-skill --keywords "k1,k2" --tags "tag1"
python3 ./skills/skill-curator/scripts/curator.py mount-tag tag1 --project-dir .
python3 ./skills/skill-curator/scripts/curator.py health --project-dir .
```

## Included Harness Adapters

- **Antigravity**: `plugin.json` (`assets/logo.png`) + `hooks.json` (`PreInvocation` reading `lastUserInput` -> `injectSteps`) + `rules/AGENTS.md` + `skills/skill-curator/SKILL.md`.
- **Claude Code**: `.claude-plugin/plugin.json` + `hooks/hooks.json` (`UserPromptSubmit` reading `prompt` -> `hookSpecificOutput.additionalContext`) + `CLAUDE.md` + automatic `.claude/skills/<name>/` project mirroring.
- **Codex & Generic `AGENTS.md` Harnesses**: Works with `npx skills add ... -a codex -g --copy`; include `rules/AGENTS.md` in `~/.codex/AGENTS.md` or `~/.agents/AGENTS.md` for always-on curation rules.
