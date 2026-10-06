"""The three screens the superadministrator gets, and the identity half.

Grouped in one file because they are one phase and share one question: who may
open this, and does it tell the truth about the running system.
"""

from __future__ import annotations

import json

import pytest

from apps.board import activity, identity

SCREENS = {
    "/comunidad/gestion/identidad": "superadmin",
    "/comunidad/gestion/estado": "superadmin",
    "/comunidad/gestion/registro": "superadmin",
    "/comunidad/gestion/medios": "admin",
}


@pytest.fixture
def boss(superadmin_id, sign_in):
    sign_in(superadmin_id)
    return superadmin_id


@pytest.mark.parametrize("url", sorted(SCREENS))
def test_every_screen_opens_for_the_superadmin(client, boss, url, repo):
    assert client.get(url).status_code == 200


@pytest.mark.parametrize("url,needs", sorted(SCREENS.items()))
def test_each_screen_refuses_the_roles_below_it(client, make_member, sign_in,
                                                url, needs, repo):
    """Medios is open to admins — they are the people who upload, and the worst
    an admin can do there is remove a picture from a post they could already
    have edited. The other three are the platform's."""
    # Everything above the rank passes too, which is what `is_at_least` means.
    allowed = {"superadmin": set(), "admin": {"admin", "owner"}}[needs]
    for role in ("user", "moderator", "admin", "owner"):
        sign_in(make_member(f"persona-{role}", role=role))
        expected = 200 if role in allowed else 403
        assert client.get(url).status_code == expected, (url, role)


# --- identity -------------------------------------------------------------

def test_the_identity_round_trips_all_three_languages(client, db, post, boss, repo):
    post("/comunidad/gestion/identidad", {
        "name-es": "Viena Latina", "tagline-es": "La comunidad en Viena.",
        "name-de": "Wien Latina", "tagline-de": "Die Community in Wien.",
        "tagline-pt-br": "A comunidade em Viena.",
        "social-instagram": "https://instagram.com/vienalatina",
        "email": "hola@vienalatina.com", "city": "Wien",
        "footer_text": "Asociación Viena Latina",
    })

    body = client.get("/comunidad/gestion/identidad").get_data(as_text=True)
    for value in ("Wien Latina", "Die Community in Wien.",
                  "A comunidade em Viena.", "https://instagram.com/vienalatina",
                  "hola@vienalatina.com", "Asociación Viena Latina"):
        assert value in body, value


def test_an_untouched_language_is_not_written_at_all(client, db, post, boss, repo):
    """A file repeating config.yaml's own values back at it would make the
    fallback meaningless and freeze today's defaults for good."""
    post("/comunidad/gestion/identidad", {"tagline-es": "Sólo esto."})

    names = json.loads(db.execute("SELECT names_json FROM site").fetchone()[0])
    assert names == {"es": {"name": "", "tagline": "Sólo esto."}}
    yaml = repo.files["data/site.yaml"].decode("utf-8")
    assert "de:" not in yaml and "pt-br:" not in yaml


def test_a_social_link_that_is_not_https_is_refused(client, db, post, boss, repo):
    """These render in the footer of every public page. `javascript:` in an href
    is a script running on vienalatina.com."""
    response = post("/comunidad/gestion/identidad",
                    {"social-instagram": "javascript:alert(1)"},
                    follow_redirects=True)

    assert "tiene que empezar por https" in response.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) AS n FROM site").fetchone()["n"] == 0


def test_a_tagline_is_flattened_to_one_line(client, db, post, boss, repo):
    """A newline inside og:description is a share card that renders raggedly in
    some readers and truncated in others."""
    post("/comunidad/gestion/identidad", {"tagline-es": "Una línea.\n\nY otra."})

    names = json.loads(db.execute("SELECT names_json FROM site").fetchone()[0])
    assert names["es"]["tagline"] == "Una línea. Y otra."


def test_the_yaml_is_what_hugo_reads(client, post, boss, repo):
    post("/comunidad/gestion/identidad", {
        "name-es": "Viena Latina", "tagline-es": "La comunidad.",
        "social-instagram": "https://instagram.com/vl",
        "social-bluesky": "https://bsky.app/profile/vl",
        "email": "hola@vienalatina.com", "city": "Wien",
    })

    yaml = repo.files["data/site.yaml"].decode("utf-8")
    assert '    name: "Viena Latina"' in yaml
    assert '    tagline: "La comunidad."' in yaml
    # sameAs is a flat list of URLs: the names of the networks appear nowhere in
    # the schema, only the addresses.
    assert 'sameAs:\n  - "https://instagram.com/vl"' in yaml
    assert 'email: "hola@vienalatina.com"' in yaml


def test_a_quote_in_a_name_cannot_break_the_file(client, post, boss, repo):
    post("/comunidad/gestion/identidad", {"name-es": 'Viena "La" Latina\\'})

    yaml = repo.files["data/site.yaml"].decode("utf-8")
    assert r'name: "Viena \"La\" Latina\\"' in yaml


# --- the activity log -----------------------------------------------------

def test_the_acts_that_change_power_are_recorded(client, db, post, boss, make_member):
    maria = make_member("maria")
    post(f"/comunidad/miembros/{maria}/rol", {"role": "moderator"})
    post(f"/comunidad/miembros/{maria}/estado", {"active": "0"})

    rows = db.execute(
        "SELECT action, object, detail FROM activity ORDER BY id").fetchall()

    assert [(r["action"], r["object"]) for r in rows] == [
        ("role.changed", "Maria"), ("member.suspended", "Maria")]
    assert "Miembro → Moderador" in rows[0]["detail"]


def test_the_log_survives_the_person_who_wrote_it(client, db, post, boss, make_member):
    """The row stays and the name goes. That a role was changed is the
    association's record; whose name was on it is the erased person's data."""
    admin_id = make_member("admina", role="admin")
    maria = make_member("maria")
    sign_in_as = client.session_transaction
    with sign_in_as() as session:
        session["member_id"] = admin_id
        session["csrf"] = "token-for-tests"
    post(f"/comunidad/miembros/{maria}/rol", {"role": "moderator"})

    with sign_in_as() as session:
        session["member_id"] = boss
    post(f"/comunidad/miembros/{admin_id}/eliminar")

    rows = db.execute("SELECT actor_id, action FROM activity "
                      "WHERE action = 'role.changed'").fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_id"] is None


def test_a_broken_log_does_not_break_the_thing_it_records(app, db, boss,
                                                          make_member, monkeypatch):
    """An administrator who could not be appointed because the note about it
    could not be saved is a worse outcome than a gap in the notes."""
    with app.test_request_context():
        monkeypatch.setattr(activity, "get_db",
                            lambda: (_ for _ in ()).throw(RuntimeError("no")))
        activity.log("role.changed", "Maria")      # must not raise


def test_the_registro_reads_as_sentences(client, db, post, boss, make_member):
    maria = make_member("maria")
    post(f"/comunidad/miembros/{maria}/rol", {"role": "moderator"})

    body = client.get("/comunidad/gestion/registro").get_data(as_text=True)

    assert "cambió el rol de Maria" in body


# --- the media library ----------------------------------------------------

def test_an_orphan_is_named_as_one_and_a_used_picture_is_not(client, db, boss, repo):
    repo.files["static/uploads/usada-aaaaaa.jpg"] = b"\xff\xd8\xff"
    repo.files["static/uploads/huerfana-bbbbbb.jpg"] = b"\xff\xd8\xff"
    db.execute("""INSERT INTO content_cache (path, sha, title, date, image)
                  VALUES ('content/post/x.es.md', 'a', 'Un artículo', '2026-01-01',
                          '/uploads/usada-aaaaaa.jpg')""")

    body = client.get("/comunidad/gestion/medios").get_data(as_text=True)

    assert "1 no la usa nadie" in body
    assert "Un artículo" in body
    # And the orphan is first, because it is the only row anybody has to act on.
    assert body.index("huerfana-bbbbbb") < body.index("usada-aaaaaa")


def test_a_picture_in_use_cannot_be_deleted(client, db, post, boss, repo):
    repo.files["static/uploads/usada-aaaaaa.jpg"] = b"\xff\xd8\xff"
    db.execute("""INSERT INTO content_cache (path, sha, title, date, image)
                  VALUES ('content/post/x.es.md', 'a', 'Un artículo', '2026-01-01',
                          '/uploads/usada-aaaaaa.jpg')""")

    response = post("/comunidad/gestion/medios/usada-aaaaaa.jpg/eliminar",
                    follow_redirects=True)

    assert "está en uso" in response.get_data(as_text=True)
    assert "static/uploads/usada-aaaaaa.jpg" in repo.files


def test_an_orphan_is_deleted_and_logged(client, db, post, boss, repo):
    repo.files["static/uploads/huerfana-bbbbbb.jpg"] = b"\xff\xd8\xff"

    post("/comunidad/gestion/medios/huerfana-bbbbbb.jpg/eliminar",
         {"sha": repo._sha(b"\xff\xd8\xff")})

    assert "static/uploads/huerfana-bbbbbb.jpg" not in repo.files
    assert db.execute("SELECT action, object FROM activity").fetchone()["object"] == \
        "huerfana-bbbbbb.jpg"


def test_the_editor_records_which_picture_a_post_uses(client, repo, db, post,
                                                      superadmin_id, sign_in):
    """Which is what makes "does anything use this?" a query rather than a
    re-read of every post's frontmatter from the git server.

    The cache is filled by the listing rather than by the write — it is keyed on
    the sha the git server reports — so opening Contenido is the step that puts
    the row there."""
    sign_in(superadmin_id)
    post("/comunidad/contenido/post/nuevo",
         {"title": "Con foto", "body": "Texto.", "date": "2026-10-03",
          "image": "/uploads/foto-abcdef.jpg"})
    client.get("/comunidad/contenido/post")

    row = db.execute("SELECT image FROM content_cache").fetchone()
    assert row["image"] == "/uploads/foto-abcdef.jpg"


# --- the status screen ----------------------------------------------------

def test_the_status_screen_says_when_the_token_cannot_write(client, boss, monkeypatch):
    """"Present" was never the problem. A token with the wrong scope is
    non-empty, looks fine, and cannot write a single file — which is exactly the
    configuration that broke publishing for three phases."""
    from apps.board import gitea, status
    monkeypatch.setattr(status.gitea, "list_directory",
                        lambda *a, **k: (_ for _ in ()).throw(
                            gitea.GiteaError("403: sin permiso de repositorio")))

    body = client.get("/comunidad/gestion/estado").get_data(as_text=True)

    assert "no puede leer el repositorio" in body
    assert "sin permiso de repositorio" in body


def test_the_status_screen_counts_what_is_there(client, db, boss, make_member, repo):
    make_member("maria")
    make_member("luisa", role="moderator")

    body = client.get("/comunidad/gestion/estado").get_data(as_text=True)

    assert "3 miembros" in body          # the superadministrator and the two
    assert "2 con algún cargo" in body   # superadministrator + moderator


# --- the status screen tells the truth about the container it is in --------

def test_the_backup_check_reads_a_path_the_container_can_see(tmp_path, monkeypatch):
    """The bug this file gained a section for.

    `BACKUP_DIR` was `/srv/board/backups` — a **host** path, read from inside a
    container that mounts only `./data`. On the real server it reported "no hay
    carpeta de copias" twenty seconds after `deploy-board.sh` had written one.
    The compose file now mounts the folder read-only at `/backups`, and this
    asserts the constant points at the mount rather than at the host.
    """
    from apps.board import status
    assert status.BACKUP_DIR == "/backups", (
        "BACKUP_DIR must be the path inside the container, not the host's — "
        "see infra/board/docker-compose.yml")


def test_a_backup_is_found_reported_and_dated(app, tmp_path, monkeypatch):
    from apps.board import status
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "board-20261006-030000.db.gz").write_bytes(b"x" * 4096)
    monkeypatch.setattr(status, "BACKUP_DIR", str(folder))

    with app.app_context():
        check = status._backups()

    assert check["state"] == "ok"
    assert "board-20261006-030000.db.gz" in check["detail"]
    assert "4 KB" in check["detail"]


def test_an_empty_backup_folder_is_the_real_alarm(app, tmp_path, monkeypatch):
    """The folder existing and holding nothing is the state worth shouting
    about — unlike the folder simply not being visible, which was mine."""
    from apps.board import status
    folder = tmp_path / "backups"
    folder.mkdir()
    monkeypatch.setattr(status, "BACKUP_DIR", str(folder))

    with app.app_context():
        assert status._backups()["state"] == "bad"


def test_a_setting_with_a_fallback_is_not_reported_as_missing(app, monkeypatch):
    """`CONTENT_REPO` is unset on the real server and publishing works, because
    app.py defaults it. Reporting that as a problem is how a panel full of
    orange teaches somebody to ignore orange.

    The ones with no fallback are set here so that the only thing left absent is
    the defaulted one — which is the state the real server is in."""
    from apps.board import status
    for name, _why, key in status.EXPECTED:
        if key is None:
            monkeypatch.setenv(name, "puesto")
    monkeypatch.delenv("CONTENT_REPO", raising=False)

    with app.test_request_context():
        check = status._settings()

    assert check["state"] == "ok"
    assert "CONTENT_REPO" in check["detail"]
    assert "por defecto" in check["detail"]


def test_a_setting_with_no_fallback_still_warns(app, monkeypatch):
    monkeypatch.delenv("MAIL_HOST", raising=False)
    from apps.board import status
    with app.test_request_context():
        check = status._settings()

    assert check["state"] == "warn"
    assert "MAIL_HOST" in check["detail"]


def test_every_expected_setting_names_a_real_config_key(app):
    """The third column is read back out of `current_app.config`, so a typo
    would render «usando None» on the page rather than failing anywhere."""
    from apps.board import status
    with app.app_context():
        for name, _why, key in status.EXPECTED:
            if key:
                assert key in app.config, (name, key)
                assert app.config[key], (name, key)
