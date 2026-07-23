# 12 — Add reconciliation and attempt review

**What to build:** Administrators can inspect failed and uncertain attempts in CLI and web UI, view evidence and resulting paths, accept proven outcomes, abandon and later reopen attempts, and safely initiate fresh retries without altering history.

**Blocked by:** 04 — Add copy, rename, and action-chain execution; 06 — Add collision suppression and explicit reprocessing; 07 — Add safe delete and quarantine actions; 08 — Add ZIP archive creation; 09 — Add safe ZIP unarchive.

**Status:** ready-for-agent

- [ ] Attempt list and detail views show source identity, planned actions, action results, evidence, failure detail, suppressions, and linked attempts.
- [ ] Reconciliation acceptance creates immutable accepted action results; uncertain actions and later actions remain blocked without conclusive evidence.
- [ ] Abandoning creates a terminal abandoned attempt and suppression; reopening is auditable and starts a fresh plan while preserving history.
