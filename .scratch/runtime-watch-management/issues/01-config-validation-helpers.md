# 01 — Prefactor: extract config validation into reusable helpers

**What to build:** The validation logic inside `load_config()` (root-boundary checks, ID uniqueness, overlap detection) is extracted into standalone `validate_watch_root()` and `validate_watch_id()` functions. `BoundaryPolicy` construction is hoisted out of the per-watch loop so a single shared policy is rebuilt on add/remove instead of each watch carrying its own copy. `OrganizerDaemon.watches` changes from `tuple` to `list[WatchFolderConfig]` so the daemon can be mutated in place. No user-facing behaviour changes.

**Blocked by:** None — can start immediately.

**Status:** completed

- [x] Extract `validate_watch_root(root, config_root, data_roots, existing_roots)` from `config.py:89-97`
- [x] Extract `validate_watch_id(watch_id, existing_ids)` from `config.py:75-78`
- [x] Hoist `BoundaryPolicy` construction to a single shared instance referenced by all watches (or rebuilt on mutation) — not a copy per watch
- [x] Convert `OrganizerDaemon.watches` from `tuple` to `list[WatchFolderConfig]`
- [x] `load_config()` calls the extracted helpers so existing validation semantics are preserved
- [x] All existing tests pass without modification

Implemented in commit `86b3c7a`. Verification passed: 285 tests, mypy, and Ruff.
