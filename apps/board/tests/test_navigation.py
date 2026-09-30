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
