# Skill Curator Rules

Always enforce three-tier skill curation whenever a user shares a GitHub or skills URL or asks to install, mount, search, or audit skills:

1. **No Direct Global Dumps**: Never copy or clone skills directly into `~/.agents/skills/` or `~/.claude/skills/`. Route incoming skills through `fetch` -> `inspect` -> user confirmation -> `store` / `mount`.
2. **Local Discovery & Safe Remediation**: Whenever asked to onboard, scan, diagnose, deduplicate, or clean up existing local skills across agent harnesses, run `scan` (`diagnose`) -> present the **Global Core** keep/demote plan (`<= 8` by default, keeping cohesive companion skills from the same repository together via `repo_groups` and flagging `soft_cap_exceeded` / `recommended_global_cap` if needed) with **Scene Tags** -> wait for explicit user confirmation -> run `remediate` / `adopt` / `demote`. Never silently delete skills or demote diverged duplicates without `--force` confirmation.
3. **Three-Tier Discipline**:
   - **Global Core** (default soft cap `8` deduplicated standalone skills, excluding `skill-curator` and external plugin bundles): Reserve strictly for everyday cross-project workflows. Enforce the cap gate and offer demote/project/vault choices when full.
   - **Project Workspace** (`.agents/skills/<name>/`, mirrored to `.claude/skills/<name>/` when Claude Code is active): Mount by **copy** (never symlinks) with `.curator-meta.json` provenance.
   - **Skill Vault**: Out-of-band local repository indexed by `INDEX.md` with **Scene Tags** at zero idle context cost. Default here when no active project is targeted.
4. **Preserve Upstream Integrity**: Never silently rewrite upstream `SKILL.md` bodies or descriptions, and never delete or demote active skills without explicit user confirmation.
5. **No Heavy Dependencies**: Use only the bundled Python 3 standard-library CLI (`curator.cmd` / `curator.py`) invoking `git` and `curl`.
