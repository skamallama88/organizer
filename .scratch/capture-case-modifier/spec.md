# Case-normalizing capture modifiers

## Problem Statement

`rename` `name:` and `move`/`copy`/`archive`/`unarchive` `destination:` templates can reference match captures (`\g<artist>`). Artist/folder names are captured verbatim and carry arbitrary case, producing `Sparrow/` + `sparrow/` duplication on case-insensitive filesystems or scattered mixed-case dirs on case-sensitive ones. There is no way to normalize a captured value's case in a template.

## Solution

Support an optional case modifier on **named** capture references: `\g<artist:lower>` and `\g<artist:upper>`. The bare `\g<artist>` form is unchanged. Numbered captures (`\1`) carry no modifier.

Implementation: a `_parse_capture_token` helper splits a capture token into the bare token for `Match.expand()` plus an optional modifier; `_expand_captures` applies `str.lower()`/`str.upper()`. Both validation paths reject unknown modifiers so a bad rule is a visible diagnostic.

## User Stories

1. As an administrator, I want to lowercase a captured artist name in a `move` destination, so artist folders are created in a consistent case and never duplicate.
2. As an administrator, I want existing rules (bare captures, numbered captures) unchanged, so the feature is backwards compatible.
3. As an administrator, I want a typo like `\g<artist:shout>` reported as a rule-validation error, so I know the rule is wrong rather than silently ignored.

## Implementation Decisions

- Template modifier (Option A) rather than a condition `case:` key (Option B), because it is local to each reference and needs no schema change.
- `_CASE_MODIFIERS = {"lower": str.lower, "upper": str.upper}`.
- `_parse_capture_token` parses `g<name:modifier>` from a capture token; numbered captures and bare named captures return no modifier.
- Validation in `_validate_action_references` (plan time) and `_validate_capture_references_pattern` (`validate_rules_document`) validates the modifier name and expands the bare token.

## Testing Decisions

- Rename with `\g<title:lower>` lowercases the captured value.
- Move destination with `\g<artist:lower>` creates the lowercased folder.
- Unknown modifier (`\g<title:shout>`) is a rule-validation diagnostic.
- Existing bare/numbered capture behaviour is preserved (existing tests unchanged).

## Out of Scope

- Per-condition default case (Option B) — template modifier only.
- Case folding beyond `lower`/`upper` (e.g. title case, locale-aware transforms).
