"""One 👍 per member per thing, and clicking it again takes it back.

The whole feature is a count and a toggle. What is worth testing is not that a
row appears — it is that a member cannot react twice (the database says so, not
the handler), that the button goes back where it was pressed without becoming an
open redirect, and that reacting to something that has been deleted is a 404
rather than a row pointing at nothing.
"""

from __future__ import annotations

import sqlite3

import pytest


def react(post, kind, target_id, back=None):
    data = {"kind": kind, "id": target_id}
    if back is not None:
        data["back"] = back
    return post("/comunidad/reaccion", data)


@pytest.fixture
def thread(post, make_member, sign_in):
    """A thread by José, for somebody else to react to."""
    jose = make_member("jose")
    sign_in(jose)
    post("/comunidad/nuevo", {"title": "Hola", "body": "Qué tal"})
    return 1


def test_reacting_adds_one_and_reacting_again_takes_it_back(
        db, post, thread, make_member, sign_in):
    sign_in(make_member("maria"))

    react(post, "thread", thread)
    assert db.execute("SELECT COUNT(*) AS n FROM reactions").fetchone()["n"] == 1

    react(post, "thread", thread)
    assert db.execute("SELECT COUNT(*) AS n FROM reactions").fetchone()["n"] == 0


def test_two_members_each_get_one(db, post, thread, make_member, sign_in):
    for name in ("maria", "luisa"):
        sign_in(make_member(name))
        react(post, "thread", thread)

    assert db.execute("SELECT COUNT(*) AS n FROM reactions").fetchone()["n"] == 2


def test_the_database_refuses_a_second_one(db, thread, make_member):
    """A partial unique index rather than a check in the handler, because the
    handler is one bug away from inserting twice and the index is not.

    UNIQUE(member_id, thread_id) on its own would not do it: SQLite treats NULLs
    as distinct, so every comment reaction — all with a NULL thread_id — would
    be unique by accident."""
    maria = make_member("maria")
    db.execute("INSERT INTO reactions (thread_id, member_id) VALUES (?, ?)", (thread, maria))

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO reactions (thread_id, member_id) VALUES (?, ?)",
                   (thread, maria))


def test_a_reaction_belongs_to_exactly_one_thing(db, thread, make_member):
    maria = make_member("maria")
    for columns, values in (("", ""), ("thread_id, comment_id", f"{thread}, 1")):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT INTO reactions ({columns + ', ' if columns else ''}member_id) "
                f"VALUES ({values + ', ' if values else ''}{maria})")


def test_comments_can_be_reacted_to_as_well(db, post, thread, make_member, sign_in):
    sign_in(make_member("maria"))
    post(f"/comunidad/tema/{thread}/comentar", {"body": "Yo también"})

    react(post, "comment", 1)

    row = db.execute("SELECT thread_id, comment_id FROM reactions").fetchone()
    assert (row["thread_id"], row["comment_id"]) == (None, 1)


def test_the_count_and_my_own_mark_show_on_the_page(client, post, thread,
                                                    make_member, sign_in):
    sign_in(make_member("maria"))
    react(post, "thread", thread)

    for url in (f"/comunidad/tema/{thread}", "/comunidad/muro"):
        body = client.get(url).get_data(as_text=True)
        assert "reaction__button--mine" in body, url
        assert ">1<" in body, url

    # Somebody else sees the count but not the filled button.
    sign_in(make_member("luisa"))
    body = client.get("/comunidad/muro").get_data(as_text=True)
    assert "reaction__button--mine" not in body
    assert ">1<" in body


def test_reacting_to_something_that_is_gone_is_a_404(db, post, thread,
                                                     owner_id, make_member, sign_in):
    sign_in(owner_id)
    post(f"/comunidad/tema/{thread}/eliminar")

    sign_in(make_member("maria"))
    assert react(post, "thread", thread).status_code == 404
    assert react(post, "thread", 9999).status_code == 404
    assert db.execute("SELECT COUNT(*) AS n FROM reactions").fetchone()["n"] == 0


def test_an_unknown_kind_is_refused(post, thread, make_member, sign_in):
    sign_in(make_member("maria"))
    assert react(post, "members", thread).status_code == 400


def test_the_back_field_cannot_leave_the_site(post, thread, make_member, sign_in):
    """`back` is a redirect somebody can type, which is an open redirect unless
    it is checked — the same hole the login form's `next` has, so it goes
    through the same function."""
    sign_in(make_member("maria"))

    response = react(post, "thread", thread, back="https://ejemplo.invalid/premio")

    assert response.status_code == 302
    assert "ejemplo.invalid" not in response.headers["Location"]


def test_the_button_comes_back_to_the_comment_it_was_pressed_on(
        post, thread, make_member, sign_in):
    sign_in(make_member("maria"))
    post(f"/comunidad/tema/{thread}/comentar", {"body": "Yo también"})

    response = react(post, "comment", 1, back=f"/comunidad/tema/{thread}#c1")

    assert response.headers["Location"].endswith(f"/comunidad/tema/{thread}#c1")


def test_erasing_a_member_takes_their_reactions(db, post, thread, owner_id,
                                                make_member, sign_in):
    maria = make_member("maria")
    sign_in(maria)
    react(post, "thread", thread)

    sign_in(owner_id)
    post(f"/comunidad/miembros/{maria}/eliminar")

    assert db.execute("SELECT COUNT(*) AS n FROM reactions").fetchone()["n"] == 0
