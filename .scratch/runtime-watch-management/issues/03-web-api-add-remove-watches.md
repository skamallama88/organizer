# 03 — Web API: POST and DELETE /watches

**What to build:** Two new REST endpoints on the FastAPI app. `POST /watches` accepts `{id, root, rules_path}`, validates using the extracted helpers, persists the updated watches list to `organizer.yaml` via atomic temp-file swap, and calls the `WatchMutator` to hot-load the new watch into the daemon. `DELETE /watches/{watch_id}` removes the watch from YAML and calls `WatchMutator.remove_watch()`. Both endpoints return the affected watch config on success, or 422/404 on failure. Works standalone via curl. The endpoints are wired into `create_app()` from both `cli.py` (daemon context) and `webapp.py` (standalone web, no-hot-reload but YAML persists).

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] `POST /watches` endpoint — parse `{id, root, rules_path}`, call `validate_watch_root` and `validate_watch_id`, persist to YAML, call `WatchMutator.add_watch()`, return watch config
- [ ] `DELETE /watches/{watch_id}` endpoint — remove from YAML, call `WatchMutator.remove_watch()`
- [ ] YAML persistence helper `_save_watches_to_disk(watches: list[dict])` — reads current `organizer.yaml`, replaces the `watches` key, writes via atomic tempfile swap (same pattern as rules save in `web.py:216-233`)
- [ ] Pass `WatchMutator` into `create_app()` from `cli.py` and `webapp.py`
- [ ] Error responses: 422 for validation errors (duplicate ID, root outside data_roots, overlapping roots, root inside config volume), 404 for unknown watch_id on DELETE
- [ ] Tests via FastAPI test client: verify POST returns new config, DELETE removes it, validation errors return 422
