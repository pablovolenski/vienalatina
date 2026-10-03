"""Private messages between two members.

Not a chat, and — since the sections were rearranged — not an inbox either.
Each conversation has exactly two people, each keeps their own unread mark, and
either can block the other, after which neither can write. That symmetry is
deliberate: a block that silences only one side is a one-way megaphone, which is
worse than having no block at all.

**Where the messages are shown moved; where the rules live did not.** The
conversation is read on the other person's page (`members.show`), because
writing to somebody is something you do to a person and not in a separate
mailbox. This module still owns every rule about it — who may read a
conversation, what counts as having read it, when the conversation comes into
being, and who may write to whom — and hands the page its contents through
`history()`. The routes that remain are the ones that *change* something: a
message sent, a block set or lifted.

Every rule here is enforced in the handler, not only in the template. A hidden
button is a courtesy to somebody using the site normally; it is not a rule, and
the difference matters most for exactly the person a block exists to stop.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, request, url_for

from . import uploads
from .db import get_db
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


def unread_sql(me: str = ":me", other: str = "m.id") -> str:
    """How many unread messages the signed-in member has from `other`.

    A string rather than a function because its one caller is the directory
    query in `members.index`, which needs the number per row and sorts on it —
    one query that answers for everybody, rather than one query per face on the
    page. The two parameters are SQL fragments chosen by this module, never by a
    request.

    `author_id = other` is what makes it unread *from them*: a member's own
    messages are not unread, and without that clause looking at your own card
    would count everything you have ever written.
    """
    return f"""
        SELECT COUNT(*) FROM messages msg
          JOIN conversation_members mine
            ON mine.conversation_id = msg.conversation_id AND mine.member_id = {me}
          JOIN conversation_members theirs
            ON theirs.conversation_id = msg.conversation_id AND theirs.member_id = {other}
         WHERE msg.author_id = {other}
           AND {other} != {me}
           AND msg.deleted_at IS NULL
           AND (mine.last_read_at IS NULL OR msg.created_at > mine.last_read_at)
    """


def history(other_id: int) -> dict:
    """The conversation with `other_id`, and the fact of having read it.

    Called by the member page, which renders it. Reading is a side effect on
    purpose: opening somebody's page *is* opening the conversation, there is no
    second step any more, and so there is no moment at which the messages are on
    screen and still counted as unread.

    Creates nothing. A conversation comes into being with the first message
    (`talk` below), so looking at a page is not enough to put a row in the
    database — which is what kept the old inbox free of entries saying "no
    messages yet" for people nobody had written to.
    """
    db = get_db()
    blocked = blocked_between(g.member["id"], other_id)
    conversation_id = existing_conversation(other_id)
    if conversation_id is None:
        return {"messages": [], "images": {}, "blocked": blocked}

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
    return {
        "messages": rows,
        "images": uploads.for_messages([row["id"] for row in rows]),
        "blocked": blocked,
    }


def _back_to(member_row, anchor: str = "") -> str:
    return url_for("members.show", login=member_row["gitea_login"]) + anchor


# --- routes ---------------------------------------------------------------

@bp.route("/privados")
@login_required
def inbox():
    """Kept as a redirect, not deleted.

    This was the *Privados* section until the people became the section. The
    route stays because members have the address in their history and admins
    have it written down, and a bookmark that lands on the directory is a
    bookmark that still works.
    """
    return redirect(url_for("members.index"))


@bp.route("/privados/con/<int:member_id>")
@login_required
def talk_redirect(member_id: int):
    """The old per-conversation address, pointed at the person's page.

    By id, because that is what the old links carry; everything written from now
    on uses the login, which is the same address as their public page.
    """
    other = get_db().execute(
        "SELECT gitea_login FROM members WHERE id = ? AND role != 'tombstone'",
        (member_id,),
    ).fetchone()
    if other is None:
        abort(404)
    return redirect(url_for("members.show", login=other["gitea_login"]))


@bp.route("/privados/con/<int:member_id>", methods=["POST"])
@login_required
def talk(member_id: int):
    """Send one message. The reading half is `history()`, on the member page.

    POST only. There used to be a GET here rendering the whole conversation;
    that page is now the person's page, and leaving a GET behind would have
    meant two addresses rendering the same thing, drifting apart.
    """
    if member_id == g.member["id"]:
        abort(400)
    db = get_db()
    other = db.execute(
        """SELECT * FROM members
            WHERE id = ? AND active = 1 AND role != 'tombstone'""",
        (member_id,),
    ).fetchone()
    if other is None:
        abort(404)
    if blocked_between(g.member["id"], member_id):
        abort(403)

    body = request.form.get("body", "").strip()[:BODY_MAX]
    try:
        staged = uploads.stage(request.files.getlist("pictures"))
    except uploads.RejectedUpload as exc:
        flash(str(exc), "error")
        return redirect(_back_to(other))
    if not body and not staged:
        flash("Escribe algo o adjunta una imagen.", "error")
        return redirect(_back_to(other))

    # Only now does the conversation come into being.
    conversation_id = existing_conversation(member_id) or conversation_with(member_id)
    cursor = db.execute(
        "INSERT INTO messages (conversation_id, author_id, body_md) VALUES (?, ?, ?)",
        (conversation_id, g.member["id"], body),
    )
    uploads.save(staged, g.member["id"], message_id=cursor.lastrowid)
    return redirect(_back_to(other, "#final"))


@bp.route("/privados/bloquear/<int:member_id>", methods=["POST"])
@login_required
def block(member_id: int):
    if member_id == g.member["id"]:
        abort(400)
    other = get_db().execute(
        "SELECT * FROM members WHERE id = ? AND role != 'tombstone'", (member_id,)
    ).fetchone()
    if other is None:
        abort(404)
    get_db().execute(
        """INSERT INTO blocks (blocker_id, blocked_id) VALUES (?, ?)
           ON CONFLICT DO NOTHING""",
        (g.member["id"], member_id),
    )
    flash("Bloqueado. Ninguno de los dos puede escribir al otro.", "ok")
    return redirect(_back_to(other))


@bp.route("/privados/desbloquear/<int:member_id>", methods=["POST"])
@login_required
def unblock(member_id: int):
    """Only your own block. Removing somebody else's would let the blocked
    person undo the thing that was done to protect against them."""
    other = get_db().execute(
        "SELECT * FROM members WHERE id = ? AND role != 'tombstone'", (member_id,)
    ).fetchone()
    if other is None:
        abort(404)
    get_db().execute("DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
                     (g.member["id"], member_id))
    flash("Desbloqueado.", "ok")
    return redirect(_back_to(other))
