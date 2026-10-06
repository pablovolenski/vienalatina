"""The role rules, from the predicates up to the routes that enforce them."""

from __future__ import annotations

import sqlite3

import pytest

from apps.board.members import may_create, may_manage


# --- the rules as plain functions ---------------------------------------

def test_only_the_superadmin_makes_admins():
    """The line this phase draws. An admin account is the platform's, not the
    association's — which is what makes it safe to hand the responsable chair to
    a customer organisation's president."""
    assert may_create("superadmin", "admin")
    assert not may_create("owner", "admin")
    assert not may_create("admin", "admin")
    assert not may_create("user", "admin")


def test_everyone_above_a_moderator_makes_users():
    assert may_create("superadmin", "user")
    assert may_create("owner", "user")
    assert may_create("admin", "user")
    assert not may_create("user", "user")


def test_neither_chair_can_be_touched_by_anybody():
    """Both single chairs, both unreachable, for different reasons. The
    superadministrator because there is nothing above them; the responsable
    because the chair belongs to the association, and a platform administrator
    quietly removing the president of the organisation they host is the move the
    separation exists to prevent."""
    for actor in ("superadmin", "owner", "admin", "moderator", "user"):
        assert not may_manage(actor, "superadmin"), actor
        assert not may_manage(actor, "owner"), actor


def test_only_the_superadmin_manages_admins():
    assert may_manage("superadmin", "admin")
    assert not may_manage("owner", "admin")
    assert not may_manage("admin", "admin")


def test_the_role_picker_offers_exactly_what_the_handler_allows():
    """They used to be one constant and one predicate, which could only agree
    for one actor once there were two levels above admin."""
    from apps.board.members import assignable_roles
    for actor in ("superadmin", "owner", "admin", "moderator", "user"):
        for role in ("admin", "moderator", "user"):
            assert (role in assignable_roles(actor)) == may_create(actor, role), (actor, role)


# --- the moderator, who curates content and not people -------------------

def test_admins_and_the_chairs_make_moderators():
    assert may_create("superadmin", "moderator")
    assert may_create("owner", "moderator")
    assert may_create("admin", "moderator")
    assert not may_create("moderator", "moderator")
    assert not may_create("user", "moderator")


def test_a_moderator_has_no_power_over_anybody():
    """The line the whole role rests on. Being trusted to judge what the public
    reads is not being trusted to suspend the person who wrote it, and a
    moderator who could do both would be a second kind of admin."""
    for target in ("superadmin", "owner", "admin", "moderator", "user"):
        assert not may_manage("moderator", target), target
        assert not may_create("moderator", target), target


def test_an_admin_may_promote_a_member_to_moderator(client, db, post, make_member, sign_in):
    admin = make_member("admina", role="admin")
    maria = make_member("maria")
    sign_in(admin)

    post(f"/comunidad/miembros/{maria}/rol", {"role": "moderator"})

    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (maria,)).fetchone()["role"] == "moderator"


def test_an_admin_still_cannot_mint_an_admin_through_the_role_form(
        client, db, post, make_member, sign_in):
    """`set_role` is admin-level so moderators can be appointed routinely. That
    must not quietly hand admins the one power the superadministrator keeps."""
    admin = make_member("admina", role="admin")
    maria = make_member("maria")
    sign_in(admin)

    assert post(f"/comunidad/miembros/{maria}/rol", {"role": "admin"}).status_code == 403
    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (maria,)).fetchone()["role"] == "user"


def test_a_moderator_cannot_change_a_role_at_all(client, db, post, make_member, sign_in):
    sign_in(make_member("luisa", role="moderator"))
    maria = make_member("maria")

    assert post(f"/comunidad/miembros/{maria}/rol", {"role": "moderator"}).status_code == 403


def test_the_role_form_refuses_a_role_that_is_not_offered(
        client, db, post, superadmin_id, make_member, sign_in):
    """`tombstone` is a real value in the CHECK constraint and would make the
    member vanish from every listing while keeping their row."""
    sign_in(superadmin_id)
    maria = make_member("maria")

    assert post(f"/comunidad/miembros/{maria}/rol", {"role": "tombstone"}).status_code == 403
    assert post(f"/comunidad/miembros/{maria}/rol", {"role": "owner"}).status_code == 403


# --- the database holds the line ----------------------------------------

def test_a_second_superadmin_is_impossible(db, superadmin_id):
    """Not a route check — a direct insert, because the point of the partial
    unique index is to survive a bug in the code above it."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO members (gitea_login, role) VALUES ('usurpador', 'superadmin')"
        )


def test_a_second_owner_is_impossible(db, owner_id):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO members (gitea_login, role) VALUES ('usurpadora', 'owner')"
        )


@pytest.mark.parametrize("chair", ["superadmin", "owner"])
def test_transfer_leaves_exactly_one_holder(app, db, superadmin_id, make_member, chair):
    """Both chairs hand on the same way. The demotion comes first, or the unique
    index refuses the promotion halfway through."""
    from apps.board.members import transfer_ownership
    holder = superadmin_id if chair == "superadmin" else make_member("presi", role="owner")
    admin_id = make_member("segunda", role="admin")

    transfer_ownership(db, holder, admin_id, chair)

    holders = db.execute("SELECT id FROM members WHERE role = ?", (chair,)).fetchall()
    assert [row["id"] for row in holders] == [admin_id]
    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (holder,)).fetchone()["role"] == "admin"


def test_a_transfer_to_something_that_is_not_a_chair_is_refused(db, superadmin_id, make_member):
    """`chair` comes from the route and never from a request, and this is what
    keeps that true if somebody ever wires it to one."""
    from apps.board.members import transfer_ownership
    with pytest.raises(ValueError):
        transfer_ownership(db, superadmin_id, make_member("otra"), "admin")


# --- the routes ----------------------------------------------------------

def test_admin_cannot_create_an_admin(client, post, make_member, sign_in):
    sign_in(make_member("admina", role="admin"))
    response = post("/comunidad/miembros/nuevo", {
        "login": "nueva", "display_name": "Nueva", "email": "n@example.com",
        "role": "admin",
    })
    assert response.status_code == 403


def test_the_superadmin_can_create_an_admin(client, db, post, superadmin_id, sign_in):
    sign_in(superadmin_id)
    post("/comunidad/miembros/nuevo", {
        "login": "nueva", "display_name": "Nueva", "email": "n@example.com",
        "role": "admin",
    })
    row = db.execute("SELECT role FROM members WHERE gitea_login = 'nueva'").fetchone()
    assert row["role"] == "admin"


def test_a_plain_user_cannot_reach_the_admin_screens(client, make_member, sign_in):
    sign_in(make_member("cualquiera"))
    assert client.get("/comunidad/miembros/nuevo").status_code == 403


def test_the_superadmin_cannot_be_suspended(client, post, superadmin_id, make_member, sign_in):
    sign_in(make_member("admina", role="admin"))
    response = post(f"/comunidad/miembros/{superadmin_id}/estado", {"active": "0"})
    assert response.status_code == 403


def test_the_superadmin_cannot_suspend_themselves(client, post, superadmin_id, sign_in):
    sign_in(superadmin_id)
    response = post(f"/comunidad/miembros/{superadmin_id}/estado", {"active": "0"})
    assert response.status_code == 403


def test_an_admin_cannot_demote_another_admin(client, post, make_member, sign_in):
    other = make_member("otra", role="admin")
    sign_in(make_member("admina", role="admin"))
    response = post(f"/comunidad/miembros/{other}/rol", {"role": "user"})
    assert response.status_code == 403






def test_erasing_a_member_keeps_their_threads_readable(app, db, post, superadmin_id, make_member, sign_in):
    author = make_member("saliente")
    db.execute("INSERT INTO threads (author_id, title, body_md) VALUES (?, 'Hola', 'Texto')",
               (author,))
    sign_in(superadmin_id)
    post(f"/comunidad/miembros/{author}/eliminar")

    assert db.execute("SELECT 1 FROM members WHERE id = ?", (author,)).fetchone() is None
    row = db.execute(
        """SELECT m.display_name FROM threads t JOIN members m ON m.id = t.author_id
            WHERE t.title = 'Hola'"""
    ).fetchone()
    assert row["display_name"] == "Miembro eliminado"


def test_the_member_page_renders_for_each_role(client, db, superadmin_id, owner_id,
                                               make_member, sign_in):
    """Every role takes a different branch through member.html — the
    superadministrator alone sees «Entregar la plataforma», both chairs see
    «Eliminar», an admin sees «Suspender» and a plain member sees none of it —
    so each one is rendered here rather than trusted.

    The page opened is always somebody else's: nobody manages themselves, the
    block is not drawn on your own page, and a test looking at it would pass for
    the wrong reason.
    """
    admin_id = make_member("admina", role="admin")
    cases = [
        (superadmin_id, "admina", ["Entregar la plataforma", "Eliminar", "Suspender"]),
        (owner_id, "admina", ["Eliminar", "Suspender"]),
        (admin_id, "usuaria", ["Suspender"]),
        (make_member("usuaria"), "admina", []),
    ]
    forbidden = {"Entregar la plataforma", "Eliminar", "Suspender"}

    for member_id, who, expected in cases:
        sign_in(member_id)
        body = client.get(f"/comunidad/miembro/{who}").get_data(as_text=True)
        assert who.title() in body
        for phrase in expected:
            assert phrase in body, (member_id, phrase)
        for phrase in forbidden - set(expected):
            assert phrase not in body, (member_id, phrase)


def test_the_responsable_cannot_reach_an_admin_from_their_page(
        client, owner_id, make_member, sign_in):
    """The association's chair runs the association. An admin account is the
    platform's, so the whole management block is absent from their page."""
    make_member("admina", role="admin")
    sign_in(owner_id)

    body = client.get("/comunidad/miembro/admina").get_data(as_text=True)

    assert "Gestionar" in body          # it is drawn — an admin is manageable…
    assert "Administrador" not in body.split("Gestionar")[1]   # …but not into or out of admin


def test_the_new_member_form_hides_the_role_choice_from_admins(
        client, superadmin_id, make_member, sign_in):
    sign_in(superadmin_id)
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







# --- erasing somebody who has left traces --------------------------------

def test_erasing_a_member_who_posted_a_picture(app, db, post, superadmin_id, make_member, sign_in):
    """This failed in production with a 500 the first time it was tried.

    `attachments.uploaded_by` is NOT NULL and does not cascade, so with a
    picture in the database SQLite refuses to delete the member — and the admin
    sees "Internal Server Error" with nothing to act on."""
    author = make_member("saliente")
    db.execute("INSERT INTO threads (author_id, title, body_md) VALUES (?, 'Hola', 'Texto')",
               (author,))
    db.execute(
        """INSERT INTO attachments
               (thread_id, stored_name, original_name, content_type, bytes, uploaded_by)
           VALUES (1, 'foto-abc123abc123.jpg', 'foto.jpg', 'image/jpeg', 10, ?)""",
        (author,),
    )
    sign_in(superadmin_id)

    response = post(f"/comunidad/miembros/{author}/eliminar")

    assert response.status_code == 302
    assert db.execute("SELECT 1 FROM members WHERE id = ?", (author,)).fetchone() is None
    # The picture stays with the thread, which survives as "Miembro eliminado" —
    # the same rule the words follow. Deleting the post removes the picture.
    row = db.execute(
        """SELECT m.display_name FROM attachments a JOIN members m ON m.id = a.uploaded_by"""
    ).fetchone()
    assert row["display_name"] == "Miembro eliminado"


def test_every_table_pointing_at_members_is_accounted_for(db):
    """The guard that makes the bug above unrepeatable.

    Read out of the live schema rather than written down twice: any future
    table with a foreign key to members(id) either cascades, or is named in
    members.py's erase lists. Otherwise erasure breaks — and it breaks at the
    moment somebody exercises a right they are entitled to, which is the worst
    possible time to find out."""
    from apps.board.members import (CLEARED_ON_ERASE, CLEARED_ON_ERASE_AFTER,
                                    REASSIGNED_ON_ERASE, REMOVED_ON_ERASE)
    handled = (REASSIGNED_ON_ERASE | CLEARED_ON_ERASE | CLEARED_ON_ERASE_AFTER
               | REMOVED_ON_ERASE)

    tables = [row["name"] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    unhandled = [
        f"{table}.{fk['from']}"
        for table in tables
        for fk in db.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()
        if fk["table"] == "members"
        and (fk["on_delete"] or "").upper() != "CASCADE"
        and (table, fk["from"]) not in handled
    ]
    assert not unhandled, (
        "these point at members(id), do not cascade, and erase_member does not "
        f"touch them, so erasing a member will fail: {unhandled}"
    )


# --- onboarding, now that there is only one account to make --------------

def test_a_new_member_has_no_password_until_they_choose_one(
        client, db, post, superadmin_id, sign_in, monkeypatch):
    """No password is generated and none is shown. Until the invitation is
    used, password_hash is NULL — and a NULL hash cannot be signed in with."""
    monkeypatch.setattr("apps.board.mail.send", lambda to, subject, body: None)
    sign_in(superadmin_id)

    post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "m@example.com",
        "role": "user",
    })

    row = db.execute("SELECT password_hash FROM members WHERE gitea_login = 'maria'"
                     ).fetchone()
    assert row is not None
    assert row["password_hash"] is None


def test_an_address_is_required(client, db, post, superadmin_id, sign_in):
    """It is the only way the invitation reaches anybody, so a member without
    one is a row that can never be used."""
    sign_in(superadmin_id)
    post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "María", "email": "", "role": "user",
    })
    assert db.execute("SELECT 1 FROM members WHERE gitea_login = 'maria'").fetchone() is None


def test_a_duplicate_is_refused_before_anything_is_written(
        client, db, post, superadmin_id, make_member, sign_in, monkeypatch):
    """The error that started this: "ya están en uso" for somebody who was not
    on the list. There is one list now, so the message and the screen agree."""
    monkeypatch.setattr("apps.board.mail.send", lambda to, subject, body: None)
    make_member("maria")
    sign_in(superadmin_id)

    response = post("/comunidad/miembros/nuevo", {
        "login": "maria", "display_name": "Otra", "email": "otra@example.com",
        "role": "user",
    }, follow_redirects=True)

    assert "ya están en uso" in response.get_data(as_text=True)
    assert db.execute(
        "SELECT COUNT(*) AS n FROM members WHERE gitea_login = 'maria'"
    ).fetchone()["n"] == 1


def test_erasing_a_member_lets_the_name_be_used_again(
        client, db, post, superadmin_id, make_member, sign_in, monkeypatch):
    """Deleting used to leave an account behind on the other server, so the
    name stayed taken somewhere invisible. With one store, gone means gone."""
    monkeypatch.setattr("apps.board.mail.send", lambda to, subject, body: None)
    sign_in(superadmin_id)
    post(f"/comunidad/miembros/{make_member('salvador')}/eliminar")

    post("/comunidad/miembros/nuevo", {
        "login": "salvador", "display_name": "Salvador", "email": "s@example.com",
        "role": "user",
    })

    assert db.execute(
        "SELECT 1 FROM members WHERE gitea_login = 'salvador'").fetchone() is not None


# --- the directory and the page -------------------------------------------
#
# Miembros used to be a five-column admin table whose last column held four
# forms, wrapping on top of each other; then it was cards, each carrying a bio,
# some links and a fold-out admin block. It is faces and names now, and
# everything about one person — their profile, the conversation with them, and
# the controls — is on their own page. Both halves are asserted, because "the
# cards are simpler" is only an improvement if nothing was lost on the way.

def test_the_directory_is_faces_and_names(client, db, make_member, sign_in):
    maria = make_member("maria")
    db.execute("UPDATE members SET bio = ? WHERE id = ?",
               ("Vivo en Ottakring.\nSegunda línea.", maria))
    sign_in(make_member("otra"))

    body = client.get("/comunidad/miembros").get_data(as_text=True)

    assert "Maria" in body and "maria" in body
    assert "/comunidad/miembro/maria" in body    # the card is the way in
    # The bio is on their page. A grid whose job is to let you find one person
    # should not be four lines of reading each.
    assert "Vivo en Ottakring." not in body


def test_their_page_is_who_they_are_and_where_you_write(client, db, make_member, sign_in):
    maria = make_member("maria")
    db.execute("UPDATE members SET bio = ?, links = ? WHERE id = ?",
               ("Vivo en Ottakring.", '[{"label": "Mi web", "url": "https://ejemplo.at"}]',
                maria))
    sign_in(make_member("otra"))

    body = client.get("/comunidad/miembro/maria").get_data(as_text=True)

    assert "Vivo en Ottakring." in body
    assert 'href="https://ejemplo.at"' in body and 'rel="nofollow noopener"' in body
    assert f'action="/comunidad/privados/con/{maria}"' in body   # the write box
    assert "No ha publicado una página pública." in body


def test_a_plain_member_sees_no_controls_and_no_addresses(client, make_member, sign_in):
    make_member("maria")
    sign_in(make_member("otra"))

    for url in ("/comunidad/miembros", "/comunidad/miembro/maria"):
        body = client.get(url).get_data(as_text=True)
        for control in ("Gestionar", "Suspender", "Cambiar rol", "Eliminar",
                        "Enviar invitación", "maria@example.com"):
            assert control not in body, f"{control} in {url}"


def test_an_admin_keeps_every_control(client, make_member, sign_in):
    make_member("maria")
    sign_in(make_member("admina", role="admin"))

    body = client.get("/comunidad/miembro/maria").get_data(as_text=True)

    for control in ("Gestionar", "Suspender", "Cambiar rol", "Enviar invitación",
                    "maria@example.com"):
        assert control in body, control
    assert "Eliminar" not in body            # erasure is the owner's alone

    # And none of it leaks back onto the directory, which is where it was.
    # Asserted on the forms rather than the words: the grid's footnote says in
    # so many words that the controls are under «Gestionar» on each page, which
    # is the sentence that sends an admin to the right place.
    grid = client.get("/comunidad/miembros").get_data(as_text=True)
    assert "/estado" not in grid and "/rol" not in grid
    assert "maria@example.com" not in grid


def test_a_photo_shows_inside_even_when_the_page_is_not_published(
        app, client, db, make_member, sign_in):
    """profiles.photo serves only published pages, which is right for the open
    internet and wrong for a directory behind the login."""
    from apps.board import uploads
    maria = make_member("maria")
    stored = "foto-abcdefabcdef.png"
    db.execute("UPDATE members SET photo_name = ?, profile_published = 0 WHERE id = ?",
               (stored, maria))
    with app.app_context():
        uploads.directory().joinpath(stored).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)

    sign_in(make_member("otra"))

    assert stored in client.get("/comunidad/miembros").get_data(as_text=True)
    assert client.get(f"/comunidad/miembro/foto/{stored}").status_code == 200
    # …and it is still not public, which is the whole distinction.
    assert client.get(f"/comunidad/foto/{stored}").status_code == 404


def test_the_members_area_photo_needs_a_session(app, client, db, make_member):
    from apps.board import uploads
    maria = make_member("maria")
    stored = "foto-abcdefabcdef.png"
    db.execute("UPDATE members SET photo_name = ? WHERE id = ?", (stored, maria))
    with app.app_context():
        uploads.directory().joinpath(stored).write_bytes(b"\x89PNG\r\n\x1a\n")

    response = client.get(f"/comunidad/miembro/foto/{stored}")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_a_moderator_gets_no_controls_either(client, make_member, sign_in):
    """A moderator curates content, not people. The block is hidden from them
    and the routes refuse them, which is the half that matters."""
    maria = make_member("maria")
    sign_in(make_member("luisa", role="moderator"))

    body = client.get("/comunidad/miembro/maria").get_data(as_text=True)
    assert "Gestionar" not in body

    assert client.get("/comunidad/miembros/nuevo").status_code == 403
    assert client.post(f"/comunidad/miembros/{maria}/estado",
                       data={"csrf_token": "token-for-tests", "active": "0"}
                       ).status_code == 403


# --- the two chairs, from the routes --------------------------------------

def test_an_admin_cannot_make_promote_or_erase_another_admin(
        client, db, post, make_member, sign_in):
    """Four doors into the same power, checked at each one. A hidden button is a
    courtesy; these are the rules."""
    other = make_member("otra", role="admin")
    sign_in(make_member("admina", role="admin"))

    assert post("/comunidad/miembros/nuevo", {
        "login": "tercera", "display_name": "Tercera",
        "email": "t@example.com", "role": "admin"}).status_code == 403
    assert post(f"/comunidad/miembros/{other}/rol", {"role": "user"}).status_code == 403
    assert post(f"/comunidad/miembros/{other}/estado", {"active": "0"}).status_code == 403
    assert post(f"/comunidad/miembros/{other}/eliminar").status_code == 403
    assert db.execute("SELECT role, active FROM members WHERE id = ?",
                      (other,)).fetchone()["role"] == "admin"


def test_the_responsable_cannot_touch_an_admin_either(
        client, db, post, owner_id, make_member, sign_in):
    """The chair this phase exists to make safe to hand over. A customer
    organisation's president runs their association and does not administer the
    platform that hosts it."""
    admin_id = make_member("admina", role="admin")
    sign_in(owner_id)

    assert post("/comunidad/miembros/nuevo", {
        "login": "nueva", "display_name": "Nueva",
        "email": "n@example.com", "role": "admin"}).status_code == 403
    assert post(f"/comunidad/miembros/{admin_id}/rol", {"role": "user"}).status_code == 403
    assert post(f"/comunidad/miembros/{admin_id}/estado", {"active": "0"}).status_code == 403


def test_nobody_reaches_the_superadmin_through_a_route(
        client, post, superadmin_id, owner_id, make_member, sign_in):
    for actor in (owner_id, make_member("admina", role="admin"), superadmin_id):
        sign_in(actor)
        assert post(f"/comunidad/miembros/{superadmin_id}/estado",
                    {"active": "0"}).status_code == 403, actor
        assert post(f"/comunidad/miembros/{superadmin_id}/rol",
                    {"role": "user"}).status_code == 403, actor
        assert post(f"/comunidad/miembros/{superadmin_id}/eliminar").status_code == 403, actor


def test_the_superadmin_cannot_erase_the_responsable(client, post, superadmin_id,
                                                     owner_id, sign_in):
    """Deliberate, and the less obvious half of the separation: a platform
    administrator quietly removing the president of the organisation they host
    is exactly what two chairs are for."""
    sign_in(superadmin_id)
    assert post(f"/comunidad/miembros/{owner_id}/eliminar").status_code == 403


def test_handing_the_platform_on_is_the_superadmins_alone(
        client, db, post, superadmin_id, owner_id, make_member, sign_in):
    admin_id = make_member("admina", role="admin")

    for actor in (owner_id, admin_id):
        sign_in(actor)
        assert post(f"/comunidad/miembros/{admin_id}/transferir-plataforma"
                    ).status_code == 403

    sign_in(superadmin_id)
    post(f"/comunidad/miembros/{admin_id}/transferir-plataforma")

    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (admin_id,)).fetchone()["role"] == "superadmin"
    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (superadmin_id,)).fetchone()["role"] == "admin"


def test_the_platform_only_goes_to_an_active_admin(client, db, post, superadmin_id,
                                                   make_member, sign_in):
    sign_in(superadmin_id)
    maria = make_member("maria")
    suspended = make_member("dormida", role="admin", active=0)

    post(f"/comunidad/miembros/{maria}/transferir-plataforma")
    post(f"/comunidad/miembros/{suspended}/transferir-plataforma")

    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (superadmin_id,)).fetchone()["role"] == "superadmin"


def test_the_superadmin_can_fill_an_empty_responsable_chair(
        client, db, post, superadmin_id, make_member, sign_in):
    """A fresh install has nobody in it — migration 7 promotes the owner and
    leaves the chair open — so somebody has to be able to seat the first one."""
    maria = make_member("maria")
    sign_in(superadmin_id)

    post(f"/comunidad/miembros/{maria}/transferir")

    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (maria,)).fetchone()["role"] == "owner"
    # And the superadministrator is still the superadministrator: filling a
    # chair you do not sit in costs you nothing.
    assert db.execute("SELECT role FROM members WHERE id = ?",
                      (superadmin_id,)).fetchone()["role"] == "superadmin"
