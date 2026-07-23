# 03 — Add rule revisions and named match captures

**What to build:** Rules support named conditions and explicit capture references, `full_path` matches container-absolute paths, invalid earlier rules produce visible precedence warnings, and UI saves use revision conflicts rather than silently overwriting concurrent or external edits.

**Blocked by:** 01 — Bootstrap the safe Organizer vertical slice; 02 — Enforce watch-folder and destination boundaries.

**Status:** ready-for-agent

- [x] Named match conditions support explicit numbered and named capture references, while invalid references fail validation.
- [x] Plans expose disabled-earlier-rule warnings and resolve `full_path` as a normalized container-visible absolute path within mounted data volumes.
- [x] Ruleset revisions make stale plans non-executable, UI saves detect conflicts, and only valid external edits become active revisions.
