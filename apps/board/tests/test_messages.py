"""Private messages, unread marks and blocking.

A conversation is addressed by the person, not by an id: `/privados/con/<id>`
is "what I have said to them and they to me", and it exists as a page before it
exists as a row. That makes the access rule structural rather than checked — a
lookup is always scoped to the member doing the looking, so there is no query
that could return somebody else's correspondence. The tests below still come at
it from the outside, because the failure mode here is not an error, it is
somebody quietly reading what is not theirs.
"""

from __future__ import annotations

import io
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def talk(member_id: int) -> str:
    return f"/comunidad/privados/con/{member_id}"


# --- who may read what ----------------------------------------------------

def test_a_third_person_sees_their_own_empty_conversation(
        client, post, make_member, sign_in):
    """The old design gave conversations their own ids, so this test had to
    check that a stranger opening one got a 404. Addressing by person removes
    the question: `/privados/con/<jose>` means *my* conversation with José, so
    a third person opening the same URL sees an empty page, not this one."""
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "Algo privado"})

    sign_in(make_member("curiosa"))
    page = client.get(talk(jose))

    assert page.status_code == 200
    assert "Algo privado" not in page.get_data(as_text=True)


def test_writing_to_somebody_does_not_join_you_to_their_other_conversations(
        client, db, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "Para José"})

    curiosa = make_member("curiosa")
    sign_in(curiosa)
    post(talk(jose), {"body": "Para José también"})

    # Two separate conversations, and neither shows the other's messages.
    assert db.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"] == 2
    assert "Para José" not in client.get(talk(jose)).get_data(as_text=True).replace(
        "Para José también", "")


def test_the_two_parties_see_it(client, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "Hola José"})

    sign_in(jose)
    assert "Hola José" in client.get(talk(maria)).get_data(as_text=True)


def test_writing_twice_reuses_the_one_conversation(client, db, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "Una"})
    post(talk(jose), {"body": "Dos"})

    assert db.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 2


# --- no ceremony before writing -------------------------------------------

def test_the_box_is_there_before_anything_has_been_said(
        client, post, make_member, sign_in):
    """There used to be an "Abrir conversación" button that created an empty
    conversation and sent you to it. The page now arrives ready to type in."""
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)

    page = client.get(talk(jose))

    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert 'name="body"' in body          # the textarea, on first arrival
    assert "Todavía no os habéis escrito" in body


def test_merely_looking_creates_nothing(client, db, make_member, sign_in):
    """Otherwise the inbox fills with "Sin mensajes todavía" for every name
    somebody was curious about."""
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)

    client.get(talk(jose))

    assert db.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"] == 0


def test_the_picker_sends_you_straight_there(client, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)

    response = client.get(f"/comunidad/privados/con?member_id={jose}")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/privados/con/{jose}")


def test_you_cannot_write_to_yourself(client, make_member, sign_in):
    maria = make_member("maria")
    sign_in(maria)
    assert client.get(talk(maria)).status_code == 400


def test_you_cannot_write_to_somebody_who_is_not_a_member(client, make_member, sign_in):
    sign_in(make_member("maria"))
    assert client.get(talk(9999)).status_code == 404


# --- unread ---------------------------------------------------------------

def test_unread_is_per_member_and_clears_on_reading(client, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "¿Vienes?"})

    badge = '<span class="tag">1</span>'

    # The sender has nothing unread — their own message does not count.
    assert badge not in client.get("/comunidad/privados").get_data(as_text=True)

    sign_in(jose)
    assert badge in client.get("/comunidad/privados").get_data(as_text=True)

    client.get(talk(maria))                       # opening marks it read
    assert badge not in client.get("/comunidad/privados").get_data(as_text=True)


# --- blocking -------------------------------------------------------------

def test_a_block_stops_both_directions(client, post, make_member, sign_in):
    """Symmetric on purpose. A block that silences only the blocked person
    leaves the blocker able to keep writing, which is a megaphone, not a
    safety feature."""
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "Hola"})
    post(f"/comunidad/privados/bloquear/{jose}")           # maria blocks jose

    assert post(talk(jose), {"body": "Otra cosa"}).status_code == 403   # …and maria too
    sign_in(jose)
    assert post(talk(maria), {"body": "¿Hola?"}).status_code == 403


def test_a_block_is_enforced_in_the_handler_not_the_template(
        client, post, make_member, sign_in):
    """The form is hidden once blocked, but hiding is a courtesy. Somebody who
    keeps the old page open, or crafts the request, meets the same refusal."""
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(jose)
    post(f"/comunidad/privados/bloquear/{maria}")

    sign_in(maria)   # never reloaded the page, still has the form
    assert post(talk(jose), {"body": "Hola"}).status_code == 403


def test_a_blocked_person_still_sees_the_history_but_no_box(
        client, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "Antes del bloqueo"})
    post(f"/comunidad/privados/bloquear/{jose}")

    body = client.get(talk(jose)).get_data(as_text=True)
    assert "Antes del bloqueo" in body
    assert 'name="body"' not in body
    assert "bloqueo entre vosotros" in body


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
    sign_in(maria)
    post(talk(jose), {"body": "Mira", "pictures": (io.BytesIO(PNG), "foto.png")},
         content_type="multipart/form-data")

    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]
    assert client.get(f"/comunidad/media/{name}").status_code == 200

    sign_in(jose)
    assert client.get(f"/comunidad/media/{name}").status_code == 200

    sign_in(make_member("curiosa"))
    assert client.get(f"/comunidad/media/{name}").status_code == 404


def test_an_empty_message_with_no_picture_is_refused(client, db, post, make_member, sign_in):
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "   "})

    assert db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"] == 0


# --- erasure --------------------------------------------------------------

def test_erasing_a_member_takes_their_private_messages(
        app, client, db, post, owner_id, make_member, sign_in):
    """Unlike a thread, which survives its author as "Miembro eliminado". A
    two-party exchange has no remainder to preserve, and keeping half of
    somebody's erased correspondence is what erasure exists to prevent."""
    maria, jose = make_member("maria"), make_member("jose")
    sign_in(maria)
    post(talk(jose), {"body": "Privado", "pictures": (io.BytesIO(PNG), "f.png")},
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
