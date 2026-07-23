# 11 — Add nested extraction and cross-watch handoffs

**What to build:** Nested archive extraction respects configured depth within the parent processing boundary. Published results carry provenance and may enter one previously unvisited destination watch folder, while same-watch repeats and pipeline cycles are prevented.

**Blocked by:** 05 — Add durable leases, completion skipping, and trigger processing; 09 — Add safe ZIP unarchive; 10 — Add 7z and RAR archive adapters.

**Status:** ready-for-agent

- [ ] Nested archives are processed only within the active parent extraction boundary up to configured depth, never concurrently through ordinary discovery.
- [ ] A resulting-path handoff carries processing lineage to a different unvisited watch folder.
- [ ] Same-watch reevaluation and watch-folder cycles are rejected while intentional forward pipelines remain possible.
