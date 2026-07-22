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

### Unarchive

```yaml
- unarchive:
    preserve_archive: false   # default: false — delete archive after extraction
    destination: <path>       # optional — default: same directory as archive
```

Extracts .zip, .7z, .rar archives. Extracted contents are placed in `destination` (or alongside the archive). On `preserve_archive: false`, the original archive is deleted after successful extraction.

Supports nested archives up to a configurable depth (default: 1 level). Exceeding the depth limit logs a warning and skips.

### Archive

```yaml
- archive:
    format: zip | 7z          # required
    destination: <path>       # required
    preserve_originals: false # default: false — delete originals after archiving
```

Matches files or folders and bundles them into a single archive file at `destination`. The archive is named after the matched item with the appropriate extension appended.

## Dry run

In dry run mode, the rule engine evaluates matches and determines which actions would fire, but no filesystem mutations occur. Each action logs what it *would* have done:

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
6. **Tracking DB update** — the item's fingerprint is recorded (or updated) so it won't be re-processed.

## Logging

- **Output**: structured text to stdout (for `docker logs`) and a rotating log file at `/config/logs/organizer.log`.
- **Rotation**: logrotate-style — keep 7 days of logs, rotate at 10MB.
- **Format** per line:

```
<timestamp> | <level> | <watch> | <rule> | <action> | <item> | <result> | <detail>
```

- **Levels**: INFO, WARN, ERROR, DRYRUN
- **Result**: OK, SKIPPED, FAILED, DRY_RUN
- **Detail**: free-text explanation (e.g., "Destination already exists", "Nested archive depth exceeded")

## Web UI log viewer

A page under each watch folder showing recent log entries for that watch. Supports filtering by level and date range. The last 1000 entries are kept in memory for the viewer; full history lives in the log file.

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
