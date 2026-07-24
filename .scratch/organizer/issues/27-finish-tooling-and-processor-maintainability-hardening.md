# 27 — Finish tooling and processor maintainability hardening

**What to build:** The Organizer codebase remains statically clean and operationally safe as item sizes grow, while the public `ItemProcessor` planning and execution seam remains stable for all callers.

**Blocked by:** 21 — Route all triggers through discovery-batch processing; 22 — Compose one shared production runtime; 23 — Integrate operational health and failure handling.

**Status:** ready-for-agent

- [ ] Make `ruff check src tests` pass and keep pytest and mypy green.
- [ ] Measure fingerprint cost for large files and folders across planning, execution, and batch processing.
- [ ] Avoid unnecessary duplicate fingerprint I/O where safety invariants permit caching or reuse.
- [ ] Reduce duplicated action execution and staging logic where doing so makes safety behavior easier to audit.
- [ ] Add regression tests proving no-overwrite, source consistency, reconciliation, and completion-skipping invariants remain intact after refactoring.
