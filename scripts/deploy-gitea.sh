#!/usr/bin/env bash
# Put this repository's Gitea settings onto the server, and prove they landed.
#
# This script exists because there was no way to do that. `deploy-board.sh`
# syncs the members area; `/srv/gitea/` has been a hand-made copy since the day
# it was set up, so every Gitea setting added to `infra/gitea/` since then has
# been written, committed, documented — and never applied. The symptom is the
# quietest possible one: the file on the server is valid, the container is
# healthy, and the setting simply is not there.
#
#   cd ~/vienalatina && git pull ...
#   sudo bash scripts/deploy-gitea.sh
#
# It never touches /srv/gitea/data — that is the database, the repositories and
# app.ini, none of which belong to this repository.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${GITEA_DIR:-/srv/gitea}"

if [ ! -d "$TARGET" ]; then
  echo "No $TARGET — this script updates an existing install, it does not create one." >&2
  exit 1
fi

echo "==> Syncing compose settings into $TARGET"
# A backup, because unlike the board's this file may have been edited by hand on
# the server, and that edit is about to be overwritten. If anything in the diff
# below is a surprise, it was a local change nobody wrote down.
if [ -f "$TARGET/docker-compose.yml" ]; then
  cp "$TARGET/docker-compose.yml" "$TARGET/docker-compose.yml.bak"
  if ! diff -u "$TARGET/docker-compose.yml.bak" "$REPO/infra/gitea/docker-compose.yml"; then
    echo "   (differences above; previous file kept as docker-compose.yml.bak)"
  fi
fi
cp "$REPO/infra/gitea/docker-compose.yml" "$TARGET/docker-compose.yml"

echo "==> Restarting"
cd "$TARGET"
docker compose up -d

# Settling time. environment-to-ini rewrites app.ini during start-up, so
# reading it back immediately can catch the previous file.
sleep 5

echo
echo "==> What the container actually has (not what we sent it)"
# The whole point of this script. Every failure this session has been a setting
# that was accepted somewhere and read by nobody, so the last word belongs to
# the running container rather than to a file we just copied.
docker compose exec -T gitea sh -c '
  echo "--- APP_NAME ---"
  grep -m1 "^APP_NAME" /data/gitea/conf/app.ini || echo "APP_NAME: not set (Gitea will show its own name)"
  echo "--- [other] ---"
  sed -n "/^\[other\]/,/^\[/p" /data/gitea/conf/app.ini | grep -v "^\[" || echo "no [other] section"
  echo "--- sign-in extras ---"
  grep -E "^(ENABLE_OPENID_SIGNIN|SHOW_REGISTRATION_BUTTON|DEFAULT_THEME)" /data/gitea/conf/app.ini \
    || echo "none of ENABLE_OPENID_SIGNIN / SHOW_REGISTRATION_BUTTON / DEFAULT_THEME are set"
' || echo "!! Could not read app.ini — is the container up? docker compose logs gitea"

echo
echo "Expected: APP_NAME = Viena Latina, SHOW_FOOTER_POWERED_BY = false,"
echo "SHOW_FOOTER_VERSION = false, ENABLE_OPENID_SIGNIN = false,"
echo "SHOW_REGISTRATION_BUTTON = false, DEFAULT_THEME = vienalatina."
echo
echo "A line that is missing above is a setting that is NOT in effect, whatever"
echo "the compose file says. If DEFAULT_THEME is set but the screens are still"
echo "grey, the theme file was never built: bash scripts/gitea-theme.sh"
