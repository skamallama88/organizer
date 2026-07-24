# Organizer

Organizer is a file organization daemon for macOS and Unraid. It watches folders,
evaluates configurable rules, and safely performs actions such as moving, copying,
renaming, archiving, unarchiving, and quarantining items.

## Production Deployment

The production image stores Organizer configuration, the Tracking DB, and logs in
`/config`. Watched content is mounted separately at `/data`. The image includes
`unrar-free` for RAR extraction.

Build and start the included Compose deployment:

```sh
docker compose up --build
```

The web UI is available at `http://127.0.0.1:8000`. The Compose configuration keeps
the published port on host loopback while the container listens on `0.0.0.0`, which
is required for Docker port forwarding.

For Unraid, replace the named volumes in `docker-compose.yml` with host paths, for
example:

```yaml
volumes:
  - /mnt/user/appdata/organizer:/config
  - /mnt/user/data:/data
```

Keep `/config` separate from `/data`. Organizer's configuration volume must not be
used as a watch folder or action destination.

## Network Binding

The standalone web command defaults to the safer localhost bind:

```sh
organizer-web
```

Set `ORGANIZER_HOST` and `ORGANIZER_PORT` to change the bind address and port:

```sh
ORGANIZER_HOST=0.0.0.0 ORGANIZER_PORT=8000 organizer-web
```

The administrative UI is unauthenticated. Any non-loopback bind logs a prominent
warning and should only be used behind a trusted reverse proxy or private network.

## Web UI

The dashboard at `/` lists configured watch folders, their health, rule count, and
recent activity. Each watch folder links to its YAML rule editor at
`/watches/<watch-id>/rules`, where validation, dry-run previews, and atomic
revision-checked saves return inline HTMX feedback. The UI serves its Jinja2
templates, CSS, HTMX, and Alpine.js assets locally under `/static`.

## CLI

Preview a plan without mutating files:

```sh
organizer check <watch-id> <item>
organizer status
```

The CLI and web app load watch roots, rules paths, data volumes, quarantine,
and global settings from `/config/organizer.yaml` (override the config path on
CLI commands with `--config-path`).

Review failed and uncertain processing attempts:

```sh
organizer review list
organizer review inspect <attempt-id>
```

## Development

The project requires Python 3.12 or newer. Install dependencies with `uv`, then run:

```sh
uv run pytest
uv run mypy
```

Rules and action semantics are documented in `docs/design/architecture.md`.
