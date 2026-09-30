#!/usr/bin/env bash
# Deploy the backend on the VM by hand, checking everything it needs first.
#
#   ./deploy/deploy.sh pull [sha]   # use the images CI built (default: this checkout's commit)
#   ./deploy/deploy.sh build        # build the images here on the VM
#
# Run from a clone of the repo on the VM, as a user in the docker group. Settings live in /opt/aviation/.env
# (template: deploy/.env.prod.example). Override with DEST=... or API_DOMAIN=... if needed.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${DEST:-/opt/aviation}"
API_DOMAIN="${API_DOMAIN:-api.sathishkottravel.com}"
IMAGES=ghcr.io/sathishkottravel/aviation-telemetry-service
MODE="${1:-}"
FAILED=0

step() { printf '\n==> %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*"; FAILED=1; }
stop_if_failed() { [[ $FAILED -eq 0 ]] || { printf '\nStopped: fix the ✗ items above and re-run. Nothing was deployed.\n'; exit 1; }; }

if [[ "$MODE" != pull && "$MODE" != build ]]; then
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
fi

step "1/6 Tools"
if docker info >/dev/null 2>&1; then ok "docker"; else fail "docker is not running, or $(id -un) is not in the docker group"; fi
if docker compose version >/dev/null 2>&1; then ok "docker compose"; else fail "docker compose missing: sudo apt install -y docker-compose-v2"; fi
if command -v curl >/dev/null; then ok "curl"; else fail "curl missing: sudo apt install -y curl"; fi
stop_if_failed

step "2/6 VM infrastructure (deploy/infra)"
if docker network inspect edge >/dev/null 2>&1; then ok "network edge"; else fail "network edge missing: run deploy/infra/setup-infra.sh first"; fi
on_edge="$(docker ps --filter network=edge --format '{{.Names}}' 2>/dev/null || true)"
if grep -q jaeger <<<"$on_edge"; then ok "jaeger running on edge"; else warn "no jaeger container on edge: traces will be dropped"; fi
if grep -q caddy <<<"$on_edge"; then ok "caddy running on edge"; else warn "no caddy container on edge: https://$API_DOMAIN won't reach the api"; fi
if [[ -d "$DEST" && -w "$DEST" ]]; then ok "$DEST is writable"; else fail "$DEST missing or not writable: sudo install -d -o $(id -un) $DEST"; fi
stop_if_failed

step "3/6 Settings in $DEST/.env"
ENV="$DEST/.env"
[[ -f "$ENV" ]] || { fail "missing: cp $REPO/deploy/.env.prod.example $ENV && chmod 600 $ENV, then fill it in"; stop_if_failed; }
[[ "$(stat -c %a "$ENV")" == 600 ]] || warn "$ENV should be private: chmod 600 $ENV"
get() {  # KEY's value as compose reads it: last line wins, CR (Windows editors) and surrounding quotes stripped
  grep -E "^$1=" "$ENV" | tail -1 | cut -d= -f2- | tr -d '\r' | sed -E "s/^([\"'])(.*)\1\$/\2/" || true
}
need() {  # need KEY [regex]: set, not a <placeholder>, optionally matching a pattern
  local v; v="$(get "$1")"
  if [[ -z "$v" || "$v" == *"<"* ]]; then fail "$1 is missing or still a placeholder"
  elif [[ -n "${2:-}" && ! "$v" =~ $2 ]]; then fail "$1 doesn't look right (expected ${3:-pattern $2})"
  else ok "$1"; fi
}
need MONGODB_URI '^mongodb(\+srv)?://' "mongodb:// or mongodb+srv://"
need RABBITMQ_URL '^amqps?://' "amqp:// or amqps://"
need API_TOKEN '^.{16,}$' "at least 16 characters"
need ADMIN_TOKEN '^.{16,}$' "at least 16 characters"
if [[ -n "$(get API_TOKEN)" && "$(get API_TOKEN)" == "$(get ADMIN_TOKEN)" ]]; then fail "API_TOKEN and ADMIN_TOKEN must differ"; fi
if [[ "$(get OTEL_ENABLED)" == true ]]; then need OTEL_EXPORTER_OTLP_ENDPOINT '^https?://' "an http(s) URL, e.g. http://jaeger:4318"; else warn "OTEL_ENABLED is not true: no traces"; fi
stop_if_failed

step "4/6 Images"
if [[ "$MODE" == pull ]]; then
  TAG="${2:-$(git -C "$REPO" rev-parse HEAD)}"
  PULL_POLICY=always
  ARCH="$(docker version -f '{{.Server.Arch}}')"
  for s in api worker producer; do
    if ! manifest="$(docker manifest inspect -v "$IMAGES/$s:$TAG" 2>/dev/null)"; then
      fail "$s:$TAG not found in GHCR (CI for that commit not finished, or the package isn't public yet)"
    elif ! grep -q "\"architecture\": \"$ARCH\"" <<<"$manifest"; then
      fail "$s:$TAG has no $ARCH image (built for another CPU): use ./deploy/deploy.sh build, or a newer commit"
    else ok "$s:$TAG in GHCR ($ARCH)"; fi
  done
  stop_if_failed
else
  TAG="local-$(git -C "$REPO" rev-parse --short HEAD)"
  PULL_POLICY=missing
  for s in api:api-service worker:telemetry-worker producer:adsb-producer; do
    printf '  building %s ...\n' "${s%%:*}"
    docker build -q -f "$REPO/${s#*:}/Dockerfile" -t "$IMAGES/${s%%:*}:$TAG" "$REPO" >/dev/null
    ok "${s%%:*}:$TAG built"
  done
fi

step "5/6 Deploy $TAG"
cp "$REPO/deploy/docker-compose.prod.yml" "$REPO/deploy/remote-deploy.sh" "$DEST/"
(cd "$DEST" && PULL_POLICY="$PULL_POLICY" bash ./remote-deploy.sh "$TAG")

step "6/6 Verify https://$API_DOMAIN"
k=(); [[ "$API_DOMAIN" == *.localhost ]] && k=(-k)   # local testing with Caddy's internal CA
code() { curl "${k[@]}" -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }
check() {  # check LABEL EXPECTED CURL_ARGS...
  local label="$1" want="$2" got; shift 2; got="$(code "$@")"
  if [[ "$got" == "$want" ]]; then ok "$label → $want"; else fail "$label returned ${got:-no response}, expected $want"; fi
}
query='{"query":"{ __typename }"}'
json=(-H 'Content-Type: application/json' -d "$query")
check "/health" 200 "https://$API_DOMAIN/health"
check "GraphQL with API_TOKEN" 200 -H "Authorization: Bearer $(get API_TOKEN)" "${json[@]}" "https://$API_DOMAIN/graphql"
check "GraphQL without a token" 401 "${json[@]}" "https://$API_DOMAIN/graphql"
if [[ $FAILED -eq 0 ]]; then
  printf '\nDeployed %s. Traces: look for flight-telemetry-api/-worker/-producer in the Jaeger UI.\n' "$TAG"
else
  printf '\nDeployed %s, but some checks failed (see ✗ above). Roll back: cd %s && ./remote-deploy.sh <previous tag>\n' "$TAG" "$DEST"
  exit 1
fi
