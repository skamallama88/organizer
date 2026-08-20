# Per-watch discovery scope

## Problem Statement

Every watch folder is discovered recursively: the periodic scanner walks the
whole subtree with `watch_root.rglob("*")` (daemon.py `_scan_watch`) and the
filesystem watcher is scheduled `recursive=True`, so every item at every depth
is an independently considered item.

A user wants a watch folder that operates **only on the immediate children** of
the watch root. When a rule renames a folder, only that folder is renamed — the
subtree moves with it as a unit but nothing *inside* it is individually touched.
This lets them sort top-level folders by name without recursing into and
re-processing their contents.

## Solution

Add a per-watch **discovery scope** option to `WatchFolderConfig`:

- `recursive` (default, current behaviour) — discover and operate on every item
  at every depth.
- `top_level` — discover and operate on only the immediate children (files and
  directories) of the watch root. Never descend into a child directory to
  independently process its contents.

Discovery is gated in both automatic paths that enumerate items: the periodic
scanner's batch walk and the filesystem-event watcher's event filtering. Manual
and forced operations (scan now, `organizer check`, reprocess/retry commands)
keep operating on whatever single item is named — the scope only limits how
items are *discovered* for automatic batches.

## User Stories

1. As an administrator, I want to set a watch folder to `top_level` scope, so
   that renaming only touches the top-level folders and never their contents.
2. As an administrator, I want a `recursive` (default) scope so the current
   recursive behaviour is preserved for existing watches.
3. As an administrator, I want to change the scope of an existing watch from the
   dashboard, so I don't need to edit `organizer.yaml` or restart.
4. As an administrator, I want the scope persisted to `organizer.yaml`, so it
   survives container restarts.
5. As an administrator, I want the same scope applied consistently to both the
   periodic scanner and the filesystem-event watcher, so a top-level watch never
   picks up nested items from events.
6. As an administrator, I want manual/forced commands (scan now, check, retry)
   to still operate on a named item regardless of scope, so I can always act on
   a specific path.

## Implementation Decisions

- `DiscoveryScope` is a `StrEnum` with values `recursive` and `top_level`,
  defined in `config.py` (where `WatchFolderConfig` lives) and imported by the
  daemon and web layers.
- `WatchFolderConfig` gains `discovery: DiscoveryScope = DiscoveryScope.RECURSIVE`.
  It stays frozen; scope changes produce a new instance via `model_copy`.
- YAML key is `discovery` under a watch entry. `load_config` accepts only
  `recursive` or `top_level` and errors otherwise; absent means `recursive`.
- `PeriodicScanner._scan_watch` enumerates `watch_root.iterdir()` for
  `top_level` and `watch_root.rglob("*")` for `recursive`, keeping the existing
  `.organizer-*` exclusion for the recursive case (direct children cannot be
  organizer-managed, so `iterdir` needs no extra filter).
- `WatcherService.handle_event` rejects any path that is not a direct child of
  the watch root when the watch is `top_level`. The observer stays `recursive`
  for simplicity; gating happens in `handle_event` where both the periodic and
  event paths already converge.
- The `PATCH /watches/{watch_id}` handler accepts `discovery` with a strict
  allowlist (`recursive`/`top_level`), following the existing `enabled` strict
  allowlist pattern. `_apply_watch_updates` persists it to YAML; `_build_watches`
  exposes it for the UI.
- Scope is added to the per-watch schedule control in `watch_list.html` as a
  dropdown, alongside `enabled` and `scan_interval`. The add-watch form does not
  set scope (defaults to `recursive`), matching how `enabled` is not set on add.

## Testing Decisions

- Config parsing: valid values, invalid value rejected, default `recursive` when
  absent (test_config.py).
- Daemon: scanner walks only direct children in `top_level`; watcher ignores
  nested events for a `top_level` watch and accepts direct children
  (test_daemon.py).
- Web API: `PATCH` accepts `recursive`/`top_level`, rejects unknown values, and
  `_build_watches`/persistence reflect the value (test_web_watches_api.py).
- Seams: `WatchFolderConfig` (unit), `PeriodicScanner`/`WatcherService` (unit
  with temp dirs), FastAPI test client for the PATCH endpoint and YAML
  persistence.

## Out of Scope

- An intermediate "folders but not their contents" scope (rename nested folders
  but not the files inside). This is a possible future extension, not part of
  this change.
- Directories-only top-level scope — both files and directories that are
  immediate children are items.
- Changing scope via the add-watch form or `POST /watches` API body (edit an
  existing watch instead, via `PATCH`).
