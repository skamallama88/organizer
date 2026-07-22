# No-overwrite destination collisions

Organizer never overwrites an existing item when moving, copying, renaming, archiving, or unarchiving. A destination collision fails the processing attempt, suppresses automatic watcher and scan retries, and requires an explicit user retry after review. This favors protecting file-server data over silently selecting a collision-resolution strategy that could destroy or obscure existing content.
