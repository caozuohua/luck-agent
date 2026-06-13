#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || {
    echo "run as root" >&2
    exit 1
}

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir="/usr/local/libexec/luck-agent"
sudoers_tmp=$(mktemp)
trap 'rm -f "$sudoers_tmp"' EXIT

/usr/bin/install -d -o root -g root -m 755 "$install_dir"
for name in restart journal backup restore repair upgrade rollback; do
    /usr/bin/install -o root -g root -m 755 \
        "$source_dir/luck-agent-$name" \
        "/usr/local/sbin/luck-agent-$name"
done
/usr/bin/install -o root -g root -m 700 \
    "$source_dir/luck-agent-restore-worker" \
    "$install_dir/luck-agent-restore-worker"

cat >"$sudoers_tmp" <<'EOF'
Cmnd_Alias LUCK_AGENT_OPS = /usr/local/sbin/luck-agent-restart, /usr/local/sbin/luck-agent-journal *, /usr/local/sbin/luck-agent-backup, /usr/local/sbin/luck-agent-restore *, /usr/local/sbin/luck-agent-repair, /usr/local/sbin/luck-agent-upgrade, /usr/local/sbin/luck-agent-rollback *
luck-agent ALL=(root) NOPASSWD: LUCK_AGENT_OPS
EOF
/usr/sbin/visudo -cf "$sudoers_tmp"
/usr/bin/install -o root -g root -m 440 "$sudoers_tmp" /etc/sudoers.d/luck-agent-ops
/usr/sbin/visudo -cf /etc/sudoers.d/luck-agent-ops
