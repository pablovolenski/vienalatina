"""Private messages between two members.

An inbox, not a chat. Each conversation has exactly two people, each keeps
their own unread mark, and either can block the other — after which neither can
write. That symmetry is deliberate: a block that silences only one side is a
one-way megaphone, which is worse than having no block at all.

Every rule here is enforced in the handler, not only in the template. A hidden
button is a courtesy to somebody using the site normally; it is not a rule, and
the difference matters most for exactly the person a block exists to stop.
"""

from __future__ import annotations

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)

from . import uploads
from .db import TOMBSTONE_LOGIN, get_db
from .render import to_html
from .security import login_required

bp = Blueprint("messages", __name__)

BODY_MAX = 20_000


def blocked_between(a: int, b: int) -> bool:
    """A block in either direction stops both directions."""
    return get_db().execute(
        """SELECT 1 FROM blocks
            WHERE (blocker_id = ? AND blocked_id = ?)
               OR (blocker_id = ? AND blocked_id = ?)""",
        (a, b, b, a),
    ).fetchone() is not None


def existing_conversation(other_id: int) -> int | None:
    """The conversation between us and `other_id`, or None. Creates nothing."""
    row = get_db().execute(
        """SELECT a.conversation_id AS id
             FROM conversation_members a
             JOIN conversation_members b ON b.conversation_id = a.conversation_id
            WHERE a.member_id = ? AND b.member_id = ?""",
        (g.member["id"], other_id),
    ).fetchone()
    return row["id"] if row else None


def conversation_with(other_id: int) -> int:
    """The conversation between the signed-in member and `other_id`, made if
    it does not exist yet."""
    db = get_db()
    row = db.execute(
        """SELECT a.conversation_id AS id
             FROM conversation_members a
             JOIN conversation_members b ON b.conversation_id = a.conversation_id
            WHERE a.member_id = ? AND b.member_id = ?""",
        (g.member["id"], other_id),
    ).fetchone()
    if row:
        return row["id"]

    conversation_id = db.execute(
        "INSERT INTO conversations DEFAULT VALUES").lastrowid
    for member_id in (g.member["id"], other_id):
        db.execute(
            "INSERT INTO conversation_members (conversation_id, member_id) VALUES (?, ?)",
            (conversation_id, member_id),
        )
    return conversation_id


def unread_count() -> int:
    """For the badge in the navigation. One query, not one per conversation."""
    if g.member is None:
        return 0
    return get_db().execute(
        """SELECT COUNT(*) AS n
             FROM messages m
             JOIN conversation_members cm
               ON cm.conversation_id = m.conversation_id AND cm.member_id = ?
            WHERE m.author_id != ?
              AND m.deleted_at IS NULL
              AND (cm.last_read_at IS NULL OR m.created_at > cm.last_read_at)""",
        (g.member["id"], g.member["id"]),
    ).fetchone()["n"]


# --- routes ---------------------------------------------------------------

@bp.route("/privados")
@login_required
def inbox():
    rows = get_db().execute(
        """SELECT c.id,
                  other.display_name AS other_name,
                  other.id           AS other_id,
                  other.gitea_login  AS other_login,
                  other.profile_published AS other_published,
                  (SELECT body_md FROM messages
                    WHERE conversation_id = c.id AND deleted_at IS NULL
                    ORDER BY created_at DESC LIMIT 1)    AS last_body,
                  (SELECT created_at FROM messages
                    WHERE conversation_id = c.id AND deleted_at IS NULL
                    ORDER BY created_at DESC LIMIT 1)    AS last_at,
                  (SELECT COUNT(*) FROM messages m
                    WHERE m.conversation_id = c.id AND m.author_id != mine.member_id
                      AND m.deleted_at IS NULL
                      AND (mine.last_read_at IS NULL OR m.created_at > mine.last_read_at)
                  ) AS unread
             FROM conversations c
             JOIN conversation_members mine
               ON mine.conversation_id = c.id AND mine.member_id = :me
             JOIN conversation_members theirs
               ON theirs.conversation_id = c.id AND theirs.member_id != :me
             JOIN members other ON other.id = theirs.member_id
            ORDER BY last_at DESC NULLS LAST""",
        {"me": g.member["id"]},
    ).fetchall()

    # Anyone you could start writing to: every active member but yourself, the
    # tombstone, and anyone either of you has blocked.
    people = get_db().execute(
        """SELECT m.id, m.display_name FROM members m
            WHERE m.id != :me AND m.active = 1
              AND m.role != 'tombstone'
              AND m.gitea_login != :ghost
              AND NOT EXISTS (SELECT 1 FROM blocks
                               WHERE (blocker_id = :me AND blocked_id = m.id)
                                  OR (blocker_id = m.id AND blocked_id = :me))
            ORDER BY m.display_name COLLATE NOCASE""",
        {"me": g.member["id"], "ghost": TOMBSTONE_LOGIN},
    ).fetchall()

    return render_template("inbox.html", conversations=rows, people=people,
                           excerpt=lambda text: (text or "")[:120])


@bp.route("/privados/con")
@login_required
def pick():
    """The select posts here as a plain GET and is sent on to the conversation.

    A form cannot put its own value into a path, and rewriting the action with
    JavaScript is not available: the Content-Security-Policy has no
    'unsafe-inline', so an inline handler is ignored without a word. One tiny
    redirect avoids the whole question.
    """
    return redirect(url_for("messages.talk",
                            member_id=request.args.get("member_id", type=int) or 0))


@bp.route("/privados/con/<int:member_id>", methods=["GET", "POST"])
@login_required
def talk(member_id: int):
    """One page for a conversation, whether or not it exists yet.

    There used to be an "Abrir conversación" step that created an empty
    conversation and sent you to it. It bought nothing — you still had to type
    a message afterwards — and it littered the inbox with rows saying "Sin
    mensajes todavía" for people nobody had actually written to. The
    conversation is now created by the first message, not by looking at
    somebody's name.
    """
    if member_id == g.member["id"]:
        abort(400)
    other = get_db().execute(
        """SELECT * FROM members
            WHERE id = ? AND active = 1 AND role != 'tombstone'""",
        (member_id,),
    ).fetchone()
    if other is None:
        abort(404)
    blocked = blocked_between(g.member["id"], member_id)
    if blocked and request.method == "POST":
        abort(403)

    db = get_db()
    conversation_id = existing_conversation(member_id)

    if request.method == "POST":
        body = request.form.get("body", "").strip()[:BODY_MAX]
        try:
            staged = uploads.stage(request.files.getlist("pictures"))
        except uploads.RejectedUpload as exc:
            flash(str(exc), "error")
            return redirect(url_for("messages.talk", member_id=member_id))
        if not body and not staged:
            flash("Escribe algo o adjunta una imagen.", "error")
            return redirect(url_for("messages.talk", member_id=member_id))

        # Only now does the conversation come into being.
        if conversation_id is None:
            conversation_id = conversation_with(member_id)
        cursor = db.execute(
            "INSERT INTO messages (conversation_id, author_id, body_md) VALUES (?, ?, ?)",
            (conversation_id, g.member["id"], body),
        )
        uploads.save(staged, g.member["id"], message_id=cursor.lastrowid)
        return redirect(url_for("messages.talk", member_id=member_id) + "#final")

    rows = []
    if conversation_id is not None:
        rows = db.execute(
            """SELECT m.*, a.display_name AS author,
                      a.gitea_login AS author_login,
                      a.profile_published AS author_published
                 FROM messages m JOIN members a ON a.id = m.author_id
                WHERE m.conversation_id = ? AND m.deleted_at IS NULL
                ORDER BY m.created_at""",
            (conversation_id,),
        ).fetchall()
        db.execute(
            """UPDATE conversation_members SET last_read_at = datetime('now')
                WHERE conversation_id = ? AND member_id = ?""",
            (conversation_id, g.member["id"]),
        )

    return render_template(
        "conversation.html", other=other, messages=rows, to_html=to_html,
        images=uploads.for_messages([row["id"] for row in rows]),
        blocked=blocked,
    )


@bp.route("/privados/bloquear/<int:member_id>", methods=["POST"])
@login_required
def block(member_id: int):
    if member_id == g.member["id"]:
        abort(400)
    get_db().execute(
        """INSERT INTO blocks (blocker_id, blocked_id) VALUES (?, ?)
           ON CONFLICT DO NOTHING""",
        (g.member["id"], member_id),
    )
    flash("Bloqueado. Ninguno de los dos puede escribir al otro.", "ok")
    return redirect(url_for("messages.inbox"))


@bp.route("/privados/desbloquear/<int:member_id>", methods=["POST"])
@login_required
def unblock(member_id: int):
    """Only your own block. Removing somebody else's would let the blocked
    person undo the thing that was done to protect against them."""
    get_db().execute("DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
                     (g.member["id"], member_id))
    flash("Desbloqueado.", "ok")
    return redirect(url_for("messages.inbox"))
