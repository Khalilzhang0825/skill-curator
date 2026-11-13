# Skill Vault Index Template

`INDEX.md` lives at the root of the **Skill Vault** and is safe to version-control in Git and edit by hand.

```markdown
# Skill Vault Index

| Name | Keywords | Scene Tags | Source | Path |
| --- | --- | --- | --- | --- |
| example-skill | lint, formatting | python, ci | https://github.com/example/repo | skills/example-skill |
```

## Notes

- One row per stored skill; re-storing updates the matching row in place.
- Escape literal `|` characters inside cells as `\|`.
- Manual edits to `Keywords`, `Scene Tags`, and `Source` are merged with `skills/<name>/.curator-meta.json` during `search`, `mount`, `mount-tag`, and `inspect`.
