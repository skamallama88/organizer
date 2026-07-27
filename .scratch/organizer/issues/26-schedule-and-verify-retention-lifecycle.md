# 26 — Schedule and verify retention lifecycle

**What to build:** Organizer periodically removes only eligible routine history and artifacts while retaining all evidence needed for failed, suppressed, abandoned, uncertain, quarantined, or reconciling work.

**Blocked by:** 22 — Compose one shared production runtime; 23 — Integrate operational health and failure handling.

**Status:** done

- [x] Schedule retention using configured retention settings in the production runtime.
- [x] Clean eligible completed and routine failed attempt records without deleting linked recovery evidence or suppressions.
- [x] Apply persistent log retention and rotation settings from configuration.
- [x] Define and test cleanup of routine staging artifacts while retaining uncertain staging artifacts and quarantine evidence.
- [x] Expose retention activity and failures through structured logs and operational status.
