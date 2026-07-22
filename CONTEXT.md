# Organizer

A file organization daemon inspired by Hazel for macOS, designed for Unraid. Watches folders, matches files against configurable rules, and performs actions (move, copy, unarchive, etc.).

## Language

**Organizer**:
The application itself. Runs as a daemon with a web UI and CLI.
_Avoid_: Sorter, FileBot, Hazel

**Watch folder**:
A directory monitored by Organizer, with its own independent set of rules.
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

**Config volume**:
Persistent storage for Organizer configuration and processing state.
_Avoid_: Config dir, settings

**Data volume**:
The file storage being watched and organized.
_Avoid_: Media dir, storage
