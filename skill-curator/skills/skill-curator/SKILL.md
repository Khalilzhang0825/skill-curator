---
name: skill-curator
description: Curate, audit, store, and copy-mount AI agent skills across Global Core, Project Workspace, and an out-of-band Skill Vault. Use when the user pastes a GitHub or skills URL, asks to install/collect/mount a skill or Scene Tag group, searches saved skills, or requests a skill health audit.
---

# Skill Curator

Manage skills across three physical tiers using `<skill_dir>/scripts/curator.py` (Python 3 stdlib + `git`/`curl`, zero external packages).

- Default config path: `~/.config/skill-curator/config.json` (overridable via `--config <path>` or `SKILL_CURATOR_CONFIG`).
- Canonical vocabulary: **Skill Vault**, **Vault Index** (`INDEX.md`), **Scene Tag**, **Global Core** (soft cap `8`), **Project Workspace**, **Mount** (copy-only, never symlinks).

## Workflow A: First-Run Initialization (`init`)

If commands fail with `CONFIG_NOT_FOUND` (or `~/.config/skill-curator/config.json` does not exist):
1. Ask the user where to keep their **Skill Vault** (default `~/.agents/skill-vault`) and confirm `global_cap` (default `8`) and `claude_sync` (`auto`).
2. Run:
   ```bash
   python3 "<skill_dir>/scripts/curator.py" init \
     --vault "<chosen_vault_dir>" \
     --config "$HOME/.config/skill-curator/config.json"
   ```
3. See [references/config-schema.md](references/config-schema.md) and [references/vault-index-template.md](references/vault-index-template.md) for field contracts.

## Workflow B: URL or Local Skill Ingestion (`fetch` -> `inspect` -> `store` -> `mount`)

Never bypass this sequence or copy directly into `~/.agents/skills` or `~/.claude/skills`.

1. **Fetch & Discover**:
   ```bash
   python3 "<skill_dir>/scripts/curator.py" fetch "<url_or_path>" --timeout 30
   ```
   - Supports GitHub repo URLs, `/tree/<ref>/<subpath>`, `/blob/<ref>/<path>/SKILL.md`, raw `SKILL.md` URLs, and local Git repos.
   - If `status == "multiple_skills_found"`, show the `skills` list (`name`, `description`, `rel_path`) and ask the user which skill(s) to inspect, then re-run with `--skill "<name_or_relpath>"` (or `--all` to stage all skills in a single clone).
2. **Pre-Install Audit**:
   ```bash
   python3 "<skill_dir>/scripts/curator.py" inspect \
     --source-dir "<staged_dir>" \
     --project-dir "<cwd>" \
     --desired-tier auto
   ```
   - Present one compact audit summary: candidate name, `warnings` (name issues, vague/bloated description, `SKILL.md` > 500 lines, `scripts_detected`), `exact_collisions`, `keyword_overlaps`, `global_core` occupancy (`count/cap`), and `recommended_tier` (`project` or `vault`; never blind `global`).
3. **Single User Confirmation**:
   - Ask once to confirm: target tier (`vault`, `project`, or `global`), optional **Scene Tags** (`--tags "tag1,tag2"`), and—only if description is vague/bloated—permission to sharpen it (`--override-description "..."`; never rewrite `SKILL.md` silently).
4. **Store & Optional Mount**:
   ```bash
   python3 "<skill_dir>/scripts/curator.py" store \
     --source-dir "<staged_dir>" \
     --keywords "<k1,k2>" \
     --tags "<tag1,tag2>" \
     --source-url "<url>"
   ```
   - If the user chose `project` or `global`, run `mount`:
     ```bash
     python3 "<skill_dir>/scripts/curator.py" mount "<name>" --target project --project-dir "<cwd>"
     ```
   - Remove temporary `staged_dir` parent created by `fetch` when done.

## Workflow C: Global Core Cap Gate

When `mount <name> --target global` returns `GLOBAL_CAP_REACHED` (or `inspect --desired-tier global` sets `global_cap_blocked: true`):
1. Show `global_count / global_cap` and `demote_candidates`.
2. Offer the user three choices:
   - **Demote** an existing Global Core skill to the Skill Vault (`demote <existing> --from global`), then mount the new skill.
   - **Mount to Project Workspace** instead (`mount <name> --target project --project-dir "<cwd>"`).
   - **Keep in Skill Vault only** (already stored at zero context cost).
3. Pass `--force` on `mount --target global` only if the user explicitly overrides the soft cap.

## Workflow D: Vault Discovery & Scene Tag Batch Mount (`search` / `mount-tag`)

When the user asks for previously saved skills or wants to activate a workflow tag:
```bash
python3 "<skill_dir>/scripts/curator.py" search "<query>" --tag "<optional_tag>" --limit 10
```
- Mount a single match with `mount <name> --target project --project-dir "<cwd>"`.
- Batch copy-mount an entire **Scene Tag** group into the active **Project Workspace**:
  ```bash
  python3 "<skill_dir>/scripts/curator.py" mount-tag "<scene_tag>" --project-dir "<cwd>"
  ```

## Workflow E: On-Demand Health Audit & Cleanup (`health` -> `demote`)

Run only when asked (never silently delete or demote skills):
```bash
python3 "<skill_dir>/scripts/curator.py" health --project-dir "<cwd>" --stale-days 30
```
- Summarize `cap_violations`, `duplicates_and_overlaps`, `bloat_and_quality`, `stale_skills` (determined strictly by `mounted_at` age), and `untracked_mounts`.
- On explicit user confirmation, archive active mounts back to the Skill Vault:
  ```bash
  python3 "<skill_dir>/scripts/curator.py" demote "<name>" --from global|project --project-dir "<cwd>"
  ```
