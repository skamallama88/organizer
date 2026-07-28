# Organizer Current-State

Organizer is a file organization daemon for macOS and Unraid. The verified
implementation applies first-match YAML rules through a shared `ItemProcessor`
module that separates planning from execution.

## Verified baseline

The current implementation and tests verify:

- Rule loading, named conditions, capture references, complete document validation, and disabled-rule diagnostics.
- Immutable plans with source fingerprints, ruleset revisions, boundary policies, destination safety, and collision checks.
- Move, staged copy, rename, direct deletion, quarantine, ZIP/7z/RAR unarchive, archive creation, nested extraction, and cross-watch lineage behavior.
- Durable attempts, action results, processing leases, suppression, retry/reprocess, reconciliation, and recovery evidence.
- Stability observations, completion skipping, and discovery-batch outcomes (`executed`, `skipped`, `deferred`, and `outside_snapshot`).
- Watcher, scanner, CLI, and web-triggered processing through the shared batch seam.
- One configured production runtime with shared processor, database, health, logging, retention, web, watcher, and scanner services.
- Config-driven watches, operational health and pause behavior, structured logs, retention lifecycle, the web dashboard, rule editor, log viewer, and reconciliation UI.
- Docker packaging and end-to-end smoke coverage for daemon startup, UI access, processing, durable attempts, and persistent logs.

## Known limitations

- The administrative UI is unauthenticated. Non-loopback exposure requires a trusted reverse proxy or private network boundary, as documented by ADR-0006.
- `ruff`, `mypy`, and the test suite are green, but production readiness still depends on deployment-specific filesystem, mount, backup, and trusted-network validation.
- Rich archive-content previews, bulk recovery actions, live UI updates, and other explicitly out-of-scope features are not part of the initial product.

## Production-readiness gate

The validated gate for additional user-facing work is documented in
[`docs/design/production-readiness.md`](../../docs/design/production-readiness.md).
It requires green automated checks, the combined runtime as the production
entry point, shared trigger services, Docker smoke coverage, observable health
and recovery evidence, and synchronized issue/documentation state.

## Historical review and roadmap

The 2026-07-24 review is retained in
[`review-findings-2026-07-24.md`](review-findings-2026-07-24.md) as historical
context. Its remediation tickets are issues 21-28; their current status is
recorded in the issue files. Its verification counts and failures describe
that date, not the current suite. Issue 29 tracks this documentation
synchronization.

Issues 30-32 are intentionally still `ready-for-agent` and are not required
by the production-readiness gate: archive output naming, metadata-preservation
verification, and further ItemProcessor maintainability work.

## Requirements

- Python 3.12 or newer.
- [`uv`](https://docs.astral.sh/uv/) for local development.
- Docker, if testing the image.

Dependencies and development tools are declared in `pyproject.toml`.

## Historical developer examples

The following explicit-path examples are retained for low-level API debugging;
normal production use should follow the config-driven root README and CLI.

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

The Compose deployment starts the combined daemon in the container, publishes
the UI on host loopback, and mounts `/config` separately from `/data`. The
Docker smoke tests cover this deployment path.

## Development Checks

Run the focused or complete test suite and static checks:

```sh
uv run pytest
uv run mypy src tests
uv run ruff check src tests
```

The public test seam is `ItemProcessor.plan()` and `ItemProcessor.execute()`.
