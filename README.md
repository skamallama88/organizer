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

To stop it without removing persistent volumes:

```sh
docker compose down
```

The web UI is available at `http://127.0.0.1:8000`. The Compose configuration keeps
the published port on host loopback while the container listens on `0.0.0.0`, which
is required for Docker port forwarding.

On first start, the image creates `/config/organizer.yaml` and `/config/rules.yaml`
in an empty config volume. The generated `data_roots` includes `/data` and any
additional bind mounts visible in the container, excluding Docker and system
mounts. Edit those files to configure watch folders and rules; existing files are
never overwritten on restart. Discovered roots are eligible data roots only and
are not automatically watched.

Inspect or edit the generated files with a temporary container:

```sh
docker compose run --rm organizer cat /config/organizer.yaml
docker compose run --rm organizer sh -c 'vi /config/organizer.yaml'
```

The generated configuration watches `/data` using `/config/rules.yaml`. Add or
replace entries under `watches` when using multiple data directories. Each watch
must have a unique `id`, an absolute `root` inside a configured `data_roots` path,
and a `rules` path. For example:

```yaml
data_roots:
  - /data
quarantine_root: /data/.quarantine
stability_interval: 5
watches:
  - id: downloads
    root: /data/Downloads
    rules: /config/rules-downloads.yaml
```

`stability_interval` (seconds) is how long a file must stop changing size and
mtime before it is acted on. On watch roots backed by the polling observer
(FUSE/Unraid user shares, NFS, CIFS/SMB), where the watcher cannot detect write
completion, it prevents files from being moved mid-copy; inotify-backed watch
roots keep the immediate write-complete fast path. Set to `0` to disable. See
`docs/design/architecture.md` for details.

`staging_cleanup_age` (seconds, default `3600`) is how old a leftover
`.organizer-staging-*` artifact must be before the periodic retention sweep
removes it. Staging directories are normally pruned when an archive/copy/
unarchive attempt finishes; this sweep reclaims artifacts left behind by
attempts that never terminate (for example a hung or password-protected
archive). An artifact is only removed once nothing in its subtree has been
modified for this long, so an actively-writing extraction is never deleted.

After editing configuration, restart the service:

```sh
docker compose restart organizer
```

### Runtime watch management

Watch folders can also be added and removed while Organizer is running. Use the
**Add Watch** control on the dashboard, or call the unauthenticated API endpoints:

```sh
curl -X POST http://127.0.0.1:8000/watches \
  -H 'Content-Type: application/json' \
  -d '{"id":"downloads","root":"/data/Downloads","rules_path":"/config/rules-downloads.yaml"}'

curl -X DELETE http://127.0.0.1:8000/watches/downloads
```

The API validates watch IDs and roots against the configured data roots, rejects
duplicate or overlapping watches, persists successful changes to
`/config/organizer.yaml`, and updates both filesystem watching and periodic
scanning without restarting the service. When `rules_path` is omitted, it
defaults to `/config/rules_<watch_id>.yaml`. Removing a watch changes its
runtime configuration only; it does not delete the watched files or its rules
file.

The default rules file contains no rules, so Organizer starts safely without
modifying files. Add rules through the web UI or edit the configured YAML file.

### Discovery scope

By default a watch folder is discovered recursively: every file and folder at
every depth is an independently considered item. Set a watch's `discovery` to
`top_level` to operate on only the immediate children of the watch root. A rule
then acts on a top-level folder as a unit — for example a `rename` renames the
folder but never individually touches anything inside it — so you can sort top
level folder names without recursing into and re-processing their contents.

```yaml
watches:
  - id: unsorted
    root: /data/Unsorted
    rules: /config/rules-unsorted.yaml
    discovery: top_level
```

The scope applies to both the periodic scanner and the filesystem-event watcher.
Manual and forced operations (a "Scan now" request, `organizer check`, or an
explicit reprocess/retry command) operate on the single named item regardless of
scope. The value is `recursive` when omitted, and can also be changed per watch
from the dashboard's Schedule controls.

## Rules YAML

Each watch folder has a rules file with a top-level `rules` list. Rules are
evaluated from top to bottom and the first matching rule wins. A rule requires a
`name`, a regular-expression `match`, and one or more ordered `actions`.

```yaml
rules:
  - name: Move videos
    match:
      field: file_name       # file_name, folder_name, or full_path
      pattern: '\.(mkv|mp4)$' # Python regular expression
    actions:
      - move:
          destination: /data/Videos
```

Relative destinations are resolved from the watch folder. Absolute destinations
must remain inside a configured `data_roots` path. Organizer never overwrites an
existing destination item, and a failed action stops the rest of that action
chain.

### Match fields and captures

Use `file_name` for a file's name, `folder_name` for the containing folder name
(or the item's name when it is a folder), and `full_path` for the normalized
absolute path. Give a match a name when an action needs its regular-expression
captures. Rename actions can use numbered captures (`\1`, `\2`) or named
captures (`\g<title>`).

```yaml
rules:
  - name: Remove tag from filename
    match:
      name: title
      field: file_name
      pattern: '^(?P<title>.*) \[processed\](?P<extension>\.[^.]+)$'
    actions:
      - rename:
          name: '\g<title>\g<extension>'
```

For multiple independent conditions, use `conditions` instead of relying only
on the default condition name `match`. Every condition must match for the rule
to apply.

```yaml
rules:
  - name: Move processed video folders
    conditions:
      folder:
        field: folder_name
        pattern: '^Season [0-9]+$'
      video:
        field: file_name
        pattern: '\.(mkv|mp4)$'
    match:
      field: file_name
      pattern: '.*'
    actions:
      - move:
          destination: /data/Processed
```

### Available actions

Move an item and preserve its name:

```yaml
actions:
  - move:
      destination: ../Videos
```

After a `move`/`copy` hoists files out of a nested tree, set
`delete_empty_dirs: true` to prune the now-empty ancestor directories up to
(but not including) the watch root. Off by default for users who rely on the
folder structure:

```yaml
actions:
  - move:
      destination: /data/Processed
      delete_empty_dirs: true
```

Copy an item and keep the original in place:

```yaml
actions:
  - copy:
      destination: /data/Backups
```

Rename an item within its current directory:

```yaml
actions:
  - rename:
      name: normalized-name.mkv
```

Direct deletion is irreversible and requires an explicit rule-level opt-in:

```yaml
rules:
  - name: Delete temporary files
    match:
      field: file_name
      pattern: '\.tmp$'
    actions:
      - delete:
          mode: direct
    allow_direct_deletion: true
```

Quarantine removes an item recoverably under the configured `quarantine_root`.
The original relative path is retained under an attempt-specific directory:

```yaml
rules:
  - name: Quarantine unknown files
    match:
      field: file_name
      pattern: '\.unknown$'
    actions:
      - delete:
          mode: quarantine
```

Create a ZIP or 7z archive. `extension` defaults to `.zip`; set
`preserve_originals` to `false` to remove the source after successful archive
publication.

```yaml
actions:
  - archive:
      destination: /data/Archives
      extension: .zip       # .zip or .7z
      preserve_originals: true
```

Unarchive ZIP, 7z, or RAR files into an extraction directory named after the
archive. `destination` defaults to the watch folder. The source is preserved by
default; set `preserve_original` to `false` to remove it after successful
extraction. Resource limits and nested extraction depth are optional.

```yaml
rules:
  - name: Extract downloads
    match:
      field: file_name
      pattern: '\.(zip|7z|rar)$'
    actions:
      - unarchive:
          destination: /data/Extracted
          preserve_original: true
          max_depth: 1
          max_entries: 10000
          max_uncompressed_bytes: 1073741824
          max_entry_bytes: 1073741824
```

Actions can be chained. Each action receives the primary result of the prior
action, and direct deletion must be the final action. Chaining is supported for
content-preserving follow-ups (`copy`, `rename`, `move`); a removal action
(`delete` or an `archive`/`unarchive` with `preserve_original: false`) must be
last in the chain, since a later action cannot act on content that was already
removed or transformed:

```yaml
rules:
  - name: Copy then rename
    match:
      field: file_name
      pattern: '\.mkv$'
    actions:
      - copy:
          destination: /data/Review
      - rename:
          name: reviewed.mkv
```

Use `allow_hard_link_removal: true` only when a direct delete or quarantine rule
is intentionally allowed to remove files that have multiple hard links. Use the
CLI or web UI dry run before enabling mutating rules:

```sh
organizer check <watch-id> <item>
```

For Unraid, use a Compose file that pulls the prebuilt image from GitHub
Container Registry instead of building it. A complete example, with host paths
for the config and data volumes:

```yaml
# docker-compose.yml
services:
  organizer:
    image: ghcr.io/skamallama88/organizer:latest
    container_name: organizer
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      ORGANIZER_HOST: "0.0.0.0"
      ORGANIZER_PORT: "8000"
    volumes:
      - /mnt/user/appdata/organizer:/config
      - /mnt/user/data:/data
```

Start it with `docker compose up -d`. Replace the `/mnt/user/...` host paths with
the appdata and data shares you want Organizer to use.

#### File permissions on shared mounts

By default the container runs as root, so directories Organizer creates under a
mounted share are `755` (root-owned) and a non-root host user (for example an SMB
account) cannot move or create files inside them. Organizer applies a `002` umask
at startup so app-created directories are `775` (group-writable). On a NAS that
runs the app with a dedicated share owner, run the container as that identity so
created files carry it:

```yaml
    user: "99:100"        # Unraid convention: nobody:users
```

If the app runs as root and the share owner differs from the container uid,
`chown` the share once so group ownership lines up, or mount with a matching uid:
the `002` umask still gives group members write access to app-created
directories regardless of the container's uid.

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
