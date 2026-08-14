# Review decisions — current-activity indicator

Working document for the post-review triage of findings against `HEAD` (ed0ae3a),
covering the working-tree changes for the dashboard "current activity" indicator
and the pre-existing mypy/py7zr cleanup body.

Each entry: decision + rationale. Implement once all decisions are made.

## Open issues

_All resolved — see decisions below._

## Implementation status

All decisions implemented (uncommitted working tree). Verification: 452 tests
pass (`-m "not smoke"`), `ruff check src tests` and `mypy src tests` clean.

- Decision 1 → `docs/design/architecture.md` (out-of-scope sentence amended).
- Decisions 2–6 → `daemon.py` (`QueueStatus`, `batch_progress` slot, `queue_status()`/`batch_progress()` on `PeriodicScanner`/`OrganizerDaemon`/`DaemonWatchMutator`/`WatchMutator` protocol), `item_processor.py` (`BatchPhase`, `BatchProgress`, `process_batch(progress=…)`), `web.py` (`Operation`/`CurrentActivity` typed models, `_queue_status()` shared helper, `_current_activity()`).
- Decision 7 → `POST /watches/{id}/scan` detail strings now "in-flight scan(s)" / "queued scan(s)".
- Decisions 8–9 → `current_activity.html` (5s htmx poll; three mutually exclusive states) + `organizer.css` (`.waiting`).
- Decision 10 → no change.

New coverage: `test_scanner_batch_progress_reflects_phase_and_is_cleared` (daemon),
`test_process_batch_reports_scanning_then_operating_progress` (processor),
reworked current-activity web tests (idle / scanning / operating / queued / no-daemon).

## Decisions

1. **Design-doc conflict (live update)** → **B: amend the doc.** Edit `architecture.md:302` to remove "live updates, dashboards" from the out-of-scope sentence ("Bulk actions, live updates, dashboards, and rich archive-content previews are outside the initial UI scope."), keeping "Bulk actions … and rich archive-content previews". Rationale: the dashboard already exists, so the sentence was stale; a status panel does not warrant an ADR.

2. **Global counts via per-watch `scan_status`** → **A: dedicated seam method for the global queue counts.** Add a daemon-global read for the queue counts (`pending_triggers`, `in_flight_batches`) on the `WatchMutator` seam, used by the dashboard poll instead of reading one watch's `scan_status`. Per-watch `scan_status` stays for `batch_running` (genuinely per-watch). Counts are atomic `len()`/`in` reads — no shared-state iteration. Shape finalised with Issues 3/4/6.

3. **Duplicated `scan_status` read pattern** → **A: extract a shared `web.py` helper** for the global queue counts, used by both `_current_activity()` and the existing `POST /watches/{id}/scan` handler.

4. **Untyped `_current_activity()` payload** → **B: typed web-side context model.** The seam returns atomic primitives; `web.py` builds a typed `CurrentActivity` dataclass for the template. Keeps the daemon's atomic-read discipline (no iterating `_in_flight` from the web thread).

5. **Template leaks `pending_triggers`** → **A: fold into Issue 4 → B.** Name the typed model's attributes for the template: `CurrentActivity(operating_watches=…, queued_scans=…)`; the seam-internal term never reaches the template.

6. **Granularity — phase distinction** → **B: add phase tracking (scanning / operating).** Shape:
   - Add `BatchPhase` (`SCANNING`, `OPERATING`) and a `BatchProgress(phase, watch_id, current_item, processed, total)` value type.
   - `ItemProcessor.process_batch` gains an optional `progress` callback invoked at phase transitions and per processed item; the adapter (`ProcessorBatchAdapter`) wires it into a slot owned by `PeriodicScanner`.
   - `PeriodicScanner` holds a single `current_progress` slot (one attribute, atomic reference swap — consistent with the atomic-read discipline; no dict iteration). Exposes phase + watch + current item for the active batch.
   - Web `CurrentActivity` gains `phase` (and current item, if cheap) for the operating watch; template shows e.g. "Scanning `downloads`" vs "Operating on `downloads`".
   - Note: honest phase requires the processor to report; there is no daemon-only shortcut. This is the largest piece of work.

7. **Terminology drift ("scans queued" vs "pending triggers")** → **A: align on "scan".** Update `POST /watches/{id}/scan`'s detail strings to say "scan queued"/"scan already running" instead of "pending trigger(s)"/"in-flight batch(es)". One user-facing word ("scan") across all surfaces, matching the glossary and the "Scan now" button. Update the endpoint's tests accordingly.

8. **Scope creep (`GET /activity` + polling)** → **A: keep it.** Live freshness is the feature's value; the isolated lightweight endpoint refreshes only the panel and never disturbs watch-list form edits. No new trust-surface concern (whole UI is already behind the ADR-0006 trusted control boundary).

9. **"Idle" while scans queued** → **A: mutually exclusive three states.** Operating → "Operating on X"; else queued → "N scans queued"; else → "Idle". The panel never claims "Idle" while work is queued.

10. **isort import order** → **B: leave as-is.** isort is not enforced (no `[tool.ruff]` config; ruff green), and the alias sits deliberately beside `import py7zr`. Enabling isort repo-wide is a separate tooling decision.
