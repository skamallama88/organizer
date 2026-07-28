# 31 — Verify metadata preservation contract

**What to build:** Administrators know which item metadata Organizer preserves during supported actions, with verified POSIX mode behavior and explicit visibility when ownership, ACLs, extended attributes, or platform-specific metadata cannot be preserved.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Supported actions preserve file contents, basenames, and supported POSIX mode bits as documented.
- [ ] Tests cover metadata behavior for files and folders where the platform supports it.
- [ ] Unsupported metadata guarantees are documented and surfaced as non-fatal warnings where Organizer can detect them.
