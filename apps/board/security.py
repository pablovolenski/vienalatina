"""CSRF tokens and the decorators that gate routes by role.

The CSRF check is installed once as a before-request hook in app.py rather than
as a decorator on each handler. A decorator is something a future route can
forget; a hook covering every unsafe method is something it has to actively opt
out of.
"""

from __future__ import annotations

import hmac
import secrets
from functools import wraps

from flask import abort, g, redirect, request, session, url_for

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf() -> None:
    if request.method in SAFE_METHODS:
        return
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    # compare_digest rather than == so a wrong token cannot be guessed a
    # character at a time by measuring how long the comparison takes.
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, "Formulario caducado. Vuelve a cargar la página e inténtalo de nuevo.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.member is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# Most trusted first. Every decorator below is "this role or anything above it",
# which is the one rule worth keeping in a list rather than in four functions:
# adding a tier then means adding a word here, not auditing every check.
LADDER = ("superadmin", "owner", "admin", "moderator", "user")


def _at_least(rank: str):
    """The roles that pass a check for `rank` — it and everything above it."""
    return LADDER[:LADDER.index(rank) + 1]


def is_at_least(role: str, rank: str) -> bool:
    """`role` is `rank` or anything above it.

    Exported because seven places outside this module had the same question
    written as a literal tuple — `g.member["role"] in ("owner", "admin")` — and
    every one of them silently excluded the superadministrator the moment a tier
    was added above owner. A tuple is a copy of a rule; this is the rule.
    """
    return role in _at_least(rank)


def admin_required(view):
    """The superadministrator and the responsable both count as admins.

    Not the other way round: an admin is not a responsable, and neither is a
    responsable the superadministrator.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.member["role"] not in _at_least("admin"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def moderator_required(view):
    """Curating the public site. Admins and the owner count as moderators.

    Deliberately separate from admin_required: a moderator decides what the
    public sees and nothing else. They cannot reach Gestión, cannot create or
    deactivate anybody, and cannot change a role — moderating content is not
    power over people.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.member["role"] not in _at_least("moderator"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    """The association's chair. The superadministrator passes it too.

    Everything the responsable may do, the superadministrator may do; the
    reverse is the whole point of there being two roles, and is
    `superadmin_required` below.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.member["role"] not in _at_least("owner"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def superadmin_required(view):
    """The platform: admin accounts, the look, the identity, the system tools.

    The only check with nothing above it. A responsable who wants one of these
    screens is a responsable who should be asking the person who installed this,
    which is exactly the line the two roles are drawn along.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.member["role"] != "superadmin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped
