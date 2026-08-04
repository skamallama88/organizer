# 05 — Entrypoint: auto-discover mount points as data_roots

**What to build:** On first container start (when no `organizer.yaml` exists yet), `docker-entrypoint.sh` parses `/proc/mounts`, identifies bind mount paths, filters out pseudo-filesystems (proc, sysfs, devtmpfs, tmpfs), the overlay rootfs, the config volume (`/config`), and internal Docker mounts (`/etc/hosts`, `/etc/resolv.conf`, `/etc/hostname`). Remaining mount points are appended to the `data_roots` list in the generated config. No watches are created for them — the user adds watches via the web UI. The existing guard (`[ ! -f /config/organizer.yaml ]`) is preserved so an existing config is never overwritten.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Parse `/proc/mounts` fields to extract mount path and filesystem type
- [ ] Filter out: `rootfs`, `overlay`, `/`, `/config`, filesystems of type `proc`, `sysfs`, `devtmpfs`, `tmpfs`, and paths under `/proc`, `/sys`, `/dev`
- [ ] Append remaining unique mount paths to `data_roots` list in the generated YAML
- [ ] Existing `[ ! -f /config/organizer.yaml ]` guard preserved — never overwrites admin config
- [ ] Test: mount a temp directory, run entrypoint, verify it appears in generated config
