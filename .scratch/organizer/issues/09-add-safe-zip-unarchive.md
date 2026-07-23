# 09 — Add safe ZIP unarchive

**What to build:** Users can extract ZIP archives into an extraction root with bounded preview inspection, staged extraction, traversal and symlink protections, collision protection, resource limits, password and corruption classification, and configurable source preservation.

**Blocked by:** 02 — Enforce watch-folder and destination boundaries; 05 — Add durable leases, completion skipping, and trigger processing.

**Status:** ready-for-agent

- [ ] ZIP preview reports a bounded read-only extraction summary without creating an attempt or mutating filesystem state.
- [ ] Extraction stages all content before no-overwrite publication to a single extraction root and rejects traversal, escaping symlinks, and resource-limit violations.
- [ ] Corrupt or protected archives remain in place with visible failure classification and suppressed automatic retries; successful extraction honors source preservation.
