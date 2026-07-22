# Architecture

## Rule schema

```yaml
rules:
  - name: <string>                        # required, unique per watch folder
    match:
      field: folder_name | file_name | full_path
      pattern: <regex>                    # always regex
    actions:
      - <action_type>:
          <action_params>
```

Rules are evaluated in order within a watch folder; **first match wins** — the first rule whose match condition fires has its actions executed, and evaluation stops for that item.

Rules are loaded from the watch folder's `rules.yaml` file. Invalid YAML, invalid regex patterns, unknown fields or actions, and missing required action parameters prevent that rule from running and are logged as errors. Other valid rules continue to run.

## Actions

### Move

```yaml
- move:
    destination: <path>       # required, absolute or relative to watch folder
```

Moves the matched file or folder to `destination`. If the destination doesn't exist, it is created.

### Copy

```yaml
- copy:
    destination: <path>       # required
```

Copies the matched item, leaving the original in place.

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

This renames `Alice Costume [cosplay].zip` to `Alice Costume.zip`. An invalid capture reference or a name that is invalid for the host filesystem fails the action and is logged.

### Unarchive

```yaml
- unarchive:
    preserve_archive: false   # default: false — delete archive after extraction
    destination: <path>       # optional — default: same directory as archive
```

Extracts .zip, .7z, .rar archives. Extracted contents are placed in `destination` (or alongside the archive). On `preserve_archive: false`, the original archive is deleted after successful extraction.

Supports nested archives up to a configurable depth (default: 1 level). Exceeding the depth limit logs a warning and skips. Corrupted, password-protected, and unsupported archives are not extracted; the failure is logged and the original archive remains in place.

### Archive

```yaml
- archive:
    format: zip | 7z          # required
    destination: <path>       # required
    preserve_originals: false # default: false — delete originals after archiving
```

Matches files or folders and bundles them into a single archive file in `destination`, which is an output directory. The archive is named after the matched item with the appropriate extension appended.

## Destination collisions

Organizer never overwrites an existing item. If a move, copy, rename, unarchive, or archive action would create an item at a path that already exists, the action fails. Remaining actions in the rule are skipped, the item remains eligible for retry, and the collision is logged as an ERROR result.

The web UI exposes failed items for review, including the source item, intended destination, rule, action, and failure detail. A user can use this review list to resolve the collision outside Organizer before retrying the item.

## Dry run

In dry run mode, the rule engine evaluates matches and determines which actions would fire, but no filesystem mutations or Tracking DB updates occur. Each action logs what it *would* have done:

```
[Dry Run] Watch: Downloads | Rule: Cosplay folders | Action: move | Item: /data/Downloads/[cosplay] armor | Target: /media/cosplay/[cosplay] armor
```

CLI: `organizer check <watch>` runs a dry run and prints results to stdout.
Web UI: each watch folder has a "Dry run" button that shows results in-app.

## Evaluation flow

1. **Item discovered** — via filesystem watcher event or periodic scan.
2. **Tracking DB check** — if the item's fingerprint (path + mtime + size) is already recorded and unchanged, skip.
3. **Rule iteration** — rules for the watch folder are evaluated in order.
4. **Match** — the item's field (folder_name, file_name, full_path) is tested against the rule's regex pattern.
5. **Action execution** — all actions in the first matching rule are executed in sequence. If any action fails, remaining actions are skipped and the failure is logged.
6. **Tracking DB update** — after every action succeeds, the item's path, size, and modification time are recorded so it won't be re-processed. Failures remain eligible for later watcher or scan retries.

## Logging

- **Output**: structured text to stdout (for `docker logs`) and a rotating log file at `/config/logs/organizer.log`.
- **Rotation**: configurable retention and size limits. Initial values are 7 days and 10MB per log file.
- **Format** per line:

```
<timestamp> | <level> | <watch> | <rule> | <action> | <item> | <result> | <detail>
```

- **Levels**: INFO, WARN, ERROR, DRYRUN
- **Result**: OK, SKIPPED, FAILED, DRY_RUN
- **Detail**: free-text explanation (e.g., "Destination already exists", "Nested archive depth exceeded")

## Web UI log viewer

A page under each watch folder showing recent log entries for that watch. Supports filtering by level and date range. The in-memory entry limit is configurable; the initial limit is 1000 entries. Full history lives in the log file.

## Tracking DB

SQLite database at `/config/organizer.db`. Schema:

```sql
CREATE TABLE processed_files (
    watch_id    TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    file_size   INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    processed_at TEXT NOT NULL,
    rule_name   TEXT,
    action      TEXT,
    PRIMARY KEY (watch_id, file_path)
);
```

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
