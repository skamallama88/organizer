# Organizer

Organizer is a file organization daemon for macOS and Unraid. The project
applies first-match YAML rules through a shared `ItemProcessor` module that
separates planning from execution.

## Current Status

### Implemented

- YAML rule loading and validation diagnostics; invalid rules do not prevent valid rules from running.
- First-match rule evaluation with named match conditions and capture references.
- Immutable plans with resolved destinations, source fingerprints, and ruleset revisions.
- Boundary policy validation: data roots, config-volume exclusion, disjoint watches, safe destinations, self/descendant-target rejection, symlink-traversal rejection, case-aware collisions, cross-watch destination warnings.
- CLI (`organizer check`) and web dry-run previews from the same plan.
- Same-filesystem moves (hard-link + source removal) and staged copies with no-overwrite publication.
- Cross-filesystem moves (staged copy, no-overwrite publication, durable result recording, source removal).
- Capture-based renames and ordered action chains using the primary resulting item; invalid type chains are rejected at planning.
- SQLite execution-attempt records with action results, resulting paths, and processing lineage.
- Structured structured-log events (stdout, rotating persistent file, in-memory web buffer) with level, watch, rule, action, item, result, and detail fields.
- Docker image packaging with unrar-free, separate `/config` and `/data` volumes, localhost-only UI binding with non-loopback warning.
- Durable processing leases for exclusive per-source-identity ownership.
- Completion skipping for unchanged items across restarts.
- Item stability observations and deferred-item handling.
- Discovery-batch processing with executed, skipped, deferred, and outside-snapshot outcomes.
- Restart recovery of nonterminal leased attempts to needs-reconciliation.
- Collision suppression: durable suppression of automatic processing after a destination collision.
- Explicit retry (`retry_attempt`): creates a fresh linked plan for a failed or suppressed attempt.
- Explicit reprocess (`reprocess_item`): permits a completed source identity to be processed again under current rules.
- Suppression visibility: `has_suppressed_attempt` and `suppressed_attempts` expose suppressed source identities.
- Watcher service (watchdog-based, debounced) and periodic scanner (asyncio interval).
- Combined daemon entry point (`organizer run`) starting web + watcher + scanner + logging.
- CLI `organizer status` showing configured watches.
- Config-driven watch discovery: Pydantic models for `organizer.yaml`, loader with validation.
- Web UI dashboard (watch list with health, rule count, activity), YAML rule editor with compare-and-swap revision save and inline HTMX validation/preview, attempt list and detail pages, structured-log viewer with level/watch/date filtering.
- Reconciliation commands: accept, abandon (with reason), retry-from-start, reopen (all behind the `AttemptReview` application seam).
- Operational health module recognising watch-folder access failures and persistence health conditions.
- Retention primitives for database and log cleanup.

### Partial / Known issues

- **ZIP archive action**: implemented. Known deviations from spec:
  - Archive output naming appends `.zip` to the full source name (`movie.mkv.zip`) rather than stripping the source extension first (`movie.zip`). (Review finding Sp2)
  - `preserve_originals` defaults to `True`; the spec default is `False`. (Review finding Sp3)
  - 7z output creates zip-format content with `.7z` extension; proper 7z archive creation is not implemented. (Review finding Sp4)
- **Copy action**: implemented. Does not update the action-chain cursor after copy. A subsequent action operates on the original source, not the copy. (Review finding Sp5)
- **Quarantine action**: implemented. After a prior rename, the quarantine path uses the renamed relative path rather than the original source path. (Review finding S5/Sp7)
- **Pre-mutation fingerprint validation**: only checks source fingerprints for destructive actions (delete/quarantine). For non-destructive actions (move, copy, rename, archive), fingerprint revalidation is skipped. (Review finding Sp1)
- **Daemon processing**: watcher/scanner bypasses `ItemProcessor.process_batch()`, using direct `plan()`/`execute()` calls. Stability checks, completion skipping, and suppression checks are not fully integrated. (Review finding F1)
- **Production entry point**: container starts the web-only `organizer-web` by default, not the combined daemon. (Review finding F2)
- **Runtime configuration**: configured `log_level` and `retention_days` are not fully wired into logger construction. (Review finding F3)
- **Reconciliation commands**: only `accept`, `abandon`, `retry-from-start`, and `reopen` are implemented; `retry-remaining` and `mark-action-applied` are deferred. (Review finding F4)
- **Rules validation**: still requires the legacy `match` key and silently synthesizes conditions; complete action semantics are not all validated before save. (Review finding F5)
- **Health integration**: trigger loop catches processing errors and discards them; no per-watch pause behavior. (Review finding F6)
- **Retention lifecycle**: database and log cleanup primitives exist but retention is not scheduled as a complete runtime lifecycle. (Review finding F7)

### Not yet implemented

- Watcher/scanner routing through `ItemProcessor.process_batch()` (issue 21).
- One shared production runtime with combined daemon entry point and full configuration wiring (issue 22).
- Integrated operational health and per-watch pause behavior (issue 23).
- Complete reconciliation command contract — `retry-remaining` and `mark-action-applied` (issue 24).
- Hardened rules-document validation (issue 25).
- Scheduled retention lifecycle (issue 26).
- Tooling cleanup — `ruff` unused imports, fingerprint I/O optimisation, action-execution deduplication (issue 27).

## Current-state review

The repository was reviewed on 2026-07-24. The full findings document is at
[`review-findings-2026-07-24.md`](review-findings-2026-07-24.md).
Remediation work is tracked in issues 21–27.

## Definition of ready for further product work

Organizer should not take on additional user-facing features until:

- `pytest`, `mypy`, and `ruff` pass.
- The combined daemon is the production entry point.
- Watcher, scanner, CLI, and web use the same processing and logging services.
- A Docker smoke test demonstrates processing, UI access, persistent attempts, and persistent logs.
- Health failures and recovery evidence behavior are observable and tested.
- The issue tracker and README accurately describe the implementation baseline.

## Requirements

- Python 3.12 or newer.
- [`uv`](https://docs.astral.sh/uv/) for local development.
- Docker, if testing the image.

Dependencies and development tools are declared in `pyproject.toml`.

## Create A Test Watch Folder

The current CLI accepts one item and one rules file explicitly. Create a small
fixture:

```sh
mkdir -p /tmp/organizer-test/downloads
printf 'movie' > /tmp/organizer-test/downloads/movie.mkv
cat > /tmp/organizer-test/downloads/rules.yaml <<'YAML'
rules:
  - name: videos
    match:
      field: file_name
      pattern: '\\.mkv$'
    actions:
      - move:
          destination: ../videos
YAML
```

Relative action destinations are resolved from the watch root. The example
targets `/tmp/organizer-test/videos/movie.mkv`.

## CLI Dry Run

Run the dry-run command:

```sh
uv run organizer check downloads \
  /tmp/organizer-test/downloads \
  /tmp/organizer-test/downloads/movie.mkv \
  /tmp/organizer-test/downloads/rules.yaml \
  --attempts-path /tmp/organizer-test/attempts.db
```

Expected output is similar to:

```text
videos: move /tmp/organizer-test/downloads/movie.mkv -> /tmp/organizer-test/videos/movie.mkv
```

Dry runs do not move the item and do not create or complete a processing
attempt. The `--attempts-path` option is optional and defaults to
`/config/organizer.db`.

## Apply A Move

The Python API is currently the apply seam. This applies the same plan used by
the CLI preview:

```sh
uv run python - <<'PY'
from pathlib import Path

from organizer.item_processor import ItemProcessor, PlanRequest

watch_root = Path('/tmp/organizer-test/downloads')
processor = ItemProcessor(Path('/tmp/organizer-test/attempts.db'))
plan = processor.plan(PlanRequest(
    watch_id='downloads',
    watch_root=watch_root,
    item=watch_root / 'movie.mkv',
    rules_path=watch_root / 'rules.yaml',
))
report = processor.execute(plan)
print(report.status)
print(processor.attempts())
PY
```

A successful apply moves the item and records a `completed` attempt with its
resulting path. If the destination already exists, the move fails without
overwriting it.

## Web Dry-Run Preview

Start the local FastAPI server:

```sh
mkdir -p /config
uv run uvicorn organizer.webapp:app --host 127.0.0.1 --port 8000
```

Open this URL in a browser:

```text
http://127.0.0.1:8000/watches/downloads/dry-run?watch_root=/tmp/organizer-test/downloads&item=/tmp/organizer-test/downloads/movie.mkv&rules_path=/tmp/organizer-test/downloads/rules.yaml
```

The endpoint renders the same plan and dry-run result as the CLI. It binds to
localhost by default because the current UI has no authentication.

## Docker

Build the image:

```sh
docker build -t organizer:local .
```

The image starts Uvicorn on `127.0.0.1:8000` inside the container. A complete
volume-mounted deployment configuration is not part of this slice yet.

## Development Checks

Run the focused or complete test suite and static checks:

```sh
uv run pytest
uv run mypy src tests
uv run ruff check src tests
```

The public test seam is `ItemProcessor.plan()` and `ItemProcessor.execute()`.
