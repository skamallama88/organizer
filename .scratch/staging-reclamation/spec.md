# Reclaim stale staging artifacts

## Problem Statement

`unarchive` creates a per-attempt staging directory inside the destination (`<dest>/.organizer-staging-<uuid>`) and prunes it after a successful publish. Attempts that fail (e.g. `PasswordRequired`, corrupt archive, resource-limit) or hang (a `started` attempt that never reaches a terminal state, e.g. a password-protected `.7z` that blocks inside py7zr) leave the staging directory behind — unbounded disk accumulation (observed: ~2,896 dirs, ~1.1–1.3 GB).

## Solution

Guarantee staging cleanup on terminal outcomes and add a periodic sweep for attempts that never terminate:

1. **Terminal-state cleanup** — add `py7zr.exceptions.PasswordRequired` to `_ARCHIVE_ERRORS` so password-protected archives finish as a `failed` attempt with suppression instead of leaking a `started` attempt + staging dir. Make the staging-creating helpers (`_unarchive_to_staging`, `_archive_to_staging`, `_copy_to_staging`) remove their staging directory on any exception.
2. **Periodic sweep** — the retention sweep walks the configured data roots (where staging actually lives) for `.organizer-staging-*` entries, removing any whose whole subtree has been unmodified for `staging_cleanup_age` seconds (default `3600`, configurable). An actively-writing extraction keeps recent mtimes and is never deleted.

## User Stories

1. As an administrator, I want failed/hung archive attempts not to leave `.organizer-staging-*` directories accumulating, so disk use stays bounded.
2. As an administrator, I want a periodic sweep to reclaim stale staging left by hung attempts, so I don't have to clean them by hand.
3. As an administrator, I want the sweep to never delete an actively-running extraction, so a large archive being written is not corrupted.
4. As an administrator, I want the stale-age threshold configurable, so I can tune between aggressiveness and safety.

## Implementation Decisions

- `staging_cleanup_age` is a global config integer (seconds, default `3600`) on `OrganizerConfig`, parsed/validated in `load_config`.
- `Retention` gains `data_roots` and `staging_cleanup_age`; `create_daemon` wires `config.data_roots` and `config.staging_cleanup_age` into it.
- `clean_staging_artifacts` sweeps the existing config staging root plus each data root. The walk descends directories only and skips unrelated `.organizer-*` dot-directories (e.g. quarantine) to stay bounded.
- Staleness uses the newest mtime anywhere in the staging subtree (`_is_stale_staging`), so an active deep extraction is never misjudged as stale.
- `py7zr.exceptions.PasswordRequired` joins `_ARCHIVE_ERRORS`; the failure handler classifies it as `password-protected archive`.

## Testing Decisions

- Retention: stale staging in a data root removed; recently-active subtree preserved; nested staging found; organizer-managed dirs skipped; `retention_run` honours `staging_cleanup_age`.
- Config: `staging_cleanup_age` parse/default/invalid.
- Daemon: `create_daemon` wires `data_roots`/`staging_cleanup_age` into retention.
- Item processor: a password-protected `.7z` unarchive reports `failed` with `password-protected`, leaves no `.organizer-staging-*`, records suppression, and finishes the attempt (no `started` leak).

## Out of Scope

- Redesigning staging to a single central root (would break same-filesystem atomic publication).
- Per-watch staging age thresholds (global only).
- Automatically terminating hung attempts (the sweep reclaims their disk, but does not unblock the stuck extraction).
