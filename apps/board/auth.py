"""Signing in, entirely on vienalatina.com.

The rule this module exists to enforce: **a member row is the membership.**
There is one place a person can be signed in, one session to end, and one
password store, and all three are here.

It used to be otherwise. Identity was delegated to the git server, which meant
a member was handed to another domain to type their password, handed back, and
could never really be signed out — that server owned the session and its logout
cannot be triggered from here. Everything confusing about the old flow came
from that one decision, so it was reversed. The git server is now what it
should always have been: somewhere the site's content is stored, which members
never see.
"""

from __future__ import annotations

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                   request, session, url_for)

from . import invites, mail, passwords
from .db import get_db

bp = Blueprint("auth", __name__)

# Gitea enforces its own minimum as well; this one is stricter so the member is
# told before the round trip rather than after it, in their own language.
PASSWORD_MIN = passwords.MINIMUM


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
            WHERE id = ? AND active = 1 AND role != 'tombstone'""",
        (member_id,),
    ).fetchone()
    if row is None:
        session.clear()
        return
    g.member = row


def safe_next(target: str) -> str:
    """Only ever inside this app.

    An absolute or protocol-relative URL here would make the login form an open
    redirect: a crafted link signs somebody in and drops them on a page
    somebody else controls, with the trust of having just arrived from their
    own community site.

    Public rather than private now, because the login form is not the only place
    that sends somebody back where they came from: any form carrying a `back`
    field has the same hole, and two copies of this rule is one copy too many.
    """
    prefix = current_app.config["URL_PREFIX"] + "/"
    return target if target.startswith(prefix) else url_for("home.index")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.member is not None:
        return redirect(url_for("home.index"))

    target = request.values.get("next", "")
    if request.method == "GET":
        return render_template("login.html", next=target)

    identifier = request.form.get("identifier", "").strip()
    password = request.form.get("password", "")

    # One message for every way this can fail — unknown name, wrong password,
    # suspended member, invited but never arrived. Distinguishing them turns
    # the form into a way to find out who is a member, one guess at a time.
    def refuse(reason: str):
        current_app.logger.info("Sign-in refused for %r: %s", identifier, reason)
        passwords.record_attempt(identifier)
        flash("Usuario o contraseña incorrectos.", "error")
        return render_template("login.html", next=target), 401

    if not identifier or not password:
        return refuse("empty")
    if passwords.too_many_attempts(identifier):
        # Said plainly rather than hidden behind the same message: somebody
        # locked out by their own typing needs to know waiting will fix it.
        flash("Demasiados intentos. Espera unos minutos y vuelve a probar.", "error")
        return render_template("login.html", next=target), 429

    member = get_db().execute(
        """SELECT * FROM members
            WHERE (gitea_login = ? COLLATE NOCASE OR email = ? COLLATE NOCASE)
              AND role != 'tombstone'""",
        (identifier, identifier),
    ).fetchone()

    # verify() is called even when there is no member, against a decoy hash, so
    # an unknown name does not answer faster than a wrong password.
    if not passwords.verify(member["password_hash"] if member else None, password):
        return refuse("bad credentials")
    if not member["active"]:
        return refuse("suspended")

    # A fresh session id on privilege change, so a cookie captured before
    # sign-in is not still valid after it.
    session.clear()
    session["member_id"] = member["id"]
    passwords.forget_attempts(identifier)
    passwords.prune_attempts()
    get_db().execute("UPDATE members SET last_seen_at = datetime('now') WHERE id = ?",
                     (member["id"],))
    return remember_session(redirect(safe_next(target)))


# --- the hint the public site reads --------------------------------------
#
# The static pages on vienalatina.com are files on disk; nothing there knows who
# is looking. The members' bar is therefore drawn by a script that asks this app
# — and asking on every page view, for every stranger who ever lands on the
# site, to be told they are nobody, is a request per page for nothing.
#
# So sign-in also sets this: a second cookie holding the digit 1, readable by
# that script, saying only "somebody signed in on this browser once". Without it
# the script makes no request at all. It is a hint and never a credential: the
# real session cookie stays HttpOnly and signed, and `sesion.json` decides
# everything. Somebody who sets this by hand gets a bar that says they are not
# signed in.
SESSION_HINT = "vl_sesion"


def remember_session(response):
    response.set_cookie(
        SESSION_HINT, "1",
        max_age=60 * 60 * 24 * 30,
        secure=current_app.config["SESSION_COOKIE_SECURE"],
        httponly=False,          # the whole point: a script has to read it
        samesite="Lax",
        path="/",                # including the static site, which is why it exists
    )
    return response


def forget_session(response):
    response.delete_cookie(SESSION_HINT, path="/")
    return response


def invite_url(token: str) -> str:
    return current_app.config["BASE_URL"].rstrip("/") + url_for(
        "auth.set_password", token=token
    )


@bp.route("/invitacion/<token>", methods=["GET", "POST"])
def set_password(token: str):
    """Where a member chooses their own password, from an invite or a reset.

    One page for both, because they differ only in the wording and how long the
    link lived. The token is the only credential: somebody arriving here is not
    signed in and cannot be.
    """
    member = invites.lookup(token)
    if member is None:
        # Deliberately one message for every reason it might fail — expired,
        # already used, never existed. Distinguishing them tells whoever holds
        # a stale link something about the account it points at.
        return render_template("set_password.html", member=None, token=token,
                               minimum=PASSWORD_MIN), 400

    if request.method == "GET":
        return render_template("set_password.html", member=member, token=token,
                               minimum=PASSWORD_MIN)

    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if password != confirm:
        flash("Las dos contraseñas no coinciden.", "error")
    elif len(password) < PASSWORD_MIN:
        flash(f"La contraseña necesita al menos {PASSWORD_MIN} caracteres.", "error")
    else:
        get_db().execute("UPDATE members SET password_hash = ? WHERE id = ?",
                         (passwords.hash_password(password), member["id"]))
        # Only now: a token that set a password is spent, but one refused for
        # being too short has to keep working or a typo locks the member out of
        # an account they have never reached.
        invites.consume(member["invite_id"])
        flash("Contraseña guardada. Ya puedes entrar.", "ok")
        return redirect(url_for("auth.login"))

    return render_template("set_password.html", member=member, token=token,
                           minimum=PASSWORD_MIN), 400


@bp.route("/recuperar", methods=["GET", "POST"])
def recover():
    """Replaces Gitea's recovery page, which is dead without a mailer."""
    if request.method == "GET":
        return render_template("recover.html")

    email = request.form.get("email", "").strip()
    member = get_db().execute(
        """SELECT * FROM members
            WHERE email = ? COLLATE NOCASE AND active = 1
              AND role != 'tombstone'""",
        (email,),
    ).fetchone()

    if member is not None and not invites.rate_limited(member["id"]):
        try:
            mail.send_reset(member["email"], member["display_name"],
                            invite_url(invites.issue(member["id"], "reset")))
        except (mail.MailFailed, mail.MailNotConfigured) as exc:
            current_app.logger.warning("Reset mail failed: %s", exc)

    # The same answer either way, whatever happened above. Saying "no account
    # with that address" would turn this form into a way to find out who is a
    # member, one address at a time.
    return render_template("recover.html", sent=True)


@bp.route("/logout", methods=["POST"])
def logout():
    """One click, and it is done.

    This used to render a page explaining that signing out had not really
    signed you out, because the session that mattered belonged to another
    server we could not reach. There is only one session now.
    """
    session.clear()
    flash("Has cerrado sesión.", "ok")
    return forget_session(redirect(url_for("auth.login")))
