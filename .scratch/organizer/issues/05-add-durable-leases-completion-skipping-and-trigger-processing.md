# 05 — Add durable leases, completion skipping, and trigger processing

**What to build:** Watcher, periodic scanner, and immediate CLI and web runs share `ItemProcessor`, defer unstable items, acquire one processing lease per source identity, skip unchanged completed items, avoid duplicate work, and expose discovery-batch outcomes.

**Blocked by:** 01 — Bootstrap the safe Organizer vertical slice; 02 — Enforce watch-folder and destination boundaries.

**Status:** ready-for-agent

- [ ] Watcher and scanner produce equivalent plans and reports for the same eligible item, with one active processing lease across all triggers.
- [ ] Restart recovery moves nonterminal leased work to reconciliation rather than automatically repeating it.
- [ ] Immediate runs report snapshot outcomes including executed, skipped, deferred, and outside-snapshot work; unstable items remain deferred with deduplicated diagnostics.
