# 14 — Package the production deployment

**What to build:** A Docker image and deployment configuration for Unraid with separate data and config volumes, required RAR tooling, localhost-only UI binding by default, and clear non-loopback exposure warnings.

**Blocked by:** 01 — Bootstrap the safe Organizer vertical slice; 10 — Add 7z and RAR archive adapters; 11 — Add nested extraction and cross-watch handoffs; 12 — Add reconciliation and attempt review; 13 — Add operational health, retention, and log viewing.

**Status:** ready-for-agent

- [x] The image runs Organizer with persistent configuration and logs separated from mounted data volumes.
- [x] RAR extraction dependencies are available in the image and their absence is reported clearly when unavailable.
- [x] The unauthenticated UI binds to localhost by default and logs a prominent warning when explicitly configured for non-loopback exposure.
