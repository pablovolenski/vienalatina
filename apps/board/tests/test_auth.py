"""Who gets in, and who does not."""

from __future__ import annotations

import pytest

from apps.board import gitea

PROTECTED = [
    "/comunidad/",
    "/comunidad/nuevo",
    "/comunidad/miembros",
    "/comunidad/miembros/nuevo",
    "/comunidad/mis-datos",
]


@pytest.mark.parametrize("path", PROTECTED)
def test_anonymous_is_sent_to_login(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/comunidad/login" in response.headers["Location"]


def _stub_gitea(monkeypatch, login):
    # exchange_code returns the whole token response now, because the editor
    # needs the refresh token to keep working past Gitea's one-hour expiry.
    monkeypatch.setattr(gitea, "exchange_code", lambda code, uri: {
        "access_token": "token", "refresh_token": "refresh", "expires_in": 3600,
    })
    monkeypatch.setattr(gitea, "fetch_user", lambda token: {
        "login": login, "full_name": login.title(), "email": f"{login}@example.com",
    })


def _callback(client, monkeypatch, login):
    _stub_gitea(monkeypatch, login)
    with client.session_transaction() as session:
        session["oauth_state"] = "state123"
    return client.get("/comunidad/auth/callback?code=abc&state=state123")


def test_a_gitea_account_is_not_a_membership(client, monkeypatch):
    """The single most important rule in the app: Gitea says who you are, the
    members table says whether you belong. The translations bot has a perfectly
    valid Gitea account and must not get in."""
    response = _callback(client, monkeypatch, "vienalatina-translations")
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert "member_id" not in session


def test_member_signs_in(client, monkeypatch, make_member):
    make_member("maria")
    response = _callback(client, monkeypatch, "maria")
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert "member_id" in session


def test_suspended_member_cannot_sign_in(client, monkeypatch, make_member):
    make_member("expulsada", active=0)
    _callback(client, monkeypatch, "expulsada")
    with client.session_transaction() as session:
        assert "member_id" not in session


def test_suspension_takes_effect_on_the_next_request(client, db, make_member, sign_in):
    """The role is read per request, not cached in the cookie, so revoking
    access does not wait for a session to expire."""
    member_id = make_member("temporal")
    sign_in(member_id)
    assert client.get("/comunidad/").status_code == 200

    db.execute("UPDATE members SET active = 0 WHERE id = ?", (member_id,))
    assert client.get("/comunidad/").status_code == 302


def test_callback_rejects_a_mismatched_state(client, monkeypatch, make_member):
    make_member("maria")
    _stub_gitea(monkeypatch, "maria")
    with client.session_transaction() as session:
        session["oauth_state"] = "the-real-state"
    client.get("/comunidad/auth/callback?code=abc&state=attacker-state")
    with client.session_transaction() as session:
        assert "member_id" not in session


def test_post_without_csrf_is_refused(client, make_member, sign_in):
    sign_in(make_member("maria"))
    response = client.post("/comunidad/nuevo", data={"title": "Hola", "body": "Texto"})
    assert response.status_code == 400


def test_login_redirect_cannot_be_pointed_offsite(client, monkeypatch, make_member):
    make_member("maria")
    _stub_gitea(monkeypatch, "maria")
    with client.session_transaction() as session:
        session["oauth_state"] = "state123"
    response = client.get(
        "/comunidad/auth/callback?code=abc&state=state123&next=https://evil.example.com/"
    )
    assert "evil.example.com" not in response.headers["Location"]


def test_responses_say_do_not_index(client):
    response = client.get("/comunidad/login")
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_logout_says_the_gitea_session_is_still_open(client, db, make_member, sign_in, post):
    """Redirecting to the login page would hide the problem: one click on
    "Entrar con Gitea" signs you straight back in, because Gitea's session and
    its record of the authorisation both survive."""
    member_id = make_member("maria")
    db.execute(
        "INSERT INTO gitea_tokens (member_id, access_token) VALUES (?, 'tok')",
        (member_id,),
    )
    sign_in(member_id)

    response = post("/comunidad/logout")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "sigue conectado" in page          # the warning, not a redirect
    assert "/user/logout" in page

    with client.session_transaction() as session:
        assert "member_id" not in session
    assert db.execute("SELECT 1 FROM gitea_tokens WHERE member_id = ?",
                      (member_id,)).fetchone() is None


def test_logout_leaves_nothing_the_server_can_act_with(client, db, make_member, sign_in, post):
    """The token is what lets this server commit as the member. Clearing the
    cookie without dropping it would end the browser's access but not ours."""
    member_id = make_member("maria")
    db.execute(
        "INSERT INTO gitea_tokens (member_id, access_token) VALUES (?, 'tok')",
        (member_id,),
    )
    sign_in(member_id)
    post("/comunidad/logout")

    assert db.execute("SELECT COUNT(*) AS n FROM gitea_tokens").fetchone()["n"] == 0
