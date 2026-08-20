# Per-watch discovery scope

Let a watch folder limit automatic discovery to its immediate children, so rules operate on top-level folders as units without recursing into and independently processing their contents.

## Decision

Add a per-watch `discovery` setting to `WatchFolderConfig` with two values:

- `recursive` (default) — discover and operate on every item at every depth, matching the historical behaviour.
- `top_level` — discover and operate on only the immediate children (files and directories) of the watch root.

`discovery` is a `StrEnum` (`DiscoveryScope`) in `config.py`. `load_config` parses the per-watch YAML key and rejects any value other than `recursive`/`top_level`; an absent key means `recursive`.

Automatic discovery is gated in the two places that enumerate items for a batch:

- `PeriodicScanner._scan_watch` walks `watch_root.iterdir()` for `top_level` and `watch_root.rglob("*")` for `recursive`.
- `WatcherService.handle_event` rejects any event path that is not a direct child of the watch root for a `top_level` watch. The watchdog observer stays `recursive=True`; gating happens where events converge.

The web layer exposes the setting through the existing per-watch controls: `PATCH /watches/{watch_id}` accepts `discovery` with a strict allowlist and `_apply_watch_updates` persists it to `organizer.yaml`. `watch_list.html` renders a Scope dropdown in the per-watch Schedule controls.

## Rationale

Renaming a top-level folder renames the whole directory as a unit — the subtree moves with it, but nothing inside is individually touched. A `top_level` watch therefore lets an administrator sort folder names without the daemon recursing into each folder and re-processing its contents, which is the user's stated goal.

## Alternatives considered

- **An intermediate "folders but not their contents" scope**: rename nested folders but never the files within. This is possible but not required by the use case, which is specifically about top-level folders. Deferred as a future extension.
- **Directories-only top-level scope**: only immediate-child directories are items. Rejected — a top-level file should still be a valid item, and restricting to directories would be surprising.
- **Gating in the observer schedule (non-recursive)**: rejects nested events earlier but couples discovery scope to observer setup and adds a reschedule path on scope change. Gating in `handle_event` keeps one scheduling model.

## Consequences

- Existing watches with no `discovery` key are unchanged (`recursive`).
- Manual and forced operations — "Scan now", `organizer check`, reprocess/retry/reopen commands — are intentionally unaffected: they name a single item and operate on it regardless of scope.
- The add-watch form/API does not set scope; it defaults to `recursive`. Scope is changed on an existing watch via `PATCH` or the dashboard.
