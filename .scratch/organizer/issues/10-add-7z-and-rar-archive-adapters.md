# 10 — Add 7z and RAR archive adapters

**What to build:** Archive creation supports 7z, and unarchive supports 7z and RAR with the same staging, failure classification, resource safety, and no-overwrite behavior as ZIP.

**Blocked by:** 08 — Add ZIP archive creation; 09 — Add safe ZIP unarchive.

**Status:** ready-for-agent

- [ ] 7z creation follows the ZIP archive action contract for files, folders, source preservation, and resulting-path recording.
- [ ] 7z and RAR extraction follow the same staged extraction, path safety, collision, and resource-limit guarantees as ZIP.
- [ ] Unsupported, corrupt, password-protected, or unavailable-RAR-tooling cases remain visible failures without destroying the input archive.
