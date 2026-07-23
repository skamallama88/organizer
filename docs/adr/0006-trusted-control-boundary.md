# Trusted control boundary for the unauthenticated UI

Organizer's initial web UI binds to localhost by default because it can configure and execute destructive filesystem operations without authentication. Remote access requires a trusted reverse proxy or private network control, and an explicit non-loopback bind emits a prominent startup warning.
