"""Public pages at vienalatina.com/su-nombre.

Two blueprints, because these live at opposite ends of the site: the page
itself sits at the top level of the public site, and the form that edits it
sits inside the members area behind the login.

**The top level is shared with the whole static site**, which is what makes
this the riskiest thing in the app. The protection is in the Caddyfile and it
is worth understanding from here: a request only reaches this module when the
path is a single segment *and* Hugo has built nothing there. The static site
always wins, decided by asking the filesystem rather than by a list of reserved
paths that would be wrong the first time somebody adds a page to the site.

So a member whose name collides with `/de/` does not shadow anything — they get
a page nobody can reach. That is why RESERVED exists as well: not to protect
the site, which the routing already does, but to stop somebody choosing a name
that will quietly never work.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, send_from_directory, url_for)

from . import uploads
from .db import get_db
from .render import to_html
from .security import login_required

# The page itself, at the root of the public site.
public_bp = Blueprint("profiles_public", __name__)
# Editing it, inside /comunidad/.
bp = Blueprint("profiles", __name__)

BIO_MAX = 2_000
LINKS_MAX = 8

# Names that Hugo already uses, or plausibly will. The routing makes a
# collision harmless rather than dangerous — Hugo wins and the profile is
# simply unreachable — so this exists to stop somebody discovering months later
# that their page has never loaded for anybody.
#
# `categories` and `tags` stay on the list although the taxonomies are gone: the
# site served those paths for a year, and a URL somebody may still have in a
# bookmark or a search index should not quietly become a member's profile.
RESERVED = {
    "de", "pt-br", "es", "en",                    # language trees
    "page", "post", "posts", "categories", "tags", "category", "tag",
    "admin", "uploads", "comunidad", "static", "assets",
    "css", "js", "img", "images", "fonts", "media",
    "index", "sitemap", "robots", "feed", "rss", "atom", "404", "llms",
    "api", "search", "login", "logout", "register", "signup",
}


def name_is_available(login: str) -> bool:
    return login.lower() not in RESERVED


def clean_links(raw: str) -> list[dict]:
    """One `label|url` per line, keeping only what is safe to render.

    The scheme check is the point. An `href` beginning `javascript:` is a
    script running on vienalatina.com, published by whoever typed it, so
    anything that is not http or https is dropped rather than shown.
    """
    links = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        label, _, url = line.partition("|")
        label, url = label.strip()[:60], url.strip()
        if not url:
            label, url = "", label
        if urlparse(url).scheme not in ("http", "https"):
            continue
        links.append({"label": label or url, "url": url[:500]})
        if len(links) >= LINKS_MAX:
            break
    return links


def _published(login: str):
    return get_db().execute(
        """SELECT * FROM members
            WHERE gitea_login = ? COLLATE NOCASE
              AND profile_published = 1 AND active = 1
              AND role != 'tombstone'""",
        (login,),
    ).fetchone()


# --- the public page ------------------------------------------------------

@public_bp.route("/<name>/")
def show_with_slash(name: str):
    """One canonical address per profile.

    Both spellings have always worked, but `url_for` picked whichever was
    registered last, so links inside the members area pointed at `/maria/`
    while everything written by hand said `/maria`. Two URLs for one page is a
    small thing until a search engine indexes both. This one sends people to
    the other and nothing renders here.
    """
    return redirect(url_for("profiles_public.show", name=name), code=301)


@public_bp.route("/<name>")
def show(name: str):
    """404 for anything that is not a published profile.

    Caddy turns that 404 into the site's own 404 page, so an unknown name
    reaching here looks to a visitor exactly like any other missing page —
    which also means an unpublished profile is indistinguishable from a member
    who does not exist.
    """
    member = _published(name)
    if member is None:
        abort(404)
    try:
        links = json.loads(member["links"] or "[]")
    except ValueError:
        links = []
    return render_template(
        "profile.html", member=member, links=links,
        bio_html=to_html(member["bio"] or ""),
        photo=(url_for("profiles.photo", stored_name=member["photo_name"])
               if member["photo_name"] else None),
    )


@bp.route("/foto/<stored_name>")
def photo(stored_name: str):
    """Deliberately public, unlike /comunidad/media/<name>.

    A profile photo is part of a page anyone can read, so it cannot sit behind
    the login. It is still tied to a published profile: unpublishing the page
    takes the picture down with it rather than leaving it reachable to anybody
    who noted the URL.
    """
    if not uploads.STORED_NAME.match(stored_name):
        abort(404)
    row = get_db().execute(
        """SELECT 1 FROM members
            WHERE photo_name = ? AND profile_published = 1 AND active = 1""",
        (stored_name,),
    ).fetchone()
    if row is None:
        abort(404)
    return send_from_directory(uploads.directory(), stored_name)


@bp.route("/miembro/foto/<stored_name>")
@login_required
def member_photo(stored_name: str):
    """The same picture, for the people inside.

    `profiles.photo` above serves only photos belonging to a *published* page,
    which is right for the open internet and wrong for Miembros: somebody who
    has not published a public page has still chosen a face for the community
    they joined, and a directory of grey squares is not a directory. So this one
    is behind the login and asks only that the member is active.

    The profile form says so in as many words, because a picture turning up
    somewhere its owner did not expect is exactly the surprise a profile form
    exists to prevent.
    """
    if not uploads.STORED_NAME.match(stored_name):
        abort(404)
    row = get_db().execute(
        """SELECT 1 FROM members
            WHERE photo_name = ? AND active = 1 AND role != 'tombstone'""",
        (stored_name,),
    ).fetchone()
    if row is None:
        abort(404)
    return send_from_directory(uploads.directory(), stored_name)


# --- editing it -----------------------------------------------------------

@bp.route("/mi-perfil", methods=["GET", "POST"])
@login_required
def edit():
    db = get_db()
    if request.method == "GET":
        try:
            links = json.loads(g.member["links"] or "[]")
        except ValueError:
            links = []
        return render_template(
            "profile_edit.html",
            links_text="\n".join(f"{item['label']}|{item['url']}" for item in links),
            available=name_is_available(g.member["gitea_login"]),
            address=url_for("profiles_public.show", name=g.member["gitea_login"]),
        )

    bio = request.form.get("bio", "").strip()[:BIO_MAX]
    links = clean_links(request.form.get("links", ""))
    published = 1 if request.form.get("published") == "on" else 0

    photo_name = g.member["photo_name"]
    try:
        staged = uploads.stage(request.files.getlist("photo"))
    except uploads.RejectedUpload as exc:
        flash(str(exc), "error")
        return redirect(url_for("profiles.edit"))
    if staged:
        # Saved with no parent id: a profile photo hangs off the member row,
        # not off a thread or a message, so it is written to disk here and the
        # filename is kept on the member.
        item = staged[0]
        uploads.directory().joinpath(item["stored_name"]).write_bytes(item["data"])
        if photo_name:
            uploads.remove(photo_name)
        photo_name = item["stored_name"]

    db.execute(
        """UPDATE members
              SET bio = ?, links = ?, profile_published = ?, photo_name = ?
            WHERE id = ?""",
        (bio, json.dumps(links, ensure_ascii=False), published, photo_name,
         g.member["id"]),
    )
    flash("Perfil guardado." if published else "Perfil guardado y sin publicar.",
          "ok")
    return redirect(url_for("profiles.edit"))
