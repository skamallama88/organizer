# Case-normalizing capture modifiers in action templates

`rename` `name:` and `move`/`copy`/`archive`/`unarchive` `destination:` templates can reference match captures (`\g<artist>`). There was no way to normalize a captured value's case, so artist/folder names mirrored whatever case the downloader produced — risking `Sparrow/` + `sparrow/` duplication on case-insensitive filesystems and scattered mixed-case dirs elsewhere.

## Decision

Add an optional case modifier to **named** capture references in action templates:

- `\g<artist:lower>` — lowercase the expanded capture.
- `\g<artist:upper>` — uppercase the expanded capture.
- Bare `\g<artist>` is unchanged (backwards compatible).

Numbered captures (`\1`) carry no modifier.

Parsing happens in a new `_parse_capture_token` helper in `ItemProcessor`, which splits a capture token into the bare token for `Match.expand()` plus an optional modifier. `_expand_captures` applies the modifier to the expanded value, and both validation paths (`_validate_action_references` at plan time and `_validate_capture_references_pattern` in `validate_rules_document`) reject unknown modifiers (e.g. `\g<artist:shout>`) so a bad rule is a visible diagnostic, not a silent no-op.

## Alternatives considered

- **Option B — `case: lower` on a condition dict**: changes the condition schema and would lower every reference to that condition. Rejected as more invasive; the template modifier is local to each reference and needs no schema change.
- **Only `lower`, not `upper`**: both are the same trivial mechanism and symmetric, so both are supported.

## Consequences

- Rules can normalize capture case in a template with a one-line change: `destination: /data/Artists/\g<artist:lower>/`.
- Existing rules and numbered captures are unaffected.
- Unknown modifiers are surfaced as rule-validation diagnostics.
