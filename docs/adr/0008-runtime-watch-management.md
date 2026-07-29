# Runtime watch management with hot-reload

Add and remove watch folders at runtime through the web UI and API, without restarting the daemon. Container mount points are auto-discovered on first start and added to `data_roots` so they become eligible watch roots.

## Decision

We extend the daemon, config loader, and web layer so watches are hot-swappable and the container entrypoint pre-populates `data_roots` from bind mounts.

### 1. Daemon hot-reload (per-watch granularity)

`WatcherService`, `PeriodicScanner`, and `OrganizerDaemon` each get `add_watch()` and `remove_watch()` methods that mutate the internal watch list and schedule or unschedule the watchdog observer for that root alone. Other watches keep running uninterrupted.

`WatcherService` stores the return value of `observer.schedule()` (a `WatchdogWatch` handle) keyed by `watch_id` so `remove_watch` can call `observer.unschedule(handle)`.

The `OrganizerDaemon.watches` field changes from `tuple[WatchFolderConfig, ...]` to `list[WatchFolderConfig]` so in-place mutation is possible.

### 2. Config model

`WatchFolderConfig` stays frozen. Add/remove on the daemon creates new instances; the watch list is replaced in-place.

The validation logic in `load_config()` is extracted into standalone functions:
- `validate_watch_root(root, config_root, data_roots, existing_roots)`
- `validate_watch_id(watch_id, existing_ids)`

These are reused by both the initial config load and the runtime API.
`load_config()` constructs one immutable `BoundaryPolicy` for the complete
validated watch set and all loaded watches reference that instance.

### 3. Web API

Two new endpoints on the existing FastAPI app:

- `POST /watches` — accepts `{id, root, rules_path}`, validates, writes to `organizer.yaml`, calls daemon mutator, returns the new watch config
- `DELETE /watches/{watch_id}` — removes from `organizer.yaml`, calls daemon mutator

YAML persistence uses the same atomic temp-file swap as the rules save path.

A thin `WatchMutator` protocol decouples the web layer from the daemon's threading model. The daemon passes an adapter into `create_app()`.

### 4. Web UI

The dashboard gets an "Add Watch" button that opens an inline form (HTMX). Fields: Watch ID (text), Root path (dropdown from `data_roots` plus subdirectory browser, or freeform), Rules path (defaults to `/config/rules_{watch_id}.yaml`).

Each watch card gets a "Remove" button with HTMX confirmation.

New partials: `watch_form.html` for the inline form, updated `dashboard.html` for the buttons.

### 5. Entrypoint auto-discovery

`docker-entrypoint.sh` scans `/proc/mounts` for bind mount points, filters out pseudo-filesystems and the config volume, and adds remaining paths to `data_roots` in the generated config. It does NOT create watches for them — the user adds watches via the UI.

## Alternatives considered

- **Full config reload (SIGHUP)**: simpler to implement but restarts all watches at once, dropping debounce state and briefly missing events.
- **Auto-watch every mount point**: creates watches without user intent. User might have a dozen mounts and only want to watch two.
- **Entrypoint does not discover mounts**: forces users to hand-edit `organizer.yaml` to add new `data_roots`. Acceptable but breaks the "mount and use" flow.

## Consequences

- Daemon now holds mutable state. The `WatcherService._watches` dict and `OrganizerDaemon.watches` list must be thread-safe. A lock protects the former; the latter is mutated only from async context.
- Existing users with a config file see no change. The `load_config()` function is backwards-compatible.
- Entrypoint auto-discovery only runs when no config file exists yet. If a user bumps `data_roots` by hand later, the entrypoint won't overwrite their config.
- Unhealthy mounts (unreadable, missing) in `data_roots` list are harmless — the UI just shows them as eligible roots, and the health checker flags them if a watch is created on them.
