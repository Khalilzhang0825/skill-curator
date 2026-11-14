# Skill Curator (Claude Code Adapter)

Follow the portable curation rules in [rules/AGENTS.md](./rules/AGENTS.md) and load the `skill-curator` skill (`skills/skill-curator/SKILL.md`) whenever the user shares a skill URL or asks to install, scan/clean up local skills, mount, search, demote, or audit skills.

## Claude Code Mirroring

- Project Workspace mounts target `.agents/skills/<name>/` and automatically mirror to `.claude/skills/<name>/` when `.claude/` or `CLAUDE.md` is present (or when `claude_sync` is `"always"`).
- Never bypass curation by copying unvetted skills directly into `~/.claude/skills/` or `~/.agents/skills/`.
