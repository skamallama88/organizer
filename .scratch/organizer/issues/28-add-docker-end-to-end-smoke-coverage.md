# 28 — Add Docker end-to-end smoke coverage

**What to build:** A repeatable deployment-level verification that the production container starts the shared Organizer daemon, serves the administrative UI, processes a watch-folder item, and persists attempts and structured logs across its lifecycle.

**Blocked by:** None — can start immediately.

**Status:** completed

- [x] A containerized smoke flow proves the production command starts the web server, watcher, periodic scanner, and shared processing runtime.
- [x] The flow proves a configured watch folder processes a matching item and records a durable completed attempt.
- [x] The flow proves the UI is reachable through the configured Docker port and structured logs persist in the configuration volume.
- [x] The flow is repeatable in local development or CI without relying on host-specific paths or services.
