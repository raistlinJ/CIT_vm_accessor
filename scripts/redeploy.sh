#!/usr/bin/env bash
# Author: Jaime Acosta
set -euo pipefail

# Redeploy the docker compose stack:
# - Stops and removes containers
# - Removes the app image (proxclient:local by default)
# - Pulls latest base images
# - Builds fresh
# - Starts stack

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Choose docker compose command (v1 or v2)
if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
elif docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
else
  echo "Error: docker compose not found (install Docker Desktop or docker-compose)." >&2
  exit 1
fi

cd "$ROOT_DIR"

# Defaults; can override via env: IMAGE=your/repo:tag
IMAGE="${IMAGE:-proxclient:local}"

echo "[redeploy] Using compose: $COMPOSE"
echo "[redeploy] Project root: $ROOT_DIR"

echo "[redeploy] Bringing down containers..."
$COMPOSE down --remove-orphans || true

echo "[redeploy] Removing image $IMAGE (if present)..."
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker rmi -f "$IMAGE" || true
else
  echo "[redeploy] Image $IMAGE not present; skipping remove."
fi

echo "[redeploy] Pulling images (base/remote)..."
$COMPOSE pull --ignore-pull-failures || true

echo "[redeploy] Building images..."
$COMPOSE build --pull

echo "[redeploy] Starting services..."
$COMPOSE up -d

echo "[redeploy] Current status:"
$COMPOSE ps

echo "[redeploy] Done."
