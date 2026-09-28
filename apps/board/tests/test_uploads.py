"""Pictures on the board.

Two things are being defended here, and they are not the same thing.

One is the member: a photo they attach must survive, be visible to other
members, and stop being visible when the post comes down.

The other is the server: an upload is the one place where a member hands over
bytes that this app later serves back. Most of these tests are about the ways
that can be abused, and they are written against the bytes rather than the
filename, because the filename is the attacker's to choose.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from apps.board import uploads

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
HTML = b"<!DOCTYPE html><script>alert(1)</script>"


def picture(data=PNG, name="foto.png"):
    return (io.BytesIO(data), name)


def post_thread(post, follow_redirects=False, **files):
    """follow_redirects is named explicitly so it cannot fall into **files and
    be posted as a form field — which it silently was, leaving two tests
    asserting against a bare 302 body."""
    payload = {"title": "Con foto", "body": "Mirad esto"}
    payload.update(files)
    return post("/comunidad/nuevo", payload, content_type="multipart/form-data",
                follow_redirects=follow_redirects)


# --- what counts as an image ---------------------------------------------

@pytest.mark.parametrize("data,expected", [
    (PNG, "png"), (JPEG, "jpg"), (WEBP, "webp"),
    (b"GIF89a" + b"\x00" * 32, "gif"),
    (b"\x00\x00\x00\x20ftypavif" + b"\x00" * 32, "avif"),
    (HTML, None),
    (b"", None),
    (b"\x89PNG", None),          # truncated magic
])
def test_the_bytes_decide(data, expected):
    assert uploads._detect(data) is expected


def test_a_script_named_like_a_picture_is_refused(app, client, db, post, make_member, sign_in):
    """The filename is the uploader's to choose, so it decides nothing. This is
    the case that makes sniffing worth the code: stored and served back as
    image/png, an HTML file is a script running on our own origin."""
    sign_in(make_member("maria"))
    response = post_thread(post, pictures=picture(HTML, "gato.png"), follow_redirects=True)

    assert "no parece una imagen" in response.get_data(as_text=True)
    assert db.execute("SELECT 1 FROM attachments").fetchone() is None


def test_a_misnamed_but_real_picture_is_kept(app, client, db, post, make_member, sign_in):
    """The mirror of the test above, and the reason the extension is ignored
    rather than compared: a JPEG called .png is somebody's phone being untidy,
    not an attack, and it is stored as what it actually is."""
    sign_in(make_member("maria"))
    post_thread(post, pictures=picture(JPEG, "foto.png"))

    row = db.execute("SELECT * FROM attachments").fetchone()
    assert row["content_type"] == "image/jpeg"
    assert row["stored_name"].endswith(".jpg")


def test_an_oversized_picture_is_refused(app, client, db, post, make_member, sign_in):
    app.config["UPLOAD_MAX_BYTES"] = 100
    sign_in(make_member("maria"))
    response = post_thread(post, pictures=picture(PNG + b"\x00" * 500), follow_redirects=True)

    assert "máximo" in response.get_data(as_text=True)
    assert db.execute("SELECT 1 FROM attachments").fetchone() is None


def test_too_many_at_once_is_refused(app, client, db, post, make_member, sign_in):
    sign_in(make_member("maria"))
    response = post("/comunidad/nuevo", {
        "title": "Muchas", "body": "Texto",
        "pictures": [picture(PNG, f"f{n}.png") for n in range(uploads.MAX_FILES + 1)],
    }, content_type="multipart/form-data", follow_redirects=True)

    assert "Como máximo" in response.get_data(as_text=True)
    assert db.execute("SELECT 1 FROM attachments").fetchone() is None


# --- a refusal must not leave wreckage -----------------------------------

def test_a_refused_picture_leaves_no_thread_behind(app, client, db, post, make_member, sign_in):
    """The reason staging is separate from saving. Insert the thread first and
    a rejected photo leaves its author looking at a post they did not finish
    writing, with no way to tell what happened."""
    sign_in(make_member("maria"))
    post_thread(post, pictures=picture(HTML, "malo.png"))

    assert db.execute("SELECT 1 FROM threads").fetchone() is None


# --- the name on disk -----------------------------------------------------

def test_the_uploaded_name_is_never_used_as_a_path(app, client, db, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post_thread(post, pictures=picture(PNG, "../../../etc/passwd.png"))

    stored = db.execute("SELECT * FROM attachments").fetchone()
    assert "/" not in stored["stored_name"]
    assert uploads.STORED_NAME.match(stored["stored_name"])
    # The original is kept, but only ever as text to show a person.
    assert stored["original_name"] == "../../../etc/passwd.png"


def test_a_name_we_did_not_generate_is_not_served(app, client, make_member, sign_in):
    sign_in(make_member("maria"))
    for name in ("../board.db", "..%2Fboard.db", "board.db", "foto.png"):
        assert client.get(f"/comunidad/media/{name}").status_code == 404


# --- who can see them -----------------------------------------------------

def test_a_picture_is_served_to_a_member(app, client, db, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post_thread(post, pictures=picture(PNG))
    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]

    response = client.get(f"/comunidad/media/{name}")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == PNG


def test_a_picture_is_not_served_to_a_stranger(app, client, db, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post_thread(post, pictures=picture(PNG))
    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]

    with client.session_transaction() as session:
        session.clear()
    response = client.get(f"/comunidad/media/{name}")
    assert response.status_code == 302
    assert "/comunidad/login" in response.headers["Location"]


def test_deleting_the_thread_takes_its_pictures_out_of_reach(
        app, client, db, post, make_member, sign_in):
    """Threads are soft-deleted, so without this check the row stays, the file
    stays, and the photo of a post somebody asked to have removed is still
    readable by anyone who noted the URL."""
    sign_in(make_member("maria"))
    post_thread(post, pictures=picture(PNG))
    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]
    thread_id = db.execute("SELECT id FROM threads").fetchone()["id"]
    assert client.get(f"/comunidad/media/{name}").status_code == 200

    post(f"/comunidad/tema/{thread_id}/eliminar")
    assert client.get(f"/comunidad/media/{name}").status_code == 404


def test_the_same_applies_to_a_deleted_comment(app, client, db, post, make_member, sign_in):
    member = make_member("maria")
    sign_in(member)
    post_thread(post, pictures=picture(PNG))
    thread_id = db.execute("SELECT id FROM threads").fetchone()["id"]
    post(f"/comunidad/tema/{thread_id}/comentar",
         {"body": "Yo también", "pictures": picture(JPEG, "mia.jpg")},
         content_type="multipart/form-data")

    name = db.execute(
        "SELECT stored_name FROM attachments WHERE comment_id IS NOT NULL").fetchone()["stored_name"]
    comment_id = db.execute("SELECT id FROM comments").fetchone()["id"]
    assert client.get(f"/comunidad/media/{name}").status_code == 200

    post(f"/comunidad/comentario/{comment_id}/eliminar")
    assert client.get(f"/comunidad/media/{name}").status_code == 404


# --- and they show up -----------------------------------------------------

def test_the_picture_appears_on_the_thread(app, client, db, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post_thread(post, pictures=picture(PNG))
    thread_id = db.execute("SELECT id FROM threads").fetchone()["id"]
    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]

    body = client.get(f"/comunidad/tema/{thread_id}").get_data(as_text=True)
    assert f"/comunidad/media/{name}" in body


def test_the_file_reaches_the_disk(app, client, db, post, make_member, sign_in):
    sign_in(make_member("maria"))
    post_thread(post, pictures=picture(PNG))
    name = db.execute("SELECT stored_name FROM attachments").fetchone()["stored_name"]

    assert (Path(app.config["UPLOAD_DIR"]) / name).read_bytes() == PNG


def test_nothing_is_written_when_no_picture_is_chosen(app, client, db, post, make_member, sign_in):
    """An empty file input still arrives in the request; it must not become a
    zero-byte attachment."""
    sign_in(make_member("maria"))
    post_thread(post, pictures=(io.BytesIO(b""), ""))

    assert db.execute("SELECT 1 FROM attachments").fetchone() is None
    assert db.execute("SELECT 1 FROM threads").fetchone() is not None
