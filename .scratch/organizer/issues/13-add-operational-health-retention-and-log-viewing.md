# 13 — Add operational health, retention, and log viewing

**What to build:** The UI shows recent structured logs and attempt status. Mount, permission, Tracking DB, and persistent-log failures pause new real execution safely; recovery evidence retention and cleanup follow the approved lifecycle rules.

**Blocked by:** 05 — Add durable leases, completion skipping, and trigger processing; 12 — Add reconciliation and attempt review.

**Status:** ready-for-agent

- [ ] The web UI renders recent structured log entries and watch status from shared application results.
- [ ] Watch-folder access failures pause only the affected watch folder, while persistence health failures pause all new real execution and leave dry runs explicitly degraded but available.
- [ ] Retention cleans only conclusively completed or failed routine artifacts and never automatically removes recovery evidence, suppressions, quarantined items, or uncertain staging artifacts.
