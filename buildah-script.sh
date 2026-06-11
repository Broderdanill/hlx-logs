#!/usr/bin/env bash
set -Eeuo pipefail

# Build hlx-logs with Buildah.
#
# Defaults are aligned with deploy/podman-play-kube.yaml:
#   image: localhost/hlx-logs:latest
#
# Usage examples:
#   ./buildah-script.sh
#   IMAGE_TAG=0.0.20 ./buildah-script.sh
#   IMAGE_NAME=localhost/hlx-logs IMAGE_TAG=test NO_CACHE=true ./buildah-script.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT_DIR="${CONTEXT_DIR:-$SCRIPT_DIR}"
CONTAINERFILE="${CONTAINERFILE:-$CONTEXT_DIR/Containerfile}"
IMAGE_NAME="${IMAGE_NAME:-localhost/hlx-logs}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_REF="${IMAGE_NAME}:${IMAGE_TAG}"
NO_CACHE="${NO_CACHE:-false}"
PULL="${PULL:-true}"

log() {
  printf '[buildah] %s\n' "$*"
}

fail() {
  printf '[buildah] ERROR: %s\n' "$*" >&2
  exit 1
}

command -v buildah >/dev/null 2>&1 || fail "buildah was not found in PATH"
[[ -f "$CONTAINERFILE" ]] || fail "Containerfile not found: $CONTAINERFILE"
[[ -f "$CONTEXT_DIR/requirements.txt" ]] || fail "requirements.txt not found in context: $CONTEXT_DIR"
[[ -d "$CONTEXT_DIR/app" ]] || fail "app directory not found in context: $CONTEXT_DIR/app"

BUILD_ARGS=(
  bud
  --format docker
  --layers
  -f "$CONTAINERFILE"
  -t "$IMAGE_REF"
)

if [[ "$NO_CACHE" == "true" || "$NO_CACHE" == "1" || "$NO_CACHE" == "yes" ]]; then
  BUILD_ARGS+=(--no-cache)
fi

if [[ "$PULL" == "false" || "$PULL" == "0" || "$PULL" == "no" ]]; then
  BUILD_ARGS+=(--pull-never)
else
  BUILD_ARGS+=(--pull)
fi

BUILD_ARGS+=("$CONTEXT_DIR")

log "Context       : $CONTEXT_DIR"
log "Containerfile : $CONTAINERFILE"
log "Image         : $IMAGE_REF"
log "No cache      : $NO_CACHE"
log "Pull base     : $PULL"

buildah "${BUILD_ARGS[@]}"

log "Build complete: $IMAGE_REF"
log "Verify image:  podman images | grep hlx-logs"
log "Run with:      podman play kube deploy/podman-play-kube.yaml"
