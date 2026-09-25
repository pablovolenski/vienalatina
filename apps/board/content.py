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
from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, url_for)

from . import gitea, tokens
from .db import get_db
from .render import to_html
from .security import admin_required

bp = Blueprint("content", __name__)

COLLECTIONS = {
    "post": {
        "label": "Artículos", "singular": "Artículo",
        "folder": "content/post", "dated": True,
    },
    "page": {
        "label": "Páginas", "singular": "Página",
        "folder": "content/page", "dated": False,
    },
}

CATEGORIES = ["Turismo", "Cultura", "Gastronomía", "Comunidad", "Comercio"]

UPLOAD_FOLDER = "static/uploads"
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "avif"}

TITLE_MAX = 140
BODY_MAX = 100_000

# Anything arriving in a URL is checked against this before it reaches a path.
# Without it, `../../` in a filename would let the editor read and overwrite any
# file in the repository — the pipeline and the Caddyfile included.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.es\.md$")


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
    entries = tokens.with_token(gitea.list_directory, folder)

    items = []
    for entry in entries:
        name = entry.get("name", "")
        if not name.endswith(".es.md"):
            continue  # generated German and Portuguese siblings are not edited here
        path, sha = entry["path"], entry["sha"]

        row = _cache_read(path, sha)
        if row is None:
            text, _ = tokens.with_token(gitea.read_file, path)
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
    if collection not in COLLECTIONS:
        abort(404)
    return COLLECTIONS[collection]


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
    }
    return fields, body, errors


def _upload_image() -> str:
    """Commit an uploaded picture and return the path the frontmatter uses.

    Deliberately a second commit rather than part of the post's. Gitea's
    contents API writes one file per request, and batching both into a single
    commit means the lower-level git trees API — noticeably more code to get
    wrong, for a benefit nobody sees beyond one fewer pipeline run.
    """
    upload = request.files.get("picture")
    if not upload or not upload.filename:
        return ""

    extension = upload.filename.rsplit(".", 1)[-1].lower()
    if extension not in IMAGE_EXTENSIONS:
        raise gitea.GiteaError(
            f"Formato de imagen no admitido. Usa: {', '.join(sorted(IMAGE_EXTENSIONS))}."
        )

    data = upload.read()
    maximum = current_app.config["UPLOAD_MAX_BYTES"]
    if len(data) > maximum:
        raise gitea.GiteaError(
            f"La imagen pesa {len(data) // 1024}KB y el máximo es {maximum // 1024}KB."
        )

    stem = slugify(upload.filename.rsplit(".", 1)[0])[:60]
    # A random suffix rather than a counter: two people uploading "foto.jpg"
    # in the same minute must not race for the same path.
    name = f"{stem}-{secrets.token_hex(3)}.{extension}"
    tokens.with_token(gitea.write_file, f"{UPLOAD_FOLDER}/{name}", data,
                      f"content: subir {name}")
    return f"/uploads/{name}"


# --- routes --------------------------------------------------------------

@bp.route("/contenido")
@bp.route("/contenido/<collection>")
@admin_required
def index(collection: str = "post"):
    meta = _collection_or_404(collection)
    try:
        items = listing(collection)
    except tokens.NeedsSignIn:
        return redirect(url_for("auth.login", next=request.path))
    except gitea.GiteaError as exc:
        flash(str(exc), "error")
        items = []
    return render_template("content_list.html", collection=collection, meta=meta,
                           collections=COLLECTIONS, items=items)


@bp.route("/contenido/<collection>/nuevo", methods=["GET", "POST"])
@admin_required
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
        name = filename_for(collection, fields["title"], fields["date"])
        path = f"{meta['folder']}/{name}"
        document = build_document(frontmatter_for(collection, fields), body)
        tokens.with_token(gitea.write_file, path, document.encode("utf-8"),
                          f"content: publicar «{fields['title']}»")
    except tokens.NeedsSignIn:
        return redirect(url_for("auth.login", next=request.path))
    except gitea.GiteaError as exc:
        return _back_to_form(collection, meta, fields, body, [str(exc)], None)

    flash("Publicado. La traducción tarda un par de minutos.", "ok")
    return redirect(url_for("content.index", collection=collection))


@bp.route("/contenido/<collection>/editar/<name>", methods=["GET", "POST"])
@admin_required
def edit(collection: str, name: str):
    meta = _collection_or_404(collection)
    name = _name_or_404(name)
    path = f"{meta['folder']}/{name}"

    if request.method == "GET":
        try:
            text, sha = tokens.with_token(gitea.read_file, path)
        except tokens.NeedsSignIn:
            return redirect(url_for("auth.login", next=request.path))
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
        document = build_document(frontmatter_for(collection, fields), body)
        tokens.with_token(gitea.write_file, path, document.encode("utf-8"),
                          f"content: actualizar «{fields['title']}»", sha=sha)
    except tokens.NeedsSignIn:
        return redirect(url_for("auth.login", next=request.path))
    except gitea.GiteaError as exc:
        return _back_to_form(collection, meta, fields, body, [str(exc)], item)

    flash("Guardado.", "ok")
    return redirect(url_for("content.index", collection=collection))


@bp.route("/contenido/<collection>/eliminar/<name>", methods=["POST"])
@admin_required
def delete(collection: str, name: str):
    meta = _collection_or_404(collection)
    name = _name_or_404(name)
    path = f"{meta['folder']}/{name}"
    try:
        _text, sha = tokens.with_token(gitea.read_file, path)
        tokens.with_token(gitea.delete_file, path, sha, f"content: eliminar {name}")
    except tokens.NeedsSignIn:
        return redirect(url_for("auth.login", next=request.path))
    except gitea.GiteaError as exc:
        flash(str(exc), "error")
        return redirect(url_for("content.index", collection=collection))

    get_db().execute("DELETE FROM content_cache WHERE path = ?", (path,))
    # The German and Portuguese siblings go too, but not from here: the next
    # pipeline run reaps them (translate.py's orphaned_siblings).
    flash("Eliminado. Las traducciones se borran en la siguiente publicación.", "ok")
    return redirect(url_for("content.index", collection=collection))


@bp.route("/contenido/<collection>/vista-previa", methods=["POST"])
@admin_required
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
