# 05 — Documentation and ADR

**What to build:** Document the new per-watch `discovery` scope in the user-facing and architectural docs.

**Blocked by:** 01

**Status:** completed

- [ ] `README.md`: under runtime watch management, document the per-watch `discovery` key with the `recursive`/`top_level` values and the rename-folders-without-touching-contents use case
- [ ] `CONTEXT.md`: add a glossary entry for the discovery scope (e.g. "Discovery scope") defining `recursive` vs `top_level` and that manual/forced commands are unaffected
- [ ] Add `docs/adr/0009-per-watch-discovery-scope.md` recording the decision, the two values, the default, and the manual-command carve-out
