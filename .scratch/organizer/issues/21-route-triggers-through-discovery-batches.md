# 21 — Route all triggers through discovery-batch processing

**What to build:** Filesystem events, periodic scans, immediate runs, and web-triggered processing use one shared discovery-batch path, so stability, completion skipping, suppression, leases, and per-item outcomes are consistent regardless of trigger.

**Blocked by:** 20 — Synchronize implementation status and documentation.

**Status:** completed

- [x] Watcher and scanner submit `ItemSnapshot` observations through the shared batch-processing contract.
- [x] Immediate CLI and web processing expose the same discovery-batch outcomes and diagnostics as watcher and scanner processing.
- [x] Concurrent triggers do not duplicate work and report deferred, skipped, outside-snapshot, failed, and executed outcomes correctly.
- [x] Integration tests prove equivalent behavior across watcher, scanner, CLI, and web-triggered processing.
