# 02 — Daemon hot-reload: add_watch / remove_watch

**What to build:** `WatcherService.add_watch(watch)` schedules a new watchdog observer for the given root, stores the handle by `watch_id`, and appends to `_watches`. `WatcherService.remove_watch(watch_id)` unschedules the observer, removes pending events, and pops from `_watches`. `PeriodicScanner` gets the same pair of methods — append and remove from its watch list. `OrganizerDaemon.add_watch(watch)` cascades to both services and appends to its own watch list. `OrganizerDaemon.remove_watch(watch_id)` cascades removal to both services and rebuilds the `BoundaryPolicy` for remaining watches. A `WatchMutator` protocol is defined so the web layer can add/remove without depending on daemon internals.

**Blocked by:** 01

**Status:** ready-for-agent

- [x] `WatcherService.add_watch(watch)` — calls `observer.schedule()` for the root, stores the returned handle keyed by `watch_id`
- [x] `WatcherService.remove_watch(watch_id)` — calls `observer.unschedule(handle)`, clears pending events for that watch, removes from `_watches` dict
- [x] `PeriodicScanner.add_watch(watch)` — appends to `_watches` list
- [x] `PeriodicScanner.remove_watch(watch_id)` — removes from `_watches` list
- [x] `OrganizerDaemon.add_watch(watch)` — cascades to `watcher.add_watch` and `scanner.add_watch`, appends to own `watches` list
- [x] `OrganizerDaemon.remove_watch(watch_id)` — cascades to `watcher.remove_watch` and `scanner.remove_watch`, rebuilds `BoundaryPolicy` for remaining watches
- [x] `WatchMutator` protocol defined with `add_watch(wc: WatchFolderConfig)` and `remove_watch(watch_id: str)` signatures
- [x] `DaemonWatchMutator` adapter wraps the `OrganizerDaemon` instance
- [x] Tests: programmatic add/remove coverage verifies observer handles, pending events, scanner membership, and policy rebuild
