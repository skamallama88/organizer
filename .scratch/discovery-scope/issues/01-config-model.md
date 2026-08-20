# 01 — Config model and parsing for discovery scope

**What to build:** Add a per-watch `discovery` scope to the configuration model and parsing. Define a `DiscoveryScope` `StrEnum` (`recursive`, `top_level`), add `discovery: DiscoveryScope = DiscoveryScope.RECURSIVE` to `WatchFolderConfig`, and parse the `discovery` key in `load_config` per watch.

**Blocked by:** None — can start immediately.

**Status:** completed

- [ ] Define `class DiscoveryScope(StrEnum)` in `config.py` with values `recursive` and `top_level`
- [ ] Add `discovery: DiscoveryScope = DiscoveryScope.RECURSIVE` field to `WatchFolderConfig` (keep it frozen)
- [ ] In `load_config`, read the optional per-watch `discovery` key; accept only `recursive` or `top_level`, raising `ConfigError` for any other value; absent defaults to `recursive`
- [ ] Ensure the watch's `boundary_policy` still shares the canonical rebuilt policy (no per-watch policy change)

**Tests (tests/test_config.py):**
- [ ] `discovery` defaults to `recursive` when the key is absent
- [ ] `discovery: top_level` parses correctly; `discovery: recursive` parses correctly
- [ ] an invalid `discovery` value raises `ConfigError`
- [ ] existing config tests continue to pass unchanged

**Docs:** update `README.md` rules section with a short `discovery` example after implementation (or via ticket 05).
