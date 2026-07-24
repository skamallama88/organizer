# 24 — Complete the reconciliation command contract

**What to build:** Administrators can safely resolve uncertain attempts using the complete recovery workflow described by the design, with every command represented consistently in the application module, CLI, API, UI, persistence, and audit history.

**Blocked by:** 20 — Synchronize implementation status and documentation.

**Status:** ready-for-agent

- [ ] Decide and document whether retry-remaining and mark-action-applied are supported in the initial product contract.
- [ ] If supported, implement both commands with evidence validation, expected resulting identities, action ordering, and suppression safety.
- [ ] Expose supported reconciliation commands consistently through `AttemptReview`, CLI, web routes, and HTMX views.
- [ ] Preserve immutable attempt history and create fresh linked attempts where new mutation is required.
- [ ] Add tests for accepted results, applied actions, retry-remaining, retry-from-start, abandon, reopen, ambiguous evidence, and failed recovery commands.
