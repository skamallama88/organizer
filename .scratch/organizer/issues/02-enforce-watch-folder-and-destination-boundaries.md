# 02 — Enforce watch-folder and destination boundaries

**What to build:** Administrators can configure disjoint watch folders and explicit allowed destination roots across container-mounted data volumes. Config-volume targets, self-targeting destinations, overlapping watches, unsafe symlink traversal, and case-only collisions are rejected; cross-watch destinations emit a non-blocking warning.

**Blocked by:** 01 — Bootstrap the safe Organizer vertical slice.

**Status:** done

- [x] Configuration accepts only disjoint watch roots and allowed destination roots within mounted data volumes, never within the config volume.
- [x] Plans reject self-targeting and descendant-targeting folder actions, unsafe symlink traversal, and collisions including case-only collisions on applicable filesystems.
- [x] A destination that is another watch folder is allowed but yields a visible configuration warning.
