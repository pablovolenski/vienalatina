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

from . import gitea, mail, strings, uploads
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
    ("BOARD_SECRET_KEY", None),
    ("CONTENT_TOKEN", None),
    ("MAIL_HOST", None),
    ("MAIL_USER", None),
    ("MAIL_PASSWORD", None),
    ("BOARD_OWNER", None),
    ("GITEA_URL", "GITEA_URL"),
    ("CONTENT_REPO", "CONTENT_REPO"),
)


def _why() -> dict[str, str]:
    """What each setting is for, read per request rather than at import.

    Spelled out here rather than kept in the table above: a key sitting in a
    tuple is a key the orphan guard cannot see, and it said so. Two settings
    share a sentence because they are two halves of one credential.
    """
    mail_credentials = strings.t("gestion.ajuste_mail_credenciales")
    return {
        "BOARD_SECRET_KEY": strings.t("gestion.ajuste_secret_key"),
        "CONTENT_TOKEN": strings.t("gestion.ajuste_content_token"),
        "MAIL_HOST": strings.t("gestion.ajuste_mail_host"),
        "MAIL_USER": mail_credentials,
        "MAIL_PASSWORD": mail_credentials,
        "BOARD_OWNER": strings.t("gestion.ajuste_owner"),
        "GITEA_URL": strings.t("gestion.ajuste_gitea_url"),
        "CONTENT_REPO": strings.t("gestion.ajuste_content_repo"),
    }


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
        return _bad(strings.t("gestion.estado_sin_token"), str(exc))

    which = "CONTENT_TOKEN" if os.environ.get("CONTENT_TOKEN") else "GITEA_ADMIN_TOKEN"
    try:
        gitea.list_directory("content/post", token)
    except gitea.GiteaError as exc:
        return _bad(strings.t("gestion.estado_token_sin_lectura", ajuste=which),
                    str(exc))
    except OSError as exc:
        return _warn(strings.t("gestion.estado_git_mudo"), str(exc))

    if which == "GITEA_ADMIN_TOKEN":
        return _warn(strings.t("gestion.estado_token_admin"),
                     strings.t("gestion.estado_token_admin_detalle"))
    return _ok(strings.t("gestion.estado_token_bien"),
               strings.t("gestion.estado_token_bien_detalle", ajuste=which))


def _mail() -> dict:
    if not mail.configured():
        return _warn(strings.t("gestion.estado_sin_correo"),
                     strings.t("gestion.estado_sin_correo_detalle"))
    host = current_app.config["MAIL_HOST"]
    port = int(current_app.config["MAIL_PORT"] or 587)
    try:
        with smtplib.SMTP(host, port, timeout=5) as server:
            server.ehlo()
    except (OSError, smtplib.SMTPException) as exc:
        return _bad(strings.t("gestion.estado_correo_mudo",
                              servidor=f"{host}:{port}"), str(exc))
    return _ok(strings.t("gestion.estado_correo_bien", servidor=f"{host}:{port}"),
               strings.t("gestion.estado_correo_bien_detalle"))


def _backups() -> dict:
    folder = Path(BACKUP_DIR)
    if not folder.is_dir():
        return _warn(strings.t("gestion.estado_sin_carpeta_copias"),
                     strings.t("gestion.estado_carpeta_inexistente",
                               carpeta=BACKUP_DIR))
    files = sorted(folder.glob("board-*.db.gz"), key=lambda f: f.stat().st_mtime)
    if not files:
        return _bad(strings.t("gestion.estado_sin_copias"),
                    strings.t("gestion.estado_carpeta_vacia", carpeta=BACKUP_DIR))
    newest = files[-1]
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        newest.stat().st_mtime, timezone.utc)
    size = newest.stat().st_size // 1024
    detail = strings.t("gestion.estado_copia_detalle", archivo=newest.name,
                       tamano=size, total=len(files))
    if age.days >= 2:
        return _warn(strings.t("gestion.estado_copia_vieja", dias=age.days), detail)
    return _ok(strings.t("gestion.estado_copia_reciente"), detail)


def _disk() -> dict:
    """The one that ends everything at once, and silently: SQLite cannot write,
    uploads fail, and the log that would say so cannot be written either."""
    # Through `uploads.directory()`, which creates it: asking statvfs about a
    # path that does not exist raises, and "the uploads folder has not been made
    # yet" is not a disk problem.
    usage = shutil.disk_usage(uploads.directory())
    free_mb = usage.free // (1024 * 1024)
    detail = strings.t("gestion.estado_disco_detalle", libres=free_mb,
                       total=usage.total // (1024 * 1024))
    if free_mb < 200:
        return _bad(strings.t("gestion.estado_disco_critico"), detail)
    if free_mb < 1000:
        return _warn(strings.t("gestion.estado_disco_justo"), detail)
    return _ok(strings.t("gestion.estado_disco_bien"), detail)


def _settings() -> dict:
    """Which expected settings the container does not have, and whether it
    matters — which are two different questions."""
    missing, defaulted = [], []
    why = _why()
    for name, key in EXPECTED:
        if os.environ.get(name):
            continue
        if key:
            defaulted.append(strings.t("gestion.estado_ajuste_defecto", ajuste=name,
                                       valor=current_app.config.get(key)))
        else:
            missing.append(strings.t("gestion.estado_ajuste_falta", ajuste=name,
                                     para_que=why[name]))

    detail = "; ".join(missing + defaulted)
    if missing:
        return _warn(strings.t("gestion.estado_falta_ajuste", n=len(missing))
                     if len(missing) == 1
                     else strings.t("gestion.estado_faltan_ajustes", n=len(missing)),
                     detail)
    if defaulted:
        # Green: nothing is broken. The values are still named, because "it
        # works and here is the value it is working with" is the answer, and
        # silence would leave somebody wondering which repository it publishes
        # to.
        return _ok(strings.t("gestion.estado_ajustes_necesarios"), detail)
    return _ok(strings.t("gestion.estado_ajustes_todos"))


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
        (strings.t("gestion.estado_publicar"), _content_token()),
        (strings.t("gestion.estado_correo"), _mail()),
        (strings.t("gestion.estado_copias"), _backups()),
        (strings.t("gestion.estado_disco"), _disk()),
        (strings.t("gestion.estado_ajustes"), _settings()),
    ]
    return render_template("status.html", checks=checks, counts=_counts(),
                           worst=min((c[1]["state"] for c in checks),
                                     key=["bad", "warn", "ok"].index))
