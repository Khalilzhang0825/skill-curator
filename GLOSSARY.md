# Skill Curator

Cross-agent skill curation and lifecycle management system that routes incoming skills across global, project, and cold storage tiers without bloating agent context.

## Language

**Plugin**:
The complete distributable unit containing skills, rules, lifecycle hooks, and helper scripts.
_Avoid_: Extension, mega-skill, bundle

**Skill**:
A standalone capability folder centered on a `SKILL.md` file following the `agentskills.io` convention.
_Avoid_: Prompt file, macro, rule snippet

**Global Core**:
The strictly capped set of cross-project skills active across every session on a user's machine.
_Avoid_: Global dump, global library

**Project Workspace**:
The active repository or working folder whose `.agents/skills/` (or `.claude/skills/`) directory holds project-scoped skills.
_Avoid_: Repo skills, local cache

**Skill Vault**:
A standalone, version-controlled local directory outside all agent discovery paths where collected skills rest at zero context cost.
_Avoid_: Cold library, archive, backup folder

**Vault Index**:
The minimal `INDEX.md` catalog inside the Skill Vault listing each stored skill's name, purpose keywords, source URL, scene tags, and path.
_Avoid_: Vector DB, skill registry

**Scene Tag**:
A workflow label attached to skills in the Vault Index (such as `frontend` or `ml`) to support one-step batch mounting without separate pack directories.
_Avoid_: Scene pack, bundle folder

**Mount**:
The act of activating a skill from the Skill Vault into a Project Workspace or Global Core so an agent can discover and use it.
_Avoid_: Download, inject

**Curator**:
The workflow (skill, rules, and trigger hooks) that intercepts incoming skill links, audits quality and duplicates, and routes them to the right tier.
_Avoid_: Router, auto-installer
