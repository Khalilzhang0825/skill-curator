---
name: skill-curator
description: Curate, audit, scan, store, and copy-mount AI agent skills across Global Core, Project Workspace, and an out-of-band Skill Vault. Use when the user shares a skill URL, asks to initialize or scan/clean up local skills, mount a skill or Scene Tag group, search saved skills, or run a health audit.
---

# Skill Curator

Manage skills across three physical tiers using `<skill_dir>/scripts/curator.cmd` (or `python3` / `py -3` / `python` with `<skill_dir>/scripts/curator.py`; stdlib + `git`/`curl` only, zero external packages).

- Default config path: `~/.config/skill-curator/config.json` (`%USERPROFILE%\.config\skill-curator\config.json` on Windows), overridable via `--config <path>` or `SKILL_CURATOR_CONFIG`.
- Canonical vocabulary: **Skill Vault**, **Vault Index** (`INDEX.md`), **Scene Tag**, **Global Core** (soft cap `8`), **Project Workspace**, **Mount** (copy-only, never symlinks).

## Workflow A: First-Run Initialization & Local Onboarding (`init` -> `scan` -> `remediate`)

If commands fail with `CONFIG_NOT_FOUND` (or `~/.config/skill-curator/config.json` does not exist):
1. Ask the user where to keep their **Skill Vault** (default `~/.agents/skill-vault`) and confirm `global_cap` (default `8`) and `claude_sync` (`auto`).
2. Initialize config and discover existing harness skill directories automatically:
   ```bash
   "<skill_dir>/scripts/curator.cmd" init \
     --vault "<chosen_vault_dir>" \
     --discover-global-dirs \
     --config "$HOME/.config/skill-curator/config.json"
   ```
   (On Windows PowerShell/CMD, substitute `$HOME\.config\skill-curator\config.json` or `%USERPROFILE%\.config\skill-curator\config.json`.)
3. Run `scan` to inspect any pre-existing skills across `~/.agents/skills`, `~/.claude/skills`, `~/.gemini/config/skills`, `~/.codex/skills`, `~/.cursor/skills`, and `~/.opencode/skills`:
   ```bash
   "<skill_dir>/scripts/curator.cmd" scan --project-dir "<cwd>"
   ```
   - If existing unmanaged skills, cap violations, bloat, or cross-harness duplicates are found, present the concise `keep_and_adopt` (`<= 8` Global Core) + `demote_to_vault` (with suggested **Scene Tags** & keywords) + `review_warnings` plan for **one confirmation**, then execute via Workflow F (`remediate`).
4. See [references/config-schema.md](references/config-schema.md) and [references/vault-index-template.md](references/vault-index-template.md) for schemas.

## Workflow B: URL or Local Skill Ingestion (`fetch` -> `inspect` -> `store` -> `mount`)

Never bypass this sequence or copy directly into `~/.agents/skills` or `~/.claude/skills`.

1. **Fetch & Discover**:
   ```bash
   "<skill_dir>/scripts/curator.cmd" fetch "<url_or_path>" --timeout 30
   ```
   - Supports GitHub repo URLs, `/tree/<ref>/<subpath>`, `/blob/<ref>/<path>/SKILL.md`, raw `SKILL.md` URLs, and local Git repos.
   - If `status == "multiple_skills_found"`, list the discovered `skills` (`name`, `description`, `rel_path`) and ask which skill(s) to inspect, then re-run with `--skill "<name_or_relpath>"` (or `--all`).
2. **Pre-Install Audit**:
   ```bash
   "<skill_dir>/scripts/curator.cmd" inspect \
     --source-dir "<staged_dir>" \
     --project-dir "<cwd>" \
     --desired-tier auto
   ```
   - Present one compact audit summary: candidate name, `warnings` (name issues, vague/bloated description, `SKILL.md` > 500 lines, `scripts_detected`), `exact_collisions`, `keyword_overlaps`, `global_core` occupancy (`count/cap`), and `recommended_tier` (`project` or `vault`; never blind `global`).
3. **Single User Confirmation**:
   - Ask once to confirm target tier (`vault`, `project`, or `global`), optional **Scene Tags** (`--tags "tag1,tag2"`), and—only if description is vague/bloated—permission to sharpen Vault copy via `--override-description "..."` (never rewrite upstream `SKILL.md` silently).
4. **Store & Optional Mount**:
   ```bash
   "<skill_dir>/scripts/curator.cmd" store \
     --source-dir "<staged_dir>" \
     --keywords "<k1,k2>" \
     --tags "<tag1,tag2>" \
     --source-url "<url>"
   ```
   - If the user chose `project` or `global`, run `mount "<name>" --target project --project-dir "<cwd>"` (or `--target global`), then remove the temporary `staged_dir` parent.

## Workflow C: Global Core Cap Gate

When `mount <name> --target global` returns `GLOBAL_CAP_REACHED` (or `inspect --desired-tier global` sets `global_cap_blocked: true`):
1. Show `global_count / global_cap` and `demote_candidates`.
2. Offer three choices:
   - **Demote** an existing Global Core skill (`demote <existing> --from global`), then mount the new skill.
   - **Mount to Project Workspace** (`mount <name> --target project --project-dir "<cwd>"`).
   - **Keep in Skill Vault only** (zero idle context cost).
3. Pass `--force` on `mount --target global` only on explicit user override.

## Workflow D: Vault Discovery & Scene Tag Batch Mount (`search` / `mount-tag`)

```bash
"<skill_dir>/scripts/curator.cmd" search "<query>" --tag "<optional_tag>" --limit 10
"<skill_dir>/scripts/curator.cmd" mount-tag "<scene_tag>" --project-dir "<cwd>"
```
- Mount a single search match with `mount <name> --target project --project-dir "<cwd>"`.

## Workflow E: Health Audit & Targeted Demote (`health` -> `demote`)

Run when asked for a mount health audit (never silently delete or demote skills):
```bash
"<skill_dir>/scripts/curator.cmd" health --project-dir "<cwd>" --stale-days 30
```
- Summarize `cap_violations`, `duplicates_and_overlaps`, `bloat_and_quality`, `stale_skills` (`mounted_at` age), and `untracked_mounts`.
- On explicit user confirmation, archive active mounts into Skill Vault:
  ```bash
  "<skill_dir>/scripts/curator.cmd" demote "<name>" --from global|project --discover-global-dirs --project-dir "<cwd>"
  ```
  (Multi-harness diverged duplicates refuse deletion unless `--force` is explicitly confirmed.)

## Workflow F: Local Skill Discovery, Diagnosis & Remediation (`scan` -> confirm -> `remediate` / `adopt`)

Use whenever the user asks to scan, diagnose, deduplicate, organize, or adopt existing local skills across harnesses (works even before `init`):
1. Run `scan --project-dir "<cwd>"` (alias `diagnose`) to inventory skills across Agents, Claude Code, Antigravity, Codex, Cursor, and OpenCode while excluding `skill-curator` and plugin bundles.
2. Summarize `cap_violations`, `cross_harness_duplicates` (`content_identical`), `bloat_and_quality`, `scripts_present`, plus the recommended `keep_and_adopt` (`<= 8`) and `demote_to_vault` plan with suggested keywords & **Scene Tags**.
3. Preview changes safely (`--dry-run`) or execute in one command after **explicit user confirmation**:
   ```bash
   "<skill_dir>/scripts/curator.cmd" remediate --dry-run --adopt <core-skill> --demote <extra-skill>
   "<skill_dir>/scripts/curator.cmd" remediate --adopt <core-skill> --demote <extra-skill>
   ```
   - Supports `--plan <plan.json_or_->` for custom per-skill `keywords`, `scene_tags`, and confirmed `override_description`.
   - To adopt a kept skill individually without removing active files: `adopt "<name>" --keywords "<k1,k2>" --tags "<tag1>"`.
