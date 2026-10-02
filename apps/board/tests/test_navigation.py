"""What each role can see and reach.

This file exists because of a bug that no other test in this suite could have
caught: *Privados*, *Mi perfil* and *Contenido* were all inside one
`{% if g.member.role in ('owner', 'admin') %}` block in base.html, so an
ordinary member signed in to a wall and a member list and nothing else. Every
feature worked. Every test passed. Most of the members area was simply invisible
to the people it was built for.

So the navigation is now asserted per role, from the rendered page, and every
section is asserted from its route as well — because a link nobody can see and a
link that 403s are the same thing to whoever needed it.
"""

from __future__ import annotations

import pytest

# Label -> the path it leads to. Both halves matter: the label is what a member
# looks for, the path is what has to answer.
SECTIONS = {
    "Inicio": "/comunidad/",
    "Muro": "/comunidad/muro",
    "Privados": "/comunidad/privados",
    "Publicaciones": "/comunidad/publicaciones",
}
ADMIN_ONLY = {"Gestión": "/comunidad/gestion"}


@pytest.fixture
def nav(client, sign_in):
    def _nav(member_id):
        sign_in(member_id)
        page = client.get("/comunidad/")
        assert page.status_code == 200
        return page.get_data(as_text=True)
    return _nav


@pytest.mark.parametrize("role", ["user", "moderator"])
def test_a_member_sees_the_four_shared_sections(role, nav, make_member):
    """The regression. Not one of these may depend on a role again."""
    body = nav(make_member("maria", role=role))
    for label in SECTIONS:
        assert f">{label}" in body or f"{label}<" in body, label


@pytest.mark.parametrize("role", ["user", "moderator"])
def test_a_member_does_not_see_gestion(role, nav, make_member):
    assert "Gestión" not in nav(make_member("maria", role=role))


def test_an_admin_sees_everything(nav, make_member):
    body = nav(make_member("admina", role="admin"))
    for label in list(SECTIONS) + list(ADMIN_ONLY):
        assert label in body, label


@pytest.mark.parametrize("role", ["user", "moderator", "admin"])
def test_the_account_items_are_in_the_corner_for_everybody(role, nav, make_member):
    """They used to be navigation entries, and two of them admin-only. A member
    with no way to reach their own profile or their own data export is the same
    bug wearing a different hat."""
    body = nav(make_member("maria", role=role))
    assert "/comunidad/mi-perfil" in body
    assert "/comunidad/mis-datos" in body
    assert "Salir" in body


@pytest.mark.parametrize("role", ["user", "moderator", "admin"])
@pytest.mark.parametrize("path", sorted(SECTIONS.values()))
def test_every_shared_section_answers_for_every_role(role, path, client, make_member, sign_in):
    sign_in(make_member("maria", role=role))
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("role", ["user", "moderator"])
def test_gestion_is_refused_below_admin(role, client, make_member, sign_in):
    """Refused by the route, not hidden by the template. A moderator who types
    the address meets the same answer as one who cannot see the link."""
    sign_in(make_member("maria", role=role))
    assert client.get("/comunidad/gestion").status_code == 403


def test_gestion_opens_for_an_admin(client, make_member, sign_in):
    sign_in(make_member("admina", role="admin"))
    assert client.get("/comunidad/gestion").status_code == 200


def test_signing_in_lands_on_inicio(client, db, post, make_member):
    """It used to land on the wall, which is one of five places something may
    have happened since the last visit."""
    from apps.board import passwords
    member_id = make_member("maria")
    db.execute("UPDATE members SET password_hash = ? WHERE id = ?",
               (passwords.hash_password("una-contraseña-larga"), member_id))

    response = post("/comunidad/login",
                    {"identifier": "maria", "password": "una-contraseña-larga"})

    assert response.status_code == 302
    assert response.headers["Location"].rstrip("/").endswith("/comunidad")


def test_the_wall_keeps_its_thread_urls(client, post, make_member, sign_in):
    """The wall moved from /comunidad/ to /comunidad/muro. Thread addresses did
    not, because people have linked to them from private messages."""
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "Hola", "body": "Qué tal"})

    assert client.get("/comunidad/tema/1").status_code == 200
    assert client.get("/comunidad/muro").status_code == 200


def test_signed_out_visitors_get_no_navigation(client):
    body = client.get("/comunidad/login").get_data(as_text=True)
    for label in list(SECTIONS) + list(ADMIN_ONLY):
        assert f'>{label}<' not in body, label


# --- reaching the published articles --------------------------------------
#
# The screen worked and nothing linked to it: Gestión offered only the static
# pages, and the Artículos tab exists only once you are already on that screen.
# A moderator, who cannot open Gestión at all, had no route to it.

def test_an_admin_reaches_the_articles_from_gestion(client, make_member, sign_in):
    sign_in(make_member("admina", role="admin"))

    body = client.get("/comunidad/gestion").get_data(as_text=True)

    assert "/comunidad/contenido/post" in body
    assert "Artículos publicados" in body


def test_a_moderator_reaches_them_from_publicaciones(client, make_member, sign_in):
    """Their only way in, so it is a link in the heading rather than a clause in
    a paragraph."""
    sign_in(make_member("luisa", role="moderator"))

    body = client.get("/comunidad/publicaciones").get_data(as_text=True)

    assert "/comunidad/contenido/post" in body
    assert "Ya publicado" in body


def test_a_plain_member_is_offered_neither(client, make_member, sign_in):
    sign_in(make_member("maria"))

    body = client.get("/comunidad/publicaciones").get_data(as_text=True)

    assert "/comunidad/contenido" not in body
    assert "Ya publicado" not in body


# --- the strip the public site draws --------------------------------------
#
# vienalatina.com is files on disk, so the bar is drawn by a script that asks
# this app who is reading. That makes this endpoint the whole privacy surface:
# it is the only thing on the public side that knows anything about anybody.

def test_a_visitor_is_told_nothing(client):
    response = client.get("/comunidad/sesion.json")

    assert response.status_code == 200
    assert response.get_json() == {"signed_in": False}
    assert response.headers["Cache-Control"] == "no-store"


def test_a_member_gets_their_name_and_their_sections(client, make_member, sign_in):
    sign_in(make_member("maria"))

    state = client.get("/comunidad/sesion.json").get_json()

    assert state["signed_in"] is True
    assert state["name"] == "Maria"
    assert [s["label"] for s in state["sections"]] == [
        "Inicio", "Muro", "Privados", "Publicaciones"]
    assert state["csrf"]                       # the bar renders a real logout form


def test_the_sections_follow_the_same_rule_as_the_navigation(
        client, make_member, sign_in):
    """Two places listing the sections would drift the first time one changed;
    the navigation and this endpoint have to agree about Gestión."""
    sign_in(make_member("admina", role="admin"))
    labels = [s["label"] for s in client.get("/comunidad/sesion.json").get_json()["sections"]]
    assert "Gestión" in labels

    sign_in(make_member("luisa", role="moderator"))
    labels = [s["label"] for s in client.get("/comunidad/sesion.json").get_json()["sections"]]
    assert "Gestión" not in labels


def test_the_answer_is_never_cached(client, make_member, sign_in):
    """A cached answer is somebody else's name on a shared machine, or a bar
    that stays up after signing out."""
    sign_in(make_member("maria"))
    assert client.get("/comunidad/sesion.json").headers["Cache-Control"] == "no-store"


def test_signing_in_sets_the_hint_and_signing_out_clears_it(client, db, post, make_member):
    """The hint is what keeps a stranger reading one article from costing a
    request to this app on every page view."""
    from apps.board import passwords
    member_id = make_member("maria")
    db.execute("UPDATE members SET password_hash = ? WHERE id = ?",
               (passwords.hash_password("una-contraseña-larga"), member_id))

    signed_in = post("/comunidad/login",
                     {"identifier": "maria", "password": "una-contraseña-larga"})
    cookie = signed_in.headers.get_all("Set-Cookie")
    assert any("vl_sesion=1" in header for header in cookie)
    assert any("HttpOnly" not in header for header in cookie if "vl_sesion" in header)

    signed_out = post("/comunidad/logout")
    assert any("vl_sesion=;" in header or "vl_sesion=\"\"" in header
               for header in signed_out.headers.get_all("Set-Cookie"))
