#!/usr/bin/env bash
# Build and push ACE Lap Tracker Docker images to Docker Hub.
#
# Usage:
#   ./build-and-push.sh                 # builds all three images, latest only
#   ./build-and-push.sh v1.1.0          # builds all three, tags :latest + :v1.1.0
#   ./build-and-push.sh v1.1.0 frontend # builds only the frontend image
#
# Prerequisites:
#   docker login  (run once before using this script)

set -euo pipefail

REGISTRY="apostle818"
APP="ace-laptimes"
VERSION="${1:-}"
SERVICE="${2:-all}"
PLATFORM="linux/amd64"

SERVICES=("backend" "frontend" "nginx")

build_and_push() {
  local svc="$1"
  local image="${REGISTRY}/${APP}-${svc}"
  local tags=("-t" "${image}:latest")

  if [[ -n "$VERSION" ]]; then
    tags+=("-t" "${image}:${VERSION}")
  fi

  echo ""
  echo "▶ Building ${image}..."
  docker buildx build \
    --platform "$PLATFORM" \
    "${tags[@]}" \
    --push \
    "./${svc}"

  echo "✓ Pushed ${image}:latest${VERSION:+ and ${image}:${VERSION}}"
}

echo "ACE Lap Tracker — build & push"
echo "Platform : $PLATFORM"
echo "Version  : ${VERSION:-latest only}"
echo "Service  : ${SERVICE}"
echo ""

if [[ "$SERVICE" == "all" ]]; then
  for svc in "${SERVICES[@]}"; do
    build_and_push "$svc"
  done
else
  build_and_push "$SERVICE"
fi

echo ""
echo "Done. To deploy on your server:"
echo "  docker compose pull && docker compose up -d"
