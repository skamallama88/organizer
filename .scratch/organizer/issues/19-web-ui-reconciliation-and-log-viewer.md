# 19 — Web UI: reconciliation and log viewer

**What to build:** Server-rendered HTML pages with HTMX for managing processing attempts and viewing logs. An attempt list page with status and watch_id filtering, showing source item, rule, action, failure detail, and created time. An attempt detail page with action results, filesystem evidence, resulting paths, and command buttons (accept resulting path, abandon with reason, retry remaining, retry from start, reopen). A log viewer page with level, watch, and date-range filtering.

**Blocked by:** 16 (needs config-discovered watches for context links and watch_id resolution)

**Status:** ready-for-agent

- [ ] Implement attempt list template: filterable by status (`failed`, `needs-reconciliation`, `all`) and watch_id, shows source path, rule, action, failure detail, timestamp
- [ ] Implement attempt detail template: source fingerprint, planned actions, per-action results, intended destinations, resulting paths, filesystem evidence, failure detail, related retry attempts; command buttons trigger HTMX POST to existing API endpoints
- [ ] Implement log viewer template: filter by level (INFO/WARN/ERROR/DRYRUN) and watch_id, shows structured log entries with timestamp, level, watch, rule, action, item, result, detail
- [ ] Wire template routes into FastAPI app (`GET /attempts` → list, `GET /attempts/{attempt_id}` → detail, `GET /logs` → viewer)
- [ ] Tests: template rendering, HTMX command interactions (accept, abandon, retry, reopen), log filtering

## Implementation note

The repository's agreed `AttemptReview` interface currently exposes `accept`, `abandon`, `retry from start`, and `reopen`, but no retry-remaining or mark-action-applied command. This ticket wires the existing commands through HTMX and does not invent a new recovery operation outside that interface. Adding retry-remaining requires a separate application-module decision and implementation seam.
