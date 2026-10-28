"""Who did what, for the superadministrator to read.

An audit trail is the natural companion to a role that can do everything: the
question "who made them an administrator?" has no other answer, and the moment
anybody asks it is exactly the moment nobody can remember.

**Deliberately not a general event store.** One row per act that changes who can
do what, or what the public sees. No page views, no successful sign-ins, no
"somebody opened Miembros" — a log that records everything is a log nobody
reads, and this one has to be readable by a person scrolling it once a month.

`log()` never raises. A failure to write the diary must not fail the thing being
recorded: an administrator who could not be appointed because the note about it
could not be saved is a worse outcome than a gap in the notes.
"""

from __future__ import annotations

from flask import Blueprint, current_app, g, render_template, request

from . import strings
from .db import get_db
from .security import superadmin_required

bp = Blueprint("activity", __name__)

PER_PAGE = 60

# A function rather than the dict it used to be, for the reason test_strings.py
# states: `t()` reads what somebody typed in Gestión off the request, so a dict
# built at import time would freeze the shipped wording into the process.
#
# The stored action keys on the left never change — they are what somebody
# greps for a year from now. Only the reading does.
def sentences() -> dict[str, str]:
    return {
        "member.created": strings.t("gestion.accion_alta"),
        "member.invited": strings.t("gestion.accion_invitacion"),
        "member.suspended": strings.t("gestion.accion_suspension"),
        "member.restored": strings.t("gestion.accion_reactivacion"),
        "member.erased": strings.t("gestion.accion_eliminacion"),
        "role.changed": strings.t("gestion.accion_rol"),
        "chair.owner": strings.t("gestion.accion_responsable"),
        "chair.superadmin": strings.t("gestion.accion_plataforma"),
        "submission.approved": strings.t("gestion.accion_propuesta_aprobada"),
        "submission.rejected": strings.t("gestion.accion_propuesta_devuelta"),
        "content.published": strings.t("gestion.accion_publicado"),
        "content.deleted": strings.t("gestion.accion_retirado"),
        "brand.saved": strings.t("gestion.accion_marca"),
        "brand.restored": strings.t("gestion.accion_marca_restaurada"),
        "identity.saved": strings.t("gestion.accion_identidad"),
        "ui.saved": strings.t("gestion.accion_textos"),
        "ui.restored": strings.t("gestion.accion_textos_restaurados"),
        "media.deleted": strings.t("gestion.accion_imagen_borrada"),
    }


def log(action: str, object_name: str = "", detail: str = "") -> None:
    """Record one act. Call it after the thing succeeded, never before.

    Swallows everything on purpose — see the module docstring. The log line it
    writes instead is what tells somebody the diary is broken.
    """
    try:
        get_db().execute(
            "INSERT INTO activity (actor_id, action, object, detail) VALUES (?, ?, ?, ?)",
            (g.member["id"] if getattr(g, "member", None) else None,
             action, str(object_name)[:200], str(detail)[:400]),
        )
    except Exception:                      # pragma: no cover - defensive
        current_app.logger.warning("could not write activity %r", action, exc_info=True)


def _fill(sentence: str, objeto: str) -> str:
    """`{objeto}` replaced, and the sentence left whole if it cannot be.

    The same bargain `strings.t()` makes, and needed for the same reason: these
    sentences are editable now, and somebody who types `{objecto}` into Gestión
    must not take this page down with a KeyError. Loudly in the log, whole on
    the screen.
    """
    try:
        return sentence.format(objeto=objeto)
    except (KeyError, IndexError) as exc:
        current_app.logger.warning("activity sentence wants %s: %s", exc, sentence)
        return sentence


@bp.route("/gestion/registro")
@superadmin_required
def index():
    page = max(0, request.args.get("p", type=int) or 0)
    rows = get_db().execute(
        """SELECT a.*, m.display_name AS actor, m.gitea_login AS actor_login
             FROM activity a
             LEFT JOIN members m ON m.id = a.actor_id
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT ? OFFSET ?""",
        (PER_PAGE + 1, page * PER_PAGE),
    ).fetchall()
    # One more than a page, asked for so that "is there another page" is known
    # without a second COUNT(*) over a table that only grows.
    more = len(rows) > PER_PAGE

    # The sentence is built here rather than in the template: Jinja's `format`
    # filter is printf-style, so a `{objeto}` placeholder would have rendered
    # literally — and an unknown action falling back to its own key is what
    # makes a log written by an older version still readable.
    read = sentences()
    entries = [{
        "at": row["created_at"],
        "actor": row["actor"],
        "sentence": _fill(read.get(row["action"], row["action"]),
                          row["object"] or "—"),
        "detail": row["detail"],
    } for row in rows[:PER_PAGE]]

    return render_template("activity.html", rows=entries, page=page, more=more)
