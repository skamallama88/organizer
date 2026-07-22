# Durable processing attempts with reconciliation

Organizer records a durable processing attempt before filesystem mutation and records action outcomes and resulting paths as execution proceeds. Filesystem mutation and Tracking DB completion cannot share a transaction, so Organizer does not promise rollback or blindly repeat uncertain actions; an uncertain outcome becomes `needs-reconciliation` and is resolved through explicit review. This preserves processing history and avoids duplicate destructive work after crashes or persistence failures.
