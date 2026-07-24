# 15 - Remediate safety review findings

**What to build:** Resolve the concrete safety and correctness defects found in the full implementation review before treating Organizer as safe for valuable data.

**Blocked by:** None

**Status:** ready-for-agent

## Findings

1. `ItemProcessor` move execution has a time-of-check/time-of-use race: a destination created after the existence check can be overwritten by `Path.rename`. Use no-replace publication semantics and cover the race with a regression test.
2. Reconciliation acceptance persists an arbitrary resulting path without validating filesystem evidence, destination policy, or expected identity.
3. Retry and reopen clear suppression before a fresh plan has been created successfully, allowing automatic processing to resume after a failed recovery command.
4. Cross-filesystem moves use `Path.rename` instead of staged copy, no-overwrite publication, durable result recording, and source removal.
5. Nested extraction can publish over a directory already present in staged archive output.
6. The rules-save HTTP route accepts an arbitrary `rules_path` and can write outside configured watch rule storage.
7. ZIP archive creation excludes empty directories and symlink items, yet may remove the source after a reported success.
8. Archive naming strips suffixes beyond the specified `.zip`, `.7z`, and `.rar` set.
9. Production logging does not write a rotating persistent `/config/logs/organizer.log`.
10. The rules-save route validates only the YAML document shape rather than complete rule validity before atomic save.

## Order

Address findings in severity order. The first slice is the move no-overwrite race because it can silently destroy an existing destination item.

## References

- `CONTEXT.md`
- `docs/adr/0005-no-overwrite-collisions.md`
- `docs/design/architecture.md`
- `.scratch/organizer/spec.md`
