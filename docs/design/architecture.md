# Architecture

## ItemProcessor module

The ItemProcessor module is the shared seam for watcher events, periodic scans, CLI commands, and web UI operations. Its interface separates planning from execution while keeping rule interpretation, action sequencing, collision handling, retry policy, logging, and Tracking DB timing inside the module.

```python
class ItemProcessor(Protocol):
    def plan(self, request: PlanRequest) -> Plan:
        """Evaluate ordered rules and produce an execution plan."""

    def execute(
        self,
        plan: Plan,
        mode: ExecutionMode = ExecutionMode.APPLY,
    ) -> ExecutionReport:
        """Apply or report a plan without exposing filesystem details."""

    def acquire_lease(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        """Acquire an exclusive processing lease for a source identity."""

    def has_completed_attempt(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        """Check whether a source identity has a completed attempt."""

    def is_stable(self, watch_id: str, snapshot: ItemSnapshot, *, now: float, stability_interval: float) -> bool:
        """Check whether an item observation is stable for the interval."""

    def process_batch(
        self,
        watch_id: str,
        watch_root: Path,
        rules_path: Path,
        snapshots: list[ItemSnapshot],
        *,
        stability_interval: float = 0.0,
        boundary_policy: BoundaryPolicy | None = None,
        now: float | None = None,
        dry_run: bool = False,
    ) -> DiscoveryBatch:
        """Process a discovery snapshot and return per-item outcomes."""

    def recover_stale_leases(self) -> list[str]:
        """Move nonterminal leased attempts to needs-reconciliation."""
```

`BoundaryPolicy`, `PlanRequest`, `Plan`, `PlannedAction`, `ExecutionMode`, `ExecutionReport`, `ActionResult`, `ItemSnapshot`, `BatchItemResult`, and `DiscoveryBatch` are immutable values at the module's interface. `BoundaryPolicy` declares mounted data roots, the excluded config volume, watch roots, and optional filesystem case capability. Planning rejects roots outside data volumes, config-volume paths, overlapping watches, unsafe symlink traversal, self/descendant targets, and destination collisions. A destination that is another watch root is allowed and adds a visible warning to the plan diagnostics. A `Plan` is the primary dry-run and preview artifact: it contains the matched rule, ordered intended actions, resolved destinations, and source fingerprint. Applying a plan revalidates the source and destinations immediately before mutation; a stale plan cannot silently apply. Callers do not parse YAML, construct actions, call filesystem adapters, or write the Tracking DB.

The module's implementation uses internal seams for the YAML rule source, filesystem, archive formats, Tracking DB, and structured event sink. These adapters translate infrastructure failures into stable processing results. The public interface remains the same for watcher, scanner, CLI, and web UI callers.

The planner does not mutate the filesystem or Tracking DB. The executor creates a durable processing attempt before execution, performs internal preflight checks, executes actions in order, records each action outcome, and stops after the first failure. Dry-run execution reports the same plan without filesystem mutation or Tracking DB completion.

## Rule schema

```yaml
rules:
  - name: <string>                        # required, unique per watch folder
    match:
      name: <string>                        # optional, unique within the rule
      field: folder_name | file_name | full_path
      pattern: <regex>                    # always regex
    actions:
      - <action_type>:
          <action_params>
    allow_direct_deletion: false            # optional; required for delete mode: direct
    allow_hard_link_removal: false           # optional; required to delete items with multiple hard links
```

Rules are evaluated in order within a watch folder; **first match wins** — the first rule whose match condition fires has its actions executed, and evaluation stops for that item. A named match condition's numbered (`\\1`) and named (`\\g<name>`) captures must be valid references in all action parameters. Invalid references disable that rule and produce a visible disabled-earlier-rule warning when a later rule matches.

Rules are loaded from the watch folder's `rules.yaml` file. Invalid YAML, invalid regex patterns, unknown fields or actions, and missing required action parameters prevent that rule from running and are logged as errors. Other valid rules continue to run.

## Actions

### Move

```yaml
- move:
    destination: <path>       # required, absolute or relative to watch folder
```

Moves the matched file or folder to `destination`. If the destination directory doesn't exist, it is created. An existing destination item is a collision and is never overwritten.

Same-filesystem moves publish via a hard link followed by source removal. Cross-filesystem moves use staged-copy semantics: a private staged copy is created on the destination filesystem, published without overwriting, the resulting path is recorded, and only then is the source removed. If source removal fails after publication, both paths are retained and the attempt enters `needs-reconciliation`; Organizer never deletes published output.

### Copy

```yaml
- copy:
    destination: <path>       # required
```

Copies the matched item through private destination staging, then publishes it without overwriting an existing item. The source must retain its planned fingerprint until publication; otherwise publication is refused.

### Delete

```yaml
- delete:
    mode: direct         # required: "direct" or "quarantine"
```

Permanently deletes the matched item. Direct deletion requires `allow_direct_deletion: true` on the rule. Direct deletion of a folder verifies a stable tree fingerprint immediately before removal; an uncertain fingerprint produces a `needs-reconciliation` attempt rather than proceeding.

```yaml
rules:
  - name: Clean up temp files
    match:
      field: file_name
      pattern: '\.tmp$'
    actions:
      - delete:
          mode: direct
    allow_direct_deletion: true
```

### Quarantine

```yaml
- delete:
    mode: quarantine
```

Recoverable removal to an Organizer-managed, attempt-specific directory under a configured quarantine root (`BoundaryPolicy.quarantine_root`). The quarantine path preserves the original relative path from the watch root and records the resulting path on the attempt.

```yaml
rules:
  - name: Quarantine unknown files
    match:
      field: file_name
      pattern: '\.unknown$'
    actions:
      - delete:
          mode: quarantine
```

The quarantine root must be configured in the boundary policy. Quarantined paths are excluded from ordinary discovery and rule planning.

Deletion (both modes) enforces:
- **Fingerprint check**: the source's current fingerprint must match the planned fingerprint immediately before mutation. A mismatch enters `needs-reconciliation`.
- **Stable-tree check for folders**: folder deletion re-verifies the tree fingerprint before removal.
- **Hard-link removal opt-in**: files with multiple hard-link directory entries require `allow_hard_link_removal: true` on the rule. Without it, the action fails.

### Rename

```yaml
- rename:
    name: <string>            # required; complete new item name
```

Renames the matched file or folder within its current parent directory. `name` can be a literal name or use `\1`, `\2`, and later capture references from the rule's match regex.

```yaml
- name: Remove cosplay tag
  match:
    field: file_name
    pattern: '^(.*) \[cosplay\](\.[^.]+)$'
  actions:
    - rename:
        name: '\1\2'
```

This renames `Alice Costume [cosplay].zip` to `Alice Costume.zip`. An invalid capture reference or a name that is invalid for the host filesystem fails validation and disables the rule.

Plans include a SHA-256 ruleset revision. Applying a plan after the rules file changes is rejected as stale. UI rule saves resolve the watch identifier to the configured rules file path, validate the complete rule semantics before an atomic compare-and-swap write, and return a conflict rather than overwriting concurrent edits. Invalid rules, including malformed YAML, invalid regex, unknown fields or actions, missing required parameters, and invalid capture references, are rejected before any file change.

### Unarchive

```yaml
- unarchive:
    preserve_archive: false   # default: false — delete archive after extraction
    destination: <path>       # optional — default: same directory as archive
```

Extracts .zip, .7z, .rar archives. Extracted contents are placed in `destination` (or alongside the archive). On `preserve_archive: false`, the original archive is deleted after successful extraction.

Supports nested archives up to a configurable depth (default: 1 level). Exceeding the depth limit logs a warning and skips. Corrupted, password-protected, and unsupported archives are not extracted; the failure is logged, recorded on the processing attempt, and the original archive remains in place.

### Archive

```yaml
- archive:
    format: zip | 7z          # required
    destination: <path>       # required
    preserve_originals: false # default: false — delete originals after archiving
```

Matches files or folders and bundles them into a single archive file in `destination`, which is an output directory. The archive output name removes at most one final recognized input archive suffix (`.zip`, `.7z`, or `.rar`, case-insensitive), preserves all other suffixes, and appends the requested extension. Thus `project` becomes `project.zip` or `project.7z`, `project.zip` becomes `project.zip` or `project.7z`, and `bundle.tar.gz` becomes `bundle.tar.gz.zip`.

## Destination collisions

Organizer never overwrites an existing item. If a move, copy, rename, unarchive, or archive action would create an item at a path that already exists, the action fails. Remaining actions in the rule are skipped, automatic watcher and scan retries are suppressed, and the collision is logged as an ERROR result. Each action consumes the primary resulting item from the prior action; direct deletion produces no result and must be final. Completed copies retain source and result identities as copy provenance.

The web UI exposes failed items for review, including the source item, intended destination, rule, action, and failure detail. Collision and archive-input failures, including password-protected archives, are not retried automatically by watcher or scan events. After the user resolves the problem, an explicit retry creates a new processing attempt; the original attempt remains in history.

## Execution and recovery

Actions execute one at a time in declared order. Each action is revalidated immediately before mutation, and its outcome is recorded against the processing attempt. A failure stops later actions; earlier successful actions remain successful and are not blindly repeated.

The executor does not promise filesystem transactions or rollback. For actions where the result can be verified, the implementation records evidence such as the resulting path and fingerprint. When the outcome is uncertain, the attempt becomes `needs-reconciliation` and is not automatically retried.

Collision and known archive-input failures are ordinary `failed` attempts and suppress automatic watcher and scan retries. An explicit user retry creates a new processing attempt. Reconciliation cases expose the evidence and allow an operator to accept resulting paths, mark an action applied, retry remaining actions, retry from the start, or abandon the attempt. `retry from the start` always creates a new attempt.

The attempt state transitions are:

```text
started -> completed
        -> failed
        -> needs-reconciliation -> abandoned
                                -> accepted (via accepted action results)
```

`completed` means every planned action succeeded and resulting paths were recorded. `failed` means the attempt requires explicit retry or review. `needs-reconciliation` means filesystem effects may exist but completion cannot be established safely. `abandoned` is a terminal state created by explicit administrator action with a recorded reason and a suppression; reopening creates a fresh plan while preserving history. Accepted action results are immutable records of administrator-confirmed resulting paths for uncertain actions during reconciliation.

A password-protected archive is a known input failure, not a process-wide failure. Organizer records a `password_protected_archive` failure for that item, leaves the archive in place, exposes it for review, and continues processing other discovered items.

## Dry run

Rule evaluation is split into two stages:

1. **Planning** — evaluates an item against ordered rules and produces an execution plan containing the matched rule and intended actions.
2. **Execution** — validates and applies an execution plan, or reports it without mutation when running in dry-run mode.

In dry run mode, the planner produces the immutable preview plan and the executor reports its intended action sequence, but no filesystem mutations or Tracking DB updates occur. Each action logs what it *would* have done:

```
[Dry Run] Watch: Downloads | Rule: Cosplay folders | Action: move | Item: /data/Downloads/[cosplay] armor | Target: /media/cosplay/[cosplay] armor
```

CLI: `organizer check <watch>` runs a dry run and prints results to stdout.
Web UI: each watch folder has a "Dry run" button that shows results in-app.

## Evaluation flow

1. **Item discovered** — via filesystem watcher event or periodic scan.
2. **Stability check** — if the item's size and modification time have not remained unchanged for the configured stability interval, the item is deferred to a later evaluation. Deferred items do not create attempts, failures, or suppressions.
3. **Tracking DB check** — if a completed processing attempt exists for the item's unchanged source fingerprint (path + content hash), skip. Items with failed or needs-reconciliation attempts are not treated as completed.
4. **Lease acquisition** — an exclusive processing lease is acquired for the source identity (canonical path + source fingerprint). If a lease is already held by another trigger, the item is reported as outside the current snapshot.
5. **Planning** — rules for the watch folder are evaluated in order. The first matching rule produces an execution plan; no later rules are considered for that item.
6. **Match** — the item's field (folder_name, file_name, full_path) is tested against the rule's regex pattern.
7. **Attempt creation** — a durable processing attempt is recorded as `started` before filesystem mutation.
8. **Preflight** — source state, action parameters, destinations, collisions, and known archive requirements are checked without promising transactional execution.
9. **Execution** — the executor applies actions in sequence, records action outcomes, and stops processing the current item's remaining actions after the first failure. The watch processing loop continues with other discovered items.
10. **Completion** — after every action succeeds, the attempt records `completed` and all resulting paths. The processing lease is released. Failures record `failed` or `needs-reconciliation` with their details. Collision failures are `failed` and require explicit user retry.

Filesystem mutation and Tracking DB writes are not one transaction. If filesystem mutation succeeds but completion recording fails, the attempt becomes `needs-reconciliation`; Organizer does not automatically repeat the filesystem actions. Reconciliation records the existing result before the item can be considered complete.

## Processing leases

A processing lease is an exclusive, durable ownership record for a source identity within a watch folder. The `processing_leases` table stores one lease per (watch_id, source_path, source_fingerprint) tuple. Lease acquisition is atomic and idempotent within the same source identity: a second acquisition attempt for the same identity returns false.

The executor acquires a lease before creating a processing attempt and releases it after the attempt becomes terminal (completed or failed). Dry-run execution does not acquire or release leases.

If a nonterminal leased attempt (status `started`) is found after a restart, `recover_stale_leases` moves it to `needs-reconciliation` and releases its lease. This prevents automatic repetition of uncertain filesystem work.

## Discovery batches

`process_batch` accepts a list of `ItemSnapshot` observations and returns a `DiscoveryBatch` with per-item outcomes. Each item is reported as one of:

- **executed** — planned and executed in this batch
- **skipped** — already completed for the same source identity
- **deferred** — not yet stable for the configured interval
- **outside_snapshot** — already leased by another trigger

The batch reports a deduplicated diagnostic when any items are deferred. Batch processing does not pause other triggers; a concurrent watcher or scan can process different items while the batch runs.

## Logging

- **Output**: structured text to stdout (for `docker logs`), a rotating log file at `/config/logs/organizer.log`, and an in-memory buffer for the web log viewer.
- **Rotation**: configurable retention and size limits. Initial values are 7 days and 10MB per log file. The sink persists each entry with `fsync`, rotates when the current file would exceed the size limit, and removes backup files older than the retention period or beyond the configured backup count.
- **Format** per line:

```
<timestamp> | <level> | <watch> | <rule> | <action> | <item> | <result> | <detail>
```

- **Levels**: INFO, WARN, ERROR, DRYRUN
- **Result**: OK, SKIPPED, FAILED, DRY_RUN
- **Detail**: free-text explanation (e.g., "Destination already exists", "Nested archive depth exceeded", "Archive is password protected")

## Web UI log viewer

A server-rendered `/logs` page shows recent structured log entries and supports filtering by level, watch, and inclusive UTC date range. The in-memory entry limit is configurable; the initial limit is 1000 entries. Full history lives in the log file. The JSON response remains available to non-HTML callers.

## Reconciliation UI

The initial reconciliation UI is server-rendered and uses HTMX actions at `/attempts` and `/attempts/{attempt_id}`. An attempt list shows failed and `needs-reconciliation` attempts with their watch folder, source item, rule, action, status, failure category, and created time. An attempt detail view shows the source fingerprint, planned actions, per-action results, intended destinations, resulting paths, filesystem evidence, failure detail, and related retry attempts.

The UI supports explicit commands for retry, accepting resulting paths, marking an action applied, retrying remaining actions, retrying from the start, and abandoning an attempt. Command handlers delegate to an attempt-review application module; they do not manipulate files or Tracking DB records directly. Every command and result is recorded. Ambiguous evidence requires explicit confirmation, and retrying from the start always creates a new processing attempt.

Accepting a resulting path requires filesystem evidence, a configured destination policy, a match against the planned action target, and a recorded fingerprint. Missing items, unsafe paths, unexpected paths, and fingerprint mismatches are rejected.

`retry from the start` and `reopen` first create a fresh plan, then execute a new attempt, and only clear the source identity suppression if the new attempt reaches `completed`. A planning failure or failed execution report preserves the suppression so automatic processing does not resume.

The attempt-review module has one application-facing interface:

```python
class AttemptReview:
    def list(self, filters) -> list[AttemptSummary]: ...
    def inspect(self, attempt_id) -> AttemptReviewDetails: ...
    def command(self, attempt_id, command) -> CommandResult: ...
```

Bulk actions, live updates, dashboards, and rich archive-content previews are outside the initial UI scope.

## Tracking DB

SQLite database at `/config/organizer.db`. Schema:

```sql
CREATE TABLE processing_attempts (
    attempt_id          TEXT PRIMARY KEY,
    watch_id            TEXT NOT NULL,
    source_path         TEXT NOT NULL,
    source_size         INTEGER NOT NULL,
    source_mtime        REAL NOT NULL,
    source_fingerprint  TEXT NOT NULL DEFAULT '',
    rule_name           TEXT,
    planned_actions     TEXT NOT NULL,
    status              TEXT NOT NULL,
    action_results      TEXT,
    retry_of_attempt_id TEXT,
    resulting_paths     TEXT,
    failure_detail      TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT
);

CREATE TABLE processing_leases (
    watch_id           TEXT NOT NULL,
    source_path        TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    attempt_id         TEXT NOT NULL,
    acquired_at        TEXT NOT NULL,
    PRIMARY KEY (watch_id, source_path, source_fingerprint)
);

CREATE TABLE item_observations (
    watch_id      TEXT NOT NULL,
    source_path   TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    first_seen_at REAL NOT NULL,
    PRIMARY KEY (watch_id, source_path)
);
```

An attempt is the unit of processing history. It can reference one source item and multiple resulting paths, so moves, renames, archives, and unarchives do not require a permanent source-path identity. `status` is one of `started`, `completed`, `failed`, or `needs-reconciliation`.

`processing_leases` stores one exclusive lease per source identity (watch_id + source_path + source_fingerprint). A lease is acquired before attempt creation and released when the attempt becomes terminal.

`item_observations` tracks when each item was first seen at its current size and modification time, enabling the stability check that defers items still being written.

For actions with reliable outcome evidence, the attempt records the action result and resulting fingerprint. Archive and unarchive operations may produce multiple paths or uncertain outcomes and can require reconciliation rather than automatic retry.

## Directory layout

```
/config/
  organizer.yaml                 # global settings (scan interval, log level, etc.)
  organizer.db                   # tracking database
  watches/
    Downloads/
      rules.yaml
    Inbox/
      rules.yaml
  logs/
    organizer.log

/data/
  Downloads/                     # watch folder contents
  Inbox/
```

## Runtime configuration

`/config/organizer.yaml` is the source of truth for watch discovery. It defines
global `scan_interval`, `stability_interval`, `log_level`, and `retention_days`
settings, mounted
`data_roots`, a `quarantine_root`, and a non-empty `watches` list. Each watch
has an `id`, an absolute `root`, and a `rules` path (relative paths resolve from
the configuration file). Startup validates that watch roots are disjoint, lie
within a data root, and do not enter the config volume. The loader resolves each
watch to a `WatchFolderConfig` and `BoundaryPolicy`; CLI and web callers accept
only a watch identifier and resolve these values from the loaded configuration.
On first container start, the entrypoint discovers non-system bind mounts from
`/proc/mounts` and adds unique paths to `data_roots`; it never creates watches
for those paths and never changes an existing configuration file.

Example:

```yaml
scan_interval: 300
stability_interval: 5
log_level: INFO
retention_days: 7
data_roots: [/data]
quarantine_root: /data/.quarantine
watches:
  - id: downloads
    root: /data/Downloads
    rules: watches/Downloads/rules.yaml
```

`stability_interval` is the number of seconds a file's size and modification time
must remain unchanged before it is planned and executed. On watch roots backed by
inotify (local ext4) the write-complete (`closed`) event already provides correct
semantics, so the fast path is kept regardless of this setting. On polling-backed
watch roots (FUSE/Unraid user shares, NFS, CIFS/SMB, 9p), the polling observer
never emits a close event, so this stability gate is the only reliable signal and
is applied before any file is moved or renamed. Set it to `0` to disable the gate.
Default is `5` seconds.

## Production deployment

The production image is built from `Dockerfile` and persists Organizer configuration,
the Tracking DB, and logs under `/config`, separately from watched content under
`/data`. The image installs `unrar-free` for RAR extraction; startup logs an explicit
error if neither `unrar` nor `unar` is available, and affected RAR inputs remain
available for review.

The production daemon (`organizer run`) starts the web server, filesystem watcher,
and periodic scanner as one process. The container binds to `0.0.0.0:8000` so Docker
forwarding works; the supplied Compose configuration publishes it only on the host's
loopback interface. `ORGANIZER_HOST` and `ORGANIZER_PORT` may override the container
bind for a trusted deployment boundary. Any non-loopback host emits a prominent
warning because the administrative UI is unauthenticated.

The web server and background services share a single `ItemProcessor`, database
connection, health checker, structured logger, and log sinks. `organizer run`
creates these shared components once and passes them to both the daemon and the
web application.
