# Organizer

A file organization daemon inspired by Hazel for macOS, designed for Unraid. Watches folders, matches files against configurable rules, and performs actions (move, copy, unarchive, etc.).

## Language

**Organizer**:
The application itself. Runs as a daemon with a web UI and CLI.
_Avoid_: Sorter, FileBot, Hazel

**Watch folder**:
A directory monitored by Organizer, with its own independent set of rules. Each watch folder has a `rules.yaml` file defining its behavior.
_Avoid_: Watched folder, monitored path

**Rule**:
A named set of match conditions and actions. Rules are evaluated in order against files in a watch folder; first match wins.
_Avoid_: Pattern, filter, workflow

**Match condition**:
A regex pattern applied to a specific field of a file or folder (folder_name, file_name, full_path). Determines whether a rule applies.
_Avoid_: Filter, matcher

**Action**:
An operation performed on a matched item. Examples: move, copy, delete, unarchive.

**Unarchive**:
The action of extracting compressed archives (.zip, .7z, .rar). The original archive is deleted by default after successful extraction (configurable via `preserve_archive` flag). Extracted files land in the same directory as the archive by default.
_Avoid_: Extract, decompress

**Tracking DB**:
A SQLite database that records file fingerprints (path, modified time, size) so Organizer can skip already-processed files across restarts.
_Avoid_: State file, index

**Config volume**:
Persistent storage mounted at `/config/` containing watch folder rule files and the tracking database.
_Avoid_: Config dir, settings

**Data volume**:
The actual file storage being watched and organized. Mounted separately from the config volume.
_Avoid_: Media dir, storage
