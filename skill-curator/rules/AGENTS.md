# Skill Curator Rules

Always enforce three-tier skill curation whenever a user shares a GitHub or skills URL or asks to install, mount, search, or audit skills:

1. **No Direct Global Dumps**: Never copy or clone skills directly into `~/.agents/skills/` or `~/.claude/skills/`. Route all incoming skills through the `skill-curator` skill (`fetch` -> `inspect` -> user confirmation -> `store` / `mount`).
2. **Three-Tier Discipline**:
   - **Global Core** (default soft cap `8` deduplicated standalone skills, excluding `skill-curator` and external plugin bundles): Reserve strictly for everyday cross-project workflows. Enforce the cap gate and offer demote/project/vault choices when full.
   - **Project Workspace** (`.agents/skills/<name>/`, mirrored to `.claude/skills/<name>/` when Claude Code is active): Mount by **copy** (never symlinks) with `.curator-meta.json` provenance.
   - **Skill Vault**: Out-of-band local repository indexed by `INDEX.md` with **Scene Tags** at zero idle context cost. Default here when no active project is targeted.
3. **Preserve Upstream Integrity**: Never silently rewrite upstream `SKILL.md` bodies or descriptions, and never delete or demote active skills without explicit user confirmation.
4. **No Heavy Dependencies**: Use only the bundled Python 3 standard-library CLI (`curator.py`) invoking `git` and `curl`.
