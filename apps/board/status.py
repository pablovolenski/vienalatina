"""Estado del sistema — what is actually true of this server right now.

WordPress calls it Site Health. It earns its place here for a specific reason:
**every outage in this project has been a configuration fact that was invisible
until somebody hit it.** A `CONTENT_TOKEN` with the wrong scope looked exactly
like a working one until an editor pressed save and got a 500. Mail was
unconfigured for a week while invitations were "sent". The settings were right
in `.env.example` and absent from the container.

So this page asks the questions nobody thinks to ask, and asks them of the
running system rather than of a file:

* the content token is **exercised**, not merely checked for emptiness — the
  single most valuable line here, because "present" was never the problem
* the mail host is resolved and connected to
* the newest backup, with its age and size
* free disk, which is the one that ends everything at once
* every setting `.env.example` documents that the container does not have

**It changes nothing.** No buttons, no fixes, no restarts — a page that answers
questions. That is deliberate: a diagnostic screen that can also act is a screen
somebody uses to act without reading, and the actions here are the ones with the
longest reach on the box.
"""

from __future__ import annotations

import os
import shutil
import smtplib
import socket
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, render_template

from . import gitea, mail, uploads
from .db import get_db
from .security import superadmin_required

bp = Blueprint("status", __name__)

# Where the compose file mounts them, read-only. **Not** the host path: this
# runs in a container, and `/srv/board/backups` exists on the host and nowhere
# inside it. Reading the host path from in here reported "no hay carpeta de
# copias" twenty seconds after deploy-board.sh had written one — a diagnostic
# page crying wolf about backups is a page you learn to scroll past, which is
# the one habit it exists to prevent.
BACKUP_DIR = "/backups"

# Settings whose absence matters, and the config key each one lands in — so the
# value actually in force is read from the app rather than restated here, where
# the two could disagree.
#
# The third column separates the two facts this panel used to merge: **absent
# and nothing works** from **absent and defaulted**. `CONTENT_REPO` is unset on
# the real server and publishing works, because app.py defaults it; reporting
# that as a problem is how a page full of orange teaches you to ignore orange.
# A config key of None means there is no fallback and the feature is simply off.
#
# Checked against os.environ because the container cannot read /srv/board/.env —
# compose substitutes it in and the file itself never arrives.
EXPECTED = (
    ("BOARD_SECRET_KEY", "las sesiones (sin esto la app no arranca)", None),
    ("CONTENT_TOKEN", "publicar en el sitio: el editor y la marca", None),
    ("MAIL_HOST", "invitaciones y recuperar contraseña", None),
    ("MAIL_USER", "autenticarse en el servidor de correo", None),
    ("MAIL_PASSWORD", "autenticarse en el servidor de correo", None),
    ("BOARD_OWNER", "sembrar al superadministrador en el primer arranque", None),
    ("GITEA_URL", "hablar con el servidor de git", "GITEA_URL"),
    ("CONTENT_REPO", "saber en qué repositorio se publica", "CONTENT_REPO"),
)


def _ok(label, detail=""):
    return {"state": "ok", "label": label, "detail": detail}


def _warn(label, detail=""):
    return {"state": "warn", "label": label, "detail": detail}


def _bad(label, detail=""):
    return {"state": "bad", "label": label, "detail": detail}


def _content_token() -> dict:
    """Not "is it set" — "does it work".

    A token with `admin` scope and no `repository` scope is present, non-empty,
    and cannot write a single file; that exact configuration broke publishing
    for three phases while every check anybody thought to run said it was fine.
    So this one reads the content directory through the API and reports what
    came back.
    """
    try:
        token = gitea.content_token()
    except gitea.GiteaError as exc:
        return _bad("No hay token para publicar", str(exc))

    which = "CONTENT_TOKEN" if os.environ.get("CONTENT_TOKEN") else "GITEA_ADMIN_TOKEN"
    try:
        gitea.list_directory("content/post", token)
    except gitea.GiteaError as exc:
        return _bad(f"El token ({which}) no puede leer el repositorio", str(exc))
    except OSError as exc:
        return _warn("No se pudo contactar con el servidor de git", str(exc))

    if which == "GITEA_ADMIN_TOKEN":
        return _warn(
            "Publicando con el token de administración",
            "Funciona, pero ese token puede modificar cualquier cuenta del "
            "servidor de git. Crea uno con permiso de repositorio y ponlo en "
            "CONTENT_TOKEN.")
    return _ok("El token de publicación funciona", f"{which}, con acceso de lectura")


def _mail() -> dict:
    if not mail.configured():
        return _warn("Sin correo saliente",
                     "Nadie puede ser invitado ni recuperar su contraseña.")
    host = current_app.config["MAIL_HOST"]
    port = int(current_app.config["MAIL_PORT"] or 587)
    try:
        with smtplib.SMTP(host, port, timeout=5) as server:
            server.ehlo()
    except (OSError, smtplib.SMTPException) as exc:
        return _bad(f"{host}:{port} no responde", str(exc))
    return _ok(f"{host}:{port} responde", "La conexión se abre; no se envía nada.")


def _backups() -> dict:
    folder = Path(BACKUP_DIR)
    if not folder.is_dir():
        return _warn("No hay carpeta de copias", f"{BACKUP_DIR} no existe.")
    files = sorted(folder.glob("board-*.db.gz"), key=lambda f: f.stat().st_mtime)
    if not files:
        return _bad("Ninguna copia de seguridad", f"{BACKUP_DIR} está vacía.")
    newest = files[-1]
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        newest.stat().st_mtime, timezone.utc)
    size = newest.stat().st_size // 1024
    detail = f"{newest.name}, {size} KB, {len(files)} en total"
    if age.days >= 2:
        return _warn(f"La copia más reciente tiene {age.days} días", detail)
    return _ok("Copia reciente", detail)


def _disk() -> dict:
    """The one that ends everything at once, and silently: SQLite cannot write,
    uploads fail, and the log that would say so cannot be written either."""
    # Through `uploads.directory()`, which creates it: asking statvfs about a
    # path that does not exist raises, and "the uploads folder has not been made
    # yet" is not a disk problem.
    usage = shutil.disk_usage(uploads.directory())
    free_mb = usage.free // (1024 * 1024)
    detail = f"{free_mb} MB libres de {usage.total // (1024 * 1024)} MB"
    if free_mb < 200:
        return _bad("Queda muy poco disco", detail)
    if free_mb < 1000:
        return _warn("Queda poco disco", detail)
    return _ok("Disco suficiente", detail)


def _settings() -> dict:
    """Which expected settings the container does not have, and whether it
    matters — which are two different questions."""
    missing, defaulted = [], []
    for name, why, key in EXPECTED:
        if os.environ.get(name):
            continue
        if key:
            defaulted.append(f"{name} — usando «{current_app.config.get(key)}» "
                             "(valor por defecto)")
        else:
            missing.append(f"{name} — {why}")

    detail = "; ".join(missing + defaulted)
    if missing:
        return _warn(
            f"{len(missing)} ajuste{'s' if len(missing) > 1 else ''} sin poner",
            detail)
    if defaulted:
        # Green: nothing is broken. The values are still named, because "it
        # works and here is the value it is working with" is the answer, and
        # silence would leave somebody wondering which repository it publishes
        # to.
        return _ok("Todos los ajustes necesarios están puestos", detail)
    return _ok("Todos los ajustes esperados están puestos")


def _counts() -> dict:
    db = get_db()
    one = lambda sql: db.execute(sql).fetchone()[0]           # noqa: E731
    return {
        "members": one("SELECT COUNT(*) FROM members WHERE role != 'tombstone'"),
        "admins": one("SELECT COUNT(*) FROM members WHERE role IN "
                      "('superadmin', 'owner', 'admin', 'moderator')"),
        # `role != 'tombstone'` because the row erased members are reassigned
        # to is inactive by construction and is not a suspended person.
        "suspended": one("SELECT COUNT(*) FROM members "
                         "WHERE active = 0 AND role != 'tombstone'"),
        "threads": one("SELECT COUNT(*) FROM threads WHERE deleted_at IS NULL"),
        "pending": one("SELECT COUNT(*) FROM submissions WHERE state = 'pending'"),
        "events": one("SELECT COUNT(*) FROM events"),
        "posts": one("SELECT COUNT(*) FROM content_cache WHERE generated = 0"),
    }


@bp.route("/gestion/estado")
@superadmin_required
def index():
    checks = [
        ("Publicar en el sitio", _content_token()),
        ("Correo saliente", _mail()),
        ("Copias de seguridad", _backups()),
        ("Disco", _disk()),
        ("Ajustes del contenedor", _settings()),
    ]
    return render_template("status.html", checks=checks, counts=_counts(),
                           worst=min((c[1]["state"] for c in checks),
                                     key=["bad", "warn", "ok"].index))
