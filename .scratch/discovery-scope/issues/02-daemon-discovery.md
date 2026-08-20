# 02 — Daemon discovery walk and watcher gating

**What to build:** Make automatic discovery honour the per-watch `discovery` scope in both places that enumerate items: the periodic scanner's batch walk and the filesystem-event watcher.

**Blocked by:** 01

**Status:** completed

- [ ] In `PeriodicScanner._scan_watch` (daemon.py), enumerate `watch_root.iterdir()` when `watch.discovery == DiscoveryScope.TOP_LEVEL`, else `watch_root.rglob("*")`; keep the existing `.organizer-*` exclusion on the recursive branch
- [ ] In `WatcherService.handle_event` (daemon.py), when the watch is `top_level`, reject any event path that is not a direct child of `watch.watch_root` (path depth == root depth + 1) before queuing it
- [ ] Confirm `ProcessorBatchAdapter.process_batch` needs no change (it already filters by existence/containment and receives only the enumerated/queued paths)
- [ ] Leave the watchdog observer scheduled `recursive=True`; gating happens in `handle_event` only

**Tests (tests/test_daemon.py):**
- [ ] scanner with `top_level` discovers only immediate children of the root (nested file/dir excluded)
- [ ] scanner with `recursive` still discovers nested items (existing behaviour preserved)
- [ ] `handle_event`/`flush` ignores a nested-path event for a `top_level` watch and accepts a direct-child event
- [ ] existing daemon tests continue to pass unchanged

**Note:** manual/forced operations are intentionally out of scope here — they name a single item and are not gated by discovery scope.
