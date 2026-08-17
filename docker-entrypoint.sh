#!/bin/sh
set -eu

CONFIG_DIR=${CONFIG_DIR:-/config}
MOUNTS_FILE=${MOUNTS_FILE:-/proc/mounts}

if [ ! -f "$CONFIG_DIR/organizer.yaml" ]; then
  mkdir -p "$CONFIG_DIR"
  mount_paths=$(awk -v config_dir="$CONFIG_DIR" '
    function unescape(value) {
      gsub(/\\040/, " ", value)
      gsub(/\\011/, "\t", value)
      gsub(/\\134/, "\\", value)
      return value
    }
    {
      path = unescape($2)
      type = $3
      if (type == "rootfs" || type == "overlay" || type == "proc" ||
          type == "sysfs" || type == "devtmpfs" || type == "tmpfs" ||
          path == "/" || path == "/data" || path == config_dir || path == "/config" ||
          path == "/etc/hosts" ||
          path == "/etc/resolv.conf" || path == "/etc/hostname" ||
          path ~ /^\/proc(\/|$)/ || path ~ /^\/sys(\/|$)/ ||
          path ~ /^\/dev(\/|$)/) next
      if (!seen[path]++) print path
    }
  ' "$MOUNTS_FILE")

  {
    cat <<'EOF'
# Organizer runtime configuration
data_roots:
  - /data
EOF
    printf '%s\n' "$mount_paths" | while IFS= read -r path; do
      if [ -n "$path" ]; then
        printf '  - %s\n' "$path"
      fi
    done || true
    cat <<'EOF'
poll_interval: 1
quarantine_root: /data/.quarantine
watches:
  - id: data
    root: /data
    rules: /config/rules.yaml
EOF
  } > "$CONFIG_DIR/organizer.yaml"
fi

if [ ! -f "$CONFIG_DIR/rules.yaml" ]; then
  cat > "$CONFIG_DIR/rules.yaml" <<'EOF'
rules: []
EOF
fi

exec "$@"
