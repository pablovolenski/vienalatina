"""What members propose for the public site, and who lets it through.

The whole of the curation policy, in three sentences. A member writes a post
here and it waits. A moderator or an admin reads it and either approves it —
which commits it to the site repository under the member's own name — or rejects
it with a reason the author can read. Moderators and admins do not queue: they
write in the editor (`content.py`) and their post is committed as they save it.

**Nothing in this module reaches git until somebody approves.** That is not a
convention, it is the reason the table exists: a commit to the content repository
*is* publication, because Gitea's webhook starts the translate → build → deploy
pipeline within seconds. A draft that lived in the repository with a "pending"
flag would already be on vienalatina.com in three languages.

Approval goes through `content.publish`, the same function the editor uses, so an
approved submission is byte-identical to a post written by a moderator and the
pipeline cannot tell the two apart. `gitea.write_file` takes an author per commit,
so git history names the member who wrote it, not the moderator who approved it.

There is deliberately no way to promote a thread from the wall into a
submission. Writing for the public is a separate act — somebody decided to
address strangers rather than the people they already know, and that decision is
the thing worth keeping visible.
"""

from __future__ import annotations

from datetime import date as date_type

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, send_from_directory, url_for)

from . import content, gitea, uploads
from .db import get_db
from .render import excerpt, to_html
from .security import login_required, moderator_required

bp = Blueprint("submissions", __name__)

TITLE_MAX = content.TITLE_MAX
BODY_MAX = content.BODY_MAX

# How many proposals one member may have waiting at once. Not a security limit:
# everyone here is a known member. It is a limit on how much unread work one
# person can put on the moderators before talking to them.
PENDING_MAX = 3

STATE_LABELS = {
    "pending": "En revisión",
    "approved": "Publicado",
    "rejected": "Devuelto",
}


def may_moderate(member=None) -> bool:
    member = member or g.member
    return member is not None and member["role"] in ("owner", "admin", "moderator")


def pending_count() -> int:
    """For the badge in the navigation, and 0 for anyone who cannot act on it —
    a member does not need to know how long the queue is."""
    if not may_moderate():
        return 0
    return get_db().execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE state = 'pending'"
    ).fetchone()["n"]


def _load(submission_id: int):
    row = get_db().execute(
        """SELECT s.*, m.display_name AS author, m.email AS author_email,
                  m.gitea_login AS author_login, m.profile_published AS author_published,
                  r.display_name AS reviewer
             FROM submissions s
             JOIN members m ON m.id = s.author_id
             LEFT JOIN members r ON r.id = s.reviewed_by
            WHERE s.id = ?""",
        (submission_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def _mine_or_403(row) -> None:
    if row["author_id"] != g.member["id"]:
        abort(403)


def _read_proposal() -> tuple[dict, list[str]]:
    """The same fields the editor asks for, minus the ones only a publisher
    decides: the date is the day it is approved, and `manual_translation` is a
    pipeline switch, not something to explain to a member."""
    fields = {
        "title": request.form.get("title", "").strip()[:TITLE_MAX],
        "body": request.form.get("body", "").strip()[:BODY_MAX],
        "description": request.form.get("description", "").strip()[:300],
        "categories": [c for c in request.form.getlist("categories")
                       if c in content.CATEGORIES],
    }
    errors = []
    if not fields["title"]:
        errors.append("El título no puede estar vacío.")
    if not fields["body"]:
        errors.append("El texto no puede estar vacío.")
    return fields, errors


# --- the member's side ----------------------------------------------------

@bp.route("/publicaciones")
@login_required
def index():
    """One page for both sides of the queue.

    A member sees what they proposed and what happened to it. A moderator sees
    that plus everything waiting, because the first thing a moderator wants on
    opening this section is the list of things to read.
    """
    db = get_db()
    mine = db.execute(
        """SELECT * FROM submissions WHERE author_id = ?
            ORDER BY CASE state WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC""",
        (g.member["id"],),
    ).fetchall()
    queue = []
    if may_moderate():
        queue = db.execute(
            """SELECT s.*, m.display_name AS author,
                      m.gitea_login AS author_login,
                      m.profile_published AS author_published
                 FROM submissions s JOIN members m ON m.id = s.author_id
                WHERE s.state = 'pending' AND s.author_id != ?
                ORDER BY s.created_at""",
            (g.member["id"],),
        ).fetchall()
    return render_template("submissions.html", mine=mine, queue=queue,
                           labels=STATE_LABELS, excerpt=excerpt,
                           can_moderate=may_moderate())


@bp.route("/publicaciones/nueva", methods=["GET", "POST"])
@login_required
def propose():
    if may_moderate():
        # Not a refusal: a moderator's post does not queue, so sending them
        # through a form that would only wait for their own approval is a
        # detour with nothing at the end of it.
        return redirect(url_for("content.new", collection="post"))

    if request.method == "GET":
        return render_template("submission_form.html", item=None, fields=None,
                               categories=content.CATEGORIES)

    fields, errors = _read_proposal()
    waiting = get_db().execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE author_id = ? AND state = 'pending'",
        (g.member["id"],),
    ).fetchone()["n"]
    if waiting >= PENDING_MAX:
        errors.append(
            f"Ya tienes {waiting} propuestas en revisión. Espera a que las lean "
            "antes de enviar otra."
        )
    staged = []
    if not errors:
        try:
            staged = uploads.stage(request.files.getlist("picture"))
        except uploads.RejectedUpload as exc:
            errors.append(str(exc))
    if errors:
        return _back_to_form(None, fields, errors)

    photo_name = _store_picture(staged)
    get_db().execute(
        """INSERT INTO submissions
               (author_id, title, body_md, description, categories, photo_name)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (g.member["id"], fields["title"], fields["body"], fields["description"],
         ", ".join(fields["categories"]), photo_name),
    )
    flash("Enviado. Un moderador lo leerá antes de publicarlo.", "ok")
    return redirect(url_for("submissions.index"))


@bp.route("/publicaciones/<int:submission_id>", methods=["GET"])
@login_required
def show(submission_id: int):
    """The author reads their own; a moderator reads anybody's. Nobody else
    reads any of it — an unapproved post is private writing."""
    row = _load(submission_id)
    if row["author_id"] != g.member["id"] and not may_moderate():
        abort(403)
    return render_template("submission.html", item=row, labels=STATE_LABELS,
                           body_html=to_html(row["body_md"]),
                           can_moderate=may_moderate(),
                           mine=row["author_id"] == g.member["id"])


@bp.route("/publicaciones/<int:submission_id>/editar", methods=["GET", "POST"])
@login_required
def edit(submission_id: int):
    """Only while it is waiting or has come back. Editing an approved one would
    change nothing: the file is already in the repository, and the editor is
    where a published post is changed."""
    row = _load(submission_id)
    _mine_or_403(row)
    if row["state"] == "approved":
        flash("Ya está publicado. Pide a un moderador que lo edite en el sitio.", "error")
        return redirect(url_for("submissions.index"))

    if request.method == "GET":
        return render_template("submission_form.html", item=row, categories=content.CATEGORIES,
                               fields={
                                   "title": row["title"], "body": row["body_md"],
                                   "description": row["description"],
                                   "categories": [c.strip() for c in
                                                  (row["categories"] or "").split(",") if c.strip()],
                               })

    fields, errors = _read_proposal()
    staged = []
    if not errors:
        try:
            staged = uploads.stage(request.files.getlist("picture"))
        except uploads.RejectedUpload as exc:
            errors.append(str(exc))
    if errors:
        return _back_to_form(row, fields, errors)

    photo_name = row["photo_name"]
    if staged:
        if photo_name:
            uploads.remove(photo_name)
        photo_name = _store_picture(staged)

    get_db().execute(
        """UPDATE submissions
              SET title = ?, body_md = ?, description = ?, categories = ?,
                  photo_name = ?, state = 'pending', note = NULL,
                  reviewed_by = NULL, reviewed_at = NULL,
                  updated_at = datetime('now')
            WHERE id = ?""",
        (fields["title"], fields["body"], fields["description"],
         ", ".join(fields["categories"]), photo_name, submission_id),
    )
    # Back to pending on every edit, including one made after a rejection: a
    # rejected post that has been rewritten is waiting again, and leaving it
    # marked "devuelto" would hide it from the queue forever.
    flash("Actualizado. Vuelve a estar en revisión.", "ok")
    return redirect(url_for("submissions.index"))


@bp.route("/publicaciones/<int:submission_id>/retirar", methods=["POST"])
@login_required
def withdraw(submission_id: int):
    row = _load(submission_id)
    _mine_or_403(row)
    if row["state"] == "approved":
        abort(403)
    get_db().execute("DELETE FROM submissions WHERE id = ?", (submission_id,))
    if row["photo_name"]:
        uploads.remove(row["photo_name"])
    flash("Propuesta retirada.", "ok")
    return redirect(url_for("submissions.index"))


@bp.route("/publicaciones/foto/<stored_name>")
@login_required
def photo(stored_name: str):
    """Behind the login and narrower than that: the author and the moderators.

    A submission's picture is not published yet, and "any signed-in member" is
    not the audience the author chose when they attached it.
    """
    if not uploads.STORED_NAME.match(stored_name):
        abort(404)
    row = get_db().execute(
        "SELECT author_id FROM submissions WHERE photo_name = ?", (stored_name,)
    ).fetchone()
    if row is None:
        abort(404)
    if row["author_id"] != g.member["id"] and not may_moderate():
        abort(404)
    return send_from_directory(uploads.directory(), stored_name)


# --- the moderator's side -------------------------------------------------

@bp.route("/publicaciones/<int:submission_id>/aprobar", methods=["POST"])
@moderator_required
def approve(submission_id: int):
    """Commit it, under the author's name, and record where it landed."""
    row = _load(submission_id)
    if row["state"] == "approved":
        flash("Esa propuesta ya estaba publicada.", "error")
        return redirect(url_for("submissions.index"))

    author = {"display_name": row["author"], "email": row["author_email"],
              "gitea_login": row["author_login"]}
    fields = {
        "title": row["title"],
        # The day it is published, not the day it was written: Hugo sorts by
        # this and the URL carries it, so a proposal that waited a week should
        # not arrive dated a week ago.
        "date": date_type.today(),
        "categories": [c.strip() for c in (row["categories"] or "").split(",") if c.strip()],
        "description": row["description"],
        "image": "",
        "manual_translation": False,
    }
    # Read the picture before the try, and catch only the error that means what
    # it says. The first version wrapped everything in `except OSError`, which
    # also catches PermissionError and every `requests` failure — so a git
    # server refusing the token was reported as a missing photograph, on a page
    # displaying that photograph. An except clause wide enough to catch the
    # network is wide enough to lie.
    picture = None
    if row["photo_name"]:
        try:
            picture = (uploads.directory() / row["photo_name"]).read_bytes()
        except FileNotFoundError:
            current_app.logger.warning("submission %s: %s is not on disk",
                                       submission_id, row["photo_name"])
            flash("No se encontró la imagen en el disco. Pide al autor que la "
                  "vuelva a subir.", "error")
            return redirect(url_for("submissions.show", submission_id=submission_id))

    try:
        if picture is not None:
            fields["image"] = content.commit_picture(picture, row["photo_name"], author)
        path = content.publish("post", fields, row["body_md"], author)
    except gitea.GiteaError as exc:
        flash(str(exc), "error")
        return redirect(url_for("submissions.show", submission_id=submission_id))

    get_db().execute(
        """UPDATE submissions
              SET state = 'approved', note = NULL, reviewed_by = ?,
                  reviewed_at = datetime('now'), published_path = ?,
                  photo_name = NULL, updated_at = datetime('now')
            WHERE id = ?""",
        (g.member["id"], path, submission_id),
    )
    # The private copy goes now that the picture is in the repository: one file,
    # in one place, and no orphan in /data/uploads that nothing will ever serve.
    if row["photo_name"]:
        uploads.remove(row["photo_name"])
    flash(f"Publicado «{row['title']}». La traducción tarda un par de minutos.", "ok")
    return redirect(url_for("submissions.index"))


@bp.route("/publicaciones/<int:submission_id>/rechazar", methods=["POST"])
@moderator_required
def reject(submission_id: int):
    """A reason is required, and it is shown to the author.

    Not politeness: without one the author has no idea whether to rewrite it,
    shorten it or drop it, and the next thing they send has the same problem.
    """
    row = _load(submission_id)
    note = request.form.get("note", "").strip()[:1_000]
    if not note:
        flash("Escribe el motivo: el autor lo va a leer.", "error")
        return redirect(url_for("submissions.show", submission_id=submission_id))
    if row["state"] == "approved":
        abort(403)

    get_db().execute(
        """UPDATE submissions
              SET state = 'rejected', note = ?, reviewed_by = ?,
                  reviewed_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ?""",
        (note, g.member["id"], submission_id),
    )
    flash("Devuelto al autor con tu motivo.", "ok")
    return redirect(url_for("submissions.index"))


# --- helpers --------------------------------------------------------------

def _store_picture(staged: list[dict]) -> str | None:
    """Write the one picture to disk without an `attachments` row.

    A submission carries at most one, and `attachments` insists a row belongs to
    exactly one of a thread, a comment or a message. Giving it a fourth possible
    parent would mean rebuilding that table again — so this follows what a
    profile photo already does: a column on the row, a file in the same
    directory, and `uploads.remove` when it goes.
    """
    if not staged:
        return None
    item = staged[0]
    (uploads.directory() / item["stored_name"]).write_bytes(item["data"])
    return item["stored_name"]


def _back_to_form(item, fields, errors):
    for message in errors:
        flash(message, "error")
    return render_template("submission_form.html", item=item, fields=fields,
                           categories=content.CATEGORIES), 400
