"""Private messages, unread marks and blocking.

The access rule is membership of the conversation, and it is worth testing
from the outside rather than trusting the query: the failure mode is not an
error, it is somebody quietly reading correspondence that is not theirs.
"""

from __future__ import annotations

import io
from pathlib import Path


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def conversation_between(client, post, a, b, sign_in):
    sign_in(a)
    response = post("/comunidad/privados/nueva", {"member_id": str(b)})
    return int(response.headers["Location"].rstrip("/").rsplit("/", 1)[1])


# --- who may read what ----------------------------------------------------

def test_a_stranger_cannot_read_a_conversation(client, post, make_member, sign_in):
    """404 rather than 403, deliberately: a member who is not in a conversation
    should not be able to tell it apart from one that does not exist."""
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)

    sign_in(make_member("curiosa"))
    assert client.get(f"/comunidad/privados/{conversation}").status_code == 404


def test_a_stranger_cannot_send_into_one_either(client, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)

    sign_in(make_member("curiosa"))
    response = post(f"/comunidad/privados/{conversation}/enviar", {"body": "Hola"})
    assert response.status_code == 404


def test_the_two_parties_can(client, db, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)
    post(f"/comunidad/privados/{conversation}/enviar", {"body": "Hola José"})

    sign_in(jose)
    body = client.get(f"/comunidad/privados/{conversation}").get_data(as_text=True)
    assert "Hola José" in body


def test_opening_the_same_person_twice_reuses_the_conversation(
        client, db, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    first = conversation_between(client, post, maria, jose, sign_in)
    second = conversation_between(client, post, maria, jose, sign_in)

    assert first == second
    assert db.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"] == 1


# --- unread ---------------------------------------------------------------

def test_unread_is_per_member_and_clears_on_reading(client, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)
    post(f"/comunidad/privados/{conversation}/enviar", {"body": "¿Vienes?"})

    badge = '<span class="tag">1</span>'

    # The sender has nothing unread — their own message does not count.
    assert badge not in client.get("/comunidad/privados").get_data(as_text=True)

    sign_in(jose)
    assert badge in client.get("/comunidad/privados").get_data(as_text=True)

    client.get(f"/comunidad/privados/{conversation}")     # opening marks it read
    assert badge not in client.get("/comunidad/privados").get_data(as_text=True)


# --- blocking -------------------------------------------------------------

def test_a_block_stops_both_directions(client, post, make_member, sign_in):
    """Symmetric on purpose. A block that silences only the blocked person
    leaves the blocker able to keep writing, which is a megaphone, not a
    safety feature."""
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)

    post(f"/comunidad/privados/bloquear/{jose}")          # maria blocks jose

    assert post(f"/comunidad/privados/{conversation}/enviar",
                {"body": "Otra cosa"}).status_code == 403   # …and maria too
    sign_in(jose)
    assert post(f"/comunidad/privados/{conversation}/enviar",
                {"body": "¿Hola?"}).status_code == 403


def test_a_block_is_enforced_in_the_handler_not_the_template(
        client, post, make_member, sign_in):
    """The form is hidden once blocked, but hiding is a courtesy. Somebody who
    keeps the old page open, or crafts the request, meets the same refusal."""
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)
    sign_in(jose)
    post(f"/comunidad/privados/bloquear/{maria}")

    sign_in(maria)   # never reloaded the page, still has the form
    assert post(f"/comunidad/privados/{conversation}/enviar",
                {"body": "Hola"}).status_code == 403


def test_a_blocked_person_cannot_start_a_new_conversation(
        client, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(f"/comunidad/privados/bloquear/{jose}")

    sign_in(jose)
    assert post("/comunidad/privados/nueva", {"member_id": str(maria)}).status_code == 403


def test_unblocking_only_removes_your_own(client, db, post, make_member, sign_in):
    """Otherwise the blocked person could lift the block that exists because
    of them."""
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(f"/comunidad/privados/bloquear/{jose}")

    sign_in(jose)
    post(f"/comunidad/privados/desbloquear/{maria}")

    assert db.execute("SELECT COUNT(*) AS n FROM blocks").fetchone()["n"] == 1


def test_a_blocked_person_is_not_offered_in_the_list(client, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(f"/comunidad/privados/bloquear/{jose}")

    assert "Jose" not in client.get("/comunidad/privados").get_data(as_text=True)


# --- pictures -------------------------------------------------------------

def test_a_picture_in_a_message_is_private_to_the_two_of_them(
        app, client, db, post, make_member, sign_in):
    """The board's pictures are for every member. These are not, and being
    signed in is nowhere near enough of a check."""
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)
    post(f"/comunidad/privados/{conversation}/enviar",
         {"body": "Mira", "pictures": (io.BytesIO(PNG), "foto.png")},
         content_type="multipart/form-data")

    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]
    assert client.get(f"/comunidad/media/{name}").status_code == 200

    sign_in(jose)
    assert client.get(f"/comunidad/media/{name}").status_code == 200

    sign_in(make_member("curiosa"))
    assert client.get(f"/comunidad/media/{name}").status_code == 404


def test_an_empty_message_with_no_picture_is_refused(client, db, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)
    post(f"/comunidad/privados/{conversation}/enviar", {"body": "   "})

    assert db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


# --- erasure --------------------------------------------------------------

def test_erasing_a_member_takes_their_private_messages(
        app, client, db, post, owner_id, make_member, sign_in):
    """Unlike a thread, which survives its author as "Miembro eliminado". A
    two-party exchange has no remainder to preserve, and keeping half of
    somebody's erased correspondence is what erasure exists to prevent."""
    maria, jose = make_member("maria"), make_member("jose")
    conversation = conversation_between(client, post, maria, jose, sign_in)
    post(f"/comunidad/privados/{conversation}/enviar",
         {"body": "Privado", "pictures": (io.BytesIO(PNG), "f.png")},
         content_type="multipart/form-data")
    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]

    sign_in(owner_id)
    post(f"/comunidad/miembros/{maria}/eliminar")

    for table in ("conversations", "conversation_members", "messages", "attachments"):
        assert db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0, table
    assert not (Path(app.config["UPLOAD_DIR"]) / name).exists()


def test_erasing_a_member_removes_blocks_either_way(
        client, db, post, owner_id, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(f"/comunidad/privados/bloquear/{jose}")
    sign_in(jose)
    post(f"/comunidad/privados/bloquear/{maria}")

    sign_in(owner_id)
    post(f"/comunidad/miembros/{maria}/eliminar")

    assert db.execute("SELECT COUNT(*) AS n FROM blocks").fetchone()["n"] == 0
