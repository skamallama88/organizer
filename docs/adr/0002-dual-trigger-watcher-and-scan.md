# File watcher + periodic scan for rule evaluation

Rule evaluation fires on two triggers: a real-time filesystem watcher (via `watchdog`) and a periodic sweep (configurable interval, default 5 minutes).

The watcher gives near-instant reaction when a file lands — the expected UX for a Hazel-like tool. The periodic scan is a safety net: watchers can miss events (network mounts, bulk copies, system sleep/wake), and the scan catches anything the watcher skipped. The scan also re-checks files whose modification time has changed, picking up in-place edits.

The cost is architectural complexity — two code paths that must converge on the same rule engine, and the tracking DB is essential to prevent duplicate processing.

The trigger orchestration lives in `organizer.daemon`. Both services submit discovered paths to the same batch adapter, which creates plans and executes them through `ItemProcessor`; processing leases provide duplicate protection. Watchdog events are limited to file creation and close-write events and are coalesced during a short debounce window. The scanner recursively enumerates each configured watch folder at the configured interval.
