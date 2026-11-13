---
status: accepted
---

# Three-tier skill storage with out-of-band Skill Vault and Scene Tags

We organize skills into three physical tiers—Global Core (soft cap 8), Project Workspace (`.agents/skills/`), and an out-of-band Skill Vault indexed by a minimal `INDEX.md` with Scene Tags—rather than adding a fourth Scene Pack plugin tier or a vector/SQLite router. Keeping the Skill Vault outside all agent discovery paths guarantees zero context tax and zero trigger collisions at scale, while Scene Tags in `INDEX.md` still support one-step batch mounting by copy into any project.

## Considered Options

- **Four-tier physical Scene Packs**: Rejected because non-plugin agents (e.g., Codex, Cursor) lack uniform UI toggle switches for plugin bundles.
- **Vector / SQLite skill router**: Rejected as premature infrastructure when a flat `INDEX.md` suffices for progressive disclosure and human readability.
- **Symlink mounting into projects**: Rejected because symlinks break when projects are committed to Git, shared across machines, or run inside isolated agent sandboxes.
