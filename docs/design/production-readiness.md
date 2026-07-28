# Production Readiness

This document is the current readiness contract for Organizer. It describes
what is verified in the repository and what still requires deployment-specific
validation. The architecture and module seam remain documented in
[`architecture.md`](architecture.md); this document does not create a second
runtime contract.

## Verified criteria

- `uv run pytest` passes, including Docker smoke coverage (256 tests verified on 2026-07-28).
- `uv run mypy src tests` passes (verified on 2026-07-28).
- `uv run ruff check src tests` passes (verified on 2026-07-28).
- `organizer run` and the production Compose image start one shared runtime containing the web UI, watcher, scanner, processor, database, health checker, logger, and retention lifecycle.
- Watcher, scanner, CLI, and web-triggered processing submit work through `ItemProcessor.process_batch()` and expose consistent per-item outcomes.
- Real execution pauses on relevant watch-folder or persistence health failures; dry runs remain explicitly available where supported; failures are observable through status and structured logs.
- Processing attempts, leases, suppressions, action results, quarantine paths, and uncertain staging artifacts remain available as recovery evidence.
- The Docker smoke test proves UI reachability, matching-item processing, durable completed-attempt recording, and persistent structured logs.
- Documentation and issue statuses identify verified behavior and remaining deployment-specific work without claiming unverified behavior.

## Remaining validation

The automated suite cannot establish every property of a real deployment. Before
calling a deployment production-ready, an administrator must validate:

- `/config` and `/data` mount permissions, capacity, backup, and restore behavior.
- Destination filesystem behavior, including case sensitivity and cross-filesystem moves, using representative data.
- The trusted control boundary for any non-loopback deployment of the unauthenticated administrative UI.
- Archive tooling and operational limits for the formats used by that deployment.
- Graceful shutdown and restart behavior under the deployment's process supervisor.

These are deployment acceptance activities, not claims that the repository
automatically verifies every environment.

## Roadmap

The local issue tracker under `.scratch/organizer/issues/` is the roadmap. Issues
1-28 describe completed or historical work; issue 29 synchronizes this current
state. New product work should be based on a new issue that names its verified
acceptance criteria and dependencies.
