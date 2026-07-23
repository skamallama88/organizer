# Organizer

A file organization daemon inspired by Hazel for macOS, designed for Unraid. Watches folders, matches files and folders against configurable rules, and performs actions such as move, copy, rename, archive, and unarchive.

## Language

**Organizer**:
The application itself. Runs as a daemon with a web UI and CLI.
_Avoid_: Sorter, FileBot, Hazel

**Watch folder**:
A directory monitored by Organizer, with its own independent set of rules. Its root must be disjoint from every other watch folder root.
_Avoid_: Watched folder, monitored path

**Rule**:
A named set of match conditions and actions that determines how Organizer handles an item.
_Avoid_: Pattern, filter, workflow

**Match condition**:
A condition that determines whether a rule applies to an item.
_Avoid_: Filter, matcher

**Item**:
A file or folder considered by a rule.

**Action**:
An operation performed on a matched item. Examples: move, copy, delete, rename, archive, unarchive.

**Rename**:
The action of changing the name of a matched file or folder while keeping it in its current parent directory.
_Avoid_: Move, relabel

**Unarchive**:
The action of extracting a compressed archive into files or folders.
_Avoid_: Extract, decompress

**Archive**:
The action of compressing matched files or folders into an archive.
_Avoid_: Compress, bundle, zip up

**Dry run**:
A mode where rules are evaluated and actions are reported without modifying items or processing state.

**Tracking DB**:
A record of items Organizer has processed so it can skip unchanged items across restarts.
_Avoid_: State file, index

**Processing lease**:
Exclusive, durable ownership of an item processing attempt within a watch folder. It prevents concurrent triggers or recovery commands from processing the same source identity at the same time; a nonterminal leased attempt found after restart enters reconciliation rather than being retried automatically.
_Avoid_: Lock, job reservation

**Reconciliation**:
Explicit review of an attempt whose filesystem outcome is uncertain. An uncertain action and every later planned action remain blocked unless filesystem evidence conclusively establishes that action's result.
_Avoid_: Automatic recovery, retry

**Processing boundary**:
The item selected for a mutating plan and its complete subtree when it is a folder. Organizer does not independently process descendants within that boundary while the enclosing item is active.
_Avoid_: Batch, recursive job

**Allowed destination root**:
A configured path within Organizer's mounted data volumes to which a watch folder may write action output. A destination that is also a watch folder is allowed but produces a non-blocking configuration warning because it can form an intentional or accidental processing pipeline.
_Avoid_: Safe path, output allowlist

**Processing lineage**:
The ordered watch folders an item has entered during a processing workflow. Organizer evaluates an item at most once per watch folder and rejects a plan that would return it to a watch folder already in its lineage.
_Avoid_: History, route

**Staged extraction**:
Archive extraction into private temporary storage before its contents are published to the configured destination. The source archive is retained and incomplete or uncertain publication remains available as reconciliation evidence.
_Avoid_: Direct extraction, partial extraction

**Staged archive**:
An archive created in private temporary storage and published to its final path only after it is complete and validated. Its source is retained until publication and action recording are known to have succeeded.
_Avoid_: Direct archive output, partial archive

**Source identity**:
The canonical path and source fingerprint of an item when its plan is created. An identical path is independent work when its fingerprint has changed.
_Avoid_: Path identity, file key

**Resulting-path handoff**:
The recorded transfer of an item's processing lineage to a resulting path, including a destination watch folder when applicable.
_Avoid_: Redirect, move event

**Direct deletion**:
Irreversible removal of an item without a recoverable resulting path. An uncertain direct deletion always requires explicit administrator confirmation during reconciliation.
_Avoid_: Delete, purge

**Quarantine**:
Recoverable removal of an item to an Organizer-managed, attempt-specific directory under a configured quarantine root. It preserves the original relative path and records its source identity and resulting path.
_Avoid_: Trash, recycle bin

**Organizer-managed path**:
A path reserved for Organizer's staging, temporary output, or quarantine artifacts. It is excluded from discovery and rule planning and may be accessed only by Organizer recovery workflows.
_Avoid_: Hidden folder, internal path

**Suppression**:
A durable prohibition on automatic processing of an exact source identity after a collision, known archive-input failure, or abandoned attempt. Only an explicit administrator command may clear it and permit a newly planned attempt.
_Avoid_: Ignore rule, retry delay

**Disabled rule warning**:
A plan and execution-report warning that one or more earlier declared rules were disabled by validation errors. Disabled rules never match, but their skipped precedence remains visible.
_Avoid_: Ignored error, fallback rule

**Named match condition**:
A uniquely named regular-expression condition in a rule. Actions reference its captures explicitly by condition name and capture number or group name.
_Avoid_: Implicit matcher, last match

**Archive output name**:
The source basename with at most one final recognized archive suffix removed before the requested output extension is appended. Remaining suffixes are preserved.
_Avoid_: Normalized filename, deduplicated name

**Extraction root**:
The single destination directory created for an unarchived archive, named from the archive basename. All extracted entries are published beneath it without flattening archive-internal directories.
_Avoid_: Extraction directory, flattened output

**Nested extraction**:
Extraction of archives discovered within a staged archive tree as part of the parent unarchive action. It is limited by configured depth and is not independently eligible for discovery until the parent attempt is terminal.
_Avoid_: Recursive watcher processing, independent extraction

**Archive resource limit**:
A configured cap on archive entry count, total uncompressed bytes, or individual entry size for a watch folder. Exceeding a limit is a permanent failed input that remains in place and requires explicit retry after limit review.
_Avoid_: Archive bomb protection, extraction quota

**Symlink item**:
A symbolic link considered as an item without dereferencing its target. Organizer never traverses it during discovery or recursive operations; supported actions operate on the link itself.
_Avoid_: Linked target, followed link

**Hard-link removal opt-in**:
The explicit rule setting required before Organizer may quarantine or directly delete a file with multiple hard-link directory entries. Plans and reports include the link count.
_Avoid_: Implicit hard-link deletion, shared-file removal

**Expected resulting identity**:
The path and fingerprint an action must produce for the following action in the same plan. A mismatch before a later action is uncertain work requiring reconciliation, not automatic retry.
_Avoid_: Intermediate state, assumed output

**Fresh retry plan**:
A newly planned attempt using the item's current path, fingerprint, active ruleset revision, and destination policy. It may link to a prior attempt for audit but never reuses that attempt's actions.
_Avoid_: Replayed plan, stale retry

**Reprocess command**:
An explicit administrator command that permits a completed source identity to be evaluated again under current rules. It creates a fresh plan and does not repeat ordinary automatic processing.
_Avoid_: Rescan, automatic replay

**Copy provenance**:
The recorded relationship between a completed source copy action and its resulting copy. A copy entering a different watch folder begins an independent processing lineage.
_Avoid_: Duplicate identity, copied history

**Archive source consistency**:
The requirement that an archive source retain its planned fingerprint, or stable tree fingerprint for folders, throughout archive creation. A changed source invalidates the temporary archive before publication or source removal.
_Avoid_: Best-effort archive, live archive

**Staged copy**:
Copying an item to private destination storage before no-overwrite publication. Organizer publishes it only after its source remains consistent; uncertain publication or cleanup is retained as recovery evidence.
_Avoid_: Direct copy, partial copy

**Cross-filesystem move**:
A move between different filesystems that uses staged copy, no-overwrite publication, durable resulting-path recording, and only then source removal. Uncertainty after publication preserves both paths for reconciliation.
_Avoid_: Atomic move, rename move

**Self-targeting action**:
An action whose resolved target is its input's canonical path or, for a folder, a descendant of that folder. Organizer rejects it as a permanent planning failure.
_Avoid_: No-op action, recursive destination

**Primary resulting item**:
The single item produced by an action and used as the next action's input. Direct deletion produces none and must be the final action; a rule is invalid when a later action cannot accept the prior result type.
_Avoid_: Multiple action outputs, implicit next input

**Bounded archive inspection**:
Read-only archive metadata inspection subject to configured entry-count and metadata-size limits. Planning reports an extraction-root summary but defers definitive archive validation to execution.
_Avoid_: Full preview extraction, unbounded archive listing

**Execution attempt**:
A durable processing record created only after Organizer has an executable current plan and a processing lease. Earlier planning and preview failures are visible diagnostics rather than failed attempts.
_Avoid_: Planning attempt, preview record

**Abandoned attempt**:
A terminal processing attempt intentionally closed by an administrator with its reason and retained evidence. Reopening it is an explicit auditable command that clears its suppression and creates a fresh retry plan; the original attempt remains abandoned.
_Avoid_: Reverted failure, deleted attempt

**Deferred item**:
An item withheld from planning because it has not remained stable for the configured interval. Organizer records deduplicated diagnostics and a stability-timeout warning without creating an attempt, failure, or suppression.
_Avoid_: Failed item, ignored upload

**Versioned fingerprint**:
A recorded identity proof comprising canonical path, item type, size, modification time at available precision, reliable filesystem identity metadata, and a content hash for destructive operations. Folders use deterministic tree fingerprints; insufficient proof prevents destructive execution.
_Avoid_: Timestamp check, weak file identity

**Full path**:
The normalized absolute path visible inside the Organizer container, using `/` separators. It may span configured mounted data volumes but never resolves outside them or through symlinks.
_Avoid_: Watch-relative path, host path

**Config-volume exclusion**:
The rule that Organizer's configuration volume may not be watched, targeted, used for staging, or used for quarantine. Validation and runtime containment checks enforce this boundary.
_Avoid_: Config data root, internal destination

**Accepted action result**:
An immutable, administrator-confirmed record that reconciliation has accepted an action's resulting identity based on recorded evidence. A remaining-action retry begins a new linked attempt from that identity.
_Avoid_: Reopened attempt, inferred success

**Discovery batch**:
The timestamped eligible-item snapshot for an immediate watch-folder run. It reports items as planned, executed, skipped, or deferred and does not pause other processing triggers.
_Avoid_: Exclusive run, full filesystem snapshot

**Recovery evidence**:
Attempts, action results, suppressions, quarantined items, and uncertain staging artifacts needed to review or resolve prior work. Organizer never deletes it automatically while its related work remains nonterminal, abandoned, suppressed, or reconciled.
_Avoid_: Temporary history, disposable logs

**Filesystem case capability**:
The known or conservatively assumed case-sensitivity behavior of a watch folder's destination filesystem. Case-insensitive destinations reject case-only output-path differences as collisions.
_Avoid_: Lowercased path, filename style

**Watch-folder health condition**:
An access, mount, or permission failure affecting a watch folder or its configured destination root. Organizer pauses that watch folder's planning and execution rather than creating per-item failures.
_Avoid_: Item failure, unavailable file

**Stable tree**:
A folder and complete subtree whose tree fingerprint remains unchanged for the configured stability interval. Organizer may quarantine or directly delete a folder only after confirming a stable tree immediately before mutation.
_Avoid_: Stable folder, recursive delete target

**Metadata preservation**:
The initial action guarantee to preserve file contents, basenames, and supported POSIX mode bits. Ownership, ACLs, extended attributes, and platform-specific metadata are not guaranteed and produce non-fatal warnings when unsupported.
_Avoid_: Full metadata copy, transparent preservation

**Trusted control boundary**:
The network boundary protecting Organizer's unauthenticated administrative UI. The UI binds to localhost by default; remote exposure requires a trusted reverse proxy or private network control.
_Avoid_: Public UI, open admin endpoint

**Ruleset revision**:
The stable version of a watch folder's validated rules used to create a plan. Plans cannot execute after the active revision changes; UI saves use compare-and-swap against it, and only valid external file changes may become a new active revision.
_Avoid_: Config version, rule timestamp

**Persistence health condition**:
An inability to durably write the Tracking DB or persistent log. Organizer pauses new real executions before mutation while stdout logging and explicitly degraded dry runs remain available.
_Avoid_: Logging warning, item failure

**Historical plan**:
The immutable validated plan retained as attempt evidence after its ruleset is no longer active. It may support inspection and acceptance of proven outcomes, but any new mutation requires a fresh plan under active policy.
_Avoid_: Reusable old plan, active rule snapshot

**Config volume**:
Persistent storage for Organizer configuration and processing state.
_Avoid_: Config dir, settings

**Data volume**:
The file storage being watched and organized.
_Avoid_: Media dir, storage
