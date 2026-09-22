"""The role rules, from the predicates up to the routes that enforce them."""

from __future__ import annotations

import sqlite3

import pytest

from apps.board import gitea
from apps.board.members import may_create, may_manage


# --- the rules as plain functions ---------------------------------------

def test_only_the_owner_makes_admins():
    assert may_create("owner", "admin")
    assert not may_create("admin", "admin")
    assert not may_create("user", "admin")


def test_admins_and_the_owner_make_users():
    assert may_create("owner", "user")
    assert may_create("admin", "user")
    assert not may_create("user", "user")


def test_the_owner_is_beyond_everyone_including_themselves():
    assert not may_manage("owner", "owner")
    assert not may_manage("admin", "owner")


def test_only_the_owner_manages_admins():
    assert may_manage("owner", "admin")
    assert not may_manage("admin", "admin")


# --- the database holds the line ----------------------------------------

def test_a_second_owner_is_impossible(db):
    """Not a route check — a direct insert, because the point of the partial
    unique index is to survive a bug in the code above it."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO members (gitea_login, role) VALUES ('usurpador', 'owner')"
        )


def test_transfer_leaves_exactly_one_owner(app, db, owner_id, make_member):
    from apps.board.members import transfer_ownership
    admin_id = make_member("segunda", role="admin")
    transfer_ownership(db, owner_id, admin_id)

    owners = db.execute("SELECT id FROM members WHERE role = 'owner'").fetchall()
    assert [row["id"] for row in owners] == [admin_id]
    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (owner_id,)).fetchone()["role"] == "admin"


# --- the routes ----------------------------------------------------------

def test_admin_cannot_create_an_admin(client, post, make_member, sign_in):
    sign_in(make_member("admina", role="admin"))
    response = post("/comunidad/miembros/nuevo", {
        "login": "nueva", "display_name": "Nueva", "email": "n@example.com",
        "role": "admin", "create_account": "",
    })
    assert response.status_code == 403


def test_owner_can_create_an_admin(client, db, post, owner_id, sign_in):
    sign_in(owner_id)
    post("/comunidad/miembros/nuevo", {
        "login": "nueva", "display_name": "Nueva", "email": "n@example.com",
        "role": "admin", "create_account": "",
    })
    row = db.execute("SELECT role FROM members WHERE gitea_login = 'nueva'").fetchone()
    assert row["role"] == "admin"


def test_a_plain_user_cannot_reach_the_admin_screens(client, make_member, sign_in):
    sign_in(make_member("cualquiera"))
    assert client.get("/comunidad/miembros/nuevo").status_code == 403


def test_the_owner_cannot_be_suspended(client, post, owner_id, make_member, sign_in):
    sign_in(make_member("admina", role="admin"))
    response = post(f"/comunidad/miembros/{owner_id}/estado", {"active": "0"})
    assert response.status_code == 403


def test_the_owner_cannot_suspend_themselves(client, post, owner_id, sign_in):
    sign_in(owner_id)
    response = post(f"/comunidad/miembros/{owner_id}/estado", {"active": "0"})
    assert response.status_code == 403


def test_an_admin_cannot_demote_another_admin(client, post, make_member, sign_in):
    other = make_member("otra", role="admin")
    sign_in(make_member("admina", role="admin"))
    response = post(f"/comunidad/miembros/{other}/rol", {"role": "user"})
    assert response.status_code == 403


def test_creating_a_user_shows_the_password_once(client, monkeypatch, post, owner_id, sign_in):
    created = {}
    monkeypatch.setattr(gitea, "admin_create_user",
                        lambda login, email, name, password: created.update(
                            login=login, password=password))
    sign_in(owner_id)
    response = post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "m@example.com",
        "role": "user", "create_account": "on",
    })
    assert created["login"] == "maria"
    assert created["password"].encode() in response.data


def test_a_rejected_gitea_call_creates_no_member(client, monkeypatch, db, post, owner_id, sign_in):
    def boom(*args, **kwargs):
        raise gitea.GiteaError("Ese usuario ya existe en Gitea.")
    monkeypatch.setattr(gitea, "admin_create_user", boom)
    sign_in(owner_id)
    post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "m@example.com",
        "role": "user", "create_account": "on",
    })
    assert db.execute("SELECT 1 FROM members WHERE gitea_login = 'maria'").fetchone() is None


def test_erasing_a_member_keeps_their_threads_readable(app, db, post, owner_id, make_member, sign_in):
    author = make_member("saliente")
    db.execute("INSERT INTO threads (author_id, title, body_md) VALUES (?, 'Hola', 'Texto')",
               (author,))
    sign_in(owner_id)
    post(f"/comunidad/miembros/{author}/eliminar")

    assert db.execute("SELECT 1 FROM members WHERE id = ?", (author,)).fetchone() is None
    row = db.execute(
        """SELECT m.display_name FROM threads t JOIN members m ON m.id = t.author_id
            WHERE t.title = 'Hola'"""
    ).fetchone()
    assert row["display_name"] == "Miembro eliminado"


def test_the_member_list_renders_for_each_role(client, db, owner_id, make_member, sign_in):
    """Every role takes a different branch through members.html — the owner
    sees transfer and erase, an admin sees suspend, a user sees neither — so
    each one is rendered here rather than trusted."""
    admin_id = make_member("admina", role="admin")
    user_id = make_member("usuaria")

    for member_id, expected in ((owner_id, "Transferir titularidad"),
                                (admin_id, "Suspender"),
                                (user_id, None)):
        sign_in(member_id)
        body = client.get("/comunidad/miembros").get_data(as_text=True)
        assert "usuaria" in body
        if expected:
            assert expected in body
        else:
            assert "Transferir titularidad" not in body
            assert "Suspender" not in body


def test_the_new_member_form_hides_the_role_choice_from_admins(
        client, owner_id, make_member, sign_in):
    sign_in(owner_id)
    assert "Administrador" in client.get("/comunidad/miembros/nuevo").get_data(as_text=True)

    sign_in(make_member("admina", role="admin"))
    body = client.get("/comunidad/miembros/nuevo").get_data(as_text=True)
    assert 'value="admin"' not in body


def test_the_export_is_only_your_own_writing(client, db, make_member, sign_in):
    mine = make_member("mia")
    theirs = make_member("suya")
    db.execute("INSERT INTO threads (author_id, title, body_md) VALUES (?, 'Mío', 'A')", (mine,))
    db.execute("INSERT INTO threads (author_id, title, body_md) VALUES (?, 'Suyo', 'B')", (theirs,))
    sign_in(mine)

    body = client.get("/comunidad/mis-datos").get_data(as_text=True)
    assert "Mío" in body
    assert "Suyo" not in body
