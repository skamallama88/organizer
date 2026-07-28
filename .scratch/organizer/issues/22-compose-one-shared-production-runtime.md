# 22 — Compose one shared production runtime

**What to build:** Starting Organizer in production runs the web UI, filesystem watcher, periodic scanner, shared `ItemProcessor`, structured logging, health checks, configured runtime settings, and graceful shutdown as one coherent daemon.

**Blocked by:** 20 — Synchronize implementation status and documentation; 21 — Route all triggers through discovery-batch processing.

**Status:** completed

- [x] `organizer run` and the production container start the combined daemon rather than a web-only process.
- [x] The web app, watcher, scanner, and CLI-triggered services share the configured processor, database, health checker, memory log sink, and persistent log sink.
- [x] Host, port, scan interval, database path, log path, log level, and retention settings come from runtime configuration without hardcoded production overrides.
- [x] Graceful shutdown stops watcher and scanner services and leaves durable processing state consistent.
- [x] A Docker smoke test proves the combined runtime processes an item, serves the UI, records an attempt, and writes a persistent log.
