# 23 — Integrate operational health and failure handling

**What to build:** Operators can see and trust Organizer's operational state when a watch folder, persistence store, or configured destination becomes unavailable; affected work pauses safely and resumes only when the relevant condition is healthy.

**Blocked by:** 21 — Route all triggers through discovery-batch processing; 22 — Compose one shared production runtime.

**Status:** ready-for-agent

- [ ] Watch-folder access, mount, and permission failures pause only the affected watch folder.
- [ ] Tracking DB or persistent-log failures pause new real execution while explicitly degraded dry runs remain available.
- [ ] Trigger loops do not silently discard processing failures; status and structured logs expose the failure and pause state.
- [ ] Recovery after health restoration is defined and tested without duplicating or replaying uncertain work.
- [ ] CLI status, web health, dashboard status, and logs present consistent operational state.
