# 01 — Bootstrap the safe Organizer vertical slice

**What to build:** A Docker-runnable Organizer with one configured watch folder, validated YAML, immutable `ItemProcessor` plans, dry-run reporting, a safe same-filesystem move, durable execution attempts, structured stdout and persistent logs, and CLI and web UI preview of the same plan.

**Blocked by:** None — can start immediately.

**Status:** ready-for-human

- [x] A configured watch folder can load valid rules, report invalid rules without disabling valid rules, and produce an immutable first-match plan.
- [x] CLI and web dry runs render the same intended move and structured dry-run event without filesystem mutation or Tracking DB completion.
- [x] Applying a valid same-filesystem move records a completed execution attempt and resulting path only after the move succeeds.
