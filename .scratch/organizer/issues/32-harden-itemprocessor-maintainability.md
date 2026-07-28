# 32 — Harden ItemProcessor maintainability

**What to build:** The shared ItemProcessor seam remains safe and easier to audit as Organizer evolves, without changing verified processing, recovery, collision, archive, or persistence behavior.

**Blocked by:** 28 — Add Docker end-to-end smoke coverage; 30 — Clarify archive output naming contract; 31 — Verify metadata preservation contract.

**Status:** ready-for-agent

- [ ] Execution responsibilities are separated into clearer internal units while preserving the ItemProcessor planning and execution interface.
- [ ] Regression coverage preserves no-overwrite publication, source consistency, reconciliation, completion skipping, archive naming, and metadata guarantees.
- [ ] Static checks and the Docker smoke flow remain green after the refactor.
