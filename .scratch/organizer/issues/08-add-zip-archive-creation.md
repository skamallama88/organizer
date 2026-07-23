# 08 — Add ZIP archive creation

**What to build:** Users can create ZIP archives for files and folders using archive output naming, staged publication, source-consistency validation, optional original preservation, and durable resulting-path records.

**Blocked by:** 02 — Enforce watch-folder and destination boundaries; 04 — Add copy, rename, and action-chain execution; 05 — Add durable leases, completion skipping, and trigger processing.

**Status:** ready-for-agent

- [ ] ZIP archive actions create correctly named archive output for files and folders and reject destination collisions without overwriting.
- [ ] Archive creation uses private staging and refuses publication or source removal if the source identity changes during creation.
- [ ] Original preservation is honored, and uncertain publication or source removal retains evidence for reconciliation.
