# Organizer Current-State Review

**Review date:** 2026-07-24

**Scope:** Current implementation compared with `CONTEXT.md`, `docs/`, and `.scratch/organizer/spec.md`.

## Historical Verification Snapshot

The following results describe the repository on 2026-07-24. They are retained
to explain the remediation roadmap and must not be read as current results.

- `uv run pytest`: 197 passed.
- `uv run mypy src tests`: passed.
- `uv run ruff check src tests`: failed with four unused-import findings.
- No end-to-end Docker/runtime test proves that the deployed daemon starts the web server, watcher, scanner, shared logging, and health services together.

## State Summary

Issues 1-19 are substantially implemented in code, but the issue status fields and `.scratch/organizer/README.md` are stale. The implementation includes rules, planning, boundaries, move/copy/rename/delete/quarantine, ZIP/7z/RAR archive behavior, nested extraction, cross-watch lineage, durable attempts and leases, suppression and retry, reconciliation, config-driven watches, CLI commands, watcher/scanner services, web UI pages, health checks, retention, logging, and Docker packaging.

The primary risk is no longer the absence of individual features. It is that the production execution path does not consistently exercise the shared `ItemProcessor` contract described by the architecture.

## Findings

### F1. Daemon processing bypasses `ItemProcessor.process_batch`

The watcher and scanner adapter processes each discovered item through direct `plan()` and `execute()` calls. This bypasses the batch contract that owns stability checks, completion skipping, suppression checks, lease coordination, deferred outcomes, and discovery-batch diagnostics.

**Impact:** Watcher/scanner behavior can diverge from immediate processing and can repeat or mishandle items that should be deferred, skipped, suppressed, or reported outside the snapshot.

**Required outcome:** All trigger paths submit snapshots through `process_batch()` and expose its outcomes and diagnostics.

### F2. The production daemon is not wired as one shared runtime

`organizer run` creates a daemon but starts a separately constructed web application. The CLI path does not use the configured runtime host and port, logger, persistent log sink, memory log sink, or health checker. The container defaults to the web-only entry point rather than the combined daemon.

**Impact:** A deployed container may expose the UI without processing files, or run processing without the UI services and configured operational behavior.

**Required outcome:** One runtime composition owns configuration, processor, logging, health, web, watcher, scanner, startup diagnostics, and graceful shutdown.

### F3. Runtime settings are not fully applied

Configured `log_level` and `retention_days` are not wired into logger construction. The daemon and CLI use hardcoded defaults in places where the runtime configuration is the source of truth.

**Impact:** Operators cannot rely on `/config/organizer.yaml` to control the deployed process.

**Required outcome:** Runtime configuration controls host/port, log level, persistent log rotation/retention, scan interval, database path, and log path consistently.

### F4. Recovery commands do not cover the complete design contract

The design calls for accepting resulting paths, marking an action applied, retrying remaining actions, retrying from the start, abandoning, and reopening. The current application-facing recovery module exposes only accept, abandon, reopen, and retry-from-start.

**Impact:** Operators cannot safely continue a partially completed action sequence without restarting from the beginning, and the UI does not provide the full documented recovery workflow.

**Required outcome:** The supported recovery command set is explicitly decided, implemented consistently across module, CLI, API, UI, persistence, and tests, or the design is amended to defer the missing commands.

### F5. Rules document validation is weaker than the canonical schema

Validation still requires the legacy `match` key and silently synthesizes a condition when `conditions` is absent. Complete action semantics are not all validated before a rules document is saved.

**Impact:** Invalid or ambiguous configuration can pass the editor's validation stage and fail later during item planning.

**Required outcome:** The canonical named-condition schema and legacy compatibility policy are explicit; complete rule and action validation occurs before save and produces useful diagnostics.

### F6. Daemon health and failure handling are under-integrated

The health module and execution persistence checks exist, but the daemon adapter catches processing errors and discards them. There is no clear structured operational event or per-watch pause behavior in the trigger loop.

**Impact:** Operators may see a healthy-looking daemon while items are silently skipped after access, configuration, or persistence failures.

**Required outcome:** Watch-folder failures pause only that watch, persistence failures pause new real execution, dry runs are explicitly degraded, and all state transitions are visible through status and structured logs.

### F7. Retention is not scheduled as a complete runtime lifecycle

Database and log cleanup primitives exist, but there is no demonstrated scheduled retention run for database records, persistent logs, staging artifacts, or other routine artifacts. Recovery evidence must remain protected while nonterminal or suppressed work exists.

**Impact:** Persistent storage can grow without operational control, or future cleanup could remove evidence needed for reconciliation.

**Required outcome:** Retention runs through an explicit lifecycle with protected recovery evidence and end-to-end tests.

### F8. Documentation and tracker state are inconsistent with code

The scratch README still describes implemented archive, extraction, configuration, UI, and logging work as missing or partial. Several issue files have unchecked criteria despite implementation commits, and issue 15 remains open despite its remediation commit.

**Impact:** Future work is planned against an inaccurate baseline and agents may duplicate completed work or miss unresolved integration gaps.

**Required outcome:** Issue statuses, checklists, README, and current-state documentation reflect verified behavior rather than commit history alone.

### F9. Tooling and maintainability debt remains

Ruff fails on four unused imports. The main processor has grown into a large executor with repeated action-specific staging, validation, and reconciliation paths. Full-content fingerprints are recalculated during planning and execution.

**Impact:** CI is not fully green, and large files or folders may cause expensive repeated I/O. Duplicated execution paths increase safety-regression risk.

**Required outcome:** Static checks pass, fingerprint work is measured and controlled, and action execution is made easier to audit without weakening the public `ItemProcessor` seam.

## Recommended Order

1. F8: synchronize documentation and tracker state so the backlog is trustworthy.
2. F1-F3: establish one correct production runtime and make every trigger use the shared batch seam.
3. F6: integrate health and failure states into runtime scheduling and observability.
4. F4-F5: complete or explicitly narrow the recovery and rules contracts.
5. F7: schedule and verify retention lifecycle behavior.
6. F9: finish tooling cleanup and performance/maintainability hardening.

## Definition Of Ready For Further Product Work

Organizer should not take on additional user-facing features until:

- `pytest`, `mypy`, and `ruff` are green.
- The combined daemon is the production entry point.
- Watcher, scanner, CLI, and web use the same processing and logging services.
- A Docker smoke test demonstrates processing, UI access, persistent attempts, and persistent logs.
- Health failures and recovery evidence behavior are observable and tested.
- The issue tracker and README accurately describe the implementation baseline.
