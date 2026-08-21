# 01 — Case-normalizing capture modifiers

**What to build:** Support `\g<artist:lower>` / `\g<artist:upper>` in action templates.

**Blocked by:** None

**Status:** completed

- [x] Add `_CASE_MODIFIERS` and `_parse_capture_token` helper in `item_processor.py`
- [x] Apply the modifier in `_expand_captures`
- [x] Validate modifiers + expand bare token in `_validate_action_references`
- [x] Validate modifiers in `_validate_capture_references_pattern` (rules-document path)
- [x] Tests: rename `:lower`, move `:lower` destination, unknown modifier rejected
- [x] README docs + ADR 0011
