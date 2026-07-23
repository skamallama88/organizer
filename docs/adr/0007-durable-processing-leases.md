# Durable processing leases

Organizer acquires a durable processing lease per source identity (canonical path + source fingerprint) before creating a processing attempt. The lease is held while the attempt is nonterminal and released when the attempt becomes terminal (completed, failed, or needs-reconciliation). One `processing_leases` row per source identity prevents concurrent watcher, scanner, CLI, and recovery triggers from processing the same item simultaneously.

A nonterminal leased attempt found after restart enters reconciliation via `recover_stale_leases` rather than being retried automatically. This preserves the original attempt history and avoids repeating uncertain filesystem work.
