"""Biblioteca de medios — every picture the public site carries.

WordPress's Media Library, and the reason it earns a place here is narrower than
WordPress's: **nothing has ever been able to see an orphan.** A picture is
committed to `static/uploads/` when a post is written; delete the post and the
picture stays in the repository, deployed, referenced by nothing, for good.
There has been no way to list them, no way to notice, and no way to remove one
short of a commit by hand.

So this is a listing with one question answered per row — which posts use this —
and one action: delete the ones nobody uses.

**Open to administrators**, unlike the rest of Gestión's new screens. They are
the people who upload, so they are the people who should be able to see what
they have uploaded; and the worst an administrator can do here is remove a
picture from a post they could already have edited.
"""

from __future__ import annotations

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)

from . import activity, content, gitea
from .db import get_db
from .security import admin_required

bp = Blueprint("media", __name__)

FOLDER = content.UPLOAD_FOLDER          # static/uploads


def _users() -> dict[str, list[str]]:
    """Which posts reference which picture, from the cache the listing fills.

    From `content_cache.image` rather than by reading every post's frontmatter:
    the whole point of that cache is that a page like this one costs one
    directory request to the git server rather than one request per post.

    A path appears in frontmatter as `/uploads/foto-abc.jpg`; the repository
    calls it `static/uploads/foto-abc.jpg`. The basename is what both agree on.
    """
    rows = get_db().execute(
        "SELECT title, image FROM content_cache WHERE image IS NOT NULL AND image != ''"
    ).fetchall()
    used: dict[str, list[str]] = {}
    for row in rows:
        used.setdefault(row["image"].rsplit("/", 1)[-1], []).append(row["title"])
    return used


@bp.route("/gestion/medios")
@admin_required
def index():
    used = _users()
    try:
        files = gitea.list_directory(FOLDER, gitea.content_token())
    except (gitea.GiteaError, OSError) as exc:
        flash(f"No se pudo leer la carpeta de imágenes: {exc}", "error")
        files = []

    items = []
    for entry in files:
        name = entry.get("name", "")
        items.append({
            "name": name,
            "url": f"/uploads/{name}",
            "sha": entry.get("sha", ""),
            "size": (entry.get("size") or 0) // 1024,
            "posts": used.get(name, []),
        })
    # Orphans first: they are the only rows anybody has to act on, and a
    # listing sorted by name buries them among the ones doing their job.
    items.sort(key=lambda item: (bool(item["posts"]), item["name"]))
    return render_template("media.html", items=items,
                           orphans=sum(1 for i in items if not i["posts"]))


@bp.route("/gestion/medios/<name>/eliminar", methods=["POST"])
@admin_required
def delete(name: str):
    """Remove a picture nobody uses.

    **Refuses one that is in use**, from the handler and not only by hiding the
    button: deleting a referenced picture leaves a broken image on a published
    page, which is worse than an orphan nobody sees.
    """
    if "/" in name or name.startswith("."):
        abort(404)
    if _users().get(name):
        flash("Esa imagen está en uso. Quítala del artículo primero.", "error")
        return redirect(url_for("media.index"))

    sha = (request.form.get("sha") or "").strip()
    try:
        gitea.delete_file(f"{FOLDER}/{name}", sha, f"medios: retirar {name}",
                          gitea.content_token())
    except (gitea.GiteaError, OSError) as exc:
        flash(f"No se pudo borrar: {exc}", "error")
        return redirect(url_for("media.index"))

    activity.log("media.deleted", name)
    flash(f"{name} borrada. El sitio se reconstruye en un par de minutos.", "ok")
    return redirect(url_for("media.index"))
