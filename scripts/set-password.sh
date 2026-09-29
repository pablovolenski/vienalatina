#!/usr/bin/env bash
# Set a member's password directly, without email.
#
# The way back in. Every member's password_hash starts NULL, including the
# owner's, and the normal route to a first password is the invitation or
# "¿olvidaste tu contraseña?" — both of which go by email. If the mailbox is
# having a bad week and nobody can sign in, this is the only door left.
#
#   sudo bash scripts/set-password.sh pablo
#
# The password is typed at a prompt and never echoed, never passed as an
# argument, and never written to shell history. It is hashed by the same code
# the application uses, inside the running container, so there is no second
# implementation to drift.
#
# Run it from the repository, on the server. It needs the board container up.

set -euo pipefail

LOGIN="${1:-}"
SERVICE="${BOARD_SERVICE:-board}"
TARGET="${BOARD_DIR:-/srv/board}"

if [ -z "$LOGIN" ]; then
  echo "Usage: sudo bash scripts/set-password.sh <usuario>" >&2
  exit 1
fi

cd "$TARGET"

if ! docker compose ps --status running --services 2>/dev/null | grep -qx "$SERVICE"; then
  echo "The $SERVICE container is not running — start it first: docker compose up -d" >&2
  exit 1
fi

# -s so it is not echoed; the confirmation catches a typo that would otherwise
# lock the account this script exists to unlock.
read -rsp "Nueva contraseña para $LOGIN: " PASSWORD; echo
read -rsp "Repítela: " CONFIRM; echo
if [ "$PASSWORD" != "$CONFIRM" ]; then
  echo "No coinciden. Nada cambiado." >&2
  exit 1
fi

# Through the environment rather than the command line: an argument is visible
# in `ps` to every user on the box for as long as the process lives.
PASSWORD="$PASSWORD" docker compose exec -T -e PASSWORD "$SERVICE" python - "$LOGIN" <<'PY'
import os, sqlite3, sys
sys.path.insert(0, "/srv")
from apps.board.passwords import MINIMUM, hash_password

login, password = sys.argv[1], os.environ["PASSWORD"]
if len(password) < MINIMUM:
    sys.exit(f"La contraseña necesita al menos {MINIMUM} caracteres. Nada cambiado.")

db = sqlite3.connect(os.environ.get("BOARD_DB", "/data/board.db"), isolation_level=None)
changed = db.execute(
    """UPDATE members SET password_hash = ?
        WHERE gitea_login = ? COLLATE NOCASE
          AND role IN ('owner', 'admin', 'user')""",
    (hash_password(password), login),
).rowcount
if not changed:
    sys.exit(f"No hay ningún miembro activo llamado «{login}». Nada cambiado.")
print(f"Contraseña actualizada para «{login}». Ya puede entrar en /comunidad/.")
PY

unset PASSWORD CONFIRM
