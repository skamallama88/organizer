# 02 — Periodic sweep of stale staging in data roots

**What to build:** A periodic sweep that reclaims `.organizer-staging-*` artifacts left by hung attempts, walking the data roots where staging actually lives.

**Blocked by:** 01

**Status:** completed

- [x] Add global `staging_cleanup_age` config (seconds, default `3600`) to `OrganizerConfig` and `load_config`
- [x] Give `Retention` `data_roots` and `staging_cleanup_age`; wire from `create_daemon`
- [x] `clean_staging_artifacts` sweeps the config staging root plus each data root
- [x] `_is_stale_staging` uses newest subtree mtime so active extractions are never deleted
- [x] `_iter_staging_entries` walks directories only and skips unrelated `.organizer-*` dot-dirs
- [x] Tests: data-root sweep, active-subtree preservation, nested discovery, organizer-dir skip, `retention_run` age handling, config parse, daemon wiring
