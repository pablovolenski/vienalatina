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


@pytest.fixture
def outbox(monkeypatch):
    """Mail captured rather than sent. No SMTP anywhere in the suite."""
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, body: sent.append(
        {"to": to, "subject": subject, "body": body}))
    return sent


@pytest.fixture
def passwords(monkeypatch):
    """Gitea's password API stubbed; the calls are what matters."""
    changed = []
    monkeypatch.setattr(gitea, "admin_set_password",
                        lambda login, password: changed.append((login, password)))
    return changed


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

def test_a_member_sets_their_own_password(app, client, db, post, make_member, passwords):
    with app.test_request_context():
        member_id = make_member("maria")
        token = invites.issue(member_id, "invite")

    response = post(f"/comunidad/invitacion/{token}",
                    {"password": "una-contrasena-larga", "confirm": "una-contrasena-larga"})

    assert response.status_code == 302
    assert passwords == [("maria", "una-contrasena-larga")]
    assert db.execute("SELECT used_at FROM invites").fetchone()["used_at"] is not None


def test_a_short_password_is_refused_and_the_link_survives(
        app, client, db, post, make_member, passwords):
    """Rejecting the password must not spend the token, or a typo locks the
    member out of an account they have never reached."""
    with app.test_request_context():
        member_id = make_member("maria")
        token = invites.issue(member_id, "invite")

    response = post(f"/comunidad/invitacion/{token}",
                    {"password": "corta", "confirm": "corta"})

    assert response.status_code == 400
    assert passwords == []
    assert db.execute("SELECT used_at FROM invites").fetchone()["used_at"] is None
    assert client.get(f"/comunidad/invitacion/{token}").status_code == 200


def test_mismatched_passwords_are_refused(app, post, make_member, passwords):
    with app.test_request_context():
        token = invites.issue(make_member("maria"), "invite")

    response = post(f"/comunidad/invitacion/{token}",
                    {"password": "una-contrasena-larga", "confirm": "otra-cosa-larga"})
    assert response.status_code == 400
    assert passwords == []


def test_a_rejection_from_gitea_leaves_the_link_usable(
        app, db, post, make_member, monkeypatch):
    def refuse(login, password):
        raise gitea.GiteaError("Gitea rechazó esa contraseña.")
    monkeypatch.setattr(gitea, "admin_set_password", refuse)

    with app.test_request_context():
        token = invites.issue(make_member("maria"), "invite")

    response = post(f"/comunidad/invitacion/{token}",
                    {"password": "una-contrasena-larga", "confirm": "una-contrasena-larga"})
    assert response.status_code == 400
    assert db.execute("SELECT used_at FROM invites").fetchone()["used_at"] is None


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
        app, client, post, owner_id, sign_in, outbox, monkeypatch):
    monkeypatch.setattr(gitea, "admin_create_user",
                        lambda login, email, name, password: None)
    sign_in(owner_id)

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
        app, client, post, owner_id, sign_in, monkeypatch):
    """Otherwise the account exists and the member simply never gets in."""
    monkeypatch.setattr(gitea, "admin_create_user",
                        lambda login, email, name, password: None)

    def explode(to, subject, body):
        raise mail.MailFailed("connection refused")
    monkeypatch.setattr(mail, "send", explode)
    sign_in(owner_id)

    response = post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "m@example.com",
        "role": "user", "create_account": "on",
    })
    page = response.get_data(as_text=True)

    assert "No se pudo enviar el correo" in page
    assert "/comunidad/invitacion/" in page


def test_linking_an_existing_account_sends_nothing(
        app, client, post, owner_id, sign_in, outbox):
    """They already have a password; an unexpected invitation would be noise."""
    sign_in(owner_id)
    post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "m@example.com",
        "role": "user", "create_account": "",
    })
    assert outbox == []
