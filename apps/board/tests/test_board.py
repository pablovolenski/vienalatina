"""Threads, comments, and who may touch them."""

from __future__ import annotations

import pytest


@pytest.fixture
def thread(db, make_member):
    author = make_member("autora")
    cursor = db.execute(
        "INSERT INTO threads (author_id, title, body_md) VALUES (?, 'Original', 'Cuerpo')",
        (author,),
    )
    return {"id": cursor.lastrowid, "author": author}


def test_a_member_can_post_and_read_it_back(client, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "Reunión del jueves", "body": "A las 19h en el café."})
    body = client.get("/comunidad/").get_data(as_text=True)
    assert "Reunión del jueves" in body


def test_markdown_is_rendered_but_html_is_not(client, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {
        "title": "Prueba",
        "body": "**fuerte** y <script>alert(1)</script>",
    })
    body = client.get("/comunidad/tema/1").get_data(as_text=True)
    assert "<strong>fuerte</strong>" in body
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_nobody_edits_someone_elses_words(client, post, thread, make_member, sign_in):
    """Not even an admin. Removing a post is visible to its author; quietly
    rewriting it is not, which is why moderation here means deletion."""
    sign_in(make_member("admina", role="admin"))
    response = post(f"/comunidad/tema/{thread['id']}/editar",
                    {"title": "Reescrito", "body": "Otra cosa"})
    assert response.status_code == 403


def test_a_user_cannot_delete_someone_elses_thread(client, post, thread, make_member, sign_in):
    sign_in(make_member("ajena"))
    assert post(f"/comunidad/tema/{thread['id']}/eliminar").status_code == 403


def test_an_admin_can_delete_any_thread(client, db, post, thread, make_member, sign_in):
    sign_in(make_member("admina", role="admin"))
    post(f"/comunidad/tema/{thread['id']}/eliminar")
    row = db.execute("SELECT deleted_at FROM threads WHERE id = ?", (thread["id"],)).fetchone()
    assert row["deleted_at"] is not None


def test_the_author_can_edit_their_own(client, db, post, thread, sign_in):
    sign_in(thread["author"])
    post(f"/comunidad/tema/{thread['id']}/editar", {"title": "Corregido", "body": "Mejor"})
    row = db.execute("SELECT title, edited_at FROM threads WHERE id = ?",
                     (thread["id"],)).fetchone()
    assert row["title"] == "Corregido"
    assert row["edited_at"] is not None


def test_a_deleted_thread_disappears_from_the_list_and_the_page(
        client, db, post, thread, sign_in):
    sign_in(thread["author"])
    post(f"/comunidad/tema/{thread['id']}/eliminar")
    assert "Original" not in client.get("/comunidad/").get_data(as_text=True)
    assert client.get(f"/comunidad/tema/{thread['id']}").status_code == 404


def test_a_deleted_thread_is_left_out_of_the_export(client, db, post, thread, sign_in):
    sign_in(thread["author"])
    post(f"/comunidad/tema/{thread['id']}/eliminar")
    assert "Original" not in client.get("/comunidad/mis-datos").get_data(as_text=True)


def test_a_closed_thread_refuses_replies(client, db, post, thread, make_member, sign_in):
    db.execute("UPDATE threads SET locked = 1 WHERE id = ?", (thread["id"],))
    sign_in(make_member("maria"))
    post(f"/comunidad/tema/{thread['id']}/comentar", {"body": "¿Hola?"})
    count = db.execute("SELECT COUNT(*) AS n FROM comments WHERE thread_id = ?",
                       (thread["id"],)).fetchone()["n"]
    assert count == 0


def test_only_admins_pin(client, post, thread, make_member, sign_in):
    sign_in(make_member("maria"))
    assert post(f"/comunidad/tema/{thread['id']}/estado",
                {"field": "pinned", "value": "1"}).status_code == 403


def test_the_state_field_is_not_a_way_into_the_query(client, post, thread, make_member, sign_in):
    """`field` is interpolated into the UPDATE, so it is checked against a fixed
    pair first. This asserts the check, not the interpolation."""
    sign_in(make_member("admina", role="admin"))
    assert post(f"/comunidad/tema/{thread['id']}/estado",
                {"field": "role", "value": "1"}).status_code == 400


def test_the_cooldown_stops_a_double_submit(app, client, post, make_member, sign_in, db):
    app.config["COOLDOWN_SECONDS"] = 60
    sign_in(make_member("rapida"))
    post("/comunidad/nuevo", {"title": "Primero", "body": "Uno"})
    post("/comunidad/nuevo", {"title": "Segundo", "body": "Dos"})
    count = db.execute("SELECT COUNT(*) AS n FROM threads").fetchone()["n"]
    assert count == 1


# --- the preview behind the Vista previa button ---------------------------
#
# A round trip rather than a markdown parser in the browser: the preview has to
# agree with what gets published, and the only way to guarantee that is to
# render it with the same function the board and the site use.

def test_the_preview_renders_the_same_markdown_the_board_does(post, make_member, sign_in):
    sign_in(make_member("maria"))

    response = post("/comunidad/previsualizar", {"body": "**hola** y _adiós_"})

    assert response.status_code == 200
    assert response.get_json()["html"] == "<p><strong>hola</strong> y <em>adiós</em></p>\n"


def test_the_preview_drops_html_exactly_as_the_board_does(post, make_member, sign_in):
    """`html=False` in render.py is the whole XSS defence. A preview that
    rendered raw HTML would be a hole in the members area and a lie about what
    posting is going to do."""
    sign_in(make_member("maria"))

    html = post("/comunidad/previsualizar",
                {"body": "<script>alert(1)</script>"}).get_json()["html"]

    assert "<script>" not in html


def test_the_preview_needs_a_session(client):
    assert client.post("/comunidad/previsualizar", data={"body": "hola"}).status_code in (302, 400)


def test_the_preview_is_never_cached(post, make_member, sign_in):
    sign_in(make_member("maria"))
    response = post("/comunidad/previsualizar", {"body": "hola"})
    assert response.headers["Cache-Control"] == "no-store"


# --- said out loud on the wall too ----------------------------------------

def test_a_thread_can_say_a_machine_wrote_it(client, post, make_member, sign_in):
    """The same disclosure inside the members area as on the public site. The
    people here know each other, which makes it more useful rather than less:
    nobody wants to answer a machine thinking they are answering a neighbour.
    """
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "Resumen de la reunión",
                              "body": "Lo que se decidió.", "ai_generated": "on"})

    page = client.get("/comunidad/muro").get_data(as_text=True)
    assert "Generado con IA" in page
    assert "Generado con IA" in client.get("/comunidad/tema/1").get_data(as_text=True)


def test_an_ordinary_thread_says_nothing_of_the_kind(client, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "Resumen", "body": "Escrito a mano."})
    assert "Generado con IA" not in client.get("/comunidad/muro").get_data(as_text=True)


def test_editing_a_thread_can_add_or_remove_the_disclosure(client, post, make_member, sign_in):
    """It is the author's statement about their own text, so it stays theirs
    to change — and an edit that forgets the field must not silently keep a
    mark the author has taken off."""
    member = make_member("maria")
    sign_in(member)
    post("/comunidad/nuevo", {"title": "Resumen", "body": "Primera versión.",
                              "ai_generated": "on"})
    post("/comunidad/tema/1/editar", {"title": "Resumen", "body": "Reescrito a mano."})
    assert "Generado con IA" not in client.get("/comunidad/tema/1").get_data(as_text=True)

    post("/comunidad/tema/1/editar", {"title": "Resumen", "body": "Otra vez con ayuda.",
                                      "ai_generated": "on"})
    assert "Generado con IA" in client.get("/comunidad/tema/1").get_data(as_text=True)


# --- a post nobody titled -------------------------------------------------

def test_a_thread_with_no_title_gets_one(client, post, make_member, sign_in):
    """Asking for a headline before a sentence is how a form stops somebody
    writing. The ones people leave blank are often the best writing on a
    wall — somebody answering a question, somebody saying a thing happened."""
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "", "body": "Ya está arreglado, gracias."})

    page = client.get("/comunidad/tema/1").get_data(as_text=True)
    assert "Sin Título" in page
    assert "Ya está arreglado" in page


def test_untitled_threads_are_numbered_so_they_can_be_told_apart(
        client, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "", "body": "Uno."})
    post("/comunidad/nuevo", {"title": "", "body": "Dos."})
    post("/comunidad/nuevo", {"title": "Con título", "body": "Tres."})
    post("/comunidad/nuevo", {"title": "", "body": "Cuatro."})

    wall = client.get("/comunidad/muro").get_data(as_text=True)
    for expected in ("Sin Título", "Sin Título 2", "Sin Título 3"):
        assert expected in wall
    assert "Sin Título 4" not in wall


def test_a_deleted_untitled_thread_does_not_give_its_number_back(
        client, post, db, make_member, sign_in):
    """A number that comes back is a thread that looks like one somebody
    remembers reading. The soft-deleted row keeps its title, and counting it
    is what prevents that."""
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "", "body": "Uno."})
    post("/comunidad/nuevo", {"title": "", "body": "Dos."})
    post("/comunidad/tema/2/eliminar")

    post("/comunidad/nuevo", {"title": "", "body": "Tres."})
    titles = [row["title"] for row in db.execute("SELECT title FROM threads ORDER BY id")]
    assert titles == ["Sin Título", "Sin Título 2", "Sin Título 3"]


def test_a_title_that_only_looks_numbered_is_left_alone(client, post, db,
                                                        make_member, sign_in):
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "Sin Título ni ganas", "body": "Uno."})
    post("/comunidad/nuevo", {"title": "", "body": "Dos."})
    titles = [row["title"] for row in db.execute("SELECT title FROM threads ORDER BY id")]
    assert titles == ["Sin Título ni ganas", "Sin Título"]


def test_a_thread_still_needs_something_to_say(client, post, make_member, sign_in):
    """The title is optional; the message is the post."""
    sign_in(make_member("maria"))
    post("/comunidad/nuevo", {"title": "Solo el título", "body": "   "})
    assert client.get("/comunidad/tema/1").status_code == 404
