# 03 — Web API: PATCH discovery scope and persistence

**What to build:** Allow editing an existing watch's `discovery` scope through the web API and persist it to `organizer.yaml`, following the existing per-watch controls (`enabled`, `scan_interval`).

**Blocked by:** 01

**Status:** completed

- [ ] In `_build_watches` (web.py), expose `discovery` (the enum value string) for each watch
- [ ] In the `PATCH /watches/{watch_id}` handler, accept an optional `discovery` field with a strict allowlist (`recursive` / `top_level`); reject any other value with `_watch_patch_error`
- [ ] Add `discovery` to the `updates` dict and apply via `watch.model_copy(update=updates)`
- [ ] In `_apply_watch_updates`, persist `discovery` to the watch dict written to `organizer.yaml`
- [ ] Include `discovery` in the non-htmx PATCH response body

**Tests (tests/test_web_watches_api.py):**
- [ ] `PATCH` with `discovery=top_level` updates the runtime watch and persists `discovery: top_level` to the on-disk config
- [ ] `PATCH` with `discovery=recursive` works and round-trips
- [ ] `PATCH` with an unknown `discovery` value returns 422 and does not change the watch
- [ ] `PATCH` with no `discovery` field leaves the existing scope untouched
- [ ] `_build_watches` output includes the current `discovery` value
