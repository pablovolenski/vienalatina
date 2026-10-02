"""The editor.

Gitea is replaced by a dictionary. What matters here is not that HTTP works —
`requests` can be trusted for that — but that the files this produces are the
files `scripts/translate.py` expects, because a post the pipeline cannot parse
publishes in Spanish and is never translated, silently. Several tests therefore
import the real translate.py and run its parser over what the editor wrote.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest

from apps.board import content, gitea

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_translate():
    """Import scripts/translate.py directly — it is a script, not a package."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "vl_translate", REPO_ROOT / "scripts" / "translate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


translate = _load_translate()


class FakeRepo:
    """A repository in a dict, with shas that change when content does."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.commits: list[str] = []
        self.authors: list[dict] = []
        self.reads = 0

    @staticmethod
    def _sha(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    def list_directory(self, path, token=None):
        out = []
        for name, data in self.files.items():
            if name.startswith(path + "/") and "/" not in name[len(path) + 1:]:
                out.append({"name": name.rsplit("/", 1)[1], "path": name,
                            "type": "file", "sha": self._sha(data)})
        return out

    def read_file(self, path, token=None):
        self.reads += 1
        if path not in self.files:
            raise gitea.GiteaError("Ese archivo ya no existe.")
        return self.files[path].decode("utf-8"), self._sha(self.files[path])

    def write_file(self, path, data, message, token=None, sha=None, member=None):
        if sha and self.files.get(path) is not None and self._sha(self.files[path]) != sha:
            raise gitea.StaleFile("Alguien más guardó este archivo mientras lo editabas.")
        self.files[path] = data
        self.commits.append(message)
        self.authors.append(gitea._identity(member))
        return self._sha(data)

    def delete_file(self, path, sha, message, token=None, member=None):
        self.files.pop(path, None)
        self.commits.append(message)


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    for name in ("list_directory", "read_file", "write_file", "delete_file"):
        monkeypatch.setattr(gitea, name, getattr(fake, name))
    return fake


@pytest.fixture
def editor(db, make_member, sign_in):
    """An admin. The editor commits through the server's own content token
    now, so there is nothing to store per person."""
    member_id = make_member("editora", role="admin")
    sign_in(member_id)
    return member_id


def publish(post, title="Sabores del barrio", body="Texto del artículo.", **extra):
    data = {"title": title, "body": body, "date": "2026-09-25"}
    data.update(extra)
    return post("/comunidad/contenido/post/nuevo", data)


# --- the contract with the pipeline --------------------------------------

def test_the_filename_is_one_translate_py_can_parse(repo, editor, post):
    publish(post)
    path, = repo.files
    assert path == "content/post/2026-09-25-sabores-del-barrio.es.md"
    assert translate.split_lang(Path(path)) == ("2026-09-25-sabores-del-barrio", "es")


def test_accents_are_folded_not_dropped():
    assert content.slugify("Gastronomía en Viena") == "gastronomia-en-viena"
    assert content.slugify("¿Qué comer?") == "que-comer"
    assert content.slugify("—") == "sin-titulo"


def test_the_frontmatter_marks_an_authored_source(repo, editor, post):
    publish(post, categories="Gastronomía", description="Un resumen.")
    path, = repo.files
    fm, body = translate.split_frontmatter(repo.files[path].decode())
    assert fm["lang"] == "es"
    assert fm["manual_translation"] is False
    assert fm["categories"] == ["Gastronomía"]
    assert "translated_from" not in fm      # what makes it a source, not output
    assert not translate.is_generated(fm)
    assert body.strip() == "Texto del artículo."


def test_two_posts_with_one_title_land_on_different_days(repo, editor, post):
    publish(post, title="Resumen del mes", date="2026-09-25")
    publish(post, title="Resumen del mes", date="2026-10-25")
    assert sorted(repo.files) == [
        "content/post/2026-09-25-resumen-del-mes.es.md",
        "content/post/2026-10-25-resumen-del-mes.es.md",
    ]


def test_the_freeze_toggle_round_trips(repo, editor, post, client):
    publish(post, manual_translation="on")
    path, = repo.files
    fm, _ = translate.split_frontmatter(repo.files[path].decode())
    assert translate.is_frozen(fm)

    name = path.rsplit("/", 1)[1]
    body = client.get(f"/comunidad/contenido/post/editar/{name}").get_data(as_text=True)
    assert 'name="manual_translation" checked' in body.replace("  ", " ")


def test_pages_are_not_dated(repo, editor, post):
    post("/comunidad/contenido/page/nuevo", {"title": "Acerca", "body": "Quiénes somos."})
    assert list(repo.files) == ["content/page/acerca.es.md"]


def test_the_commit_message_does_not_skip_translation(repo, editor, post):
    publish(post)
    assert repo.commits and all("[skip-translate]" not in m for m in repo.commits)


# --- concurrency and safety ----------------------------------------------

def test_a_stale_sha_is_refused_with_something_readable(repo, editor, post, client):
    publish(post)
    path, = repo.files
    name = path.rsplit("/", 1)[1]
    repo.files[path] = "---\ntitle: Otra cosa\n---\n\nAlguien llegó antes.\n".encode()

    response = post(f"/comunidad/contenido/post/editar/{name}",
                    {"title": "Mi versión", "body": "Texto", "date": "2026-09-25",
                     "sha": "unasha-vieja"})
    assert response.status_code == 400
    assert "Alguien más guardó" in response.get_data(as_text=True)
    assert b"Alguien" in repo.files[path]      # the other edit survived


@pytest.mark.parametrize("name", [
    "..%2F..%2Fetc%2Fpasswd", ".woodpecker.yml", "config.yaml",
    "hola.de.md", "hola.es.md.bak", "a/b.es.md",
])
def test_only_spanish_source_filenames_are_reachable(repo, editor, client, name):
    assert client.get(f"/comunidad/contenido/post/editar/{name}").status_code == 404


def test_a_generated_sibling_is_hidden_and_refused(repo, editor, client):
    repo.files["content/post/hola.es.md"] = (
        b"---\ntitle: Hola\nlang: es\ntranslated_from: de\n---\n\nGenerado.\n")
    assert "Hola" not in client.get("/comunidad/contenido").get_data(as_text=True)

    response = client.get("/comunidad/contenido/post/editar/hola.es.md",
                          follow_redirects=True)
    assert "traducción automática" in response.get_data(as_text=True)


def test_deleting_leaves_the_siblings_to_the_pipeline(repo, editor, post):
    publish(post)
    path, = repo.files
    repo.files["content/post/2026-09-25-sabores-del-barrio.de.md"] = b"generado"

    post(f"/comunidad/contenido/post/eliminar/{path.rsplit('/', 1)[1]}")
    assert path not in repo.files
    # Reaping the German file is translate.py's job, on the next run.
    assert "content/post/2026-09-25-sabores-del-barrio.de.md" in repo.files


# --- who may publish -----------------------------------------------------

CONTENT_ROUTES = [
    "/comunidad/contenido",
    "/comunidad/contenido/post",
    "/comunidad/contenido/post/nuevo",
]


@pytest.mark.parametrize("path", CONTENT_ROUTES)
def test_a_plain_member_cannot_publish(repo, client, make_member, sign_in, path):
    """Posting to the board is not the same permission as publishing to the
    public site, so the editor is admins and the owner only."""
    sign_in(make_member("vecina"))
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", CONTENT_ROUTES)
def test_anonymous_is_sent_to_login(repo, client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/comunidad/login" in response.headers["Location"]


def test_a_commit_is_attributed_to_whoever_wrote_it(repo, client, post, editor, db):
    """One token does the committing, so the author has to be named explicitly
    or git history would credit every post to the same service account and
    there would be nobody to ask about a page a year from now."""
    publish(post)

    assert repo.authors, "nothing was committed"
    assert repo.authors[-1] == {"name": "Editora", "email": "editora@example.com"}


# --- images --------------------------------------------------------------

def _image(name="foto.jpg", size=32):
    return (io.BytesIO(b"x" * size), name)


def test_an_uploaded_image_is_committed_and_referenced(repo, editor, post):
    publish(post, picture=_image())
    uploads = [p for p in repo.files if p.startswith("static/uploads/")]
    assert len(uploads) == 1
    document = repo.files["content/post/2026-09-25-sabores-del-barrio.es.md"].decode()
    fm, _ = translate.split_frontmatter(document)
    assert fm["image"].startswith("/uploads/")
    assert fm["image"].endswith(".jpg")


def test_two_uploads_of_one_filename_do_not_collide(repo, editor, post):
    publish(post, title="Uno", picture=_image())
    publish(post, title="Dos", picture=_image())
    uploads = [p for p in repo.files if p.startswith("static/uploads/")]
    assert len(uploads) == 2


def test_a_script_disguised_as_an_image_is_refused(repo, editor, post):
    response = publish(post, picture=_image("payload.svg"))
    assert response.status_code == 400
    assert "Formato de imagen no admitido" in response.get_data(as_text=True)
    assert not repo.files


def test_an_oversized_image_says_so_instead_of_failing(app, repo, editor, post):
    app.config["UPLOAD_MAX_BYTES"] = 1024
    response = publish(post, picture=_image(size=4096))
    assert response.status_code == 400
    assert "máximo" in response.get_data(as_text=True)


# --- listing, preview, cache ---------------------------------------------

def test_the_listing_reads_each_file_once(repo, editor, client):
    publish_paths = {
        "content/post/2026-09-01-uno.es.md": b"---\ntitle: Uno\ndate: 2026-09-01\nlang: es\n---\n\nA\n",
        "content/post/2026-09-02-dos.es.md": b"---\ntitle: Dos\ndate: 2026-09-02\nlang: es\n---\n\nB\n",
    }
    repo.files.update(publish_paths)

    client.get("/comunidad/contenido")
    after_first = repo.reads
    assert after_first == 2

    body = client.get("/comunidad/contenido").get_data(as_text=True)
    assert repo.reads == after_first      # served from content_cache
    assert "Uno" in body and "Dos" in body


def test_the_listing_refreshes_when_a_file_changes(repo, editor, client):
    repo.files["content/post/2026-09-01-uno.es.md"] = (
        b"---\ntitle: Uno\ndate: 2026-09-01\nlang: es\n---\n\nA\n")
    client.get("/comunidad/contenido")
    repo.files["content/post/2026-09-01-uno.es.md"] = (
        b"---\ntitle: Uno corregido\ndate: 2026-09-01\nlang: es\n---\n\nA\n")
    body = client.get("/comunidad/contenido").get_data(as_text=True)
    assert "Uno corregido" in body


def test_the_preview_renders_markdown_and_escapes_html(repo, editor, post):
    response = post("/comunidad/contenido/post/vista-previa", {
        "title": "Prueba", "date": "2026-09-25",
        "body": "**fuerte** y <script>alert(1)</script>",
    })
    page = response.get_data(as_text=True)
    assert "<strong>fuerte</strong>" in page
    assert "<script>alert(1)</script>" not in page
    assert not repo.files      # a preview must not publish anything


# --- who may write which collection --------------------------------------
#
# One blueprint, two answers. A moderator curates what the public reads, so
# posts are theirs. The static pages are the site's own structure — "Acerca de",
# contact — and changing those is an administrator's job. The check lives in
# _collection_or_404, which every route here calls before doing anything.

def test_a_moderator_may_publish_a_post(repo, db, post, make_member, sign_in):
    sign_in(make_member("luisa", role="moderator"))

    response = post("/comunidad/contenido/post/nuevo",
                    {"title": "Aviso", "body": "Texto.", "date": "2026-09-25"})

    assert response.status_code == 302
    assert list(repo.files) == ["content/post/2026-09-25-aviso.es.md"]


def test_a_moderator_may_not_touch_the_static_pages(repo, client, post,
                                                    make_member, sign_in):
    sign_in(make_member("luisa", role="moderator"))

    assert client.get("/comunidad/contenido/page").status_code == 403
    assert client.get("/comunidad/contenido/page/nuevo").status_code == 403
    assert post("/comunidad/contenido/page/nuevo",
                {"title": "Acerca de", "body": "Texto."}).status_code == 403
    assert repo.files == {}


def test_a_member_may_not_use_the_editor_at_all(repo, client, post,
                                                make_member, sign_in):
    """Their route to the public site is a proposal, which waits for somebody."""
    sign_in(make_member("maria"))

    for path in ("/comunidad/contenido", "/comunidad/contenido/post/nuevo",
                 "/comunidad/contenido/page/nuevo"):
        assert client.get(path).status_code == 403, path
    assert repo.files == {}


def test_the_tabs_only_offer_what_this_member_may_open(repo, client, make_member, sign_in):
    """A tab that always produces a 403 is worse than no tab."""
    sign_in(make_member("luisa", role="moderator"))
    assert "Páginas" not in client.get("/comunidad/contenido").get_data(as_text=True)

    sign_in(make_member("admina", role="admin"))
    assert "Páginas" in client.get("/comunidad/contenido").get_data(as_text=True)


# --- when the git server refuses the token -------------------------------
#
# This is the shape of a real outage, and it went undiagnosed for two phases.
# `_content_request` answered 401/403 with PermissionError, for a caller
# (tokens.py) that had been deleted — so the editor 500ed and approving a
# submission blamed a missing photograph. Both are asserted here, from the
# outside, because both were invisible from the inside.

class RefusingRepo:
    """Gitea with a token that has no repository scope: 403 to everything."""

    class Response:
        status_code = 403

        @staticmethod
        def json():
            return {}

    def request(self, *args, **kwargs):
        return self.Response()


@pytest.fixture
def refused(monkeypatch):
    monkeypatch.setattr(gitea, "requests", RefusingRepo())


def test_the_editor_says_what_is_wrong_instead_of_500ing(
        refused, client, make_member, sign_in):
    sign_in(make_member("admina", role="admin"))

    for path in ("/comunidad/contenido", "/comunidad/contenido/page"):
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        assert "CONTENT_TOKEN" in body and "repository" in body, path


def test_a_refused_write_does_not_escape_as_a_permission_error(
        refused, client, post, make_member, sign_in):
    """PermissionError is an OSError, which is how it ended up being reported as
    a missing file somewhere else entirely. It must not leave gitea.py."""
    sign_in(make_member("admina", role="admin"))

    response = post("/comunidad/contenido/post/nuevo",
                    {"title": "Aviso", "body": "Texto.", "date": "2026-09-25"})

    assert response.status_code == 400          # back to the form, not a 500
    assert "rechazó el token" in response.get_data(as_text=True)


def test_an_unexpected_error_renders_the_site_is_own_page(app, client, make_member,
                                                          sign_in, monkeypatch):
    """Flask's default 500 page is in English, has no way back, and says nothing.
    Ours says something and is logged with the path.

    PROPAGATE_EXCEPTIONS off on purpose: the fixture sets TESTING, which re-raises
    instead of running the handler, and the handler is the subject here."""
    app.config["PROPAGATE_EXCEPTIONS"] = False
    sign_in(make_member("maria"))

    def explode():
        raise RuntimeError("something nobody anticipated")
    monkeypatch.setattr("apps.board.board.get_db", explode)

    response = client.get("/comunidad/muro")

    assert response.status_code == 500
    body = response.get_data(as_text=True)
    assert "Algo falló de nuestro lado" in body
    assert "Viena Latina" in body               # the site's chrome, not Flask's


# --- the byline ----------------------------------------------------------
#
# The commit has named the author since the editor was built; the page did not,
# so a reader could not tell whose article they were reading. These check the
# three places it can go wrong: publishing, approving somebody else's proposal,
# and editing — where re-signing the post with the editor's name would be worse
# than having no byline at all.

def test_a_published_post_names_its_author(repo, db, post, make_member, sign_in):
    import yaml
    author = make_member("luisa", role="moderator")
    db.execute("UPDATE members SET display_name = ?, profile_published = 1 WHERE id = ?",
               ("Luisa Fernández", author))
    sign_in(author)

    publish(post)

    path, = repo.files
    front = yaml.safe_load(repo.files[path].decode().split("---")[1])
    assert front["author"] == "Luisa Fernández"
    assert front["author_url"] == "/luisa"


def test_without_a_published_page_the_byline_does_not_link(
        repo, db, post, make_member, sign_in):
    import yaml
    author = make_member("luisa", role="moderator")
    db.execute("UPDATE members SET profile_published = 0 WHERE id = ?", (author,))
    sign_in(author)

    publish(post)

    path, = repo.files
    front = yaml.safe_load(repo.files[path].decode().split("---")[1])
    assert front["author"] == "Luisa"
    assert "author_url" not in front       # a byline linking to a 404 is worse


def test_editing_a_post_keeps_the_author_it_already_had(
        repo, editor, post, client):
    """The one that matters. An admin fixing a typo in somebody's article must
    not end up signing it — the same rule the board has about editing other
    people's words."""
    import yaml
    repo.files["content/post/2026-09-25-de-otro.es.md"] = (
        "---\ntitle: De otro\ndate: 2026-09-25\nlang: es\n"
        "manual_translation: false\nauthor: Salvador Dalí\nauthor_url: /salvador\n"
        "---\n\nTexto.\n").encode("utf-8")

    page = client.get("/comunidad/contenido/post/editar/2026-09-25-de-otro.es.md")
    body = page.get_data(as_text=True)
    assert 'name="author" value="Salvador Dalí"' in body
    assert 'name="author_url" value="/salvador"' in body

    post("/comunidad/contenido/post/editar/2026-09-25-de-otro.es.md",
         {"title": "De otro", "body": "Texto corregido.", "date": "2026-09-25",
          "sha": repo._sha(repo.files["content/post/2026-09-25-de-otro.es.md"]),
          "author": "Salvador Dalí", "author_url": "/salvador"})

    front = yaml.safe_load(
        repo.files["content/post/2026-09-25-de-otro.es.md"].decode().split("---")[1])
    assert front["author"] == "Salvador Dalí"
    assert front["author_url"] == "/salvador"


def test_an_author_url_cannot_point_off_the_site(repo, editor, post):
    """It arrives as a hidden field, which is a convention and not a promise."""
    import yaml
    post("/comunidad/contenido/post/nuevo",
         {"title": "Aviso", "body": "Texto.", "date": "2026-09-25",
          "author": "Alguien", "author_url": "https://ejemplo.invalid/premio"})

    path, = repo.files
    front = yaml.safe_load(repo.files[path].decode().split("---")[1])
    assert "author_url" not in front


def test_a_page_carries_no_byline(repo, editor, post):
    """Acerca de and Contacto are the site speaking, not a person."""
    import yaml
    post("/comunidad/contenido/page/nuevo", {"title": "Acerca de", "body": "Texto."})

    path, = repo.files
    front = yaml.safe_load(repo.files[path].decode().split("---")[1])
    assert "author" not in front
