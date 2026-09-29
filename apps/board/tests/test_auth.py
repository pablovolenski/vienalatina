"""Who gets in, and who does not.

Signing in happens here now, on this domain, against a hash in our own
database. These tests are mostly about the ways it must refuse, and about
refusing them all in the same words — a login form that is more specific
about failure is a way to find out who is a member.
"""

from __future__ import annotations

import pytest

from apps.board import passwords

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


def sign_up(db, make_member, login="maria", password="una-contrasena-larga", **kwargs):
    """A member who has actually set a password, which is what most of these
    need and what `make_member` alone does not give."""
    member_id = make_member(login, **kwargs)
    db.execute("UPDATE members SET password_hash = ? WHERE id = ?",
               (passwords.hash_password(password), member_id))
    return member_id


def attempt(post, identifier, password, **kwargs):
    return post("/comunidad/login",
                {"identifier": identifier, "password": password}, **kwargs)


# --- getting in -----------------------------------------------------------

def test_a_member_signs_in_with_their_password(client, db, post, make_member):
    sign_up(db, make_member)
    response = attempt(post, "maria", "una-contrasena-larga")

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["member_id"]


def test_the_email_works_as_well_as_the_username(client, db, post, make_member):
    sign_up(db, make_member)
    assert attempt(post, "MARIA@example.com", "una-contrasena-larga").status_code == 302


def test_signing_in_starts_a_new_session(client, db, post, make_member):
    """A cookie captured before sign-in must not still be good after it."""
    member_id = sign_up(db, make_member)
    with client.session_transaction() as session:
        session["planted"] = "before"

    attempt(post, "maria", "una-contrasena-larga")

    with client.session_transaction() as session:
        assert session["member_id"] == member_id
        assert "planted" not in session


# --- and the ways it must not --------------------------------------------

def test_every_refusal_reads_the_same(client, db, post, make_member):
    """Unknown name, wrong password, suspended member, invited but never
    arrived. Four different situations, one answer, because the difference
    between them is exactly what an outsider would like to learn."""
    sign_up(db, make_member, "maria")
    sign_up(db, make_member, "expulsada", active=0)
    make_member("invitada")          # no password_hash at all

    pages = [
        attempt(post, "nadie", "una-contrasena-larga"),
        attempt(post, "maria", "otra-contrasena"),
        attempt(post, "expulsada", "una-contrasena-larga"),
        attempt(post, "invitada", "una-contrasena-larga"),
    ]

    assert {page.status_code for page in pages} == {401}
    assert len({page.get_data() for page in pages}) == 1


def test_a_member_with_no_password_cannot_sign_in(client, db, post, make_member):
    """NULL must never behave as "matches anything" — every member starts this
    way, including the owner, the moment the column is added."""
    make_member("invitada")
    attempt(post, "invitada", "")
    attempt(post, "invitada", "cualquier-cosa")

    with client.session_transaction() as session:
        assert "member_id" not in session


def test_guessing_is_rate_limited(client, db, post, make_member):
    sign_up(db, make_member)
    for _ in range(passwords.ATTEMPT_LIMIT):
        attempt(post, "maria", "mal")

    blocked = attempt(post, "maria", "una-contrasena-larga")
    assert blocked.status_code == 429
    with client.session_transaction() as session:
        assert "member_id" not in session


def test_getting_it_right_clears_the_count(client, db, post, make_member):
    """Somebody who mistypes three times and then succeeds should not be part
    way to a lockout for the rest of the afternoon."""
    sign_up(db, make_member)
    for _ in range(3):
        attempt(post, "maria", "mal")
    attempt(post, "maria", "una-contrasena-larga")

    assert db.execute(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE identifier = 'maria'"
    ).fetchone()["n"] == 0


def test_suspension_takes_effect_on_the_next_request(client, db, make_member, sign_in):
    """Read from the database on every request rather than trusted from the
    cookie, so removing somebody does not wait for their session to expire."""
    member_id = make_member("maria")
    sign_in(member_id)
    assert client.get("/comunidad/").status_code == 200

    db.execute("UPDATE members SET active = 0 WHERE id = ?", (member_id,))
    assert client.get("/comunidad/").status_code == 302


def test_post_without_csrf_is_refused(client, make_member, sign_in):
    sign_in(make_member("maria"))
    assert client.post("/comunidad/nuevo",
                       data={"title": "Hola", "body": "Texto"}).status_code == 400


def test_login_redirect_cannot_be_pointed_offsite(client, db, post, make_member):
    """Otherwise a crafted link signs somebody in and lands them on a page
    somebody else controls, carrying the trust of having just arrived from
    their own community site."""
    sign_up(db, make_member)
    for target in ("https://evil.example.com/", "//evil.example.com/",
                   "/etc/passwd", "http://vienalatina.com.evil.test/"):
        response = attempt(post, "maria", "una-contrasena-larga",
                           follow_redirects=False)
        assert response.status_code == 302
        # The form carries `next`; none of these may survive it.
        response = post("/comunidad/login", {
            "identifier": "maria", "password": "una-contrasena-larga",
            "next": target})
        assert response.headers.get("Location", "").startswith("/comunidad/")


def test_responses_say_do_not_index(client):
    assert client.get("/comunidad/login").headers["X-Robots-Tag"] == "noindex, nofollow"


# --- and out --------------------------------------------------------------

def test_logout_is_one_click_and_final(client, make_member, sign_in, post):
    """It used to render a page apologising that signing out had not really
    signed you out, because the session that mattered lived on another server.
    There is only one session now."""
    sign_in(make_member("maria"))

    response = post("/comunidad/logout")

    assert response.status_code == 302
    assert "/comunidad/login" in response.headers["Location"]
    with client.session_transaction() as session:
        assert "member_id" not in session
    assert client.get("/comunidad/").status_code == 302


def test_nothing_signs_you_back_in_without_a_password(client, db, post, make_member):
    """The original complaint: Salir worked, then one click on Entrar let you
    straight back in, because another server still considered you signed in."""
    sign_up(db, make_member)
    attempt(post, "maria", "una-contrasena-larga")
    post("/comunidad/logout")

    page = client.get("/comunidad/login").get_data(as_text=True)
    assert 'name="password"' in page          # a form, not a redirect
    assert client.get("/comunidad/").status_code == 302
