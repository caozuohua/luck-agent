#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || {
    echo "run as root" >&2
    exit 1
}

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sudoers_tmp=$(mktemp)
trap 'rm -f "$sudoers_tmp"' EXIT

/usr/bin/install -o root -g root -m 755 \
    "$source_dir/luck-agent-restart" \
    /usr/local/sbin/luck-agent-restart

cat >"$sudoers_tmp" <<'EOF'
Cmnd_Alias LUCK_AGENT_RESTART = /usr/local/sbin/luck-agent-restart
luck-agent ALL=(root) NOPASSWD: LUCK_AGENT_RESTART
EOF

/usr/sbin/visudo -cf "$sudoers_tmp"
/usr/bin/install -o root -g root -m 440 \
    "$sudoers_tmp" /etc/sudoers.d/luck-agent-restart
/usr/sbin/visudo -cf /etc/sudoers.d/luck-agent-restart
