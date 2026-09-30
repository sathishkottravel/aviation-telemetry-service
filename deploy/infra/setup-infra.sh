#!/usr/bin/env bash
# Run Caddy (your /etc/caddy config) and Jaeger in Docker on the shared network "edge", replacing the Caddy installed
# on the host and any old Jaeger container.
#
#   sudo ./setup-infra.sh             # validate, start Jaeger, stop the old setup, start Caddy
#   sudo ./setup-infra.sh --rollback  # stop the containers, restore the old Caddy/Jaeger
set -euo pipefail
cd "$(dirname "$0")"

CADDYFILE=/etc/caddy/Caddyfile
STOPPED=.stopped   # what this script stopped, for --rollback

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
host_caddy() { command -v systemctl >/dev/null && systemctl list-unit-files caddy.service --no-legend 2>/dev/null | grep -q caddy; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
command -v docker >/dev/null || die "Docker is not installed"
if ! docker compose version >/dev/null 2>&1; then
  log "Installing Docker Compose v2 (Ubuntu package docker-compose-v2)"
  if ! { apt-get update -qq && apt-get install -y -qq docker-compose-v2 >/dev/null; }; then
    die "could not install docker-compose-v2; see README.md → Troubleshooting"
  fi
fi

if [[ "${1:-}" == "--rollback" ]]; then
  log "Rolling back"
  docker compose stop caddy jaeger || true
  if [[ -f $STOPPED ]]; then
    while read -r kind id; do
      if [[ $kind == container ]]; then docker start "$id" >/dev/null && log "restarted container $id"; fi
      if [[ $kind == unit ]]; then systemctl enable --now "$id" && log "started $id"; fi
    done < "$STOPPED"
    rm -f "$STOPPED"
  fi
  exit 0
fi

log "Validating $CADDYFILE (nothing is stopped yet)"
[[ -f $CADDYFILE ]] || die "$CADDYFILE not found"
env_args=()
[[ -f caddy.env ]] && env_args=(--env-file caddy.env)
docker run --rm "${env_args[@]}" -v /etc/caddy:/etc/caddy:ro caddy:2 caddy validate --config "$CADDYFILE" --adapter caddyfile \
  || die "the Caddyfile is not valid (see above); nothing was changed"

docker network inspect edge >/dev/null 2>&1 || { docker network create edge >/dev/null; log "created network edge"; }

log "Starting Jaeger"
docker compose up -d --wait jaeger || die "Jaeger did not become healthy (docker compose logs jaeger)"

log "Stopping the old setup"
for id in $(docker ps --format '{{.ID}} {{.Image}} {{.Label "com.docker.compose.project"}}' | awk 'tolower($2) ~ /jaeger/ && $3 != "infra" {print $1}'); do
  docker stop "$id" >/dev/null && echo "container $id" >> "$STOPPED" && log "stopped old Jaeger container $id"
done
if host_caddy && systemctl is-active --quiet caddy; then
  systemctl disable --now caddy && echo "unit caddy" >> "$STOPPED" && log "stopped the host Caddy"
fi

log "Starting Caddy"
docker compose up -d caddy || die "Caddy failed to start; run: sudo $0 --rollback"
sleep 3
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost/ || true)"
[[ "$code" != "000" ]] || { docker compose logs --tail 30 caddy; die "Caddy is not answering on port 80; run: sudo $0 --rollback"; }
log "Done: Caddy (port 80 answered $code) and Jaeger are running on network edge"
log "Reload after editing $CADDYFILE: docker compose exec caddy caddy reload --config $CADDYFILE"
