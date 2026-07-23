# 06 — Add collision suppression and explicit reprocessing

**What to build:** No-overwrite collisions become visible failed attempts with durable suppression. Administrators can explicitly retry or reprocess using a fresh current plan, while source changes, ruleset changes, and current destination policy are revalidated.

**Blocked by:** 04 — Add copy, rename, and action-chain execution; 05 — Add durable leases, completion skipping, and trigger processing.

**Status:** ready-for-agent

- [x] A collision leaves existing data unchanged, stops later actions, records a failed execution attempt, and suppresses automatic processing of that source identity.
- [x] Explicit retry and reprocess commands create fresh plans and linked history rather than reusing historical actions.
- [x] A changed or absent source is visible to the administrator and cannot receive stale rule captures or destinations.
