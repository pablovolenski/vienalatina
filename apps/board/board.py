"""Threads and comments.

A deliberate split between two things often lumped together as "moderation":

* **Deleting** someone else's post is an admin power. Sometimes something has
  to come down, and the person who wrote it is not always around to do it.
* **Editing** someone else's post is nobody's power but the author's. An admin
  who could rewrite a member's words could put a sentence in their mouth that
  the member gets to see attributed to themselves. Removing is visible;
  silently rewriting is not.

The plan drafted for this feature said admins could do both. This is the one
place the implementation departs from it, on purpose.
"""

from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from . import auth, uploads
from .db import get_db
from .render import excerpt, to_html
from .security import admin_required, is_at_least, login_required

bp = Blueprint("board", __name__)

PER_PAGE = 20
TITLE_MAX = 140
BODY_MAX = 20_000


def is_admin() -> bool:
    return is_at_least(g.member["role"], "admin")


def may_delete(row) -> bool:
    return is_admin() or row["author_id"] == g.member["id"]


def may_edit(row) -> bool:
    return row["author_id"] == g.member["id"]


def cooldown_remaining() -> int:
    """Seconds this member must still wait. Guards against a stuck key or a
    double-submitted form, not against a determined spammer — the door is the
    members table, and everyone behind it is known."""
    seconds = current_app.config["COOLDOWN_SECONDS"]
    if seconds <= 0:
        return 0
    row = get_db().execute(
        """SELECT MAX(created_at) AS last FROM (
               SELECT created_at FROM threads  WHERE author_id = :id
               UNION ALL
               SELECT created_at FROM comments WHERE author_id = :id
           )""",
        {"id": g.member["id"]},
    ).fetchone()
    if not row or not row["last"]:
        return 0
    elapsed = get_db().execute(
        "SELECT CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', ?) AS INTEGER) AS s",
        (row["last"],),
    ).fetchone()["s"]
    return max(0, seconds - int(elapsed))


def _clean(title: str, body: str) -> tuple[str, str] | None:
    title = title.strip()[:TITLE_MAX]
    body = body.strip()[:BODY_MAX]
    if not title or not body:
        flash("El título y el mensaje no pueden estar vacíos.", "error")
        return None
    return title, body


# --- reactions ------------------------------------------------------------
#
# One 👍 per member per thread or comment, and clicking it again takes it back.
# Counted in one query per page rather than one per card: a wall of twenty
# posts should not be forty round trips to SQLite for a number beside a thumb.

def _reactions(column: str, ids: list[int]) -> dict[int, dict]:
    """{parent id: {"count": n, "mine": bool}} for a whole page at once."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = get_db().execute(
        f"""SELECT {column} AS parent, COUNT(*) AS total,
                   MAX(member_id = ?) AS mine
              FROM reactions WHERE {column} IN ({marks})
             GROUP BY {column}""",
        (g.member["id"], *ids),
    ).fetchall()
    return {row["parent"]: {"count": row["total"], "mine": bool(row["mine"])}
            for row in rows}


def reactions_for_threads(ids: list[int]) -> dict[int, dict]:
    return _reactions("thread_id", ids)


def reactions_for_comments(ids: list[int]) -> dict[int, dict]:
    return _reactions("comment_id", ids)


def _thread_or_404(thread_id: int):
    row = get_db().execute(
        "SELECT * FROM threads WHERE id = ? AND deleted_at IS NULL", (thread_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return row


def _comment_or_404(comment_id: int):
    row = get_db().execute(
        "SELECT * FROM comments WHERE id = ? AND deleted_at IS NULL", (comment_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/muro")
@login_required
def threads():
    """The wall. It used to be the root of the members area, and `Inicio` is
    there now: the first thing after signing in should say what has happened
    everywhere, not drop somebody into one of the five places it could have
    happened. Thread URLs are untouched, so nothing anyone has linked to moves.
    """
    page = max(1, request.args.get("page", 1, type=int))
    rows = get_db().execute(
        """SELECT t.*, m.display_name AS author,
                  m.gitea_login AS author_login, m.profile_published AS author_published,
                  (SELECT COUNT(*) FROM comments c
                    WHERE c.thread_id = t.id AND c.deleted_at IS NULL) AS replies
             FROM threads t JOIN members m ON m.id = t.author_id
            WHERE t.deleted_at IS NULL
            ORDER BY t.pinned DESC, t.created_at DESC
            LIMIT ? OFFSET ?""",
        (PER_PAGE + 1, (page - 1) * PER_PAGE),
    ).fetchall()
    # One row past the page size answers "is there a next page" without a
    # second COUNT(*) over the whole table.
    has_next = len(rows) > PER_PAGE
    shown = rows[:PER_PAGE]
    return render_template("threads.html", threads=shown, page=page,
                           has_next=has_next, excerpt=excerpt,
                           reactions=reactions_for_threads([t["id"] for t in shown]))


@bp.route("/tema/<int:thread_id>")
@login_required
def thread(thread_id: int):
    row = _thread_or_404(thread_id)
    db = get_db()
    author = db.execute(
        """SELECT display_name, gitea_login, profile_published
             FROM members WHERE id = ?""",
        (row["author_id"],)).fetchone()
    comments = db.execute(
        """SELECT c.*, m.display_name AS author,
                  m.gitea_login AS author_login, m.profile_published AS author_published
             FROM comments c JOIN members m ON m.id = c.author_id
            WHERE c.thread_id = ? AND c.deleted_at IS NULL
            ORDER BY c.created_at""",
        (thread_id,),
    ).fetchall()
    return render_template("thread.html", thread=row, author=author,
                           comments=comments, body_html=to_html(row["body_md"]),
                           to_html=to_html, may_edit=may_edit, may_delete=may_delete,
                           is_admin=is_admin(),
                           thread_images=uploads.for_threads([thread_id]).get(thread_id, []),
                           comment_images=uploads.for_comments([c["id"] for c in comments]),
                           reactions=reactions_for_threads([thread_id]),
                           comment_reactions=reactions_for_comments(
                               [c["id"] for c in comments]))


@bp.route("/nuevo", methods=["GET", "POST"])
@login_required
def new_thread():
    if request.method == "GET":
        return render_template("thread_form.html", thread=None)

    cleaned = _clean(request.form.get("title", ""), request.form.get("body", ""))
    if cleaned is None:
        return redirect(url_for("board.new_thread"))
    wait = cooldown_remaining()
    if wait:
        flash(f"Espera {wait} segundos antes de publicar otra vez.", "error")
        return redirect(url_for("board.new_thread"))

    # Checked before the thread exists, so a refused picture does not leave a
    # half-made post behind for its author to find and wonder about.
    try:
        staged = uploads.stage(request.files.getlist("pictures"))
    except uploads.RejectedUpload as exc:
        flash(str(exc), "error")
        return redirect(url_for("board.new_thread"))

    title, body = cleaned
    cursor = get_db().execute(
        "INSERT INTO threads (author_id, title, body_md) VALUES (?, ?, ?)",
        (g.member["id"], title, body),
    )
    uploads.save(staged, g.member["id"], thread_id=cursor.lastrowid)
    return redirect(url_for("board.thread", thread_id=cursor.lastrowid))


@bp.route("/tema/<int:thread_id>/editar", methods=["GET", "POST"])
@login_required
def edit_thread(thread_id: int):
    row = _thread_or_404(thread_id)
    if not may_edit(row):
        abort(403)
    if request.method == "GET":
        return render_template("thread_form.html", thread=row)

    cleaned = _clean(request.form.get("title", ""), request.form.get("body", ""))
    if cleaned is None:
        return redirect(url_for("board.edit_thread", thread_id=thread_id))
    title, body = cleaned
    get_db().execute(
        "UPDATE threads SET title = ?, body_md = ?, edited_at = datetime('now') WHERE id = ?",
        (title, body, thread_id),
    )
    return redirect(url_for("board.thread", thread_id=thread_id))


@bp.route("/tema/<int:thread_id>/eliminar", methods=["POST"])
@login_required
def delete_thread(thread_id: int):
    row = _thread_or_404(thread_id)
    if not may_delete(row):
        abort(403)
    get_db().execute("UPDATE threads SET deleted_at = datetime('now') WHERE id = ?", (thread_id,))
    flash("Tema eliminado.", "ok")
    return redirect(url_for("board.threads"))


@bp.route("/tema/<int:thread_id>/comentar", methods=["POST"])
@login_required
def comment(thread_id: int):
    row = _thread_or_404(thread_id)
    if row["locked"] and not is_admin():
        flash("Este tema está cerrado.", "error")
        return redirect(url_for("board.thread", thread_id=thread_id))

    body = request.form.get("body", "").strip()[:BODY_MAX]
    if not body:
        flash("El comentario no puede estar vacío.", "error")
        return redirect(url_for("board.thread", thread_id=thread_id))
    wait = cooldown_remaining()
    if wait:
        flash(f"Espera {wait} segundos antes de comentar otra vez.", "error")
        return redirect(url_for("board.thread", thread_id=thread_id))

    try:
        staged = uploads.stage(request.files.getlist("pictures"))
    except uploads.RejectedUpload as exc:
        flash(str(exc), "error")
        return redirect(url_for("board.thread", thread_id=thread_id))

    cursor = get_db().execute(
        "INSERT INTO comments (thread_id, author_id, body_md) VALUES (?, ?, ?)",
        (thread_id, g.member["id"], body),
    )
    uploads.save(staged, g.member["id"], comment_id=cursor.lastrowid)
    return redirect(url_for("board.thread", thread_id=thread_id) + "#final")


@bp.route("/comentario/<int:comment_id>/editar", methods=["GET", "POST"])
@login_required
def edit_comment(comment_id: int):
    row = _comment_or_404(comment_id)
    if not may_edit(row):
        abort(403)
    if request.method == "GET":
        return render_template("comment_form.html", comment=row)

    body = request.form.get("body", "").strip()[:BODY_MAX]
    if not body:
        flash("El comentario no puede estar vacío.", "error")
        return redirect(url_for("board.edit_comment", comment_id=comment_id))
    get_db().execute(
        "UPDATE comments SET body_md = ?, edited_at = datetime('now') WHERE id = ?",
        (body, comment_id),
    )
    return redirect(url_for("board.thread", thread_id=row["thread_id"]))


@bp.route("/comentario/<int:comment_id>/eliminar", methods=["POST"])
@login_required
def delete_comment(comment_id: int):
    row = _comment_or_404(comment_id)
    if not may_delete(row):
        abort(403)
    get_db().execute("UPDATE comments SET deleted_at = datetime('now') WHERE id = ?", (comment_id,))
    flash("Comentario eliminado.", "ok")
    return redirect(url_for("board.thread", thread_id=row["thread_id"]))


@bp.route("/reaccion", methods=["POST"])
@login_required
def react():
    """Add my reaction, or take it back if it is already there.

    One route for both parents, because the form posts which kind it is and the
    alternative is two routes that differ by a column name. `kind` is checked
    against a fixed pair before it reaches the statement, so nothing a member
    types gets near the SQL.

    The parent is checked for real: a thread that has been deleted, or an id
    that never existed, is a 404 rather than a row pointing at nothing.
    """
    kind = request.form.get("kind")
    if kind not in ("thread", "comment"):
        abort(400, "Campo desconocido.")
    target_id = request.form.get("id", type=int)
    if not target_id:
        abort(400, "Falta el identificador.")

    if kind == "thread":
        row = _thread_or_404(target_id)
        column, anchor = "thread_id", url_for("board.thread", thread_id=row["id"])
    else:
        row = _comment_or_404(target_id)
        column = "comment_id"
        anchor = url_for("board.thread", thread_id=row["thread_id"]) + f"#c{row['id']}"

    db = get_db()
    existing = db.execute(
        f"SELECT id FROM reactions WHERE {column} = ? AND member_id = ?",
        (target_id, g.member["id"]),
    ).fetchone()
    if existing:
        db.execute("DELETE FROM reactions WHERE id = ?", (existing["id"],))
    else:
        db.execute(f"INSERT INTO reactions ({column}, member_id) VALUES (?, ?)",
                   (target_id, g.member["id"]))

    # Back where they were, including which comment: a reaction should not move
    # the page, and there is no JavaScript here to stop the form navigating.
    # Through safe_next, because a `back` field taken at face value is an open
    # redirect — the same hole the login form's `next` is checked for.
    return redirect(auth.safe_next(request.form.get("back") or anchor))


@bp.route("/tema/<int:thread_id>/estado", methods=["POST"])
@admin_required
def set_state(thread_id: int):
    _thread_or_404(thread_id)
    field = request.form.get("field")
    if field not in ("pinned", "locked"):
        abort(400, "Campo desconocido.")
    value = 1 if request.form.get("value") == "1" else 0
    # `field` is checked against a fixed pair above, so it never carries
    # anything a member typed into the statement.
    get_db().execute(f"UPDATE threads SET {field} = ? WHERE id = ?", (value, thread_id))
    return redirect(url_for("board.thread", thread_id=thread_id))
