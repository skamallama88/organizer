# Organizer

Organizer is a file organization daemon for macOS and Unraid. The current
vertical slice previews and applies first-match YAML move, copy, rename, and
delete rules through a shared `ItemProcessor` module.

## Current Status

Implemented:

- YAML rule loading and validation diagnostics.
- First-match rule evaluation.
- Immutable plans with resolved move destinations.
- Boundary policy validation for mounted data roots, config-volume exclusion,
  disjoint watches, safe destinations, and case-aware collisions.
- CLI and web dry-run previews.
- Same-filesystem moves and staged copies with no-overwrite collision protection.
- Capture-based renames and ordered action chains using the primary resulting item.
- SQLite execution-attempt records.
- Structured in-memory events for dry runs and execution.
- Docker image packaging.
- Durable processing leases for exclusive per-source-identity ownership.
- Completion skipping for unchanged items across restarts.
- Item stability observations and deferred-item handling.
- Discovery-batch processing with executed, skipped, deferred, and
  outside-snapshot outcomes.
- Restart recovery of nonterminal leased attempts to needs-reconciliation.

Not implemented yet:

- Filesystem watcher and periodic scanner.
- Archive and unarchive actions.
- Multi-watch configuration loading.
- Rule editor, status pages, log viewer, and reconciliation UI.
- Persistent structured log files.

The complete product specification is in [`spec.md`](spec.md). The first
vertical-slice ticket is in [`issues/01-bootstrap-safe-organizer-slice.md`](issues/01-bootstrap-safe-organizer-slice.md).

Boundary policy is currently supplied to the `ItemProcessor` planning seam as
an immutable `BoundaryPolicy`. Multi-watch configuration loading and UI editing
remain future work; this ticket validates the policy whenever a plan is made.

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
