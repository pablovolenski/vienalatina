"""Gestión → Textos: changing what the software says.

The screen is **generic**, and that is the whole economy of it: it iterates
`strings.catalogue()` and draws a box per entry, so extracting another hundred
strings later costs nothing here at all. There is no field list in this module
and there must never be one.

The same two destinations as `brand.py` and `identity.py`, written in the same
order and for the same reason: SQLite first, so a change to the members area's
own words is never blocked by the git server, then the commit of `data/ui.yaml`
that carries the public half to the next build.
"""

from __future__ import annotations

import json

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                   request, url_for)

from . import activity, gitea, strings
from .db import get_db
from .security import superadmin_required

bp = Blueprint("ui", __name__)

DATA_FILE = "data/ui.yaml"

# Above this, the form draws a textarea instead of a line. Measured rather than
# guessed: the longest label is 32 characters and the shortest explanation is
# well past eighty, so the break falls in the gap between the two kinds.
LONG = 80


def _matches(key: str, entry: dict, needle: str) -> bool:
    if not needle:
        return True
    haystack = " ".join([key, entry["note"], *entry["langs"].values()]).lower()
    return needle in haystack


def _rows(area: str, needle: str) -> list[dict]:
    """What the form draws: one entry per string, with what is stored on it."""
    given = strings.stored()
    out = []
    for key, entry in sorted(strings.catalogue().items()):
        if entry["area"] != area and area != "todo":
            continue
        if not _matches(key, entry, needle):
            continue
        public = strings.is_public(key)
        out.append({
            "key": key,
            "note": entry["note"],
            "public": public,
            "long": any(len(text) > LONG for text in entry["langs"].values()),
            # The shipped word is the placeholder and what was typed is the
            # value — the split `identity.py` uses, and for the same reason: a
            # field prefilled with the default saves the default the first time
            # anybody presses the button, and freezes it from then on.
            "langs": [{
                "code": code,
                "label": strings.LANG_LABELS[code],
                "default": entry["langs"].get(code, ""),
                "stored": (given.get(key) or {}).get(code, ""),
            } for code in (strings.LANGS if public else (strings.DEFAULT_LANG,))],
        })
    return out


@bp.route("/gestion/textos", methods=["GET", "POST"])
@superadmin_required
def edit():
    area = request.args.get("area", "nav")
    needle = request.args.get("q", "").strip().lower()[:80]
    if area not in strings.areas() and area != "todo":
        area = strings.areas()[0]

    if request.method == "GET":
        return render_template("ui_form.html", state=strings.state(),
                               areas=strings.areas(), area=area, q=needle,
                               rows=_rows(area, needle))

    # Only this page's fields are read, so saving one area cannot blank another:
    # the form posts what it drew, and what it did not draw is left alone.
    given = dict(strings.stored())
    touched = 0
    for row in _rows(area, needle):
        key = row["key"]
        entry = strings.catalogue()[key]
        values = {}
        for lang in row["langs"]:
            code = lang["code"]
            text = (request.form.get(f"{key}:{code}") or "").strip()[:strings.VALUE_MAX]
            # One line for a label, because a newline in a menu entry is a menu
            # that renders raggedly and nobody can see why.
            text = " ".join(text.split()) if not row["long"] else text.strip()
            if text and text != entry["langs"].get(code):
                values[code] = text
        if values:
            if given.get(key) != values:
                touched += 1
            given[key] = values
        elif key in given:
            given.pop(key)
            touched += 1

    get_db().execute(
        """INSERT INTO strings (id, strings_json, updated_by, updated_at, published_at)
           VALUES (1, ?, ?, datetime('now'), NULL)
           ON CONFLICT(id) DO UPDATE SET
               strings_json = excluded.strings_json,
               updated_by = excluded.updated_by,
               updated_at = excluded.updated_at,
               published_at = NULL""",
        (json.dumps(given, sort_keys=True, ensure_ascii=False), g.member["id"]),
    )
    # The override read is memoised per request; this one has just changed it.
    g.pop("string_overrides", None)
    activity.log("ui.saved", detail=f"{touched} en {area}")

    if _publish():
        flash("Guardado. El sitio público se reconstruye en un par de minutos.", "ok")
    return redirect(url_for("ui.edit", area=area, q=needle or None))


@bp.route("/gestion/textos/publicar", methods=["POST"])
@superadmin_required
def publish():
    """Try the commit again after the git server was unreachable."""
    if _publish():
        flash("Publicado. El sitio público se reconstruye en un par de minutos.", "ok")
    return redirect(url_for("ui.edit"))


@bp.route("/gestion/textos/restaurar", methods=["POST"])
@superadmin_required
def restore():
    """Back to the words the software shipped with."""
    get_db().execute("DELETE FROM strings WHERE id = 1")
    g.pop("string_overrides", None)
    activity.log("ui.restored")
    flash("Restaurados los textos originales.", "ok")
    _publish()
    return redirect(url_for("ui.edit"))


# --- the public half ------------------------------------------------------

def _quote(value: str) -> str:
    """A double-quoted YAML scalar. Backslashes and quotes are the only two
    characters that can end one, and both are escapable inside it."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_for(given: dict) -> str:
    """`data/ui.yaml`, written rather than dumped — the same trade `brand.py`
    and `identity.py` make, and the values here are even better known: short
    strings, one per line, no structure below two levels.

    **Only what was actually overridden is written**, and only the languages
    that were. A file repeating the theme's own words back at it would make the
    fallback in `t.html` meaningless and freeze today's wording for good — and a
    language written out as an empty string would stop the pipeline filling it.
    """
    lines = ["# Generado desde Gestión → Textos en vienalatina.com/comunidad.",
             "# No editar a mano: el próximo guardado lo sobrescribe."]
    for key in sorted(given):
        if not strings.is_public(key):
            continue
        values = {code: text for code, text in given[key].items() if text}
        if not values:
            continue
        # Without the `site.` prefix: inside this file the namespace is implied,
        # and `t.html` looks the bare key up.
        lines.append(f"{key[len(strings.PUBLIC):]}:")
        for code in strings.LANGS:
            if values.get(code):
                lines.append(f"  {code}: {_quote(values[code])}")
    return "\n".join(lines) + "\n"


def _publish() -> bool:
    """Commit the public half. True if it got there.

    Returns False and says why rather than raising, exactly as the other two
    screens do: the words are already saved here, and a superadministrator whose
    change took effect in the members area needs to know the public site is a
    step behind, not to meet a traceback.
    """
    try:
        token = gitea.content_token()
    except gitea.GiteaError as exc:
        flash(f"Guardado aquí. El sitio público no se ha actualizado: {exc}", "error")
        return False

    try:
        _, sha = gitea.read_file(DATA_FILE, token)
    except gitea.GiteaError:
        sha = None

    try:
        gitea.write_file(DATA_FILE, yaml_for(strings.stored()).encode("utf-8"),
                         "textos: palabras del sitio", token, sha=sha)
    except (gitea.GiteaError, OSError) as exc:
        current_app.logger.warning("ui publish failed: %s", exc)
        flash(f"Guardado aquí. El sitio público no se ha actualizado: {exc}", "error")
        return False

    get_db().execute("UPDATE strings SET published_at = datetime('now') WHERE id = 1")
    return True
