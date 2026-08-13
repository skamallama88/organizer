# Per-watch controls — review follow-up decisions

Decisions from the code-review of the per-watch controls feature (working tree
against HEAD `09cfcd5`). Reviewed one issue at a time; implementation happens
once all decisions are made.

Review scope: the uncommitted feature implementing a per-watch "scan now"
trigger, an enabled toggle, and a per-watch rescan interval (minutes in the UI,
seconds in `organizer.yaml`).

## Agenda

1. Inline web-layer scan execution (`_scan_watch_sync`) — Standards + Spec
2. Duplicated watch-root discovery walk — Standards
3. Duplicated "find watch by id, replace in list" shape (4 sites) — Standards
4. `config.py` parallel per-watch lists (Data Clumps) — Standards
5. Middle Man: `trigger_scan` double delegation — Standards
6. Minutes↔seconds lossy round-trip + UI shows global value as editable — Standards + Spec
7. Inconsistent `HX-Request` vs `_is_html_request` dispatch — Standards
8. `reprocess_attempt` operates on disabled folders (breaks spec 2) — Spec
9. PATCH `enabled` accepts arbitrary strings as `False` — Spec

## Decisions

### 1. Inline web-layer scan execution (`_scan_watch_sync`) — **Option A: remove the fallback**

`POST /watches/{watch_id}/scan` must not run a mutating operate cycle inside the
web request. When no daemon is wired (`watch_mutator is None`), it returns an
error instead of scanning. Only the `organizer-web` (no-daemon) mode is affected;
production `organizer run` always wires a daemon and is unchanged.

Concretely:
- Delete `_scan_watch_sync` and the mutator-less branch in `scan_watch_now`.
- Remove the now-unused `ProcessorBatchAdapter` import from `web.py`.
- Return an explicit error when `watch_mutator is None` — message to read
  "no daemon running; start `organizer run` to enable scan/operate".
- Replace `test_scan_now_without_mutator_runs_sync_scan` with a test asserting
  the no-daemon error.

Rationale: this is the only place where the web layer both plans and executes;
removing it restores the thin-handler standard. The fallback existed only for
test support and is dead in every production deploy.

### 2. Duplicated watch-root discovery walk — **Option B: name the literal**

Add a module-level constant in `daemon.py`, e.g. `_ORGANIZER_PREFIX = ".organizer-"`,
and use it in the two remaining sites: `WatcherService.handle_event`
(daemon.py:197) and `PeriodicScanner._scan_watch` (daemon.py:389). The web.py
occurrence (inside `_scan_watch_sync`) disappears with decision #1. No
`discover_items()` helper — that would be speculative for a single caller.

### 3. Duplicated "find watch by id, replace in list" shape — **Option C: extract only the web pair**

Add a small `_replace_runtime_watch(watch_id, replacement)` closure in
`create_app` used by the `PATCH /watches/{watch_id}` handler and its rollback
path (currently two identical loops at web.py:1636 and web.py:1658). Leave the
two daemon methods (`PeriodicScanner.update_watch`, `OrganizerDaemon.update_watch`)
as-is — their post-steps differ (`_wake()` vs `_rebuild_boundary_policy()`) and
the loops are short.

### 4. `config.py` parallel per-watch lists — **Option C: build watches in-loop, reuse `rebuild_boundary_policy()`**

Restructure `load_config` so the validation loop builds each `WatchFolderConfig`
directly (with a base `BoundaryPolicy` carrying data_roots/config_root/
allowed_destinations/quarantine_root) and appends it to one `watches` list, then
calls the existing `rebuild_boundary_policy(watches)` to attach the shared
policy with watch_roots/watch_ids. Deletes all four parallel lists and the
`zip`. The overlap check reads existing roots from the built `watches` list.
Final `tuple(watches)` for `OrganizerConfig`.

Rationale: reuses the canonical helper the daemon/web already use, removes the
list-sync hazard (the class of bug hit during initial implementation), and
avoids a new type.

### 5. Middle Man: `trigger_scan` double delegation — **Option A: leave as-is**

Keep the two-hop chain (web → `DaemonWatchMutator.trigger_scan` →
`OrganizerDaemon.trigger_scan` → `scanner.trigger`). `DaemonWatchMutator` is a
thin adapter by design (its add/remove/update are one-line forwards too) and
`OrganizerDaemon.trigger_scan` is a facade that keeps `daemon.scanner` private.
Dropping either hop would break the adapter seam or leak daemon internals.

### 6. Lossy minutes↔seconds round-trip + global shown as editable — **Option D: empty = inherit**

The schedule input is empty when a watch has no per-watch override, with a
placeholder showing the effective default (e.g. `Default (5 min)`). Typing a
whole-minute number stores a per-watch override (`minutes × 60` seconds in
YAML); clearing the field removes the override (inherits global). Concretely:
- `_build_watches` exposes the per-watch override in minutes (or `None`), the
  global default for display, and whether the watch has an override.
- Template renders `value` from the override only, `placeholder` from the
  default; keep the `min=1 step=1` number input.
- PATCH handler: non-empty value stores `minutes * 60`; empty/absent clears
  `scan_interval` to `None`.
- Inherited globals are never shown as an editable number, so an incidental
  save cannot bake an override. Whole-minute values round-trip losslessly.
- No fractional minutes in the UI for now; a hand-edited sub-minute override
  displays rounded and an explicit save would normalize it (documented edge).

### 7. Inconsistent `HX-Request` vs `_is_html_request` dispatch — **Option B: local strict helper**

Add a strict `_is_htmx(request)` helper (returns true only when the
`HX-Request` header is `"true"`) and use it in the three new handlers
(`scan_watch_now`, `update_watch`, `_watch_patch_error`). This fixes the one
real defect — `scan_watch_now` currently uses the broad `_is_html_request`, so
a non-htmx POST with a `text/html` Accept header would receive an HTML fragment
instead of JSON.

The full standardization (Option C — route **every** fragment-returning mutating
handler through `_is_htmx`, including the ~12 pre-existing attempt-command POST
endpoints and `add_watch`/`remove_watch`) is intentionally deferred and filed as
a GitHub issue.

### 8. `reprocess_attempt` operates on disabled folders — **Option C: disable automatic operation only**

The `enabled` toggle governs **automatic** operation only. The user's intent:
"remove automatic scanning/operation but can still allow forced for testing."
Confirmed with the user: the **Scan now button is also a forced action** and
must work on disabled folders.

Gated by `enabled` (automatic paths):
- Periodic scanner (`_due_watches`)
- Filesystem-event watcher (`handle_event`, `flush`)

Allowed on disabled folders (forced/manual + non-mutating):
- **Scan now** (`POST /watches/{id}/scan`) — currently returns 409 on disabled;
  this gate must be **removed**
- Explicit attempt commands: `reprocess`, `retry`, `retry-remaining`, `reopen`
- Record-only reconciliation commands: `accept`, `mark-action-applied`, `abandon`
- Dry-runs (previews, no mutation)

Consequences:
- `PeriodicScanner.run()` pending-trigger path must drop the `watch.enabled`
  check (manual triggers bypass enabled); `_due_watches` keeps gating periodic
  scans.
- `scan_watch_now` drops its disabled 409 branch.
- `CONTEXT.md` "Enabled watch folder" term is softened to *automatic* operation
  ("no automatic scanning or filesystem-event processing"; manual/forced
  operations remain available).
- Tests to flip: `test_scan_now_disabled_watch_rejected` →
  scan-now allowed on disabled; `test_scanner_trigger_ignores_disabled_watch` →
  trigger scans disabled watches. `test_scanner_skips_disabled_watches`
  (periodic gating) stays.

### 9. PATCH `enabled` accepts arbitrary strings as `False` — **Option A: strict allowlist**

`update_watch` accepts only `True`/`"true"`/`"on"`/`"1"` as enabled and
`False`/`"false"`/`"off"`/`"0"` as disabled; any other value returns a 422
("enabled must be true or false") surfaced via `_watch_patch_error`. The form's
hidden input only ever sends `"true"`/`"false"`, so all legit paths are covered;
unknowns must never silently disable a folder.

## Implementation status

All nine decisions implemented in the working tree:

- `_scan_watch_sync` removed; scan endpoint returns 501 when no daemon.
- `_ORGANIZER_PREFIX` constant added (daemon.py).
- `_replace_runtime_watch` closure added (create_app).
- `load_config` builds watches in-loop and reuses `rebuild_boundary_policy()`.
- trigger_scan delegation left as-is.
- Empty interval = inherit; placeholder shows default; whole minutes = override.
- `_is_htmx` strict helper used by the new handlers (Option C → GH issue #23).
- Scan-now allowed on disabled folders; periodic scanner + watcher stay gated.
- `enabled` parsing uses a strict allowlist (422 on unknown).

Verification: `pytest` 433 passed, `ruff` clean, `mypy` at the pre-existing
baseline. Live browser check confirmed scan-now-on-disabled, empty-inherit
placeholder, forced-move on a disabled watch, and automatic gating.
