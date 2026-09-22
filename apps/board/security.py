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


def admin_required(view):
    """Owner counts as an admin. Admin does not count as owner."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.member["role"] not in ("owner", "admin"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.member["role"] != "owner":
            abort(403)
        return view(*args, **kwargs)
    return wrapped
