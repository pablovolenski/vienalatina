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

from . import gitea, invites, mail, tokens
from .db import get_db

bp = Blueprint("auth", __name__)

# Gitea enforces its own minimum as well; this one is stricter so the member is
# told before the round trip rather than after it, in their own language.
PASSWORD_MIN = 10


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
    return render_template("login.html", next=request.args.get("next", ""),
                           # No site-admin token means no password can be set,
                           # so offering recovery here would only lead somebody
                           # to a page that refuses.
                           can_recover=gitea.admin_configured())


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
        flash("No se recibió el código de autorización. Inténtalo de nuevo.", "error")
        return redirect(url_for("auth.login"))

    try:
        credentials = gitea.exchange_code(code, redirect_uri())
        profile = gitea.fetch_user(credentials["access_token"])
    except gitea.GiteaError as exc:
        current_app.logger.warning("OAuth failed: %s", exc)
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))
    except Exception:  # network trouble, malformed JSON, Gitea down
        current_app.logger.exception("OAuth failed unexpectedly")
        flash("No se pudo contactar con el servidor de cuentas. "
              "Inténtalo más tarde.", "error")
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

    # Kept so the editor can commit as this person rather than as a bot. Stored
    # in the database, never in the cookie — see apps/board/tokens.py.
    tokens.save(member["id"], credentials)

    target = request.args.get("next") or session.pop("oauth_next", "") or ""
    # Only ever redirect within this app: an absolute URL here would make the
    # login page an open redirect that phishing can point anywhere.
    if not target.startswith(current_app.config["URL_PREFIX"] + "/"):
        target = url_for("board.threads")
    return redirect(target)


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
    if not gitea.admin_configured():
        # Checked before the form is drawn rather than when it is submitted.
        # The server cannot save the password either way — but learning that
        # after choosing one, typing it twice and pressing the button reads as
        # "I did something wrong", which is the opposite of true. Answering
        # this way gives nothing away: the refusal is about the server, not
        # about the token or any account behind it.
        return render_template("set_password.html", member=None, token=token,
                               minimum=PASSWORD_MIN, unavailable=True), 503

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
        try:
            gitea.admin_set_password(member["gitea_login"], password)
        except gitea.GiteaError as exc:
            flash(str(exc), "error")
        else:
            # Only now: a token that set a password is spent, but one whose
            # password Gitea rejected has to keep working or the member is
            # locked out by a typo.
            invites.consume(member["invite_id"])
            flash("Contraseña guardada. Ya puedes entrar.", "ok")
            return redirect(url_for("auth.login"))

    return render_template("set_password.html", member=member, token=token,
                           minimum=PASSWORD_MIN), 400


@bp.route("/recuperar", methods=["GET", "POST"])
def recover():
    """Replaces Gitea's recovery page, which is dead without a mailer."""
    if not gitea.admin_configured():
        # A reset link leads to a page that sets a password through Gitea's
        # admin API. Without the token that page cannot save anything, so
        # sending the mail would put a dead link in somebody's inbox and — the
        # worse half — the identical answer below would hide that from
        # everyone, including the admin. Refuse out loud instead. This says
        # nothing about any account, only about the server.
        return render_template("recover.html", unavailable=True), 503

    if request.method == "GET":
        return render_template("recover.html")

    email = request.form.get("email", "").strip()
    member = get_db().execute(
        """SELECT * FROM members
            WHERE email = ? COLLATE NOCASE AND active = 1
              AND role IN ('owner', 'admin', 'user')""",
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
    # Drop the Gitea token too. Signing out should stop the server being able to
    # act as you, not just stop the browser being able to ask it to.
    if g.member is not None:
        tokens.forget(g.member["id"])
    session.clear()

    # Deliberately NOT a redirect back to the login page.
    #
    # Clearing this session does not touch the Gitea session in the same
    # browser, and Gitea remembers that this app was authorised. So the next
    # click on "Entrar con Gitea" gets a code back immediately and signs the
    # person straight back in without a password — which on a laptop shared
    # around an association means "Salir" was telling them something untrue.
    #
    # Gitea cannot be signed out from here: its logout has been POST-only since
    # 1.11.2, and a cross-site POST would need Gitea's CSRF token. `prompt=login`
    # would be the other way round it, and is undocumented in every released
    # version of Gitea's OAuth2 provider — not something to rest this on.
    #
    # So the honest thing is to say so and point at the one place that can
    # finish the job.
    return render_template("logged_out.html", gitea_url=current_app.config["GITEA_URL"].rstrip("/"))
