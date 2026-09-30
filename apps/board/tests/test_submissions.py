"""The moderation queue.

The property every test here circles is the same one: **nothing a member writes
reaches the site repository until a moderator approves it.** A commit to that
repository is publication — Gitea's webhook starts the translate, build and
deploy pipeline — so "queued in git with a flag" would already be live in three
languages. The Gitea layer is therefore replaced by a dictionary, and several
tests assert it was *not* written to, which is the assertion that matters most.

The second property is attribution. Approval commits under the author's name and
email, not the moderator's, because git history is where somebody looks months
later to ask who wrote a post.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from apps.board import gitea

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeRepo:
    """Only what a submission needs: writes, with who made them."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.commits: list[str] = []
        self.authors: list[dict] = []

    def write_file(self, path, data, message, token=None, sha=None, member=None):
        self.files[path] = data
        self.commits.append(message)
        self.authors.append(gitea._identity(member))
        return hashlib.sha1(data).hexdigest()


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(gitea, "write_file", fake.write_file)
    return fake


@pytest.fixture
def maria(make_member):
    return make_member("maria", role="user")


@pytest.fixture
def luisa(make_member):
    return make_member("luisa", role="moderator")


def propose(post, title="Feria en Ottakring", body="Habrá **empanadas**.", **extra):
    data = {"title": title, "body": body}
    data.update(extra)
    return post("/comunidad/publicaciones/nueva", data)


def only_submission(db):
    return db.execute("SELECT * FROM submissions").fetchone()


# --- a member proposes ----------------------------------------------------

def test_a_proposal_is_a_row_and_not_a_commit(repo, db, post, maria, sign_in):
    """The whole point of the table. If this ever commits, the post is on
    vienalatina.com before anybody has read it."""
    sign_in(maria)
    propose(post, description="Una feria", categories=["Comunidad"])

    row = only_submission(db)
    assert (row["state"], row["title"], row["categories"]) == (
        "pending", "Feria en Ottakring", "Comunidad")
    assert repo.files == {}
    assert repo.commits == []


def test_an_empty_proposal_is_refused(db, post, maria, sign_in):
    sign_in(maria)
    assert propose(post, title="  ", body="  ").status_code == 400
    assert db.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 0


def test_only_so_many_may_wait_at_once(db, post, maria, sign_in):
    """A bound on how much unread work one person can put on the moderators
    without talking to them."""
    from apps.board.submissions import PENDING_MAX
    sign_in(maria)
    for n in range(PENDING_MAX):
        propose(post, title=f"Propuesta {n}")

    refused = propose(post, title="Una más")

    assert refused.status_code == 400
    assert db.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == PENDING_MAX


def test_a_moderator_is_sent_to_the_editor_instead(client, luisa, sign_in):
    """Their posts do not queue, so a form that would wait for their own
    approval is a detour with nothing at the end of it."""
    sign_in(luisa)
    response = client.get("/comunidad/publicaciones/nueva")

    assert response.status_code == 302
    assert "/comunidad/contenido/post/nuevo" in response.headers["Location"]


def test_a_moderator_publishing_directly_does_commit(repo, db, post, luisa, sign_in):
    """The other half of the policy: curation applies to members' posts, and a
    moderator is the person doing the curating."""
    sign_in(luisa)
    post("/comunidad/contenido/post/nuevo",
         {"title": "Aviso", "body": "Texto.", "date": "2026-09-25"})

    assert list(repo.files) == ["content/post/2026-09-25-aviso.es.md"]
    assert db.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 0


# --- who may read an unapproved post -------------------------------------

def test_a_stranger_cannot_read_somebody_else_is_draft(client, db, post, maria,
                                                       make_member, sign_in):
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]

    sign_in(make_member("curiosa"))
    assert client.get(f"/comunidad/publicaciones/{submission_id}").status_code == 403


def test_the_author_and_the_moderators_can(client, db, post, maria, luisa, sign_in):
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]

    assert client.get(f"/comunidad/publicaciones/{submission_id}").status_code == 200
    sign_in(luisa)
    assert client.get(f"/comunidad/publicaciones/{submission_id}").status_code == 200


def test_the_queue_is_only_listed_for_moderators(client, db, post, maria,
                                                 luisa, make_member, sign_in):
    sign_in(maria)
    propose(post, title="Feria de Ottakring")

    sign_in(make_member("otro"))
    assert "Feria de Ottakring" not in client.get(
        "/comunidad/publicaciones").get_data(as_text=True)

    sign_in(luisa)
    assert "Feria de Ottakring" in client.get(
        "/comunidad/publicaciones").get_data(as_text=True)


def test_a_member_cannot_approve_their_own(repo, client, db, post, maria, sign_in):
    """The one that would make the whole queue decorative."""
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]

    response = post(f"/comunidad/publicaciones/{submission_id}/aprobar")

    assert response.status_code == 403
    assert repo.files == {}
    assert only_submission(db)["state"] == "pending"


def test_a_member_cannot_reject_either(client, db, post, maria, make_member, sign_in):
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]

    sign_in(make_member("otro"))
    assert post(f"/comunidad/publicaciones/{submission_id}/rechazar",
                {"note": "No me gusta"}).status_code == 403


# --- approval -------------------------------------------------------------

def test_approving_commits_once_under_the_author_is_name(
        repo, db, post, maria, luisa, sign_in):
    """Attribution is the reason `write_file` takes an author at all. A post
    approved by Luisa but written by María has to say María in git."""
    sign_in(maria)
    propose(post, categories=["Comunidad"])
    submission_id = only_submission(db)["id"]

    sign_in(luisa)
    post(f"/comunidad/publicaciones/{submission_id}/aprobar")

    path, = repo.files
    assert path.startswith("content/post/") and path.endswith("-feria-en-ottakring.es.md")
    assert repo.authors == [{"name": "Maria", "email": "maria@example.com"}]
    row = only_submission(db)
    assert (row["state"], row["published_path"], row["reviewed_by"]) == (
        "approved", path, luisa)


def test_the_approved_file_is_what_the_pipeline_expects(repo, db, post, maria,
                                                        luisa, sign_in):
    """The same contract the editor's own tests hold it to: Spanish source, no
    `translated_from`, categories as a list."""
    import yaml
    sign_in(maria)
    propose(post, categories=["Comunidad", "Cultura"], description="Una feria")
    submission_id = only_submission(db)["id"]

    sign_in(luisa)
    post(f"/comunidad/publicaciones/{submission_id}/aprobar")

    path, = repo.files
    text = repo.files[path].decode("utf-8")
    front = yaml.safe_load(text.split("---")[1])
    assert front["lang"] == "es"
    assert front["manual_translation"] is False
    assert front["categories"] == ["Comunidad", "Cultura"]
    assert front["description"] == "Una feria"
    assert "translated_from" not in front
    assert "empanadas" in text


def test_approving_twice_publishes_once(repo, db, post, maria, luisa, sign_in):
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]

    sign_in(luisa)
    post(f"/comunidad/publicaciones/{submission_id}/aprobar")
    post(f"/comunidad/publicaciones/{submission_id}/aprobar")

    assert len(repo.commits) == 1


def test_a_picture_is_private_until_approval_and_committed_after(
        app, repo, client, db, post, maria, luisa, make_member, sign_in):
    """Three things at once, because they are one story: the picture is readable
    by the author and the moderators and nobody else, it is not in the
    repository, and approving puts it there and takes the private copy away."""
    sign_in(maria)
    propose(post, picture=(io.BytesIO(PNG), "feria.png"))
    row = only_submission(db)
    stored = row["photo_name"]
    url = f"/comunidad/publicaciones/foto/{stored}"

    assert (Path(app.config["UPLOAD_DIR"]) / stored).exists()
    assert client.get(url).status_code == 200          # the author
    sign_in(make_member("curiosa"))
    assert client.get(url).status_code == 404          # anybody else
    sign_in(luisa)
    assert client.get(url).status_code == 200          # a moderator
    assert repo.files == {}                            # still nothing published

    post(f"/comunidad/publicaciones/{row['id']}/aprobar")

    assert any(path.startswith("static/uploads/") for path in repo.files)
    assert only_submission(db)["photo_name"] is None
    assert not (Path(app.config["UPLOAD_DIR"]) / stored).exists()
    assert client.get(url).status_code == 404


def test_a_file_that_is_not_an_image_is_refused(db, post, maria, sign_in):
    sign_in(maria)
    response = propose(post, picture=(io.BytesIO(b"<html>nope</html>"), "foto.png"))

    assert response.status_code == 400
    assert db.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 0


# --- rejection ------------------------------------------------------------

def test_rejecting_needs_a_reason_and_shows_it_to_the_author(
        repo, client, db, post, maria, luisa, sign_in):
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]

    sign_in(luisa)
    post(f"/comunidad/publicaciones/{submission_id}/rechazar", {"note": "  "})
    assert only_submission(db)["state"] == "pending"     # no reason, no rejection

    post(f"/comunidad/publicaciones/{submission_id}/rechazar",
         {"note": "Falta decir dónde es."})

    assert only_submission(db)["state"] == "rejected"
    assert repo.files == {}
    sign_in(maria)
    assert "Falta decir dónde es." in client.get(
        "/comunidad/publicaciones").get_data(as_text=True)


def test_editing_a_rejected_one_puts_it_back_in_the_queue(
        client, db, post, maria, luisa, sign_in):
    """Otherwise a rewritten post stays marked "devuelto" and never reaches the
    queue again — the author waits for a review that cannot happen."""
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]
    sign_in(luisa)
    post(f"/comunidad/publicaciones/{submission_id}/rechazar", {"note": "Falta el dónde."})

    sign_in(maria)
    post(f"/comunidad/publicaciones/{submission_id}/editar",
         {"title": "Feria en Ottakring", "body": "En la plaza, el sábado."})

    row = only_submission(db)
    assert (row["state"], row["note"], row["reviewed_by"]) == ("pending", None, None)
    sign_in(luisa)
    assert "Feria en Ottakring" in client.get(
        "/comunidad/publicaciones").get_data(as_text=True)


def test_nobody_else_may_edit_or_withdraw_it(client, db, post, maria,
                                             luisa, make_member, sign_in):
    """A moderator approves or returns a submission. Rewriting somebody's words
    and publishing them under their name is not one of the two — the same line
    the board draws between deleting a post and editing it."""
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]

    for other in (luisa, make_member("otro")):
        sign_in(other)
        assert post(f"/comunidad/publicaciones/{submission_id}/editar",
                    {"title": "Otra cosa", "body": "Otro texto"}).status_code == 403
        assert post(f"/comunidad/publicaciones/{submission_id}/retirar").status_code == 403


def test_withdrawing_takes_the_picture_with_it(app, db, post, maria, sign_in):
    sign_in(maria)
    propose(post, picture=(io.BytesIO(PNG), "feria.png"))
    row = only_submission(db)

    post(f"/comunidad/publicaciones/{row['id']}/retirar")

    assert db.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 0
    assert not (Path(app.config["UPLOAD_DIR"]) / row["photo_name"]).exists()


def test_an_approved_one_cannot_be_edited_or_withdrawn(
        repo, db, post, maria, luisa, sign_in):
    """It is in the repository now. Changing it there is what the editor is
    for, and withdrawing a row would not unpublish anything."""
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]
    sign_in(luisa)
    post(f"/comunidad/publicaciones/{submission_id}/aprobar")

    sign_in(maria)
    post(f"/comunidad/publicaciones/{submission_id}/editar",
         {"title": "Otra cosa", "body": "Otro texto"})
    post(f"/comunidad/publicaciones/{submission_id}/retirar")

    row = only_submission(db)
    assert (row["state"], row["title"]) == ("approved", "Feria en Ottakring")


# --- erasure --------------------------------------------------------------

def test_erasing_the_author_takes_their_submissions_and_photos(
        app, repo, db, post, owner_id, maria, sign_in):
    """A submission is private writing, so it goes the way private messages do.
    What remains of an approved one is the post in the repository, which carries
    their name in a commit that is not ours to rewrite from here."""
    sign_in(maria)
    propose(post, picture=(io.BytesIO(PNG), "feria.png"))
    stored = only_submission(db)["photo_name"]

    sign_in(owner_id)
    post(f"/comunidad/miembros/{maria}/eliminar")

    assert db.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 0
    assert not (Path(app.config["UPLOAD_DIR"]) / stored).exists()


def test_erasing_a_reviewer_keeps_the_submission(db, post, owner_id, maria,
                                                 make_member, sign_in):
    """Who reviewed something is a fact about the submission, not personal data
    of the reviewer, so the name goes and the row stays."""
    reviewer = make_member("luisa", role="moderator")
    sign_in(maria)
    propose(post)
    submission_id = only_submission(db)["id"]
    sign_in(reviewer)
    post(f"/comunidad/publicaciones/{submission_id}/rechazar", {"note": "Falta el dónde."})

    sign_in(owner_id)
    post(f"/comunidad/miembros/{reviewer}/eliminar")

    row = only_submission(db)
    assert (row["state"], row["reviewed_by"], row["note"]) == (
        "rejected", None, "Falta el dónde.")
