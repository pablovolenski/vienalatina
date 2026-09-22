"""Members and roles.

Three roles, and the rules between them are short enough to state in full:

* exactly one **owner**, who creates and removes admins and can hand ownership
  on; nobody can deactivate or demote them, including themselves
* **admins** create and deactivate users, and moderate the board
* **users** post, comment, and edit or delete their own writing

The predicates live as plain functions at the top of this module so they can be
tested without a request, a session or a browser — and so that reading them
does not mean reading route handlers.
"""

from __future__ import annotations

import json
import re
import sqlite3

from flask import (Blueprint, Response, abort, flash, g, redirect,
                   render_template, request, url_for)

from . import gitea
from .db import TOMBSTONE_LOGIN, get_db
from .security import admin_required, login_required, owner_required

bp = Blueprint("members", __name__)

# Gitea's own rule, restated: letters, digits, and . - _ inside, never at the
# edges. Checked here so a bad name fails before we create anything anywhere.
LOGIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,38}[A-Za-z0-9])?$")

ROLE_LABELS = {"owner": "Responsable", "admin": "Administrador", "user": "Usuario"}


def may_create(actor_role: str, target_role: str) -> bool:
    """Who may bring whom in. Admins cannot mint more admins."""
    if target_role == "admin":
        return actor_role == "owner"
    if target_role == "user":
        return actor_role in ("owner", "admin")
    return False


def may_manage(actor_role: str, target_role: str) -> bool:
    """Deactivate, reactivate, or change the role of an existing member."""
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


def erase_member(db: sqlite3.Connection, member_id: int) -> None:
    """Remove a member and their personal data, keeping the conversation intact.

    GDPR erasure means the name, login and address go. It does not mean the
    threads other people replied to should vanish, so authorship moves to the
    tombstone row instead of cascading or dangling.
    """
    ghost = tombstone_id(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("UPDATE threads  SET author_id = ? WHERE author_id = ?", (ghost, member_id))
        db.execute("UPDATE comments SET author_id = ? WHERE author_id = ?", (ghost, member_id))
        db.execute("UPDATE members  SET created_by = NULL WHERE created_by = ?", (member_id,))
        db.execute("DELETE FROM members WHERE id = ? AND role != 'owner'", (member_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


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
            ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                     m.display_name COLLATE NOCASE"""
    ).fetchall()
    return render_template("members.html", members=rows, labels=ROLE_LABELS)


@bp.route("/miembros/nuevo", methods=["GET", "POST"])
@admin_required
def new():
    if request.method == "GET":
        return render_template("member_new.html", can_make_admin=g.member["role"] == "owner")

    login = request.form.get("login", "").strip()
    display_name = request.form.get("display_name", "").strip()
    email = request.form.get("email", "").strip()
    role = request.form.get("role", "user")
    create_account = request.form.get("create_account") == "on"

    if not may_create(g.member["role"], role):
        abort(403)
    if not LOGIN_RE.match(login):
        flash("El usuario solo puede tener letras, números, punto, guion y guion bajo.", "error")
        return redirect(url_for("members.new"))
    if create_account and "@" not in email:
        flash("Hace falta un correo válido para crear la cuenta en Gitea.", "error")
        return redirect(url_for("members.new"))

    db = get_db()
    if db.execute("SELECT 1 FROM members WHERE gitea_login = ?", (login,)).fetchone():
        flash("Ese usuario ya es miembro.", "error")
        return redirect(url_for("members.new"))

    password = None
    if create_account:
        password = gitea.generate_password()
        try:
            gitea.admin_create_user(login, email, display_name or login, password)
        except gitea.GiteaError as exc:
            flash(str(exc), "error")
            return redirect(url_for("members.new"))

    try:
        db.execute(
            """INSERT INTO members (gitea_login, display_name, email, role, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (login, display_name or login, email, role, g.member["id"]),
        )
    except sqlite3.IntegrityError:
        flash("No se pudo dar de alta a ese miembro.", "error")
        return redirect(url_for("members.new"))

    # Shown once and never stored: Gitea has the hash, we have nothing.
    return render_template("member_created.html", login=login, password=password,
                           role_label=ROLE_LABELS[role])


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
@owner_required
def set_role(member_id: int):
    target = _load(member_id)
    role = request.form.get("role", "")
    if role not in ("admin", "user") or not may_manage(g.member["role"], target["role"]):
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
    erase_member(get_db(), member_id)
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
