#!/usr/bin/env bash
# Put the current checkout of the members area onto the server.
#
# The app's code is baked into the image by docker/board/Dockerfile (`COPY apps
# /srv/apps`), so pulling new commits changes nothing on its own — the container
# keeps running the code that was in the image when it was built. `docker
# compose up -d --force-recreate` does not help either: same tag, same layers,
# same old code. That is a quiet failure, because everything reports success and
# the site behaves exactly as it did before.
#
# This script is the whole update, in the order that matters:
#
#   git pull                                  # done by you, first
#   sudo bash scripts/deploy-board.sh
#
# It never touches /srv/board/.env. That file holds the secrets and exists only
# on the server; the repository has .env.example instead, and the two drifting
# apart is expected — new settings appear in the example and have to be copied
# across by hand.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${BOARD_DIR:-/srv/board}"
IMAGE="vienalatina/board:1"

cd "$REPO"

echo "==> Building $IMAGE from $(git rev-parse --short HEAD)"
docker build -t "$IMAGE" -f docker/board/Dockerfile .

echo "==> Syncing compose files into $TARGET (never .env)"
mkdir -p "$TARGET"
cp "$REPO/infra/board/docker-compose.yml" "$TARGET/docker-compose.yml"
cp "$REPO/infra/board/.env.example" "$TARGET/.env.example"

if [ ! -f "$TARGET/.env" ]; then
  echo "No $TARGET/.env — copy .env.example to .env and fill it in first." >&2
  exit 1
fi

# Settings that appear in the example and not in the live file. Nothing is
# copied automatically: some of them are secrets, and a blank line silently
# added to .env is worse than a line missing loudly.
missing="$(comm -23 \
  <(grep -oE '^[A-Z][A-Z0-9_]*=' "$TARGET/.env.example" | sort -u) \
  <(grep -oE '^[A-Z][A-Z0-9_]*=' "$TARGET/.env"         | sort -u) || true)"
if [ -n "$missing" ]; then
  echo
  echo "!! These settings exist in .env.example but not in your .env:"
  echo "$missing" | sed 's/^/     /'
  echo "   Add them to $TARGET/.env and run this again if the feature needs them."
  echo
fi

echo "==> Restarting"
cd "$TARGET"
docker compose up -d --force-recreate

# The schema is applied at start-up with CREATE TABLE IF NOT EXISTS, so a new
# table arrives with the new code. If the container is not up a few seconds
# later it died during that, and the log says why.
sleep 3
docker compose ps
echo
echo "Recent log:"
docker compose logs --tail 20 board
