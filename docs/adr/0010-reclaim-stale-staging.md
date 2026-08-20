# Reclaim stale staging artifacts

`unarchive` (and archive/copy/move) create per-attempt staging directories inside the destination (`<dest>/.organizer-staging-<uuid>`) and normally prune them after a successful publish. Attempts that fail or never terminate can leave these behind, accumulating unbounded disk use.

## Decision

Guarantee staging cleanup on terminal outcomes and add a periodic sweep for attempts that never terminate.

### 1. Terminal-state cleanup

- `py7zr.exceptions.PasswordRequired` is added to `_ARCHIVE_ERRORS`. Password-protected archives now reach the failure handler, which classifies the attempt as `password-protected`, finishes it as `failed`, and records a suppression — instead of leaking a `started` attempt and a staging dir.
- The staging-creating helpers (`_unarchive_to_staging`, `_archive_to_staging`, `_copy_to_staging`) remove their staging directory on **any** exception and re-raise. This makes the invariant "extract/archive/copy to staging or clean up on failure" hold for every failure type, not just the enumerated archive errors.

### 2. Periodic sweep for hung attempts

A hung extraction (the daemon blocks inside py7zr and never returns) cannot be cleaned by a `finally`. A periodic sweep reclaims it instead.

- The retention sweep now walks the configured **data roots** (where staging actually lives, adjacent to each action target) in addition to the existing config-volume staging root, looking for `.organizer-staging-*` entries.
- A staging entry is only removed when the **newest modification time anywhere in its subtree** is older than `staging_cleanup_age` (configurable, default `3600` seconds). An actively-writing large extraction keeps recent mtimes deep in the tree and is never mistaken for a stale artifact.
- The walk descends directories only and does not enter unrelated organizer-managed dot-directories (e.g. quarantine), keeping the sweep bounded on large trees.

## Alternatives considered

- **A single central staging root**: would make cleanup trivial but breaks atomic publication — files are published with `os.link` and directories with a same-filesystem rename, so staging must live on the target's filesystem (`target.parent`). Centralizing would force a cross-filesystem copy for every action.
- **Track staging paths in the DB keyed to the attempt**: precise but adds schema, and hung attempts are re-created every scan tick, so the DB would accumulate `started` attempts too. A pure filesystem sweep is simpler and covers crash/kill cases regardless of attempt records.

## Consequences

- Staging artifacts from failed or hung archive/copy/unarchive attempts are reclaimed periodically instead of accumulating without bound.
- A new config value `staging_cleanup_age` (seconds) controls how stale an artifact must be before removal; the default `3600` is safely longer than any normal extraction while bounding accumulation from hung attempts.
- Actively-running extractions are never deleted because the sweep requires the entire subtree to be quiescent for the full age window.
