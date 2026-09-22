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
