"""What the site is called, what it says it is, and where to find it.

Separate from `brand.py`, which is how the site *looks*, because the two have
different audiences and different failure modes: a wrong colour is ugly and a
wrong contact address is a message nobody receives. They are also published to
different files, so an organisation can change its tagline without touching its
palette.

Everything here used to live in `config.yaml`, which means it could only be
changed by somebody with the repository — the one thing this phase exists to
end. The same arrangement `brand.py` uses carries it: SQLite first for the
members area, then a commit of `data/site.yaml` for the public site, in that
order so a git outage never blocks a change that does not need git.

**Hugo cannot have `config.yaml` overridden by a data file**, so the name and
the tagline are read through `partials/site-name.html` and
`partials/site-tagline.html`, which prefer `site.Data.site` and fall back to the
config. Every template that shows either calls one of them, so the two cannot
drift — and a site with no `data/site.yaml` renders exactly what it rendered
before any of this existed.
"""

from __future__ import annotations

import json
import re

from flask import (Blueprint, current_app, flash, g, redirect,
                   render_template, request, url_for)

from . import activity, gitea
from .db import get_db
from .security import superadmin_required

bp = Blueprint("identity", __name__)

DATA_FILE = "data/site.yaml"

# The languages the site is built in, with the config.yaml values they fall back
# to. Written here rather than read from config.yaml because the members area
# does not parse the site's configuration — and because a fallback that silently
# follows a file somebody edits is a fallback nobody can predict.
LANGUAGES = (
    ("es", "Español", "Viena Latina",
     "Comunidad latinoamericana en Viena — turismo, cultura, gastronomía, "
     "comunidad y comercio."),
    ("de", "Deutsch", "Viena Latina",
     "Lateinamerikanische Community in Wien — Tourismus, Kultur, Gastronomie, "
     "Gemeinschaft und Handel."),
    ("pt-br", "Português (Brasil)", "Viena Latina",
     "Comunidade latino-americana em Viena — turismo, cultura, gastronomia, "
     "comunidade e comércio."),
)

# The networks offered, in the order they are drawn. A fixed list rather than a
# free "add a link" box: these become `sameAs` in the Organization schema, which
# is a claim that an account and this site are the same body — and a list of
# arbitrary URLs is not that claim, it is a bookmark bar.
NETWORKS = (
    ("instagram", "Instagram", "https://instagram.com/…"),
    ("facebook", "Facebook", "https://facebook.com/…"),
    ("whatsapp", "WhatsApp", "https://chat.whatsapp.com/…"),
    ("youtube", "YouTube", "https://youtube.com/@…"),
    ("mastodon", "Mastodon", "https://mastodon.social/@…"),
    ("bluesky", "Bluesky", "https://bsky.app/profile/…"),
)

NAME_MAX = 80
TAGLINE_MAX = 300
FOOTER_MAX = 200

# Same rule as a profile link, and for the same reason: `javascript:` in an href
# is a script running on vienalatina.com, and these are rendered in the footer of
# every page.
URL_RE = re.compile(r"^https?://[^\s<>\"']{3,300}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")


def _row():
    return get_db().execute("SELECT * FROM site WHERE id = 1").fetchone()


def _loads(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def current() -> dict:
    """Everything the form and both halves of the site read.

    Works with no row at all, which is every install until somebody opens the
    screen — so the defaults here are the values `config.yaml` has today.
    """
    row = _row()
    names = _loads(row["names_json"] if row else None)
    social = _loads(row["social_json"] if row else None)

    languages = {}
    for code, _label, name, tagline in LANGUAGES:
        given = names.get(code) if isinstance(names.get(code), dict) else {}
        languages[code] = {
            "name": (given.get("name") or "").strip() or name,
            "tagline": (given.get("tagline") or "").strip() or tagline,
            # What was actually stored, for the form: a field showing the
            # fallback as its value would save the fallback the first time
            # anybody pressed the button, freezing it.
            "stored_name": (given.get("name") or "").strip(),
            "stored_tagline": (given.get("tagline") or "").strip(),
        }

    def column(name, default=""):
        return ((row[name] if row else None) or default)

    return {
        "languages": languages,
        "social": {key: social.get(key, "") for key, _, _ in NETWORKS},
        "email": column("email"),
        "phone": column("phone"),
        "city": column("city"),
        "street": column("street"),
        "share_image": column("share_image"),
        "footer_text": column("footer_text"),
        "updated_at": row["updated_at"] if row else None,
        "published_at": row["published_at"] if row else None,
        "customised": row is not None,
    }


def _read(form, errors: list[str]) -> dict:
    """The whole form, checked. Nothing here is written as it arrived."""
    names = {}
    for code, label, _name, _tagline in LANGUAGES:
        name = (form.get(f"name-{code}") or "").strip()[:NAME_MAX]
        tagline = (form.get(f"tagline-{code}") or "").strip()[:TAGLINE_MAX]
        # One line, always: a tagline with a newline in it is an og:description
        # that renders raggedly in some readers and truncated in others.
        tagline = re.sub(r"\s+", " ", tagline)
        if name or tagline:
            names[code] = {"name": name, "tagline": tagline}

    social = {}
    for key, label, _placeholder in NETWORKS:
        url = (form.get(f"social-{key}") or "").strip()
        if not url:
            continue
        if not URL_RE.match(url):
            errors.append(f"La dirección de {label} tiene que empezar por https://.")
            continue
        social[key] = url

    email = (form.get("email") or "").strip()[:120]
    if email and not EMAIL_RE.match(email):
        errors.append("Ese correo de contacto no parece una dirección.")
        email = ""

    share = (form.get("share_image") or "").strip()
    if share and not re.match(r"^/uploads/[A-Za-z0-9._-]+$", share):
        errors.append("La imagen al compartir tiene que ser una de /uploads/.")
        share = ""

    return {
        "names": names, "social": social, "email": email,
        "phone": (form.get("phone") or "").strip()[:40],
        "city": (form.get("city") or "").strip()[:80],
        "street": (form.get("street") or "").strip()[:160],
        "share_image": share,
        "footer_text": (form.get("footer_text") or "").strip()[:FOOTER_MAX],
    }


# --- routes ----------------------------------------------------------------

@bp.route("/gestion/identidad", methods=["GET", "POST"])
@superadmin_required
def edit():
    if request.method == "GET":
        return render_template("identity_form.html", state=current(),
                               languages=LANGUAGES, networks=NETWORKS)

    errors: list[str] = []
    fields = _read(request.form, errors)
    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(url_for("identity.edit"))

    get_db().execute(
        """INSERT INTO site (id, names_json, social_json, email, phone, city,
                             street, share_image, footer_text,
                             updated_by, updated_at, published_at)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), NULL)
           ON CONFLICT(id) DO UPDATE SET
               names_json = excluded.names_json,
               social_json = excluded.social_json,
               email = excluded.email, phone = excluded.phone,
               city = excluded.city, street = excluded.street,
               share_image = excluded.share_image,
               footer_text = excluded.footer_text,
               updated_by = excluded.updated_by,
               updated_at = excluded.updated_at,
               published_at = NULL""",
        (json.dumps(fields["names"], sort_keys=True, ensure_ascii=False),
         json.dumps(fields["social"], sort_keys=True), fields["email"],
         fields["phone"], fields["city"], fields["street"],
         fields["share_image"], fields["footer_text"], g.member["id"]),
    )
    activity.log("identity.saved")
    if _publish():
        flash("Guardado. El sitio público se reconstruye en un par de minutos.", "ok")
    return redirect(url_for("identity.edit"))


@bp.route("/gestion/identidad/publicar", methods=["POST"])
@superadmin_required
def publish():
    if _publish():
        flash("Publicado. El sitio público se reconstruye en un par de minutos.", "ok")
    return redirect(url_for("identity.edit"))


# --- the public site's copy ------------------------------------------------

def _quote(value: str) -> str:
    """A double-quoted YAML scalar. Backslashes and quotes are the only two
    characters that can end one, and both are escapable inside it."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_for(state: dict) -> str:
    """`data/site.yaml`, written rather than dumped — the same trade `brand.py`
    makes, for the same reason: PyYAML is not a dependency of the members area
    and is not worth becoming one for thirty lines of output.

    Only what was actually stored is written. A file repeating `config.yaml`'s
    own values back at it would make the fallback meaningless and freeze the
    defaults at whatever this version thought they were.
    """
    lines = ["# Generado desde Gestión → Identidad en vienalatina.com/comunidad.",
             "# No editar a mano: el próximo guardado lo sobrescribe."]

    languages = {code: data for code, data in state["languages"].items()
                 if data["stored_name"] or data["stored_tagline"]}
    if languages:
        lines.append("languages:")
        for code, data in languages.items():
            lines.append(f"  {code}:")
            if data["stored_name"]:
                lines.append(f"    name: {_quote(data['stored_name'])}")
            if data["stored_tagline"]:
                lines.append(f"    tagline: {_quote(data['stored_tagline'])}")

    social = [url for url in state["social"].values() if url]
    if social:
        # A flat list, because `sameAs` is a list of URLs and the names of the
        # networks are nowhere in the schema.
        lines.append("sameAs:")
        lines += [f"  - {_quote(url)}" for url in social]
        lines.append("social:")
        for key, url in state["social"].items():
            if url:
                lines.append(f"  {key}: {_quote(url)}")

    for key in ("email", "phone", "city", "street", "share_image", "footer_text"):
        if state[key]:
            lines.append(f"{key}: {_quote(state[key])}")

    return "\n".join(lines) + "\n"


def _publish() -> bool:
    """Commit the identity. True if it got there.

    Returns False and says why rather than raising, exactly as `brand._publish`
    does: the values are already saved here, and a superadministrator whose
    change took effect in the members area needs to know the public site is a
    step behind, not to meet a traceback.
    """
    state = current()
    try:
        token = gitea.content_token()
    except gitea.GiteaError as exc:
        flash(f"Guardado aquí. El sitio público no se ha actualizado: {exc}", "error")
        return False

    # The contents API wants the current sha to replace a file and refuses one
    # when creating, so the read comes first and a missing file is the normal
    # case the first time. Same shape as `brand._write`.
    try:
        _, sha = gitea.read_file(DATA_FILE, token)
    except gitea.GiteaError:
        sha = None

    try:
        gitea.write_file(DATA_FILE, yaml_for(state).encode("utf-8"),
                         "identidad: datos del sitio", token, sha=sha)
    except (gitea.GiteaError, OSError) as exc:
        current_app.logger.warning("identity publish failed: %s", exc)
        flash(f"Guardado aquí. El sitio público no se ha actualizado: {exc}", "error")
        return False

    get_db().execute("UPDATE site SET published_at = datetime('now') WHERE id = 1")
    return True
