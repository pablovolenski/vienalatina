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

from flask import Blueprint, g, jsonify, render_template, request, url_for

from . import submissions
from .db import TOMBSTONE_LOGIN, get_db
from .render import excerpt, to_html
from .security import admin_required, csrf_token, login_required

bp = Blueprint("home", __name__)

RECENT = 5

# The longest text the preview will render. The forms themselves cap what can be
# saved; this is only here so a crafted request cannot ask for a megabyte of
# markdown to be parsed on every keystroke.
BODY_MAX = 100_000


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


@bp.route("/sesion.json")
def session_state():
    """Who is signed in, for the bar the public site draws.

    vienalatina.com is files on disk; it cannot know who is reading it, and
    baking the answer into those files would mean caching one member's name for
    the next visitor. So the browser asks here instead, and this is the only
    place that decides — including which sections to offer, read from the same
    rule the navigation uses, so the two cannot drift.

    Three properties worth stating, because each is a way this leaks:

    * **A visitor gets `{"signed_in": false}` and nothing else.** No names, no
      counts, no hint about who else exists.
    * **`no-store`.** A cached answer is somebody else's name on a shared
      machine, or a bar that stays after signing out.
    * **No badge counts.** They would need queries on every public page view,
      for a number nobody can act on until they are inside anyway.
    """
    if g.member is None:
        return _no_store(jsonify({"signed_in": False}))

    sections = [
        {"label": "Inicio", "url": url_for("home.index")},
        {"label": "Muro", "url": url_for("board.threads")},
        {"label": "Privados", "url": url_for("messages.inbox")},
        {"label": "Publicaciones", "url": url_for("submissions.index")},
    ]
    if g.member["role"] in ("owner", "admin"):
        sections.append({"label": "Gestión", "url": url_for("home.gestion")})

    return _no_store(jsonify({
        "signed_in": True,
        "name": g.member["display_name"] or g.member["gitea_login"],
        "sections": sections,
        "profile": url_for("profiles.edit"),
        "logout": url_for("auth.logout"),
        # Signing out is a POST, so the bar needs the token to render a real
        # form. Handing it to a script on our own pages gives nothing away: a
        # page on another origin cannot read this response — no CORS header
        # allows it — and the session cookie is SameSite=Lax, so it is not sent
        # with a cross-site POST in the first place.
        "csrf": csrf_token(),
    }))


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/previsualizar", methods=["POST"])
@login_required
def preview():
    """Render markdown the way the site will, for the Vista previa button.

    A round trip rather than a markdown parser in the browser, deliberately. The
    preview has to agree with what gets published, and the only way to guarantee
    that is to render it with the same function — `render.to_html`, with raw HTML
    disabled, which is the whole of the XSS defence here. A second parser in
    JavaScript would be a second opinion about what somebody's text means, and
    the two would disagree the first time anybody wrote something unusual.
    """
    body = request.form.get("body", "")[:BODY_MAX]
    return _no_store(jsonify({"html": to_html(body)}))


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
