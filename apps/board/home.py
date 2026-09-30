"""Inicio — the one screen a member lands on.

Every section of the members area was added in its own phase, and each one added
an entry to a flat navigation until signing in meant choosing between five words
with no idea which one held anything new. This page answers that first question:
what has happened since I was last here, and what is waiting for me.

It owns no data. Every panel is a query the module responsible already runs for
its own page — the wall's newest threads, the unread count from `messages`, the
queue from `submissions` — gathered here and linked back there. Nothing on this
page is the only way to reach anything.
"""

from __future__ import annotations

from flask import Blueprint, g, render_template

from . import submissions
from .db import TOMBSTONE_LOGIN, get_db
from .render import excerpt
from .security import admin_required, login_required

bp = Blueprint("home", __name__)

RECENT = 5


@bp.route("/")
@login_required
def index():
    db = get_db()
    threads = db.execute(
        """SELECT t.id, t.title, t.body_md, t.created_at, m.display_name AS author,
                  m.gitea_login AS author_login, m.profile_published AS author_published,
                  (SELECT COUNT(*) FROM comments c
                    WHERE c.thread_id = t.id AND c.deleted_at IS NULL) AS replies
             FROM threads t JOIN members m ON m.id = t.author_id
            WHERE t.deleted_at IS NULL
            ORDER BY t.created_at DESC LIMIT ?""",
        (RECENT,),
    ).fetchall()

    conversations = db.execute(
        """SELECT other.id AS other_id, other.display_name AS other_name,
                  other.gitea_login AS other_login,
                  other.profile_published AS other_published,
                  (SELECT COUNT(*) FROM messages msg
                    WHERE msg.conversation_id = c.id AND msg.author_id != mine.member_id
                      AND msg.deleted_at IS NULL
                      AND (mine.last_read_at IS NULL OR msg.created_at > mine.last_read_at)
                  ) AS unread,
                  (SELECT MAX(created_at) FROM messages
                    WHERE conversation_id = c.id AND deleted_at IS NULL) AS last_at
             FROM conversations c
             JOIN conversation_members mine
               ON mine.conversation_id = c.id AND mine.member_id = :me
             JOIN conversation_members theirs
               ON theirs.conversation_id = c.id AND theirs.member_id != :me
             JOIN members other ON other.id = theirs.member_id
            ORDER BY last_at DESC NULLS LAST LIMIT :limit""",
        {"me": g.member["id"], "limit": RECENT},
    ).fetchall()

    mine = db.execute(
        """SELECT id, title, state, note FROM submissions
            WHERE author_id = ? ORDER BY created_at DESC LIMIT ?""",
        (g.member["id"], RECENT),
    ).fetchall()

    # Only the people who can act on it get the queue; for everyone else the
    # panel is not drawn, rather than drawn empty.
    queue = []
    if submissions.may_moderate():
        queue = db.execute(
            """SELECT s.id, s.title, s.created_at, m.display_name AS author,
                      m.gitea_login AS author_login,
                      m.profile_published AS author_published
                 FROM submissions s JOIN members m ON m.id = s.author_id
                WHERE s.state = 'pending' AND s.author_id != ?
                ORDER BY s.created_at LIMIT ?""",
            (g.member["id"], RECENT),
        ).fetchall()

    people = db.execute(
        """SELECT COUNT(*) AS n FROM members
            WHERE active = 1 AND role != 'tombstone' AND gitea_login != ?""",
        (TOMBSTONE_LOGIN,),
    ).fetchone()["n"]

    return render_template("home.html", threads=threads, conversations=conversations,
                           mine=mine, queue=queue, people=people, excerpt=excerpt,
                           state_labels=submissions.STATE_LABELS)


@bp.route("/gestion")
@admin_required
def gestion():
    """Administration, which is two links and a count rather than a screen of
    its own — Miembros and the static pages both already have their pages. It
    exists so that neither of them has to sit in the navigation every member
    sees, which is how *Contenido* ended up looking like a tab for everybody.
    """
    db = get_db()
    counts = {
        "members": db.execute(
            "SELECT COUNT(*) AS n FROM members WHERE role != 'tombstone'").fetchone()["n"],
        "suspended": db.execute(
            "SELECT COUNT(*) AS n FROM members WHERE active = 0 AND role != 'tombstone'"
        ).fetchone()["n"],
        "moderators": db.execute(
            "SELECT COUNT(*) AS n FROM members WHERE role IN ('owner', 'admin', 'moderator')"
        ).fetchone()["n"],
        "pending": db.execute(
            "SELECT COUNT(*) AS n FROM submissions WHERE state = 'pending'").fetchone()["n"],
    }
    return render_template("gestion.html", counts=counts)
