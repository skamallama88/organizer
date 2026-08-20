# 01 — Terminal-state staging cleanup + password handling

**What to build:** Guarantee staging cleanup when an attempt reaches a terminal state, and fix the password-protected-archive leak that left attempts `started`.

**Blocked by:** None

**Status:** completed

- [x] Add `py7zr.exceptions.PasswordRequired` to `_ARCHIVE_ERRORS` in `item_processor.py`
- [x] Classify it as `password-protected archive` in the `execute` failure handler
- [x] Make `_unarchive_to_staging` remove staging on any exception (`except Exception`) and re-raise
- [x] Make `_archive_to_staging` and `_copy_to_staging` remove staging on any exception
- [x] Test: password-protected `.7z` reports `failed`, leaves no `.organizer-staging-*`, records suppression, finishes the attempt
