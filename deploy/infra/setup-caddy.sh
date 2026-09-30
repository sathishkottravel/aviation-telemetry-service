#!/usr/bin/env bash
# Switch the VM's Caddy from the host install (systemd) to the Docker container in this folder, using the same
# /etc/caddy config and certificates.
#
#   sudo ./setup-caddy.sh             # validate, stop the host Caddy, start the container
#   sudo ./setup-caddy.sh --rollback  # stop the container, start the host Caddy again
set -euo pipefail
cd "$(dirname "$0")"

CADDYFILE=/etc/caddy/Caddyfile
IMAGE=caddy:2

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
host_caddy() { command -v systemctl >/dev/null && systemctl list-unit-files caddy.service --no-legend 2>/dev/null | grep -q caddy; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
command -v docker >/dev/null || die "Docker is not installed"
if ! docker compose version >/dev/null 2>&1; then
  log "Installing Docker Compose v2 (Ubuntu package docker-compose-v2)"
  if ! { apt-get update -qq && apt-get install -y -qq docker-compose-v2 >/dev/null; }; then
    die "could not install docker-compose-v2; see README.md → Troubleshooting"
  fi
fi

if [[ "${1:-}" == "--rollback" ]]; then
  log "Rolling back to the host Caddy"
  docker compose down
  if host_caddy; then systemctl enable --now caddy; fi
  log "Done"
  exit 0
fi

log "Validating $CADDYFILE in the container (nothing is stopped yet)"
[[ -f "$CADDYFILE" ]] || die "$CADDYFILE not found"
env_args=()
[[ -f caddy.env ]] && env_args=(--env-file caddy.env)
docker run --rm "${env_args[@]}" --network host -v /etc/caddy:/etc/caddy:ro "$IMAGE" \
  caddy validate --config "$CADDYFILE" --adapter caddyfile \
  || die "the config is not valid inside the container (see above); nothing was changed"

if host_caddy && systemctl is-active --quiet caddy; then
  log "Stopping the host Caddy (systemd)"
  systemctl disable --now caddy
fi

log "Starting the Caddy container"
if ! docker compose up -d; then
  docker compose logs --tail 30 || true
  die "the container failed to start; run: sudo $0 --rollback"
fi

sleep 3
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost/ || true)"
if [[ "$(docker compose ps --status running -q | wc -l)" -eq 0 || "$code" == "000" ]]; then
  docker compose logs --tail 30 || true
  die "Caddy is not answering on port 80; run: sudo $0 --rollback"
fi
log "Caddy is running in Docker (port 80 answered $code). Logs: docker compose logs -f"
log "Reload after editing $CADDYFILE: docker compose exec caddy caddy reload --config $CADDYFILE"
