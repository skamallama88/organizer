# 24 — Complete the reconciliation command contract

**What to build:** Administrators can safely resolve uncertain attempts using the complete recovery workflow described by the design, with every command represented consistently in the application module, CLI, API, UI, persistence, and audit history.

**Blocked by:** 20 — Synchronize implementation status and documentation.

**Status:** completed

- [x] Retry-remaining and mark-action-applied are supported in the initial product contract.
- [x] Implement both commands with evidence validation, expected resulting identities, action ordering, and suppression safety.
- [x] Expose supported reconciliation commands consistently through `AttemptReview`, CLI, web routes, and HTMX views.
- [x] Preserve immutable attempt history and create fresh linked attempts where new mutation is required.
- [x] Add focused reconciliation command tests.
