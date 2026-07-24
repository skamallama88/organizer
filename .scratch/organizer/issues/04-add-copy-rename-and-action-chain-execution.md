# 04 — Add copy, rename, and action-chain execution

**What to build:** Users can rename with named captures and safely copy items through staged publication. Ordered multi-action rules use a primary resulting item, reject invalid type chains, preserve copy provenance, and record result identities.

**Blocked by:** 01 — Bootstrap the safe Organizer vertical slice; 02 — Enforce watch-folder and destination boundaries; 03 — Add rule revisions and named match captures.

**Status:** done

- [x] Rename applies a complete valid name with explicit captures and records its resulting identity.
- [x] Copy uses private destination staging and no-overwrite publication, refusing to publish when source consistency cannot be established.
- [x] Multi-action execution follows primary resulting-item semantics, stops after failure, and rejects chains whose next action cannot accept the prior result type.
