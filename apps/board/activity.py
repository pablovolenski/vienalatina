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

from .db import get_db
from .security import superadmin_required

bp = Blueprint("activity", __name__)

PER_PAGE = 60

# action key -> the sentence a person reads. Built here rather than stored,
# because the stored key is what a later reader greps for and the wording is
# what changes. `{object}` is whatever the act was done to.
SENTENCES = {
    "member.created": "dio de alta a {object}",
    "member.invited": "envió una invitación a {object}",
    "member.suspended": "suspendió a {object}",
    "member.restored": "reactivó a {object}",
    "member.erased": "eliminó a {object}",
    "role.changed": "cambió el rol de {object}",
    "chair.owner": "nombró responsable a {object}",
    "chair.superadmin": "entregó la plataforma a {object}",
    "submission.approved": "publicó la propuesta «{object}»",
    "submission.rejected": "devolvió la propuesta «{object}»",
    "content.published": "publicó «{object}»",
    "content.deleted": "retiró «{object}»",
    "brand.saved": "cambió el aspecto del sitio",
    "brand.restored": "restauró el aspecto de fábrica",
    "identity.saved": "cambió la identidad del sitio",
    "media.deleted": "borró la imagen {object}",
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
    # filter is printf-style, so a `{object}` placeholder would have rendered
    # literally — and an unknown action falling back to its own key is what
    # makes a log written by an older version still readable.
    entries = [{
        "at": row["created_at"],
        "actor": row["actor"],
        "sentence": SENTENCES.get(row["action"], row["action"]).format(
            object=row["object"] or "—"),
        "detail": row["detail"],
    } for row in rows[:PER_PAGE]]

    return render_template("activity.html", rows=entries, page=page, more=more)
