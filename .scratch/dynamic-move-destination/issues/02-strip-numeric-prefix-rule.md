# Rule: strip numeric-underscore prefix from filenames

Status: resolved
Type: task
Blocked by:

## Generated YAML

```yaml
rules:
  - name: Strip numeric prefix
    match:
      name: rest
      field: file_name
      pattern: '^\d+_(?P<rest>.+)$'
    actions:
      - rename:
          name: '\g<rest>'
```

## Behaviour

| Before | After |
|---|---|
| `001_report.pdf` | `report.pdf` |
| `42_notes.txt` | `notes.txt` |
| `2024_07_29_log.txt` | `07_29_log.txt` (only the first `\d+_` is stripped) |
| `42_` | **no match** — `.+` requires at least one character after the underscore |
| `no_number.txt` | **no match** — no leading digits |
| `abc_123.txt` | **no match** — digits aren't at the start |

## Notes

- The `name: rest` on the match is optional here (only one condition, so
  `\g<rest>` resolves to the default `match` condition) but included for
  clarity.
- Works on both files and folders — `file_name` captures the item's own name
  regardless of type.
- The `.+` quantifier naturally enforces the "if nothing after the initial
  underscore do not proceed" requirement.
