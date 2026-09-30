#!/usr/bin/env bash
# Deploy one image tag of the backend on the VM. If it doesn't become healthy, roll back to the previous tag.
#
#   usage: ./remote-deploy.sh <image-tag>
#
# Runs in the directory holding docker-compose.prod.yml and .env (the workflow copies it to /opt/aviation).
# Exit code 0 = new tag is live; 1 = deploy failed (the previous tag is running again if there was one).
set -euo pipefail
cd "$(dirname "$0")"

NEW_TAG="${1:?usage: $0 <image-tag>}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
PULL_POLICY="${PULL_POLICY:-always}"  # "missing" to test with locally built images
IMAGES="ghcr.io/sathishkottravel/aviation-telemetry-service"
COMPOSE=(docker compose -f docker-compose.prod.yml)
PREVIOUS_TAG="$(cat .deployed-tag 2>/dev/null || true)"

deploy() {
  IMAGE_TAG="$1" "${COMPOSE[@]}" up -d --pull "$PULL_POLICY" --quiet-pull --remove-orphans \
    --wait --wait-timeout "$WAIT_TIMEOUT"
}

prune_old_images() {
  # Keep the current and previous tag (for rollback); touch only this project's images on the shared VM.
  docker images --format '{{.Repository}}:{{.Tag}}' | grep "^$IMAGES/" \
    | grep -v -e ":$NEW_TAG\$" -e ":${PREVIOUS_TAG:-none}\$" | xargs -r docker rmi >/dev/null || true
}

echo "Deploying $NEW_TAG (previous: ${PREVIOUS_TAG:-none})"
if deploy "$NEW_TAG"; then
  echo "$NEW_TAG" > .deployed-tag
  prune_old_images
  echo "Deployed $NEW_TAG"
  exit 0
fi

echo "Deploy of $NEW_TAG failed; state and recent logs:" >&2
IMAGE_TAG="$NEW_TAG" "${COMPOSE[@]}" ps >&2 || true
IMAGE_TAG="$NEW_TAG" "${COMPOSE[@]}" logs --tail 50 >&2 || true

if [[ -n "$PREVIOUS_TAG" && "$PREVIOUS_TAG" != "$NEW_TAG" ]]; then
  echo "Rolling back to $PREVIOUS_TAG" >&2
  if deploy "$PREVIOUS_TAG"; then
    echo "Rolled back to $PREVIOUS_TAG" >&2
  else
    echo "Rollback to $PREVIOUS_TAG failed as well" >&2
  fi
fi
exit 1
