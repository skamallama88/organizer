# Organizer implementation specification

## Problem Statement

File-server organization on Unraid is currently manual and repetitive. Items arrive in watch folders with names, extensions, or folder structures that indicate where they belong, but sorting them requires repeated human intervention. Archive extraction and creation add further manual work, and file operations need to be observable and safe because collisions, partial failures, and destructive actions can affect valuable data.

## Solution

Organizer is a Docker-hosted daemon for Unraid with a web UI and CLI. It monitors multiple watch folders using filesystem events and periodic scans, evaluates ordered YAML rules against files and folders, and performs actions such as move, copy, delete, rename, archive, and unarchive.

Organizer first produces an immutable execution plan. The plan can be previewed as a dry run or executed against the filesystem. Execution is action-by-action, never overwrites existing items, records durable processing attempts, emits structured logs, and exposes failed or uncertain work for explicit review and recovery.

## User Stories

1. As an Unraid server owner, I want Organizer to run in a Docker container, so that it fits my existing server deployment model.
2. As an Unraid server owner, I want Organizer to use separate configuration and data volumes, so that configuration and processing state survive container replacement without mixing with organized content.
3. As an administrator, I want to manage Organizer through a web UI, so that I can configure and monitor it from another computer.
4. As an administrator, I want a CLI, so that I can script checks, immediate runs, status inspection, and recovery operations.
5. As an administrator, I want multiple independent watch folders, so that different shares can have different organization rules.
6. As an administrator, I want each watch folder to have its own ordered rules, so that organization behavior is local and understandable.
7. As an administrator, I want rules stored in YAML, so that they are portable, reviewable, and version-controllable.
8. As an administrator, I want the web UI to edit or manage YAML-backed rules, so that I do not need to edit files manually for routine changes.
9. As a rule author, I want to match a file name, folder name, or full path, so that rules can target the relevant part of an item.
10. As a rule author, I want match conditions to use regular expressions, so that I can express precise naming patterns.
11. As a rule author, I want rules evaluated in declaration order with first-match-wins semantics, so that rule precedence is predictable.
12. As a rule author, I want a rule to perform multiple actions in order, so that workflows such as rename then move are possible.
13. As a user, I want Organizer to process both files and folders, so that folder-based media and individual files can be organized consistently.
14. As a user, I want to move a matched item to a destination, so that items are sorted into their intended folders.
15. As a user, I want to copy a matched item without removing the original, so that I can retain the source while creating an organized copy.
16. As a user, I want to delete a matched item, so that disposable files can be removed automatically.
17. As a user, I want to rename a matched item, so that unwanted tags or naming fragments can be removed.
18. As a rule author, I want rename names to support regex capture references, so that meaningful portions of an original name can be preserved.
19. As a user, I want to archive matched files or folders into ZIP or 7z archives, so that completed or infrequently used content can be compressed.
20. As a user, I want archive actions to optionally preserve the originals, so that I can choose between compression and cleanup.
21. As a user, I want to unarchive ZIP, 7z, and RAR files, so that incoming archives are automatically expanded.
22. As a user, I want unarchive actions to default to the archive's directory, so that extracted content can participate in subsequent organization.
23. As a user, I want to override the unarchive destination, so that extracted content can go directly to a chosen location.
24. As a user, I want successful unarchive operations to remove the original archive by default, so that watch folders do not retain redundant archives.
25. As a user, I want to preserve the original archive when configured, so that I can retain it for backup or audit purposes.
26. As a user, I want nested archives handled up to a configurable depth, so that common archive-within-archive cases are supported without unbounded recursion.
27. As a user, I want a dry run from the CLI, so that I can see what a watch folder would do before changing files.
28. As a user, I want a dry run from the web UI, so that I can review intended actions without using the CLI.
29. As a user, I want dry runs to perform no filesystem mutations, so that previews are safe.
30. As a user, I want dry runs to avoid updating processing state, so that previews do not make real work appear complete.
31. As a user, I want dry-run output to show the matched rule, action, source item, and intended target, so that I can understand the result before applying it.
32. As a user, I want filesystem events to trigger processing promptly, so that items are organized soon after arrival.
33. As a user, I want periodic scans as a safety net, so that missed filesystem events do not leave items permanently unprocessed.
34. As an administrator, I want the watcher and scanner to use the same processing module, so that behavior does not vary by trigger.
35. As a user, I want unchanged completed items skipped across restarts, so that periodic scans do not repeat successful work.
36. As a user, I want every processing attempt recorded durably, so that I can understand what Organizer tried and what resulted.
37. As a user, I want action outcomes recorded as execution proceeds, so that partial progress is visible after a failure.
38. As a user, I want successful attempts to record resulting paths, so that moves, renames, archives, and unarchives are traceable.
39. As a user, I want failures to stop subsequent actions in the same rule, so that later actions do not operate on an unexpected state.
40. As a user, I want failed items to remain visible for review, so that I can resolve problems rather than lose track of them.
41. As a user, I want destination collisions to fail without overwriting existing items, so that existing data is protected.
42. As a user, I want collision failures suppressed from automatic retries, so that the same warning is not generated indefinitely.
43. As a user, I want to explicitly retry a collision after resolving it, so that recovery is deliberate and safe.
44. As a user, I want explicit retries to create new processing attempts, so that the original failure history remains intact.
45. As a user, I want uncertain filesystem outcomes to enter reconciliation, so that Organizer does not blindly repeat possibly completed destructive work.
46. As an administrator, I want reconciliation cases to show evidence and resulting paths, so that I can make an informed recovery decision.
47. As an administrator, I want to accept resulting paths during reconciliation, so that successfully completed filesystem work can be recorded without repeating it.
48. As an administrator, I want to mark an action applied during reconciliation, so that known partial progress can be acknowledged.
49. As an administrator, I want to retry remaining actions during reconciliation, so that recovery can continue without repeating known-successful actions.
50. As an administrator, I want to abandon an unrecoverable attempt, so that it no longer blocks review while remaining in history.
51. As a user, I want logs written to stdout, so that Docker logs show Organizer activity.
52. As a user, I want logs written to a rotating persistent file, so that historical activity remains available after container restarts.
53. As a user, I want structured log fields for watch folder, rule, action, item, result, and detail, so that activity can be traced and filtered.
54. As a user, I want a web log viewer, so that I can inspect recent activity without accessing the container.
55. As an administrator, I want invalid YAML and invalid rules reported as errors while valid rules remain usable, so that one configuration mistake does not disable every watch rule.
56. As an administrator, I want invalid regular expressions and missing action parameters reported clearly, so that configuration errors can be corrected.
57. As a user, I want corrupted, password-protected, and unsupported archives to remain in place when unarchive fails, so that failed inputs are not silently destroyed.
58. As a user, I want password-protected archives recorded as failed items for review, so that I know which archives need a password or manual handling.
59. As a user, I want a password-protected archive to be skipped without stopping processing of other items, so that one blocked archive does not halt the watch folder.
60. As an administrator, I want the CLI to support an immediate run, so that I can trigger processing without waiting for the next scan.
61. As an administrator, I want the CLI to show watch status, so that I can inspect last activity and processing outcomes.
62. As an administrator, I want the CLI and web UI to consume the same plans and execution reports, so that interfaces present consistent behavior.

## Implementation Decisions

- Organizer is a Python application using FastAPI for the backend, HTMX and Alpine.js for the web UI, Typer for the CLI, and watchdog for filesystem events.
- Organizer is packaged as a Docker image for deployment on Unraid.
- The system has one shared `ItemProcessor` module. Watcher, periodic scanner, CLI, and web UI callers use its interface rather than implementing rule or action behavior themselves.
- The primary module interface separates planning from execution. Planning accepts a watch context, item snapshot, and validated rules, and returns an immutable `Plan`. Execution accepts a `Plan` and an execution mode and returns an `ExecutionReport`.
- A `Plan` is the primary preview artifact. It contains the matched rule, ordered intended actions, resolved destinations, and source fingerprint. Applying a stale plan must fail validation rather than silently apply.
- Planning performs no filesystem mutation or Tracking DB update.
- Execution performs internal preflight validation, revalidates source and destination state immediately before each mutation, executes actions sequentially, records action outcomes, and stops after the first failure.
- Rules are loaded from YAML and evaluated in declaration order. The first matching rule wins; later rules are not considered for that item.
- Match conditions use regular expressions against `folder_name`, `file_name`, or `full_path`.
- Supported actions are move, copy, delete, rename, archive, and unarchive.
- Rename keeps the item in its current parent directory. Its complete replacement name may contain regular-expression capture references from the match condition.
- Archive operates on files or folders and supports ZIP and 7z output. The destination is an output directory, and originals are removed only after successful archiving unless `preserve_originals` is enabled.
- Unarchive supports ZIP, 7z, and RAR input. Extraction defaults to the archive directory and may specify a destination. Originals are removed only after successful extraction unless `preserve_archive` is enabled. Nested extraction has a configurable depth with an initial default of one level.
- Password-protected archives are classified as `password_protected_archive` failures. They remain in place, create failed processing attempts with review details, suppress automatic retries, and do not stop processing other discovered items.
- Invalid YAML, regexes, fields, actions, or required parameters produce diagnostics and do not prevent valid rules from being used.
- Organizer never overwrites an existing item. A collision fails the attempt, stops later actions, suppresses automatic watcher and scan retries, and is surfaced for explicit review and retry.
- A durable processing attempt is recorded before filesystem mutation. It stores the original source path and fingerprint, rule, planned actions, status, action results, resulting paths, and failure detail.
- Attempt statuses are `started`, `completed`, `failed`, and `needs-reconciliation`.
- An attempt reaches `completed` only after every planned action succeeds and resulting paths are recorded. Failed attempts are not treated as completed during later scans.
- Explicit retry creates a new processing attempt and preserves the original attempt history.
- Filesystem mutation and Tracking DB writes are not one transaction. If the filesystem result or completion recording is uncertain, the attempt enters `needs-reconciliation` and is not automatically repeated.
- Reconciliation supports accepting resulting paths, marking an action applied, retrying remaining actions, retrying from the start with a new attempt, or abandoning the attempt. Commands and outcomes must be auditable.
- Dry-run execution reports intended actions using the same immutable plan but performs no filesystem mutation and no Tracking DB completion.
- The watcher and periodic scanner converge on `ItemProcessor`; both use the same unchanged-item and attempt-state policies.
- Structured logs are emitted to stdout and a rotating persistent log. The initial retention defaults are seven days and 10 MB per log file; the in-memory web viewer limit initially defaults to 1,000 entries. These values are configurable.
- The Tracking DB is SQLite in the persistent configuration volume. Configuration, rules, processing state, and logs are separate from the data volume.
- Internal seams may be injected for YAML rule source, filesystem operations, archive formats, Tracking DB persistence, and structured event sinks. They are implementation seams, not application-facing contracts.
- The primary testing seam is the `ItemProcessor` interface. Tests should use local adapters or in-memory substitutes behind its internal seams rather than testing callers through filesystem or Tracking DB details.

### Acceptance Criteria

- A watch folder can load valid YAML rules and report invalid rules without disabling valid rules.
- A matching item produces an immutable plan with the first matching rule and ordered actions.
- A dry run produces action reports and logs without changing files or processing state.
- A real execution applies actions in order and records a `completed` attempt only after all actions succeed.
- A failed action stops later actions for its item and records a `failed` attempt with the failed action and failure detail; processing continues with other discovered items.
- A destination collision leaves the existing item unchanged, records a `failed` attempt, and is not retried automatically.
- A password-protected archive remains in place, records a `password_protected_archive` failure for review, and does not terminate the watch processing loop.
- An explicit retry creates a new attempt linked to the prior attempt and preserves the prior attempt history.
- A successful move, rename, archive, or unarchive records all resulting paths on its attempt.
- An uncertain filesystem or Tracking DB outcome produces a `needs-reconciliation` attempt rather than automatic repeated mutation.
- ZIP, 7z, and RAR unarchive behavior extracts supported unprotected archives and leaves corrupted, password-protected, or unsupported archives in place with a failure detail.
- ZIP and 7z archive behavior handles files and folders and removes originals only after a successful archive unless original preservation is enabled.
- Rename supports literal complete names and regex capture references; invalid capture references or host-invalid names fail the action.
- Watcher and periodic scan processing produce equivalent `ItemProcessor` plans and execution reports for the same unchanged item and rules.
- CLI dry run, immediate run, and status operations, and their web UI counterparts, render the same plans and execution reports from `ItemProcessor`.
- Structured logs contain the watch folder, rule, action, item, result, and failure detail for each executed or dry-run action.

## Testing Decisions

- Tests assert externally observable behavior through the `ItemProcessor` interface. They should not assert private planner classes, internal adapter interactions, SQL implementation details, or framework internals.
- The primary test seam is the `ItemProcessor` interface, through its planning and execution operations. This is the highest existing seam and should remain the main contract for watcher, scanner, CLI, web UI, and recovery behavior.
- Tests use local-substitutable filesystem and Tracking DB adapters so scenarios can run without modifying real server data.
- Rule tests cover declaration order, first-match-wins, file and folder items, regex matching, capture-based rename, invalid rules, and missing action parameters.
- Plan tests cover resolved destinations, source fingerprints, ordered actions, immutable preview data, and stale-plan rejection.
- Execution tests cover move, copy, delete, rename, archive, and unarchive outcomes for files and folders.
- Safety tests prove that dry runs do not mutate the filesystem or Tracking DB, collisions never overwrite, archive originals are removed only after success, and failed extraction leaves the source archive in place.
- Recovery tests cover action failure, skipped later actions, explicit retry creating a new attempt, collision review, reconciliation, accepted resulting paths, retrying remaining actions, and abandon behavior.
- Archive failure tests cover password-protected archives, failed-attempt recording, review visibility, suppressed automatic retry, and continuation with other items.
- Tracking tests cover completed-attempt skipping, failed-attempt eligibility, resulting paths after mutation, and completion-recording failure.
- Trigger integration tests verify that filesystem events and periodic scans converge on equivalent `ItemProcessor` results. The two triggers should not duplicate processing of an unchanged completed item.
- Logging tests assert structured event content and dry-run distinction through the event-sink seam, not through exact logger implementation details.
- CLI and web tests should verify that they render plans, reports, failures, and reconciliation states from the shared module results. They should not duplicate action semantics.
- No prior application test suite exists. New tests establish the initial behavior contract at the `ItemProcessor` seam.

## Out of Scope

- Authentication, authorization, multi-user accounts, or remote tenancy.
- Cloud storage integrations or remote issue trackers.
- Automatic collision renaming, overwriting, deletion of conflicting destinations, or silent conflict resolution.
- Automatic retry of collision failures or uncertain filesystem outcomes.
- Guaranteed rollback or transactional filesystem mutation.
- Password discovery, password management, or decryption workflows for protected archives.
- Archive formats other than ZIP and 7z for creation, or ZIP, 7z, and RAR for extraction.
- Arbitrary plugin distribution or third-party action marketplaces.
- A full SPA frontend; the accepted architecture uses server-rendered HTML with HTMX and Alpine.js.
- Selective application of individual actions from a plan. The plan is reviewable, but execution applies its ordered action sequence.
- Automatic content deduplication or file identity beyond the agreed source fingerprint and processing-attempt model.

## Further Notes

- The repository currently contains documentation only; implementation modules and tests must be created.
- `CONTEXT.md` is the canonical source for domain vocabulary. `docs/adr/` records durable architectural outcomes, and `docs/design/architecture.md` contains detailed behavior and interfaces.
- The first implementation milestone should be a vertical slice through YAML loading, `ItemProcessor` planning, dry run, one safe filesystem action, durable attempts, and structured logging.
- Remaining uncertainty: exact YAML schema validation library, exact CLI command names beyond the already discussed `check`, `run`, and `status`, web route names, archive library choices, the detailed reconciliation UI, web rule-editing model, delete safeguard, retry policy for non-collision failures, reconciliation validation, watch-folder stability detection, archive naming edge cases, archive path-traversal protections, and atomic YAML-save behavior.
