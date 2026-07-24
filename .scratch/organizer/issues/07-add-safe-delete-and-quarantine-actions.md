# 07 — Add safe delete and quarantine actions

**What to build:** Rules explicitly select direct deletion or attempt-specific quarantine. Folder removal requires a stable tree, hard-link removal requires opt-in, uncertain direct deletion enters reconciliation, and managed quarantine paths are excluded from discovery.

**Blocked by:** 02 — Enforce watch-folder and destination boundaries; 04 — Add copy, rename, and action-chain execution; 05 — Add durable leases, completion skipping, and trigger processing.

**Status:** done

- [x] Delete rules require direct-deletion opt-in or a configured quarantine root, and no implicit delete mode exists.
- [x] Quarantine preserves source identity and original relative path in an attempt-specific managed location excluded from ordinary processing.
- [x] Direct deletion and folder removal enforce fingerprint or stable-tree checks; uncertain deletion is never automatically accepted or retried.
