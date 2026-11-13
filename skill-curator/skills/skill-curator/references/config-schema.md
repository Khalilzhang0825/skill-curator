# Skill Curator Configuration Schema

Default config location: `~/.config/skill-curator/config.json` (override with `--config <path>` or `SKILL_CURATOR_CONFIG`).

```json
{
  "vault_path": "~/.agents/skill-vault",
  "global_core_dirs": [
    "~/.agents/skills"
  ],
  "global_cap": 8,
  "claude_sync": "auto",
  "stale_days": 30
}
```

## Fields

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `vault_path` | `string` | Required | Standalone directory holding `skills/`, `INDEX.md`, and optionally `config.json`. |
| `global_core_dirs` | `string[]` | `["~/.agents/skills"]` | Managed Global Core directories counted (deduplicated) against `global_cap`. |
| `global_cap` | `integer` | `8` | Soft cap (`>= 1`) on unique standalone skills in Global Core (excluding `skill-curator` and plugin bundles). |
| `claude_sync` | `string` | `"auto"` | Project mirroring to `.claude/skills/`: `"auto"` (when `.claude` or `CLAUDE.md` exists), `"always"`, or `"never"`. |
| `stale_days` | `integer` | `30` | Age threshold in days (`>= 1`) applied to `.curator-meta.json` `mounted_at` timestamps during `health` audits. |
