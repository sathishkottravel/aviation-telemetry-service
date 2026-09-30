#!/usr/bin/env bash
# One-shot setup of the shared VM infrastructure: Caddy + Jaeger v2 all-in-one on the Docker network "edge".
# Also migrates from an existing Caddy/Jaeger (a Caddy installed on the host, or older containers).
#
#   sudo ./setup-infra.sh                  # asks before installing Docker or stopping anything
#   sudo ./setup-infra.sh --yes            # no prompts
#   sudo ./setup-infra.sh --rollback       # undo the switch: stop the new Caddy, restart what was stopped
#   sudo ./setup-infra.sh --help
#
# Run it from a copy of this folder on the VM (e.g. a git clone of the repo). The only manual step is putting your
# project site files into /opt/infra/sites/*.caddy beforehand (e.g. deploy/caddy/api.caddy).
# Safe to re-run: it refreshes the configs and restarts only what needs it.
set -euo pipefail

INFRA_DIR="${INFRA_DIR:-/opt/infra}"
JAEGER_DOMAIN="${JAEGER_DOMAIN:-}"
OTEL_DOMAIN="${OTEL_DOMAIN:-}"
OTEL_INGEST_TOKEN="${OTEL_INGEST_TOKEN:-}"
DEFAULT_JAEGER_DOMAIN="jaeger.sathishkottravel.com"
DEFAULT_OTEL_DOMAIN="otel.sathishkottravel.com"
CADDY_IMAGE="caddy:2"
ASSUME_YES=0
FIREWALL=1
MODE="setup"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILES=(docker-compose.yml Caddyfile jaeger-config.yaml .env.example)
STATE_FILE=""   # set after INFRA_DIR is final

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --yes                   Don't ask for confirmation
  --rollback              Stop the new Caddy and restart the previously stopped Caddy/Jaeger
  --jaeger-domain NAME    Jaeger UI hostname   (default: jaeger.sathishkottravel.com; or JAEGER_DOMAIN)
  --otel-domain NAME      OTLP ingest hostname (default: otel.sathishkottravel.com; or OTEL_DOMAIN)
  --otel-token TOKEN      OTLP ingest bearer token (or OTEL_INGEST_TOKEN; asked for if missing)
  --infra-dir DIR         Install directory    (default: /opt/infra; or INFRA_DIR)
  --no-firewall           Don't touch the VM's firewall
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1 ;;
    --rollback) MODE="rollback" ;;
    --jaeger-domain) JAEGER_DOMAIN="${2:?}"; shift ;;
    --otel-domain) OTEL_DOMAIN="${2:?}"; shift ;;
    --otel-token) OTEL_INGEST_TOKEN="${2:?}"; shift ;;
    --infra-dir) INFRA_DIR="${2:?}"; shift ;;
    --no-firewall) FIREWALL=0 ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done
STATE_FILE="$INFRA_DIR/.setup-state"

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  local answer
  read -r -p "    $1 [y/N] " answer
  [[ "$answer" =~ ^[Yy] ]]
}

compose() { (cd "$INFRA_DIR" && docker compose --progress quiet "$@"); }
have() { command -v "$1" >/dev/null 2>&1; }
has_systemd_unit() { have systemctl && systemctl list-unit-files "$1.service" --no-legend 2>/dev/null | grep -q "^$1.service"; }

# ---- state for rollback: what this script stopped ----------------------------------------------------------------
STOPPED_UNITS=()
STOPPED_CONTAINERS=()
load_state() {
  if [[ -f "$STATE_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$STATE_FILE"
  fi
}
save_state() {
  install -d "$INFRA_DIR"
  {
    printf 'STOPPED_UNITS=(%s)\n' "${STOPPED_UNITS[*]:-}"
    printf 'STOPPED_CONTAINERS=(%s)\n' "${STOPPED_CONTAINERS[*]:-}"
  } > "$STATE_FILE"
}

rollback() {
  log "Rolling back"
  load_state
  if [[ -f "$INFRA_DIR/docker-compose.yml" ]]; then
    if compose stop caddy >/dev/null 2>&1; then ok "stopped the new Caddy container"; fi
  fi
  local c u
  for c in "${STOPPED_CONTAINERS[@]}"; do
    if docker start "$c" >/dev/null; then ok "restarted container $c"; else warn "could not restart container $c"; fi
  done
  for u in "${STOPPED_UNITS[@]}"; do
    if systemctl enable --now "$u" >/dev/null 2>&1; then ok "re-enabled and started $u"; else warn "could not start $u"; fi
  done
  [[ ${#STOPPED_CONTAINERS[@]} -eq 0 && ${#STOPPED_UNITS[@]} -eq 0 ]] && warn "nothing recorded to restart"
  rm -f "$STATE_FILE"
  log "Rollback done. The new Jaeger container keeps running (it publishes no ports); stop it with: cd $INFRA_DIR && docker compose down"
}

# ---- steps ---------------------------------------------------------------------------------------------------------
preflight() {
  log "Checking prerequisites"
  [[ $EUID -eq 0 ]] || die "run as root: sudo $0"
  for f in "${CONFIG_FILES[@]}" sites/README.md; do
    [[ -f "$SRC_DIR/$f" ]] || die "missing $SRC_DIR/$f; run the script from the repo's deploy/infra folder"
  done
  if ! have docker; then
    warn "Docker is not installed"
    confirm "Install Docker Engine with the official script (https://get.docker.com)?" || die "Docker is required"
    have curl || die "curl is required to install Docker"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
  fi
  docker info >/dev/null 2>&1 || die "Docker is installed but not running (systemctl start docker)"
  docker compose version >/dev/null 2>&1 || die "the Docker compose plugin is missing (apt install docker-compose-plugin)"
  have curl || die "curl is required"
  ok "docker $(docker version --format '{{.Server.Version}}'), $(docker compose version --short 2>/dev/null || echo compose)"
}

open_firewall() {
  [[ $FIREWALL -eq 1 ]] || { log "Skipping firewall (--no-firewall)"; return; }
  log "Opening ports 80/tcp, 443/tcp, 443/udp in the VM firewall"
  if have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-service=http --add-service=https --add-port=443/udp >/dev/null
    firewall-cmd --reload >/dev/null
    ok "firewalld updated"
  elif have iptables; then
    local rule proto port pos
    for rule in "tcp 80" "tcp 443" "udp 443"; do
      read -r proto port <<<"$rule"
      if iptables -C INPUT -p "$proto" --dport "$port" -j ACCEPT 2>/dev/null; then
        ok "$port/$proto already allowed"
        continue
      fi
      # Oracle images end INPUT with a REJECT rule; insert before it.
      pos="$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')"
      if [[ -n "$pos" ]]; then
        iptables -I INPUT "$pos" -p "$proto" --dport "$port" -j ACCEPT
      else
        iptables -A INPUT -p "$proto" --dport "$port" -j ACCEPT
      fi
      ok "allowed $port/$proto"
    done
    if have netfilter-persistent; then
      netfilter-persistent save >/dev/null 2>&1 && ok "rules saved (netfilter-persistent)"
    else
      warn "netfilter-persistent not found; rules last until reboot (apt install iptables-persistent)"
    fi
  else
    warn "no firewalld or iptables found; skipped"
  fi
  warn "also allow TCP 80, 443 (and UDP 443) in the Oracle Console → VCN → Security List; that can't be done from the VM"
}

install_files() {
  log "Installing configs to $INFRA_DIR"
  docker network inspect edge >/dev/null 2>&1 || { docker network create edge >/dev/null; ok "created Docker network edge"; }
  install -d -m 755 "$INFRA_DIR" "$INFRA_DIR/sites"
  local f ts
  ts="$(date +%Y%m%d-%H%M%S)"
  for f in "${CONFIG_FILES[@]}"; do
    if [[ -f "$INFRA_DIR/$f" ]] && ! cmp -s "$SRC_DIR/$f" "$INFRA_DIR/$f"; then
      install -d "$INFRA_DIR/backups"
      cp -p "$INFRA_DIR/$f" "$INFRA_DIR/backups/$f.$ts"
      ok "backed up changed $f to backups/$f.$ts"
    fi
    install -m 644 "$SRC_DIR/$f" "$INFRA_DIR/$f"
  done
  install -m 644 "$SRC_DIR/sites/README.md" "$INFRA_DIR/sites/README.md"
  ok "configs in place"
}

set_env_key() {  # set_env_key KEY VALUE: replace or append a line in .env
  local file="$INFRA_DIR/.env" key="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -v "^${key}=" "$file" > "$tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

env_value() { grep -E "^$1=" "$INFRA_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true; }

configure_env() {
  log "Configuring $INFRA_DIR/.env"
  local env_file="$INFRA_DIR/.env" answer
  [[ -f "$env_file" ]] || { : > "$env_file"; ok "created .env"; }
  chmod 600 "$env_file"

  local current_jaeger current_otel current_token
  current_jaeger="$(env_value JAEGER_DOMAIN)"
  current_otel="$(env_value OTEL_DOMAIN)"
  current_token="$(env_value OTEL_INGEST_TOKEN)"
  [[ "$current_token" == "<token>" ]] && current_token=""

  if [[ -z "$JAEGER_DOMAIN" ]]; then
    JAEGER_DOMAIN="${current_jaeger:-$DEFAULT_JAEGER_DOMAIN}"
    if [[ -z "$current_jaeger" && $ASSUME_YES -eq 0 ]]; then
      read -r -p "    Jaeger UI hostname [$JAEGER_DOMAIN]: " answer; JAEGER_DOMAIN="${answer:-$JAEGER_DOMAIN}"
    fi
  fi
  if [[ -z "$OTEL_DOMAIN" ]]; then
    OTEL_DOMAIN="${current_otel:-$DEFAULT_OTEL_DOMAIN}"
    if [[ -z "$current_otel" && $ASSUME_YES -eq 0 ]]; then
      read -r -p "    OTLP ingest hostname [$OTEL_DOMAIN]: " answer; OTEL_DOMAIN="${answer:-$OTEL_DOMAIN}"
    fi
  fi
  if [[ -z "$OTEL_INGEST_TOKEN" ]]; then
    OTEL_INGEST_TOKEN="$current_token"
    if [[ -z "$OTEL_INGEST_TOKEN" ]]; then
      [[ $ASSUME_YES -eq 1 ]] && die "OTEL_INGEST_TOKEN is required (--otel-token, or set it in $env_file)"
      read -r -s -p "    OTLP ingest token (reuse your existing one; empty = generate a new one): " answer; echo
      OTEL_INGEST_TOKEN="${answer:-$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 43)}"
      [[ -n "$answer" ]] || warn "generated a new token; update your other projects' OTEL_EXPORTER_OTLP_HEADERS"
    fi
  fi
  set_env_key JAEGER_DOMAIN "$JAEGER_DOMAIN"
  set_env_key OTEL_DOMAIN "$OTEL_DOMAIN"
  set_env_key OTEL_INGEST_TOKEN "$OTEL_INGEST_TOKEN"
  ok "JAEGER_DOMAIN=$JAEGER_DOMAIN, OTEL_DOMAIN=$OTEL_DOMAIN, OTEL_INGEST_TOKEN=(set)"
}

check_sites() {
  log "Checking project sites in $INFRA_DIR/sites"
  local sites=()
  shopt -s nullglob
  sites=("$INFRA_DIR"/sites/*.caddy)
  shopt -u nullglob
  if [[ ${#sites[@]} -eq 0 ]]; then
    warn "no *.caddy files: only $JAEGER_DOMAIN and $OTEL_DOMAIN will be served"
    warn "copy your site files first (e.g. deploy/caddy/api.caddy and the other sites from /etc/caddy/Caddyfile)"
    confirm "Continue without project sites?" || die "aborted; add site files to $INFRA_DIR/sites and re-run"
  else
    local s
    for s in "${sites[@]}"; do ok "$(basename "$s")"; done
  fi
}

validate_config() {
  log "Validating the Caddy configuration (nothing has been stopped yet)"
  docker pull -q "$CADDY_IMAGE" >/dev/null
  local out
  if ! out="$(docker run --rm --env-file "$INFRA_DIR/.env" \
        -v "$INFRA_DIR/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$INFRA_DIR/sites:/etc/caddy/sites:ro" \
        "$CADDY_IMAGE" caddy validate --config /etc/caddy/Caddyfile 2>&1)"; then
    printf '%s\n' "$out" | grep -iE "error" >&2 || printf '%s\n' "$out" >&2
    die "invalid Caddy configuration; fix $INFRA_DIR/Caddyfile or sites/*.caddy and re-run"
  fi
  ok "valid"
}

start_jaeger() {
  log "Starting Jaeger (no ports published, so it can run next to the old setup)"
  compose pull -q
  compose up -d jaeger >/dev/null
  local _ status
  for _ in $(seq 1 30); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q jaeger)" 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] && { ok "Jaeger healthy"; return; }
    sleep 2
  done
  compose logs --tail 30 jaeger >&2 || true
  die "Jaeger did not become healthy"
}

stop_old_setup() {
  log "Looking for the old Caddy/Jaeger"
  local units=() containers=() u c
  if have systemctl; then
    for u in caddy jaeger jaeger-all-in-one; do
      if has_systemd_unit "$u" && systemctl is-active --quiet "$u"; then units+=("$u"); fi
    done
  fi
  # Containers outside this stack that publish 80/443 or run a Jaeger image.
  while read -r c; do [[ -n "$c" ]] && containers+=("$c"); done < <(
    { docker ps --filter publish=80 -q; docker ps --filter publish=443 -q
      docker ps --format '{{.ID}} {{.Image}}' | awk 'tolower($2) ~ /jaeger/ {print $1}'; } | sort -u |
    while read -r id; do
      [[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id")" == "infra" ]] || echo "$id"
    done)

  if [[ ${#units[@]} -eq 0 && ${#containers[@]} -eq 0 ]]; then
    ok "nothing to stop"
  else
    for u in "${units[@]}"; do warn "host service: $u (will be stopped and disabled)"; done
    for c in "${containers[@]}"; do warn "container: $(docker inspect -f '{{.Name}} ({{.Config.Image}})' "$c" | sed 's#^/##') (will be stopped, not removed)"; done
    confirm "Stop these now? (short outage on ports 80/443 until the new Caddy is up)" || die "aborted; nothing was stopped"
    load_state
    for u in "${units[@]}"; do systemctl disable --now "$u" >/dev/null 2>&1; STOPPED_UNITS+=("$u"); ok "stopped and disabled $u"; done
    for c in "${containers[@]}"; do docker stop "$c" >/dev/null; STOPPED_CONTAINERS+=("$c"); ok "stopped $(docker inspect -f '{{.Name}}' "$c" | sed 's#^/##')"; done
    save_state
  fi

  # Anything else still holding 80/443 (e.g. nginx, a Caddy started by hand)?
  if have ss; then
    local busy
    busy="$(ss -ltnpH '( sport = :80 or sport = :443 )' 2>/dev/null | grep -v docker-proxy || true)"
    if [[ -n "$busy" ]]; then
      printf '%s\n' "$busy" >&2
      rollback
      die "ports 80/443 are still in use by the process(es) above; stop them and re-run"
    fi
  else
    warn "ss not found; can't check whether something else still holds ports 80/443"
  fi
}

start_caddy() {
  log "Starting Caddy"
  if ! compose up -d caddy >/dev/null; then
    compose logs --tail 30 caddy >&2 || true
    rollback
    die "Caddy failed to start; rolled back"
  fi
  # Picks up Caddyfile/site changes when the container already existed (bind mounts don't trigger a recreate).
  compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
  ok "Caddy running"
}

verify() {
  log "Verifying over HTTPS (certificates are issued on first use; this can take a minute)"
  local insecure=() code _
  [[ "$JAEGER_DOMAIN" == *.localhost ]] && insecure=(-k)   # local testing with Caddy's internal CA
  for _ in $(seq 1 24); do
    code="$(curl "${insecure[@]}" -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$JAEGER_DOMAIN/" || true)"
    [[ "$code" == "200" ]] && break
    sleep 5
  done
  if [[ "$code" != "200" ]]; then
    compose logs --tail 40 caddy >&2 || true
    warn "https://$JAEGER_DOMAIN returned '$code'. Common causes: DNS not pointing here, or ports 80/443 closed in the Oracle Security List"
    if confirm "Roll back to the old setup?"; then rollback; die "rolled back"; fi
    die "left the new setup running; fix the cause and re-run, or run: sudo $0 --rollback"
  fi
  ok "https://$JAEGER_DOMAIN → 200 (Jaeger UI)"
  code="$(curl "${insecure[@]}" -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "https://$OTEL_DOMAIN/v1/traces" || true)"
  if [[ "$code" == "401" ]]; then ok "https://$OTEL_DOMAIN/v1/traces without token → 401"
  else warn "OTLP without token returned $code (expected 401)"; fi
  code="$(curl "${insecure[@]}" -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
    -H "Authorization: Bearer $OTEL_INGEST_TOKEN" -H 'Content-Type: application/x-protobuf' "https://$OTEL_DOMAIN/v1/traces" || true)"
  if [[ "$code" == "200" ]]; then ok "https://$OTEL_DOMAIN/v1/traces with token → 200"
  else warn "OTLP with token returned $code (expected 200)"; fi
}

summary() {
  load_state
  log "Done"
  echo "    Stack:     cd $INFRA_DIR && docker compose ps"
  echo "    Jaeger UI: https://$JAEGER_DOMAIN"
  echo "    OTLP:      https://$OTEL_DOMAIN (Authorization: Bearer <OTEL_INGEST_TOKEN>)"
  if [[ ${#STOPPED_CONTAINERS[@]} -gt 0 || ${#STOPPED_UNITS[@]} -gt 0 ]]; then
    echo "    Rollback:  sudo $0 --rollback"
    [[ ${#STOPPED_CONTAINERS[@]} -gt 0 ]] && echo "    Once happy, remove the old containers: docker rm ${STOPPED_CONTAINERS[*]}"
    [[ " ${STOPPED_UNITS[*]} " == *" caddy "* ]] && echo "    Once happy, remove the host Caddy: sudo apt remove caddy (it is stopped and disabled now)"
  fi
  echo "    Next:      deploy the backend (deploy/README.md); site changes: edit sites/*.caddy and re-run this script"
}

# ---- main ------------------------------------------------------------------------------------------------------------
if [[ "$MODE" == "rollback" ]]; then
  [[ $EUID -eq 0 ]] || die "run as root: sudo $0 --rollback"
  rollback
  exit 0
fi

preflight
open_firewall
install_files
configure_env
check_sites
validate_config
start_jaeger
stop_old_setup
start_caddy
verify
summary
