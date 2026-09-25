#!/usr/bin/env bash
# Install (or reinstall) the Viena Latina theme for Gitea.
#
# Members sign in to /comunidad/ through Gitea, so Gitea's sign-in form and
# authorize dialog are part of the journey whether or not anyone ever browses a
# repository. This makes those two screens look like the rest of the platform.
#
#   bash scripts/gitea-theme.sh
#
# Run it again after every Gitea upgrade. The theme is built by concatenating
# Gitea's own light theme with our overrides, so the base has to come from the
# version that is actually installed — a new release can add variables, and a
# copy frozen in this repository would slowly drift out of date in ways nobody
# would notice until a page looked wrong.

set -euo pipefail

GITEA_DIR="${GITEA_DIR:-/srv/gitea}"
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE="${SERVICE:-gitea}"

OVERRIDES="$REPO_DIR/infra/gitea/theme-vienalatina.overrides.css"
LOGO="$REPO_DIR/infra/gitea/assets/logo.svg"

CUSTOM="$GITEA_DIR/data/gitea"        # GITEA_CUSTOM inside the container is /data/gitea
CSS_DIR="$CUSTOM/public/assets/css"
IMG_DIR="$CUSTOM/public/assets/img"

for file in "$OVERRIDES" "$LOGO"; do
  [ -f "$file" ] || { echo "Missing $file" >&2; exit 1; }
done

echo "== reading the installed Gitea's light theme"
cd "$GITEA_DIR"
BASE="$(docker compose exec -T "$SERVICE" \
          cat /app/gitea/public/assets/css/theme-gitea-light.css)"

if [ -z "$BASE" ]; then
  echo "Could not read Gitea's own theme — is the container running?" >&2
  echo "Check with: cd $GITEA_DIR && docker compose ps" >&2
  exit 1
fi

sudo mkdir -p "$CSS_DIR" "$IMG_DIR"

echo "== writing theme-vienalatina.css"
{
  printf '/* Built by scripts/gitea-theme.sh on %s.\n' "$(date -u +%FT%TZ)"
  printf '   Gitea theme-gitea-light.css + infra/gitea/theme-vienalatina.overrides.css\n'
  printf '   Do not edit here: rerun the script. */\n'
  printf '%s\n' "$BASE"
  cat "$OVERRIDES"
} | sudo tee "$CSS_DIR/theme-vienalatina.css" > /dev/null

echo "== writing the logo and favicon"
# logo.svg is the header mark, favicon.svg the tab icon. Gitea also looks for
# PNG fallbacks; without them it falls back to its own, which is why the tab
# can still show a cup in older browsers. Converting needs a tool this box does
# not have, and an SVG favicon covers everything current.
sudo cp "$LOGO" "$IMG_DIR/logo.svg"
sudo cp "$LOGO" "$IMG_DIR/favicon.svg"

sudo chown -R 1000:1000 "$CUSTOM/public"

echo
echo "Installed. Now make it the default (once):"
echo "  add  GITEA__ui__DEFAULT_THEME=vienalatina  to $GITEA_DIR/docker-compose.yml"
echo "  then cd $GITEA_DIR && sudo docker compose up -d"
echo
echo "Already set? A restart is enough to pick up the new file:"
echo "  cd $GITEA_DIR && sudo docker compose restart $SERVICE"
