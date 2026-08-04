# Runtime watch management

## Problem Statement

Organizer currently requires a container restart to add or remove a watch folder. The only way to configure watches is by hand-editing `organizer.yaml` on the config volume. Users who bind-mount new data directories into the container must manually add them to `data_roots` in the config, then restart — a friction point for the "mount and go" workflow.

Additionally, the default entrypoint config hardcodes `/data` as the sole `data_root`, so bind-mounting additional directories on first run produces no config entry for them.

## Solution

A runtime watch management system with three layers:

1. **Daemon hot-reload** — the watcher and scanner acquire `add_watch()` / `remove_watch()` methods so watches can be added and removed in-place without interrupting existing watches or restarting the daemon.

2. **Web API and UI** — new `POST /watches` and `DELETE /watches/{watch_id}` endpoints let users manage watches through the dashboard without shell access. The YAML config is updated atomically alongside the live daemon state.

3. **Entrypoint auto-discovery** — on first start, `docker-entrypoint.sh` scans `/proc/mounts` for bind mount points and adds them to `data_roots`. Users then add watches via the UI. No YAML editing required.

## User Stories

1. As an administrator, I want to add a new watch folder from the web UI, so that I can start organizing a new data directory without restarting the container.
2. As an administrator, I want to remove a watch folder from the web UI, so that I can stop monitoring a directory without downtime or YAML editing.
3. As an administrator, I want to see all active watch folders on the dashboard, so that I know what the system is monitoring at a glance.
4. As an administrator, I want the list of available data volumes visible when adding a watch, so that I know which paths are valid watch roots.
5. As an administrator, I want to add and remove watches via HTTP API, so that I can automate infrastructure workflows.
6. As an administrator, I want the YAML config file kept in sync with the live daemon state, so that the config survives container restarts.
7. As an administrator, I want to bind-mount a new data volume and have it recognized on first container start, so that I don't need to hand-edit `data_roots`.
8. As an administrator, I want adding an invalid watch (outside data volume, overlapping with another watch, inside config volume) to produce a clear error, so that I understand why it was rejected.
9. As an administrator, I want removing a watch to stop both the filesystem watcher and the periodic scanner for that root, so that no further items from that root are discovered.

## Implementation Decisions

- Watch management uses per-watch granularity: the watchdog observer's `schedule()` handle is stored keyed by `watch_id` so `unschedule()` can remove one root without disrupting others.
- `WatchFolderConfig` instances remain frozen. Adding a watch creates a new instance; removing a watch removes it from the daemon's list.
- `BoundaryPolicy` is rebuilt on every mutation so that the remaining watches' policies correctly reflect the current set of watch roots and IDs.
- A thin `WatchMutator` protocol decouples the web layer from the daemon. The daemon passes an adapter into `create_app()`.
- YAML persistence uses atomic temp-file swap (the same pattern as the rules save path in `web.py`).
- The daemon's `watches` field changes from `tuple` to `list` to support in-place mutation.
- Validation logic from `load_config()` is extracted into reusable functions (`validate_watch_root`, `validate_watch_id`) so the runtime API and config loading share the same checks.
- Entrypoint auto-discovery runs only when no `organizer.yaml` exists (first start only). It never overwrites an existing config.

## Testing Decisions

- Prefactor (Ticket 01) is tested implicitly — all existing config tests continue to pass without changes.
- Daemon hot-reload (Ticket 02) is tested via programmatic calls: add a watch, drop a file into its root, assert the processor receives it; remove a watch, assert the observer no longer reports events for that root.
- Web API (Ticket 03) is tested via the test client on the FastAPI app: `POST /watches` returns the new config, `DELETE /watches/{watch_id}` removes it, validation errors return 422.
- Seams: `WatchFolderConfig` validation (unit), `WatcherService`/`PeriodicScanner` add/remove (unit with fake paths), FastAPI test client for endpoints, `OrganizerDaemon.add_watch`/`remove_watch` (integration with real temp directories).
- Prior art: existing tests in `tests/test_config.py`, `tests/test_daemon.py`, and `tests/test_item_processor.py` follow the same patterns.

## Out of Scope

- Editing watch properties (e.g., changing a watch root or rules path) — remove and re-add instead.
- Editing `data_roots` or `quarantine_root` at runtime — these remain configuration-time-only.
- Bulk watch operations (import/export) — one watch at a time via the UI.
- Auto-creating watches for discovered mount points — only `data_roots` entries are auto-populated.

## Further Notes

- Unhealthy mounts in `data_roots` (unreadable, missing) are harmless — the UI lists them as eligible roots, and the health checker flags them only if a watch is created on them.
- The existing `organizer-web` entrypoint (no daemon) still gets the API endpoints but without hot-reload — the YAML changes persist for the next daemon start.
