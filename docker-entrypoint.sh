#!/bin/sh
set -eu

if [ ! -f /config/organizer.yaml ]; then
  mkdir -p /config
  cat > /config/organizer.yaml <<'EOF'
# Organizer runtime configuration
data_roots:
  - /data
quarantine_root: /data/.quarantine
watches:
  - id: data
    root: /data
    rules: /config/rules.yaml
EOF
fi

if [ ! -f /config/rules.yaml ]; then
  cat > /config/rules.yaml <<'EOF'
rules: []
EOF
fi

exec "$@"
