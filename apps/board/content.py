"""Writing the public site from inside the members area.

Replaces what Decap CMS does at /admin/, for one reason: Decap has no supported
way to be restyled, so it will always look like a different product bolted onto
the side of this one.

Posts are files in a git repository, so this is a form that commits a file.
There is no working copy, no queue and no new state — `apps/board/gitea.py`
talks to Gitea's contents API, Gitea's push webhook fires Woodpecker, and the
translate → build → deploy pipeline runs exactly as it does for a Decap commit.
Nothing downstream can tell the difference, which is the property that makes
this safe to switch on while Decap is still running.

The filename and frontmatter rules below are not this module's to invent: they
are the contract `scripts/translate.py` reads. `<basename>.es.md` is what
`split_lang()` parses, the date prefix is what stops two posts with one title
colliding on a URL, and the absence of `translated_from` is what marks a file as
something a person wrote.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from datetime import date as date_type
from datetime import datetime

import yaml
from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from . import gitea
from .db import get_db
from .render import to_html
from .security import moderator_required
# One list of accepted formats for the whole app, kept in the module that
# knows what each one looks like on the wire, so the editor and the board
# cannot drift apart about what a picture is.
from .uploads import IMAGE_EXTENSIONS

bp = Blueprint("content", __name__)

# `role` is the floor for writing in this collection, checked by
# _collection_or_404 rather than by a decorator, because the two collections in
# one blueprint do not share an answer: a moderator curates what the public
# reads, and the static pages of the site are not that — they are the site's own
# structure, and changing "Acerca de" is an administrator's job.
COLLECTIONS = {
    "post": {
        "label": "Artículos", "singular": "Artículo", "new_label": "Nuevo artículo",
        "folder": "content/post", "dated": True, "role": "moderator",
    },
    "page": {
        # `new_label` spelled out rather than "Nuevo " + the singular: Spanish
        # gives the article a gender, and the button read "Nuevo página".
        "label": "Páginas", "singular": "Página", "new_label": "Nueva página",
        "folder": "content/page", "dated": False, "role": "admin",
    },
}

CATEGORIES = ["Turismo", "Cultura", "Gastronomía", "Comunidad", "Comercio"]

UPLOAD_FOLDER = "static/uploads"

TITLE_MAX = 140
BODY_MAX = 100_000

# Anything arriving in a URL is checked against this before it reaches a path.
# Without it, `../../` in a filename would let the editor read and overwrite any
# file in the repository — the pipeline and the Caddyfile included.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.es\.md$")

# A byline links to a profile on this site or to nothing. Same shape as a
# username, with the leading slash, so no form can turn an author link into a
# link somewhere else.
AUTHOR_URL = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._-]{0,38}$")


# --- the contract with translate.py --------------------------------------

def slugify(text: str) -> str:
    """Accents folded rather than dropped, so "Gastronomía" is not "gastronom"."""
    folded = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-") or "sin-titulo"


def filename_for(collection: str, title: str, when: date_type | None) -> str:
    slug = slugify(title)
    if COLLECTIONS[collection]["dated"]:
        return f"{when:%Y-%m-%d}-{slug}.es.md"
    return f"{slug}.es.md"


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def build_document(fields: dict, body: str) -> str:
    """Key order matches what the pipeline already writes, so a file edited here
    and a file written by translate.py read the same in a diff."""
    front = yaml.safe_dump(fields, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
    return f"---\n{front}---\n\n{body.strip()}\n"


def frontmatter_for(collection: str, form: dict) -> dict:
    fields = {"title": form["title"]}
    if COLLECTIONS[collection]["dated"]:
        fields["date"] = form["date"]
    fields["lang"] = "es"
    fields["manual_translation"] = form["manual_translation"]
    if collection == "post":
        if form["categories"]:
            fields["categories"] = form["categories"]
        if form["description"]:
            fields["description"] = form["description"]
        if form["image"]:
            fields["image"] = form["image"]
        # Who wrote it, for the page rather than for git. The commit has named
        # the author since the editor was built; the article did not, so a
        # reader could not tell whose it was or click through to them.
        #
        # `author_url` is only ever set when that member has published their
        # page — a byline linking to a 404 is worse than a byline that does not
        # link. translate.py copies both into the German and Portuguese
        # siblings by itself: it carries the whole frontmatter and translates
        # only title and description, and a name is not translated.
        if form.get("author"):
            fields["author"] = form["author"]
        if form.get("author_url"):
            fields["author_url"] = form["author_url"]
    return fields


# --- listing, with the cache doing the work ------------------------------

def _cache_read(path: str, sha: str):
    return get_db().execute(
        "SELECT * FROM content_cache WHERE path = ? AND sha = ?", (path, sha)
    ).fetchone()


def _cache_write(path: str, sha: str, fm: dict) -> None:
    get_db().execute(
        """INSERT INTO content_cache (path, sha, title, date, categories, generated)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
               sha = excluded.sha, title = excluded.title, date = excluded.date,
               categories = excluded.categories, generated = excluded.generated,
               updated_at = datetime('now')""",
        (path, sha, str(fm.get("title", "")), str(fm.get("date", "")),
         ", ".join(fm.get("categories") or []), 1 if fm.get("translated_from") else 0),
    )


def listing(collection: str) -> list[dict]:
    folder = COLLECTIONS[collection]["folder"]
    entries = gitea.list_directory(folder, gitea.content_token())

    items = []
    for entry in entries:
        name = entry.get("name", "")
        if not name.endswith(".es.md"):
            continue  # generated German and Portuguese siblings are not edited here
        path, sha = entry["path"], entry["sha"]

        row = _cache_read(path, sha)
        if row is None:
            text, _ = gitea.read_file(path, gitea.content_token())
            fm, _body = split_frontmatter(text)
            _cache_write(path, sha, fm)
            row = _cache_read(path, sha)

        if row["generated"]:
            continue  # written by the pipeline; editing it would be overwritten
        items.append({
            "name": name, "path": path, "sha": sha,
            "title": row["title"] or name,
            "date": row["date"], "categories": row["categories"],
        })

    items.sort(key=lambda item: (item["date"], item["name"]), reverse=True)
    return items


# --- form handling -------------------------------------------------------

def _collection_or_404(collection: str) -> dict:
    """404 for a collection that does not exist, 403 for one this member may not
    write to. Every route here calls it before doing anything else, which is
    what makes the per-collection rule real rather than a note in a template."""
    if collection not in COLLECTIONS:
        abort(404)
    meta = COLLECTIONS[collection]
    if meta["role"] == "admin" and g.member["role"] not in ("owner", "admin"):
        abort(403)
    return meta


def visible_collections() -> dict:
    """The collections this member may write to, for the tabs. A tab that only
    ever produces a 403 is worse than no tab."""
    return {name: meta for name, meta in COLLECTIONS.items()
            if meta["role"] != "admin" or g.member["role"] in ("owner", "admin")}


def _name_or_404(name: str) -> str:
    if not SAFE_NAME.match(name):
        abort(404)
    return name


def _read_form(collection: str) -> tuple[dict, str, list[str]]:
    """Returns (fields, body, errors). Always returns something renderable, so a
    rejected form comes back with the writer's text still in it."""
    errors = []
    title = request.form.get("title", "").strip()[:TITLE_MAX]
    body = request.form.get("body", "").strip()[:BODY_MAX]

    raw_date = request.form.get("date", "").strip()
    when = None
    if COLLECTIONS[collection]["dated"]:
        try:
            when = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            errors.append("La fecha debe tener el formato AAAA-MM-DD.")

    categories = [c for c in request.form.getlist("categories") if c in CATEGORIES]

    # Carried through the form as hidden fields, the way the date and the image
    # path are: they belong to the document being edited, not to whoever is
    # editing it, so saving a correction must not re-sign the post. Checked on
    # the way back in because a hidden field is only a convention — the URL has
    # to be a path on this site and nothing else.
    author = request.form.get("author", "").strip()[:80]
    author_url = request.form.get("author_url", "").strip()
    if author_url and not AUTHOR_URL.match(author_url):
        author_url = ""
    image = request.form.get("image", "").strip()
    if image and not re.match(r"^/uploads/[A-Za-z0-9._-]+$", image):
        errors.append("La imagen no es válida.")
        image = ""

    if not title:
        errors.append("El título no puede estar vacío.")
    if not body:
        errors.append("El cuerpo no puede estar vacío.")

    fields = {
        "title": title, "date": when, "categories": categories,
        "description": request.form.get("description", "").strip(),
        "image": image,
        "manual_translation": request.form.get("manual_translation") == "on",
        "author": author, "author_url": author_url,
    }
    return fields, body, errors


def commit_picture(data: bytes, filename: str, author=None) -> str:
    """Commit a picture into the site repository and return the path the
    frontmatter uses.

    Deliberately a second commit rather than part of the post's. Gitea's
    contents API writes one file per request, and batching both into a single
    commit means the lower-level git trees API — noticeably more code to get
    wrong, for a benefit nobody sees beyond one fewer pipeline run.

    Takes bytes rather than an upload so that approving a member's submission
    can commit the picture it has had on disk since they proposed it, through
    exactly this code.
    """
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension not in IMAGE_EXTENSIONS:
        raise gitea.GiteaError(
            f"Formato de imagen no admitido. Usa: {', '.join(sorted(IMAGE_EXTENSIONS))}."
        )
    maximum = current_app.config["UPLOAD_MAX_BYTES"]
    if len(data) > maximum:
        raise gitea.GiteaError(
            f"La imagen pesa {len(data) // 1024}KB y el máximo es {maximum // 1024}KB."
        )

    stem = slugify(filename.rsplit(".", 1)[0])[:60]
    # A random suffix rather than a counter: two people uploading "foto.jpg"
    # in the same minute must not race for the same path.
    name = f"{stem}-{secrets.token_hex(3)}.{extension}"
    gitea.write_file(f"{UPLOAD_FOLDER}/{name}", data,
                     f"content: subir {name}", gitea.content_token(),
                     member=author or g.member)
    return f"/uploads/{name}"


def _upload_image() -> str:
    upload = request.files.get("picture")
    if not upload or not upload.filename:
        return ""
    return commit_picture(upload.read(), upload.filename)


def _field(row, name, default=None):
    """One lookup that works on a sqlite3.Row and on a plain dict.

    They disagree about what a missing key is — IndexError for one, KeyError for
    the other — and `approve` hands this a dict built from a row, so both arrive
    here.
    """
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def byline(author) -> dict:
    """The two frontmatter keys naming a writer, from their member row.

    Takes the row (or the plain dict `submissions.approve` builds from one), so
    that a post published through the editor and a proposal approved by somebody
    else produce the same two keys from the same code — and in the second case
    they name the member, not the moderator who pressed the button.
    """
    if author is None:
        return {}
    login = _field(author, "gitea_login", "")
    name = _field(author, "display_name", "") or login
    if not name:
        return {}
    published = _field(author, "profile_published", 0)
    return {
        "author": name,
        "author_url": f"/{login}" if published and login else "",
    }


def publish(collection: str, fields: dict, body: str, author) -> str:
    """Write one post or page into the repository and return its path.

    The single place a commit is made from a finished form, shared by the editor
    and by a moderator approving a submission, so both produce a byte-identical
    file and the same commit shape. `author` is who wrote it, not who pressed
    the button: `gitea.write_file` sends author and committer per commit, so an
    approved submission keeps the member's name in git history.
    """
    fields = dict(fields, **byline(author))
    name = filename_for(collection, fields["title"], fields["date"])
    path = f"{COLLECTIONS[collection]['folder']}/{name}"
    document = build_document(frontmatter_for(collection, fields), body)
    gitea.write_file(path, document.encode("utf-8"),
                     f"content: publicar «{fields['title']}»",
                     gitea.content_token(), member=author)
    return path


# --- routes --------------------------------------------------------------

@bp.route("/contenido")
@bp.route("/contenido/<collection>")
@moderator_required
def index(collection: str = "post"):
    meta = _collection_or_404(collection)
    try:
        items = listing(collection)
    except gitea.GiteaError as exc:
        flash(str(exc), "error")
        items = []
    return render_template("content_list.html", collection=collection, meta=meta,
                           collections=visible_collections(), items=items)


@bp.route("/contenido/<collection>/nuevo", methods=["GET", "POST"])
@moderator_required
def new(collection: str):
    meta = _collection_or_404(collection)
    if request.method == "GET":
        return render_template("content_form.html", collection=collection, meta=meta,
                               categories=CATEGORIES, item=None, fields=None,
                               body="", today=date_type.today().isoformat())

    fields, body, errors = _read_form(collection)
    if errors:
        return _back_to_form(collection, meta, fields, body, errors, None)

    try:
        picture = _upload_image()
        if picture:
            fields["image"] = picture
        publish(collection, fields, body, g.member)
    except gitea.GiteaError as exc:
        return _back_to_form(collection, meta, fields, body, [str(exc)], None)

    flash("Publicado. La traducción tarda un par de minutos.", "ok")
    return redirect(url_for("content.index", collection=collection))


@bp.route("/contenido/<collection>/editar/<name>", methods=["GET", "POST"])
@moderator_required
def edit(collection: str, name: str):
    meta = _collection_or_404(collection)
    name = _name_or_404(name)
    path = f"{meta['folder']}/{name}"

    if request.method == "GET":
        try:
            text, sha = gitea.read_file(path, gitea.content_token())
        except gitea.GiteaError as exc:
            flash(str(exc), "error")
            return redirect(url_for("content.index", collection=collection))

        fm, body = split_frontmatter(text)
        if fm.get("translated_from"):
            flash("Ese archivo lo genera la traducción automática; no se edita aquí.",
                  "error")
            return redirect(url_for("content.index", collection=collection))

        fields = {
            "title": fm.get("title", ""),
            "date": fm.get("date"),
            "categories": fm.get("categories") or [],
            "description": fm.get("description", ""),
            "image": fm.get("image", ""),
            "manual_translation": bool(fm.get("manual_translation")),
            # Carried through the form untouched. Correcting a typo in somebody
            # else's post must not re-sign it with the name of whoever is
            # fixing it — the same rule the board has about editing other
            # people's words, applied to the byline.
            "author": fm.get("author", ""),
            "author_url": fm.get("author_url", ""),
        }
        return render_template("content_form.html", collection=collection, meta=meta,
                               categories=CATEGORIES, item={"name": name, "sha": sha},
                               fields=fields, body=body,
                               today=date_type.today().isoformat())

    fields, body, errors = _read_form(collection)
    sha = request.form.get("sha", "")
    item = {"name": name, "sha": sha}
    if errors:
        return _back_to_form(collection, meta, fields, body, errors, item)

    try:
        picture = _upload_image()
        if picture:
            fields["image"] = picture
        # Deliberately not through publish(): that stamps the byline of whoever
        # is publishing, and an edit keeps the author the file already names.
        document = build_document(frontmatter_for(collection, fields), body)
        gitea.write_file(path, document.encode("utf-8"),
                         f"content: actualizar «{fields['title']}»",
                         gitea.content_token(), sha=sha, member=g.member)
    except gitea.GiteaError as exc:
        return _back_to_form(collection, meta, fields, body, [str(exc)], item)

    flash("Guardado.", "ok")
    return redirect(url_for("content.index", collection=collection))


@bp.route("/contenido/<collection>/eliminar/<name>", methods=["POST"])
@moderator_required
def delete(collection: str, name: str):
    meta = _collection_or_404(collection)
    name = _name_or_404(name)
    path = f"{meta['folder']}/{name}"
    try:
        _text, sha = gitea.read_file(path, gitea.content_token())
        gitea.delete_file(path, sha, f"content: eliminar {name}", gitea.content_token(), member=g.member)
    except gitea.GiteaError as exc:
        flash(str(exc), "error")
        return redirect(url_for("content.index", collection=collection))

    get_db().execute("DELETE FROM content_cache WHERE path = ?", (path,))
    # The German and Portuguese siblings go too, but not from here: the next
    # pipeline run reaps them (translate.py's orphaned_siblings).
    flash("Eliminado. Las traducciones se borran en la siguiente publicación.", "ok")
    return redirect(url_for("content.index", collection=collection))


@bp.route("/contenido/<collection>/vista-previa", methods=["POST"])
@moderator_required
def preview(collection: str):
    """Rendered on the server and returned as a whole page.

    No JavaScript and no fetch: the Content-Security-Policy forbids inline
    script, and a preview is not worth a second way of talking to the server.
    """
    meta = _collection_or_404(collection)
    fields, body, _errors = _read_form(collection)
    sha = request.form.get("sha", "")
    item = {"name": request.form.get("name", ""), "sha": sha} if sha else None
    return render_template("content_form.html", collection=collection, meta=meta,
                           categories=CATEGORIES, item=item, fields=fields, body=body,
                           today=date_type.today().isoformat(),
                           preview_html=to_html(body))


def _back_to_form(collection, meta, fields, body, errors, item):
    for message in errors:
        flash(message, "error")
    return render_template("content_form.html", collection=collection, meta=meta,
                           categories=CATEGORIES, item=item, fields=fields, body=body,
                           today=date_type.today().isoformat()), 400
