# 16 — Config-driven watch discovery

**What to build:** Parse `/config/organizer.yaml` to discover watch folder definitions, their rules paths, data volume roots, quarantine root, and global settings (scan interval, log level, retention). Wire the resolved `WatchFolderConfig` and `BoundaryPolicy` into the existing CLI and web endpoints so they accept a `watch_id` and look up configuration from the loaded file instead of requiring explicit parameters per call. Add CLI `status` command showing configured watches, rule counts, and health.

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] Define Pydantic models for `organizer.yaml` (global settings, watches, data volumes, quarantine root)
- [ ] Implement config file loader with validation (missing required fields, invalid paths, overlapping watches)
- [ ] Wire config resolution into existing web endpoints (`/watches/{watch_id}/dry-run`, `/watches/{watch_id}/rules`, `/attempts`)
- [ ] Wire config resolution into existing CLI commands (`check`, `review list/inspect/accept/abandon/reopen/retry`)
- [ ] Add `organizer status` CLI command showing configured watches, rules path, last activity, health
- [ ] Update `webapp.py` to load config on startup and pass to the app factory
- [ ] Tests: config loading, validation errors, status output, endpoint resolution
