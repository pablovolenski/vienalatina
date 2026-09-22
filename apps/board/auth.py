"""Sign-in through Gitea.

The rule this module exists to enforce: **a Gitea account is not a membership.**
Gitea answers "who is this person"; the members table answers "may they be
here". Conflating the two would admit every account on the instance, including
the `vienalatina-translations` bot, and would mean anyone who ever gets a Gitea
account for an unrelated reason silently gains access to the board.
"""

from __future__ import annotations

import secrets

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                   request, session, url_for)

from . import gitea
from .db import get_db

bp = Blueprint("auth", __name__)


def redirect_uri() -> str:
    return current_app.config["BASE_URL"].rstrip("/") + url_for("auth.callback")


def load_member() -> None:
    """Attach the signed-in member to `g`, or None. Runs on every request.

    The role is read from the database each time rather than cached in the
    session, so demoting or deactivating somebody takes effect on their next
    click instead of whenever their cookie happens to expire.
    """
    g.member = None
    member_id = session.get("member_id")
    if member_id is None:
        return
    row = get_db().execute(
        """SELECT * FROM members
            WHERE id = ? AND active = 1 AND role IN ('owner', 'admin', 'user')""",
        (member_id,),
    ).fetchone()
    if row is None:
        session.clear()
        return
    g.member = row


@bp.route("/login")
def login():
    if g.member is not None:
        return redirect(url_for("board.threads"))
    return render_template("login.html", next=request.args.get("next", ""))


@bp.route("/login/start")
def start():
    # State ties the callback to this browser session; without it, an attacker
    # can feed you their own authorization code and log you into their account.
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    session["oauth_next"] = request.args.get("next", "")
    return redirect(gitea.authorize_url(state, redirect_uri()))


@bp.route("/auth/callback")
def callback():
    expected = session.pop("oauth_state", None)
    given = request.args.get("state")
    if not expected or not given or not secrets.compare_digest(expected, given):
        flash("El inicio de sesión no se pudo verificar. Inténtalo de nuevo.", "error")
        return redirect(url_for("auth.login"))

    code = request.args.get("code", "")
    if not code:
        flash("Gitea no devolvió un código de autorización.", "error")
        return redirect(url_for("auth.login"))

    try:
        token = gitea.exchange_code(code, redirect_uri())
        profile = gitea.fetch_user(token)
    except gitea.GiteaError as exc:
        current_app.logger.warning("OAuth failed: %s", exc)
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))
    except Exception:  # network trouble, malformed JSON, Gitea down
        current_app.logger.exception("OAuth failed unexpectedly")
        flash("No se pudo contactar con Gitea. Inténtalo más tarde.", "error")
        return redirect(url_for("auth.login"))

    login_name = (profile.get("login") or "").strip()
    db = get_db()
    member = db.execute(
        """SELECT * FROM members
            WHERE gitea_login = ? AND active = 1 AND role IN ('owner', 'admin', 'user')""",
        (login_name,),
    ).fetchone()

    if member is None:
        # Says nothing about whether the account exists, is inactive, or was
        # never a member: an outsider who reaches this page learns only that
        # they are not in.
        current_app.logger.info("Rejected sign-in for non-member %r", login_name)
        flash("Tu cuenta no tiene acceso a esta área. Pide a un administrador que te dé de alta.",
              "error")
        return redirect(url_for("auth.login"))

    db.execute(
        """UPDATE members
              SET display_name = ?, email = ?, last_seen_at = datetime('now')
            WHERE id = ?""",
        (profile.get("full_name") or login_name, profile.get("email") or "", member["id"]),
    )

    # A fresh session id on privilege change, so a cookie captured before login
    # is not still valid after it.
    session.clear()
    session["member_id"] = member["id"]

    target = request.args.get("next") or session.pop("oauth_next", "") or ""
    # Only ever redirect within this app: an absolute URL here would make the
    # login page an open redirect that phishing can point anywhere.
    if not target.startswith(current_app.config["URL_PREFIX"] + "/"):
        target = url_for("board.threads")
    return redirect(target)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Sesión cerrada.", "ok")
    return redirect(url_for("auth.login"))
