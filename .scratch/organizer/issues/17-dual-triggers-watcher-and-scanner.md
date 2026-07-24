# 17 — Dual triggers: watcher and periodic scanner

**What to build:** Add `watchdog`-based filesystem watcher and an asyncio periodic scanner that both call `process_batch()` for configured watch folders. Create a combined daemon entry point that runs web server + watcher + scanner together. Add CLI `organizer run` command to start the daemon. The watcher reacts to `IN_CLOSE_WRITE` and `IN_CREATE` events with debouncing. The scanner runs on a configurable interval (default 5 min) and covers items the watcher may have missed.

**Blocked by:** 16 (daemon needs config to discover watch folders and their paths)

**Status:** ready-for-agent

- [ ] Add `watchdog` to `pyproject.toml` dependencies
- [ ] Implement watcher service: observe `IN_CLOSE_WRITE` / `IN_CREATE` events per watch root, debounce/coalesce, call `process_batch()` with discovered items
- [ ] Implement scanner service: asyncio interval timer, scan each configured watch root, call `process_batch()`
- [ ] Create daemon entry point that starts web server + watcher + scanner with graceful shutdown
- [ ] Add `organizer run` CLI command to start the daemon
- [ ] Watcher and scanner both feed the same logging and health-checking infrastructure
- [ ] Tests: watcher event handling, scanner interval, daemon lifecycle, shutdown
