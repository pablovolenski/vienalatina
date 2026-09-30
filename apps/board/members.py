"""Members and roles.

Four roles, and the rules between them are short enough to state in full:

* exactly one **owner**, who creates and removes admins and can hand ownership
  on; nobody can deactivate or demote them, including themselves
* **admins** create, deactivate and promote members, moderate the board, and
  publish and edit the static pages of the public site
* **moderators** approve or reject what members propose for the public site,
  and publish their own posts without waiting for anybody. They have no power
  over people: moderating content is not the same authority, and giving one the
  other is how a curation role turns into a second admin by accident
* **members** post on the wall, write privately, keep a public profile, and
  propose posts for the public site

The predicates live as plain functions at the top of this module so they can be
tested without a request, a session or a browser — and so that reading them
does not mean reading route handlers.
"""

from __future__ import annotations

import json
import re
import sqlite3

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from . import auth, invites, mail, profiles, uploads
from .db import TOMBSTONE_LOGIN, get_db
from .security import admin_required, login_required, owner_required

bp = Blueprint("members", __name__)

# Gitea's own rule, restated: letters, digits, and . - _ inside, never at the
# edges. Checked here so a bad name fails before we create anything anywhere.
LOGIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,38}[A-Za-z0-9])?$")

# "Miembro" rather than "Usuario" on the screens, while the stored value stays
# `user`: renaming the value would mean a migration and rewriting every role
# check for a word nobody types.
ROLE_LABELS = {"owner": "Responsable", "admin": "Administrador",
               "moderator": "Moderador", "user": "Miembro"}

# Who may be given which role on the Miembros screen, most trusted first, so
# the form and the predicates below cannot disagree about the list.
ASSIGNABLE_ROLES = ("admin", "moderator", "user")


def may_create(actor_role: str, target_role: str) -> bool:
    """Who may bring whom in. Admins cannot mint more admins."""
    if target_role == "admin":
        return actor_role == "owner"
    if target_role in ("moderator", "user"):
        return actor_role in ("owner", "admin")
    return False


def may_manage(actor_role: str, target_role: str) -> bool:
    """Deactivate, reactivate, or change the role of an existing member.

    A moderator appears nowhere in this function, deliberately: they approve
    posts and nothing else. Being trusted to judge what the public reads is not
    the same as being trusted to suspend the person who wrote it.
    """
    if target_role == "owner":
        return False  # the owner is out of reach of everyone, themselves included
    if target_role == "admin":
        return actor_role == "owner"
    return actor_role in ("owner", "admin")


def tombstone_id(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT id FROM members WHERE gitea_login = ?", (TOMBSTONE_LOGIN,)).fetchone()
    return row["id"]


def transfer_ownership(db: sqlite3.Connection, owner_id: int, target_id: int) -> None:
    """Hand ownership to an admin, atomically.

    The demotion has to come first. With the partial unique index in place, a
    promote-then-demote order would momentarily ask for two owners and the
    database would refuse — correctly, but confusingly.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("UPDATE members SET role = 'admin' WHERE id = ?", (owner_id,))
        db.execute("UPDATE members SET role = 'owner' WHERE id = ?", (target_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# Every column pointing at members(id) that does not cascade has to be dealt
# with before the row can go, or SQLite refuses the delete and the admin gets a
# 500 with nothing to read. These two sets are the list, and
# test_members.py checks them against the schema's actual foreign keys — so a
# table added later fails a test here rather than a button in production. That
# is not hypothetical: `attachments` was added and forgotten, and the first
# person who tried to remove a member who had posted a photo got the 500.
REASSIGNED_ON_ERASE = {
    ("threads", "author_id"),
    ("comments", "author_id"),
    # The picture belongs to the thread, which survives as "Miembro eliminado",
    # so it is reassigned rather than deleted, exactly like the words around it.
    # Removing the picture itself means deleting the post it is attached to.
    ("attachments", "uploaded_by"),
}
CLEARED_ON_ERASE = {("members", "created_by")}

# Private correspondence is not reassigned to the tombstone, it goes. A thread
# outlives its author because other people replied and the conversation would
# otherwise lose its shape; a two-party exchange has no such remainder, and
# keeping half of somebody's erased correspondence is close to the thing
# erasure exists to prevent. The other person's copy goes with it, which is the
# uncomfortable half of that choice and is meant to be.
REMOVED_ON_ERASE = {
    ("conversation_members", "member_id"),
    ("messages", "author_id"),
    ("blocks", "blocker_id"),
    ("blocks", "blocked_id"),
    # A submission is private writing until a moderator approves it, so it goes
    # with the rest of the private writing. The remainder of an approved one is
    # the published post in the site repository, which already carries this
    # person's name in the commit and is not ours to rewrite from here.
    ("submissions", "author_id"),
}

# Who reviewed somebody else's submission is a fact about that submission, not
# personal data of the reviewer, so it survives their erasure with the name
# removed rather than taking the submission down with it.
CLEARED_ON_ERASE_AFTER = {("submissions", "reviewed_by")}


def _conversations_of(member_id: int) -> str:
    return "SELECT conversation_id FROM conversation_members WHERE member_id = ?"


def erase_member(db: sqlite3.Connection, member_id: int) -> list[str]:
    """Remove a member and their personal data, keeping the board intact.

    GDPR erasure means the name, login and address go. It does not mean the
    threads other people replied to should vanish, so public authorship moves
    to the tombstone row instead of cascading or dangling. Private messages are
    the exception and are deleted outright — see REMOVED_ON_ERASE.

    Returns the stored names of pictures whose rows have gone, so the caller can
    take them off disk. Deleting files is not done here because this function
    is a transaction: a file removed inside one cannot be put back if the
    transaction rolls away underneath it.
    """
    ghost = tombstone_id(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        # Read before deleting: once the conversations are gone there is
        # nothing left to work out which files they carried.
        photo = db.execute("SELECT photo_name FROM members WHERE id = ?",
                           (member_id,)).fetchone()
        orphaned = [photo["photo_name"]] if photo and photo["photo_name"] else []
        orphaned += [row["stored_name"] for row in db.execute(
            f"""SELECT a.stored_name FROM attachments a
                  JOIN messages m ON m.id = a.message_id
                 WHERE m.conversation_id IN ({_conversations_of(member_id)})""",
            (member_id,),
        )]
        # Attachments first: they point at messages without cascading, so
        # deleting the conversation while they exist is refused.
        db.execute(
            f"""DELETE FROM attachments WHERE message_id IN (
                    SELECT id FROM messages
                     WHERE conversation_id IN ({_conversations_of(member_id)}))""",
            (member_id,),
        )
        db.execute(
            f"DELETE FROM conversations WHERE id IN ({_conversations_of(member_id)})",
            (member_id,),
        )
        db.execute("DELETE FROM blocks WHERE blocker_id = ? OR blocked_id = ?",
                   (member_id, member_id))
        orphaned += [row["photo_name"] for row in db.execute(
            "SELECT photo_name FROM submissions WHERE author_id = ? AND photo_name IS NOT NULL",
            (member_id,),
        )]
        db.execute("DELETE FROM submissions WHERE author_id = ?", (member_id,))
        for table, column in sorted(CLEARED_ON_ERASE_AFTER):
            db.execute(f"UPDATE {table} SET {column} = NULL WHERE {column} = ?",
                       (member_id,))
        # Table and column names come from the module constants above, never
        # from a request, so the interpolation is not a place user input can
        # reach.
        for table, column in sorted(REASSIGNED_ON_ERASE):
            db.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                       (ghost, member_id))
        for table, column in sorted(CLEARED_ON_ERASE):
            db.execute(f"UPDATE {table} SET {column} = NULL WHERE {column} = ?",
                       (member_id,))
        db.execute("DELETE FROM members WHERE id = ? AND role != 'owner'", (member_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return orphaned


def _load(member_id: int):
    row = get_db().execute(
        "SELECT * FROM members WHERE id = ? AND role != 'tombstone'", (member_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/miembros")
@login_required
def index():
    rows = get_db().execute(
        """SELECT m.*, c.display_name AS creator
             FROM members m
             LEFT JOIN members c ON c.id = m.created_by
            WHERE m.role != 'tombstone'
            ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1
                                 WHEN 'moderator' THEN 2 ELSE 3 END,
                     m.display_name COLLATE NOCASE"""
    ).fetchall()
    return render_template("members.html", members=rows, labels=ROLE_LABELS)


@bp.route("/miembros/nuevo", methods=["GET", "POST"])
@admin_required
def new():
    if request.method == "GET":
        return render_template(
            "member_new.html",
            can_make_admin=g.member["role"] == "owner",
            # Without mail the invitation cannot leave the building, so the
            # admin has to pass the link on by hand. Worth knowing before
            # filling the form rather than after.
            can_send_mail=mail.configured(),
        )

    login = request.form.get("login", "").strip()
    display_name = request.form.get("display_name", "").strip()
    email = request.form.get("email", "").strip()
    role = request.form.get("role", "user")

    if not may_create(g.member["role"], role):
        abort(403)
    if not LOGIN_RE.match(login):
        flash("El usuario solo puede tener letras, números, punto, guion y guion bajo.", "error")
        return redirect(url_for("members.new"))
    if not profiles.name_is_available(login):
        # The name would collide with a path the site already serves. The
        # routing gives the static site every collision, so nothing breaks —
        # their public page would simply never load. Refused here so nobody
        # finds that out months later.
        flash(f"«{login}» coincide con una dirección del sitio. Elige otro.", "error")
        return redirect(url_for("members.new"))
    if "@" not in email:
        # Required now, not optional: the address is how the invitation gets
        # there, and a member with no way to set a password is a row that can
        # never be used.
        flash("Hace falta un correo válido: ahí llega la invitación.", "error")
        return redirect(url_for("members.new"))

    db = get_db()
    if db.execute("""SELECT 1 FROM members
                      WHERE gitea_login = ? COLLATE NOCASE
                         OR email = ? COLLATE NOCASE""",
                  (login, email)).fetchone():
        # One message for either collision. Which of the two it was is not
        # something an admin needs and not something worth leaking if this
        # screen is ever opened by somebody it should not be.
        flash("Ese usuario o ese correo ya están en uso.", "error")
        return redirect(url_for("members.new"))

    try:
        cursor = db.execute(
            """INSERT INTO members (gitea_login, display_name, email, role, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (login, display_name or login, email, role, g.member["id"]),
        )
    except sqlite3.IntegrityError:
        flash("No se pudo dar de alta a ese miembro.", "error")
        return redirect(url_for("members.new"))

    # No password is set here and none is generated. The member chooses their
    # own through the invitation, and until they do, password_hash is NULL and
    # cannot be signed in with.
    invite_link = None
    mail_problem = None
    link = auth.invite_url(invites.issue(cursor.lastrowid, "invite"))
    try:
        mail.send_invite(email, display_name or login, link)
    except mail.MailNotConfigured:
        invite_link, mail_problem = link, "unconfigured"
    except mail.MailFailed:
        invite_link, mail_problem = link, "failed"

    return render_template("member_created.html", login=login,
                           email=email, created=True,
                           invite_link=invite_link, mail_problem=mail_problem,
                           role_label=ROLE_LABELS[role])


@bp.route("/miembros/<int:member_id>/invitar", methods=["POST"])
@admin_required
def invite(member_id: int):
    """Send a fresh invitation to somebody who is already a member.

    This exists because creating the account and inviting the person were one
    action, and there are several ordinary ways to end up needing only the
    second: the admin made the account by hand in the account server and added
    the member with the box unticked, the mail failed the first time, the link
    sat unopened for more than a week, or the address was wrong and has been
    corrected. Before this, every one of those left a member with an account
    nobody knows the password to and no way to reach it.

    Issuing a new token invalidates the previous one, which `invites.issue`
    already guarantees — so a link that has been forwarded, or is sitting in a
    mailbox somebody else can read, stops working the moment a new one is sent.
    """
    target = _load(member_id)
    if not target["email"]:
        flash(f"{target['display_name']} no tiene correo. Añádelo primero.", "error")
        return redirect(url_for("members.index"))
    if not target["active"] or target["role"] == "tombstone":
        abort(403)

    link = auth.invite_url(invites.issue(member_id, "invite"))
    try:
        mail.send_invite(target["email"], target["display_name"], link)
    except (mail.MailNotConfigured, mail.MailFailed) as exc:
        # The link is shown rather than withheld: it is already issued and
        # valid, and the alternative is an admin who knows only that something
        # did not work.
        current_app.logger.warning("Invite mail to %s failed: %s", target["email"], exc)
        flash(f"No se pudo enviar el correo. Pásale este enlace: {link}", "error")
        return redirect(url_for("members.index"))

    flash(f"Invitación enviada a {target['email']}.", "ok")
    return redirect(url_for("members.index"))


@bp.route("/miembros/<int:member_id>/estado", methods=["POST"])
@admin_required
def set_active(member_id: int):
    target = _load(member_id)
    if not may_manage(g.member["role"], target["role"]):
        abort(403)
    active = 1 if request.form.get("active") == "1" else 0
    get_db().execute("UPDATE members SET active = ? WHERE id = ?", (active, member_id))
    flash(f"{target['display_name']}: acceso {'restaurado' if active else 'suspendido'}.", "ok")
    return redirect(url_for("members.index"))


@bp.route("/miembros/<int:member_id>/rol", methods=["POST"])
@admin_required
def set_role(member_id: int):
    """Admin-level rather than owner-level, now that there is a role between
    the two worth handing out routinely.

    Two predicates, both of them: `may_manage` for reaching this person at all,
    and `may_create` for the role being given — you may only grant a role you
    could have created somebody with, which keeps "admins cannot mint admins"
    true here as well without restating it.
    """
    target = _load(member_id)
    role = request.form.get("role", "")
    if role not in ASSIGNABLE_ROLES:
        abort(403)
    if not may_manage(g.member["role"], target["role"]):
        abort(403)
    if not may_create(g.member["role"], role):
        abort(403)
    get_db().execute("UPDATE members SET role = ? WHERE id = ?", (role, member_id))
    flash(f"{target['display_name']} ahora es {ROLE_LABELS[role].lower()}.", "ok")
    return redirect(url_for("members.index"))


@bp.route("/miembros/<int:member_id>/transferir", methods=["POST"])
@owner_required
def transfer(member_id: int):
    target = _load(member_id)
    if target["role"] != "admin" or not target["active"]:
        flash("Solo puedes transferir la titularidad a un administrador activo.", "error")
        return redirect(url_for("members.index"))
    transfer_ownership(get_db(), g.member["id"], target["id"])
    flash(f"{target['display_name']} es ahora el responsable. Tú eres administrador.", "ok")
    return redirect(url_for("members.index"))


@bp.route("/miembros/<int:member_id>/eliminar", methods=["POST"])
@owner_required
def erase(member_id: int):
    target = _load(member_id)
    if target["role"] == "owner":
        abort(403)
    # Files only after the transaction has committed: a rollback can put the
    # rows back, and nothing can put the pictures back.
    for stored_name in erase_member(get_db(), member_id):
        uploads.remove(stored_name)
    flash(f"{target['display_name']} eliminado. Sus mensajes quedan como «Miembro eliminado».", "ok")
    return redirect(url_for("members.index"))


@bp.route("/mis-datos")
@login_required
def export():
    """Everything this member wrote, as JSON. Their data, on request."""
    db = get_db()
    me = g.member
    threads = db.execute(
        """SELECT id, title, body_md, created_at, edited_at FROM threads
            WHERE author_id = ? AND deleted_at IS NULL ORDER BY created_at""",
        (me["id"],),
    ).fetchall()
    comments = db.execute(
        """SELECT id, thread_id, body_md, created_at, edited_at FROM comments
            WHERE author_id = ? AND deleted_at IS NULL ORDER BY created_at""",
        (me["id"],),
    ).fetchall()
    payload = {
        "member": {
            "gitea_login": me["gitea_login"],
            "display_name": me["display_name"],
            "email": me["email"],
            "role": me["role"],
            "created_at": me["created_at"],
        },
        "threads": [dict(row) for row in threads],
        "comments": [dict(row) for row in comments],
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="mis-datos.json"'},
    )
