# Move destination does not expand capture references

Status: open
Type: bug
Blocked by:

## Summary

The `move` and `copy` actions treat their `destination` field as a literal
path string. Regex capture references like `\1` or `\g<name>` pass through
unexpanded and produce literal path segments (e.g. a folder literally named
`\1`), which is never the intended behaviour.

## Root cause

Four independent code paths in `src/organizer/item_processor.py` all gate on
`set(action) == {"rename"}` — none of them consider `move` or `copy`.

### 1. `plan()` — destination never expanded

File: `src/organizer/item_processor.py:596-604`

```python
destination = action[kind].get("destination")       # raw string
root = Path(destination)                              # no _expand_captures call
destination_root = self._resolve_destination(root)
current = destination_root / current.name             # original filename always preserved
```

Contrast with `rename` at line 512:

```python
target_name = self._expand_captures(name, matches)   # captures ARE expanded
```

**Minimal fix at line 599:** replace `Path(destination)` with
`Path(self._expand_captures(destination, matches))`.

### 2. `_validate_action_references` — only checks rename

File: `src/organizer/item_processor.py:1395-1406`

```python
@staticmethod
def _validate_action_references(actions, matches):
    for action in actions:
        if set(action) == {"rename"} and isinstance(action["rename"], dict):
            # validates \1, \g<name> exist in the matches dict
```

Move/copy destinations with capture references pass validation silently. A
`\g<nonexistent>` in a move destination causes a `KeyError` at plan time
instead of a clear validation error.

**Fix:** extend the check to move/copy `destination` fields.

### 3. `_validate_capture_references_pattern` — only checks rename

File: `src/organizer/item_processor.py:1310-1331`

```python
@staticmethod
def _validate_capture_references_pattern(actions, conditions):
    for action in actions:
        if set(action) == {"rename"} and isinstance(action["rename"], dict):
            # validates that group names/numbers exist in the compiled pattern
```

Same pattern — move/copy destinations containing `\g<category>` are never
checked against the condition regex at rule-load time.

**Fix:** extend the check to move/copy `destination` fields.

### 4. `_validate_action_params` — only requires a non-empty string

File: `src/organizer/item_processor.py:1302-1305`

```python
elif kind in ("move", "copy"):
    destination = params.get("destination")
    if not isinstance(destination, str) or not destination:
        raise ValueError("action destination is required")
```

No structural check for embedded capture references. A destination of
`../\g<category>/subdir` would be accepted as-is even though `\g<category>`
is a placeholder, not a literal folder name.

---

## Semantic gap: move always preserves the original filename

Even if captures were expanded in the destination path (fix #1), line 604
always appends the original item name:

```python
current = destination_root / current.name
```

For the motivating example (`report [finance].zip` → `../finance/report [finance].zip`),
this is actually *correct behaviour* — the expanded destination provides the
directory, and `current.name` provides the filename. No additional rename
is needed.

However, if a rule author writes:

```yaml
- move:
    destination: '../\g<category>/\g<category>-report.zip'
```

the expanded directory would include `finance/finance-report.zip` but then
`current.name` would overwrite the filename back to the original. This is an
edge case and can be handled via chaining a `rename` after the `move`.

---

## Workaround

Currently impossible. Rule 1 in the original request (route tagged items into
per-category folders) cannot be expressed until at least fix #1 is implemented.
