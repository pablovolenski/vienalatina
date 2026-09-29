"""Public pages at vienalatina.com/su-nombre.

Two things are being defended. One is the member: their page appears only
because they published it, and disappears the moment they stop.

The other is the public site, which shares this namespace. That defence lives
in the Caddyfile and cannot be tested from here — what can be tested is the
half that backs it up, which is that a name colliding with the site is refused
before anybody relies on it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from apps.board import profiles

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def published(db, make_member):
    def _published(login="maria", **columns):
        member_id = make_member(login)
        columns.setdefault("profile_published", 1)
        columns.setdefault("bio", "Vivo en Viena desde 2019.")
        sets = ", ".join(f"{name} = ?" for name in columns)
        db.execute(f"UPDATE members SET {sets} WHERE id = ?",
                   (*columns.values(), member_id))
        return member_id
    return _published


# --- what the public sees -------------------------------------------------

def test_a_published_profile_is_public(client, published):
    published()
    page = client.get("/maria")

    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Maria" in body and "Vivo en Viena desde 2019." in body


def test_an_unpublished_profile_is_not(client, published):
    published(profile_published=0)
    assert client.get("/maria").status_code == 404


def test_an_unknown_name_and_an_unpublished_one_look_the_same(client, published):
    """Both 404, so a visitor cannot tell a member who has not published from
    somebody who was never here. Caddy turns either into the site's own 404."""
    published(profile_published=0)
    assert client.get("/maria").status_code == client.get("/nadie").status_code == 404


def test_a_suspended_member_has_no_page(client, db, published):
    member_id = published()
    db.execute("UPDATE members SET active = 0 WHERE id = ?", (member_id,))
    assert client.get("/maria").status_code == 404


def test_the_page_does_not_wear_the_members_area_chrome(client, published):
    """It is a page on the public site. Rendering it inside the private
    navigation would put "Área privada" and a Salir button on something
    anybody can read."""
    published()
    body = client.get("/maria").get_data(as_text=True)
    assert "Área privada" not in body
    assert "Salir" not in body


# --- links, which is where a public page gets dangerous -------------------

@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "  javascript:alert(1)",
])
def test_a_script_link_is_dropped(url):
    """An href beginning javascript: is a script running on vienalatina.com,
    published by whoever typed it into the form."""
    assert profiles.clean_links(f"Malo|{url}") == []


def test_ordinary_links_survive():
    links = profiles.clean_links(
        "Instagram|https://instagram.com/maria\nhttp://ejemplo.com\n\n")
    assert links == [
        {"label": "Instagram", "url": "https://instagram.com/maria"},
        {"label": "http://ejemplo.com", "url": "http://ejemplo.com"},
    ]


def test_the_number_of_links_is_capped():
    many = "\n".join(f"Uno|https://ejemplo.com/{n}" for n in range(50))
    assert len(profiles.clean_links(many)) == profiles.LINKS_MAX


def test_links_are_rendered_without_passing_on_our_ranking(client, db, published):
    published(links=json.dumps([{"label": "Web", "url": "https://ejemplo.com"}]))
    body = client.get("/maria").get_data(as_text=True)
    assert 'rel="nofollow noopener"' in body


def test_the_bio_cannot_smuggle_html(client, published):
    """Same markdown renderer as the board, where html=False is the whole
    defence — worth asserting on the one page that is public."""
    published(bio="<script>alert(1)</script> y **negrita**")
    body = client.get("/maria").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "<strong>negrita</strong>" in body


# --- names that would never work ------------------------------------------

@pytest.mark.parametrize("name", ["de", "pt-br", "categories", "admin",
                                  "comunidad", "robots", "DE"])
def test_a_name_the_site_already_uses_is_refused(client, db, post, owner_id,
                                                 sign_in, name, monkeypatch):
    """Not because it would break the site — Caddy gives the static site every
    collision — but because their page would never load and nobody would know
    why."""
    monkeypatch.setattr("apps.board.mail.send", lambda to, subject, body: None)
    sign_in(owner_id)
    post("/comunidad/miembros/nuevo", {
        "login": name, "display_name": "X", "email": "x@example.com", "role": "user",
    })
    assert db.execute("SELECT 1 FROM members WHERE gitea_login = ? COLLATE NOCASE",
                      (name,)).fetchone() is None


# --- the photo, which is public and must stop being public ----------------

def test_the_photo_is_public_while_the_page_is(app, client, db, post, make_member,
                                               sign_in, published):
    member_id = published()
    sign_in(member_id)
    post("/comunidad/mi-perfil", {"published": "on", "bio": "Hola",
                                  "photo": (io.BytesIO(PNG), "yo.png")},
         content_type="multipart/form-data")

    name = db.execute("SELECT photo_name FROM members WHERE id = ?",
                      (member_id,)).fetchone()["photo_name"]
    assert name
    with client.session_transaction() as session:
        session.clear()                       # a stranger, not signed in
    assert client.get(f"/comunidad/foto/{name}").status_code == 200


def test_unpublishing_takes_the_photo_down_too(app, client, db, post, sign_in,
                                               published):
    member_id = published()
    sign_in(member_id)
    post("/comunidad/mi-perfil", {"published": "on", "bio": "Hola",
                                  "photo": (io.BytesIO(PNG), "yo.png")},
         content_type="multipart/form-data")
    name = db.execute("SELECT photo_name FROM members WHERE id = ?",
                      (member_id,)).fetchone()["photo_name"]

    post("/comunidad/mi-perfil", {"bio": "Hola"})     # checkbox unticked

    assert client.get("/maria").status_code == 404
    assert client.get(f"/comunidad/foto/{name}").status_code == 404


def test_a_board_picture_cannot_be_served_as_a_profile_photo(
        app, client, db, post, make_member, sign_in):
    """The profile photo route is public, so it must only ever serve a file
    that a published profile points at — never anything else in the same
    directory, which is where the private board images live."""
    member = make_member("maria")
    sign_in(member)
    post("/comunidad/nuevo", {"title": "Privado", "body": "Texto",
                              "pictures": (io.BytesIO(PNG), "foto.png")},
         content_type="multipart/form-data")
    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]

    with client.session_transaction() as session:
        session.clear()
    assert client.get(f"/comunidad/foto/{name}").status_code == 404


def test_erasing_a_member_takes_the_photo_off_disk(
        app, client, db, post, owner_id, sign_in, published):
    member_id = published()
    sign_in(member_id)
    post("/comunidad/mi-perfil", {"published": "on", "bio": "Hola",
                                  "photo": (io.BytesIO(PNG), "yo.png")},
         content_type="multipart/form-data")
    name = db.execute("SELECT photo_name FROM members WHERE id = ?",
                      (member_id,)).fetchone()["photo_name"]
    assert (Path(app.config["UPLOAD_DIR"]) / name).exists()

    sign_in(owner_id)
    post(f"/comunidad/miembros/{member_id}/eliminar")

    assert not (Path(app.config["UPLOAD_DIR"]) / name).exists()


# --- publishing is a decision ---------------------------------------------

def test_a_new_member_has_no_page(client, make_member):
    make_member("nueva")
    assert client.get("/nueva").status_code == 404


def test_the_form_says_what_publishing_means(client, make_member, sign_in):
    sign_in(make_member("maria"))
    body = client.get("/comunidad/mi-perfil").get_data(as_text=True)
    assert "cualquiera puede verla" in body
    assert "buscadores" in body
