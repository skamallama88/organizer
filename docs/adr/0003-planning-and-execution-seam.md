# Planning and execution are separate ItemProcessor stages

Organizer separates rule evaluation from filesystem mutation through the `ItemProcessor` module. Planning produces an immutable execution plan that can be previewed by the CLI and web UI; execution consumes that plan in apply or dry-run mode. This keeps watcher, scanner, CLI, and web callers on one deep module while preventing preview behavior from becoming a second implementation of rule and action semantics.
