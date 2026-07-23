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
```

`BoundaryPolicy`, `PlanRequest`, `Plan`, `PlannedAction`, `ExecutionMode`, `ExecutionReport`, and `ActionResult` are immutable values at the module's interface. `BoundaryPolicy` declares mounted data roots, the excluded config volume, watch roots, and optional filesystem case capability. Planning rejects roots outside data volumes, config-volume paths, overlapping watches, unsafe symlink traversal, self/descendant targets, and destination collisions. A destination that is another watch root is allowed and adds a visible warning to the plan diagnostics. A `Plan` is the primary dry-run and preview artifact: it contains the matched rule, ordered intended actions, resolved destinations, and source fingerprint. Applying a plan revalidates the source and destinations immediately before mutation; a stale plan cannot silently apply. Callers do not parse YAML, construct actions, call filesystem adapters, or write the Tracking DB.

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

### Copy

```yaml
- copy:
    destination: <path>       # required
```

Copies the matched item, leaving the original in place. An existing destination item is a collision and is never overwritten.

### Delete

```yaml
- delete: {}
```

Permanently deletes the matched item. No confirmation.

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

Plans include a SHA-256 ruleset revision. Applying a plan after the rules file changes is rejected as stale. UI rule saves compare the submitted revision with the current file revision and return a conflict rather than overwriting concurrent edits.

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

Matches files or folders and bundles them into a single archive file in `destination`, which is an output directory. The archive is named after the matched item with the appropriate extension appended.

## Destination collisions

Organizer never overwrites an existing item. If a move, copy, rename, unarchive, or archive action would create an item at a path that already exists, the action fails. Remaining actions in the rule are skipped, automatic watcher and scan retries are suppressed, and the collision is logged as an ERROR result.

The web UI exposes failed items for review, including the source item, intended destination, rule, action, and failure detail. Collision and archive-input failures, including password-protected archives, are not retried automatically by watcher or scan events. After the user resolves the problem, an explicit retry creates a new processing attempt; the original attempt remains in history.

## Execution and recovery

Actions execute one at a time in declared order. Each action is revalidated immediately before mutation, and its outcome is recorded against the processing attempt. A failure stops later actions; earlier successful actions remain successful and are not blindly repeated.

The executor does not promise filesystem transactions or rollback. For actions where the result can be verified, the implementation records evidence such as the resulting path and fingerprint. When the outcome is uncertain, the attempt becomes `needs-reconciliation` and is not automatically retried.

Collision and known archive-input failures are ordinary `failed` attempts and suppress automatic watcher and scan retries. An explicit user retry creates a new processing attempt. Reconciliation cases expose the evidence and allow an operator to accept resulting paths, mark an action applied, retry remaining actions, retry from the start, or abandon the attempt. `retry from the start` always creates a new attempt.

The attempt state transitions are:

```text
started -> completed
        -> failed
        -> needs-reconciliation
```

`completed` means every planned action succeeded and resulting paths were recorded. `failed` means the attempt requires explicit retry or review. `needs-reconciliation` means filesystem effects may exist but completion cannot be established safely.

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
2. **Tracking DB check** — if a completed processing attempt exists for the item's unchanged source fingerprint (path + mtime + size), skip. Items with failed or needs-reconciliation attempts are not treated as completed.
3. **Planning** — rules for the watch folder are evaluated in order. The first matching rule produces an execution plan; no later rules are considered for that item.
4. **Match** — the item's field (folder_name, file_name, full_path) is tested against the rule's regex pattern.
5. **Attempt creation** — a durable processing attempt is recorded as `started` before filesystem mutation.
6. **Preflight** — source state, action parameters, destinations, collisions, and known archive requirements are checked without promising transactional execution.
7. **Execution** — the executor applies actions in sequence, records action outcomes, and stops processing the current item's remaining actions after the first failure. The watch processing loop continues with other discovered items.
8. **Completion** — after every action succeeds, the attempt records `completed` and all resulting paths. Failures record `failed` or `needs-reconciliation` with their details. Collision failures are `failed` and require explicit user retry.

Filesystem mutation and Tracking DB writes are not one transaction. If filesystem mutation succeeds but completion recording fails, the attempt becomes `needs-reconciliation`; Organizer does not automatically repeat the filesystem actions. Reconciliation records the existing result before the item can be considered complete.

## Logging

- **Output**: structured text to stdout (for `docker logs`) and a rotating log file at `/config/logs/organizer.log`.
- **Rotation**: configurable retention and size limits. Initial values are 7 days and 10MB per log file.
- **Format** per line:

```
<timestamp> | <level> | <watch> | <rule> | <action> | <item> | <result> | <detail>
```

- **Levels**: INFO, WARN, ERROR, DRYRUN
- **Result**: OK, SKIPPED, FAILED, DRY_RUN
- **Detail**: free-text explanation (e.g., "Destination already exists", "Nested archive depth exceeded", "Archive is password protected")

## Web UI log viewer

A page under each watch folder showing recent log entries for that watch. Supports filtering by level and date range. The in-memory entry limit is configurable; the initial limit is 1000 entries. Full history lives in the log file.

## Reconciliation UI

The initial reconciliation UI is server-rendered and uses HTMX actions. An attempt list shows failed and `needs-reconciliation` attempts with their watch folder, source item, rule, action, status, failure category, and created time. An attempt detail view shows the source fingerprint, planned actions, per-action results, intended destinations, resulting paths, filesystem evidence, failure detail, and related retry attempts.

The UI supports explicit commands for retry, accepting resulting paths, marking an action applied, retrying remaining actions, retrying from the start, and abandoning an attempt. Command handlers delegate to an attempt-review application module; they do not manipulate files or Tracking DB records directly. Every command and result is recorded. Ambiguous evidence requires explicit confirmation, and retrying from the start always creates a new processing attempt.

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
```

An attempt is the unit of processing history. It can reference one source item and multiple resulting paths, so moves, renames, archives, and unarchives do not require a permanent source-path identity. `status` is one of `started`, `completed`, `failed`, or `needs-reconciliation`.

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
