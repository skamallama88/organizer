# 25 — Harden complete rules-document validation

**What to build:** Rule authors receive complete, accurate validation before a rules document is saved or activated, using the canonical named-condition schema and clear diagnostics for invalid action semantics.

**Blocked by:** 20 — Synchronize implementation status and documentation.

**Status:** completed

- [x] Define and document the supported relationship between canonical named conditions and the legacy `match` shape.
- [x] Validate rule names, conditions, regexes, capture references, action parameters, action ordering, result types, destinations, and destructive-action opt-ins before save.
- [x] Keep valid rules usable when other rules are invalid while preserving disabled-earlier-rule diagnostics.
- [x] Ensure UI and CLI validation use the same complete validation behavior as planning.
- [x] Add tests covering invalid YAML, invalid fields, invalid actions, missing parameters, unsafe destinations, invalid chains, and valid mixed rule documents.
