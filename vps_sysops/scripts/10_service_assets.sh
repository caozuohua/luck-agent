#!/usr/bin/env bash
set -u
assets=()
add() { assets+=("$1"); }
while read -r unit load active sub rest; do
  [[ "$unit" == *.service ]] || continue
  status="inactive"; [[ "$active" == "active" ]] && status="active"
  add "{\"asset_type\":\"systemd\",\"service_id\":\"${unit%.service}\",\"label\":\"$unit\",\"status\":\"$status\"}"
done < <(systemctl list-units --all --type=service --no-legend --no-pager 2>/dev/null || true)
while read -r unit load active sub rest; do
  [[ "$unit" == *.service ]] || continue
  status="inactive"; [[ "$active" == "active" ]] && status="active"
  add "{\"asset_type\":\"user_systemd\",\"service_id\":\"${unit%.service}\",\"label\":\"$unit\",\"status\":\"$status\"}"
done < <(systemctl --user list-units --all --type=service --no-legend --no-pager 2>/dev/null || true)
while read -r id image command status ports name; do
  [[ -n "${id:-}" ]] || continue
  add "{\"asset_type\":\"docker\",\"service_id\":\"$id\",\"label\":\"${name:-$id}\",\"status\":\"active\",\"metadata\":{\"image\":\"$image\",\"ports\":\"$ports\"}}"
done < <(docker ps --format '{{.ID}} {{.Image}} {{.Command}} {{.Status}} {{.Ports}} {{.Names}}' 2>/dev/null || true)
printf '{"assets":[%s]}\n' "$(IFS=,; echo "${assets[*]}")"
