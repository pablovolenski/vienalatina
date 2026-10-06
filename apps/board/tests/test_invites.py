"""Invitations, password resets, and the rules that keep a link from being a
permanent key to somebody's account.

A token here is a bearer credential: whoever holds it sets the password. So
most of these tests are about the ways a token must *stop* working, and about
what the pages give away to somebody who is only guessing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apps.board import gitea, invites, mail
from apps.board import passwords as board_passwords


@pytest.fixture
def outbox(monkeypatch):
    """Mail captured rather than sent. No SMTP anywhere in the suite."""
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, body: sent.append(
        {"to": to, "subject": subject, "body": body}))
    return sent


@pytest.fixture
def stored(db):
    """What ended up in the database. No stub: setting a password is a write
    to our own table now, not a call to somebody else's API."""
    def _stored(login):
        return db.execute("SELECT password_hash FROM members WHERE gitea_login = ?",
                          (login,)).fetchone()["password_hash"]
    return _stored


def link_in(message: str) -> str:
    for word in message.split():
        if "/comunidad/invitacion/" in word:
            return word
    raise AssertionError("no invite link in the message")


def token_in(message: str) -> str:
    return link_in(message).rsplit("/", 1)[1]


# --- the tokens themselves ------------------------------------------------

def test_the_database_never_holds_the_token_itself(app, db, make_member):
    """A leaked backup should be a list of useless hashes, not live keys."""
    with app.test_request_context():
        member_id = make_member("maria")
        token = invites.issue(member_id, "invite")

    stored = db.execute("SELECT token_hash FROM invites").fetchone()["token_hash"]
    assert token not in stored
    assert len(stored) == 64          # sha256 hex, not the 43-char token


def test_a_token_works_once(app, db, make_member):
    with app.test_request_context():
        member_id = make_member("maria")
        token = invites.issue(member_id, "invite")

        found = invites.lookup(token)
        assert found["id"] == member_id

        invites.consume(found["invite_id"])
        assert invites.lookup(token) is None


def test_an_expired_token_is_refused(app, db, make_member):
    with app.test_request_context():
        member_id = make_member("maria")
        token = invites.issue(member_id, "reset")
        db.execute(
            "UPDATE invites SET expires_at = ? WHERE member_id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), member_id),
        )
        assert invites.lookup(token) is None


def test_issuing_a_new_token_kills_the_old_one(app, db, make_member):
    """Asking for a second reset should not leave the first one live in an
    inbox somebody else can read."""
    with app.test_request_context():
        member_id = make_member("maria")
        first = invites.issue(member_id, "reset")
        second = invites.issue(member_id, "reset")

        assert invites.lookup(first) is None
        assert invites.lookup(second) is not None


def test_a_suspended_member_cannot_use_their_link(app, db, make_member):
    with app.test_request_context():
        member_id = make_member("expulsada")
        token = invites.issue(member_id, "invite")
        db.execute("UPDATE members SET active = 0 WHERE id = ?", (member_id,))
        assert invites.lookup(token) is None


def test_a_made_up_token_is_refused(app):
    with app.test_request_context():
        assert invites.lookup("not-a-real-token") is None
        assert invites.lookup("") is None


# --- setting the password -------------------------------------------------

def test_a_member_sets_their_own_password(app, client, db, post, make_member, stored):
    with app.test_request_context():
        member_id = make_member("maria")
        token = invites.issue(member_id, "invite")

    response = post(f"/comunidad/invitacion/{token}",
                    {"password": "una-contrasena-larga", "confirm": "una-contrasena-larga"})

    assert response.status_code == 302
    assert db.execute("SELECT used_at FROM invites").fetchone()["used_at"] is not None
    # Stored as a hash, never as what they typed.
    assert "una-contrasena-larga" not in (stored("maria") or "")
    assert board_passwords.verify(stored("maria"), "una-contrasena-larga")


def test_and_can_then_actually_sign_in(app, client, db, post, make_member):
    """The end of the chain, joined up: the invitation leads to a password that
    the login form accepts. Tested together because each half passing on its
    own is how a flow ends up broken in the middle."""
    with app.test_request_context():
        token = invites.issue(make_member("maria"), "invite")
    post(f"/comunidad/invitacion/{token}",
         {"password": "una-contrasena-larga", "confirm": "una-contrasena-larga"})

    response = post("/comunidad/login",
                    {"identifier": "maria", "password": "una-contrasena-larga"})

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["member_id"]


def test_a_short_password_is_refused_and_the_link_survives(
        app, client, db, post, make_member):
    """Rejecting the password must not spend the token, or a typo locks the
    member out of an account they have never reached."""
    with app.test_request_context():
        member_id = make_member("maria")
        token = invites.issue(member_id, "invite")

    response = post(f"/comunidad/invitacion/{token}",
                    {"password": "corta", "confirm": "corta"})

    assert response.status_code == 400
    assert True
    assert db.execute("SELECT used_at FROM invites").fetchone()["used_at"] is None
    assert client.get(f"/comunidad/invitacion/{token}").status_code == 200


def test_mismatched_passwords_are_refused(app, post, make_member):
    with app.test_request_context():
        token = invites.issue(make_member("maria"), "invite")

    response = post(f"/comunidad/invitacion/{token}",
                    {"password": "una-contrasena-larga", "confirm": "otra-cosa-larga"})
    assert response.status_code == 400
    assert True




def test_a_dead_link_says_nothing_about_the_account(client):
    body = client.get("/comunidad/invitacion/inventado").get_data(as_text=True)
    assert "ya no sirve" in body
    # Not "expired", not "already used", not "unknown" — those distinctions tell
    # the holder of a stale link something about the account behind it.
    assert "caducado" not in body


# --- recovery -------------------------------------------------------------

def test_recovery_emails_a_member(app, client, post, db, make_member, outbox):
    make_member("maria")
    response = post("/comunidad/recuperar", {"email": "maria@example.com"})

    assert response.status_code == 200
    assert len(outbox) == 1
    assert outbox[0]["to"] == "maria@example.com"
    with app.test_request_context():
        assert invites.lookup(token_in(outbox[0]["body"])) is not None


def test_recovery_answers_the_same_for_an_unknown_address(client, post, outbox):
    """Otherwise the form is a way to find out who is a member, one address at
    a time."""
    known = post("/comunidad/recuperar", {"email": "maria@example.com"})
    unknown = post("/comunidad/recuperar", {"email": "nadie@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.get_data() == unknown.get_data()
    assert outbox == []


def test_recovery_stops_after_a_few_tries(client, post, make_member, outbox):
    """A reset form with no limit is a way to mail-bomb somebody using your
    server's reputation."""
    make_member("maria")
    for _ in range(6):
        post("/comunidad/recuperar", {"email": "maria@example.com"})

    assert len(outbox) == invites.RESET_LIMIT


def test_a_suspended_member_gets_no_reset(client, post, make_member, outbox):
    make_member("expulsada", active=0)
    post("/comunidad/recuperar", {"email": "expulsada@example.com"})
    assert outbox == []


# --- inviting from the members screen -------------------------------------

def test_creating_a_member_emails_them_instead_of_showing_a_password(
        app, client, post, superadmin_id, sign_in, outbox, monkeypatch):
    monkeypatch.setattr(gitea, "admin_create_user",
                        lambda login, email, name, password: None)
    sign_in(superadmin_id)

    response = post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "m@example.com",
        "role": "user", "create_account": "on",
    })
    page = response.get_data(as_text=True)

    assert len(outbox) == 1
    assert outbox[0]["to"] == "m@example.com"
    assert "m@example.com" in page
    # The admin never sees a password, so there is none to pass on or mislay.
    assert "/comunidad/invitacion/" not in page


def test_when_mail_fails_the_admin_is_given_the_link(
        app, client, post, superadmin_id, sign_in, monkeypatch):
    """Otherwise the account exists and the member simply never gets in."""
    monkeypatch.setattr(gitea, "admin_create_user",
                        lambda login, email, name, password: None)

    def explode(to, subject, body):
        raise mail.MailFailed("connection refused")
    monkeypatch.setattr(mail, "send", explode)
    sign_in(superadmin_id)

    response = post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "m@example.com",
        "role": "user", "create_account": "on",
    })
    page = response.get_data(as_text=True)

    assert "No se pudo enviar el correo" in page
    assert "/comunidad/invitacion/" in page



# --- when the server cannot send at all -----------------------------------

def test_an_unconfigured_server_does_not_blame_the_mail_server(
        app, client, post, superadmin_id, sign_in, monkeypatch):
    """No MAIL_HOST is not a failure, and saying "no se pudo enviar" sends the
    admin hunting for an SMTP error that was never produced."""
    monkeypatch.setattr(gitea, "admin_create_user",
                        lambda login, email, name, password: None)
    app.config["MAIL_HOST"] = ""
    sign_in(superadmin_id)

    page = post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "m@example.com",
        "role": "user", "create_account": "on",
    }).get_data(as_text=True)

    assert "todavía no envía correo" in page
    assert "No se pudo enviar el correo" not in page
    assert "/comunidad/invitacion/" in page


def test_the_form_warns_before_it_is_filled_in(app, client, superadmin_id, sign_in):
    app.config["MAIL_HOST"] = ""
    sign_in(superadmin_id)
    assert "no envía correo" in client.get(
        "/comunidad/miembros/nuevo").get_data(as_text=True)

    app.config["MAIL_HOST"] = "smtp.example.com"
    assert "no envía correo" not in client.get(
        "/comunidad/miembros/nuevo").get_data(as_text=True)


# --- when the server cannot set passwords at all --------------------------
#
# Without GITEA_ADMIN_TOKEN nothing in this file can complete. The point of
# these four is that the refusal arrives *before* somebody does work, not
# after — which is how it was found: a member chose a password, typed it
# twice, pressed save, and met the name of an environment variable.







def test_recovery_is_always_offered_now(app, client):
    """It used to be hidden when the server could not reach the account system
    to change a password. The password is ours; there is nothing to be unable
    to reach."""
    page = client.get("/comunidad/login")
    assert page.status_code == 200
    assert "/comunidad/recuperar" in page.get_data(as_text=True)



# --- inviting somebody who is already a member ----------------------------
#
# Creating the account and inviting the person used to be one action, so a
# member added any other way had no route in at all: no invitation was ever
# issued for them and nothing could issue one later.

def test_an_existing_member_can_be_invited(app, client, post, superadmin_id, make_member,
                                           sign_in, outbox):
    """The case that prompted this: the admin made the account by hand, added
    the member with the box unticked, and nobody could reach the account —
    including the admin, who never knew the password."""
    member_id = make_member("salvador")
    sign_in(superadmin_id)

    post(f"/comunidad/miembros/{member_id}/invitar")

    assert len(outbox) == 1
    assert outbox[0]["to"] == "salvador@example.com"
    with app.test_request_context():
        assert invites.lookup(token_in(outbox[0]["body"])) is not None


def test_re_inviting_kills_the_previous_link(app, client, post, superadmin_id, make_member,
                                             sign_in, outbox):
    """A link that was forwarded, or is sitting in a mailbox somebody else can
    read, must stop working the moment a replacement is sent."""
    member_id = make_member("salvador")
    sign_in(superadmin_id)

    post(f"/comunidad/miembros/{member_id}/invitar")
    post(f"/comunidad/miembros/{member_id}/invitar")

    with app.test_request_context():
        assert invites.lookup(token_in(outbox[0]["body"])) is None
        assert invites.lookup(token_in(outbox[1]["body"])) is not None


def test_an_admin_can_invite_without_waiting_for_the_owner(
        app, client, post, make_member, sign_in, outbox):
    sign_in(make_member("admina", role="admin"))
    post(f"/comunidad/miembros/{make_member('salvador')}/invitar")
    assert len(outbox) == 1


def test_a_plain_user_cannot_invite(client, post, make_member, sign_in, outbox):
    sign_in(make_member("cualquiera"))
    response = post(f"/comunidad/miembros/{make_member('salvador')}/invitar")

    assert response.status_code == 403
    assert outbox == []


def test_a_member_with_no_address_is_refused_before_a_token_is_made(
        app, client, db, post, superadmin_id, sign_in, outbox):
    """Issuing the token first would invalidate a previous, working invitation
    in exchange for one that cannot be delivered."""
    member_id = db.execute(
        "INSERT INTO members (gitea_login, display_name, role) VALUES ('sincorreo', 'Sin', 'user')"
    ).lastrowid
    sign_in(superadmin_id)

    post(f"/comunidad/miembros/{member_id}/invitar", follow_redirects=True)

    assert outbox == []
    assert db.execute("SELECT 1 FROM invites").fetchone() is None


def test_a_suspended_member_cannot_be_invited(client, post, superadmin_id, make_member,
                                              sign_in, outbox):
    sign_in(superadmin_id)
    response = post(f"/comunidad/miembros/{make_member('fuera', active=0)}/invitar")

    assert response.status_code == 403
    assert outbox == []


def test_when_the_mail_fails_the_admin_is_handed_the_link(
        app, client, post, superadmin_id, make_member, sign_in, monkeypatch):
    def explode(to, subject, body):
        raise mail.MailFailed("connection refused")
    monkeypatch.setattr(mail, "send", explode)
    sign_in(superadmin_id)

    page = post(f"/comunidad/miembros/{make_member('salvador')}/invitar",
                follow_redirects=True).get_data(as_text=True)

    assert "/comunidad/invitacion/" in page
