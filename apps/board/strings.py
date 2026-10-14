"""Every word the software says, in one place and editable.

Nothing this software said could be changed without a commit. «Inicio» was a
word in `base.html`, again in `home.py`'s section list, again three times in
`config.yaml`, again in an `aria-label` and again in the breadcrumb schema — six
edits to rename one section, none of them reachable from Gestión. That is the
thing `identity.py` exists to end for the site's *name*, and it stopped before
it reached the *labels*.

### The catalogue is the default, not the value

`strings/*.yaml` ships with the code, inside the image, through the Dockerfile's
existing `COPY apps /srv/apps`. It holds what the software says when nobody has
said otherwise. Only the **differences** are stored, which is `brand.FACTORY`'s
rule and it buys the same thing: a wording I improve in a later version improves
for every install that has not deliberately overridden it, instead of arriving
frozen at whatever this version happened to think.

### Two namespaces, because there are two runtimes

`site.*` is the public site: three languages, written to `data/ui.yaml` and read
by Hugo on the next build. Everything else is the members area: one language
today, read from SQLite and true on the next page load. The prefix is what
decides, and it is load-bearing — it picks the store, the number of boxes on the
form, and whether a save needs the git server at all.

The entries are keyed by language even where only `es` is filled, so the members
area can gain a second language without a format change. `_note` is for whoever
is editing, and the underscore is the rule: every other key in an entry is a
language code.

### What this deliberately does not do

It is not an i18n framework. There is no plural machinery, no gender, no
context disambiguation and no `.po` files — this is a dict with a fallback, and
the moment it needs more than that it should become Babel rather than grow into
a worse Babel.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from flask import current_app, g

from .db import get_db

CATALOGUE_DIR = Path(__file__).resolve().parent / "strings"

# The site's languages, in the order the form draws them. Spanish first because
# it is the one the association writes in and the only one the members area has.
LANGS = ("es", "de", "pt-br")
LANG_LABELS = {"es": "Español", "de": "Deutsch", "pt-br": "Português (Brasil)"}
DEFAULT_LANG = "es"

# A key under this prefix is the public site's and goes to `data/ui.yaml`.
PUBLIC = "site."

VALUE_MAX = 2000


def _load() -> dict:
    """Every file in `strings/`, merged, keyed by the area it came from.

    Returns {key: {"langs": {...}, "note": str, "area": str}}. Read once per
    process at first use — the files are in the image and cannot change while
    it runs, so re-reading them per request would be work for nothing.
    """
    catalogue: dict[str, dict] = {}
    for path in sorted(CATALOGUE_DIR.glob("*.yaml")):
        area = path.stem
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key, entry in loaded.items():
            if not isinstance(entry, dict):
                raise ValueError(f"{path.name}: «{key}» is not a mapping")
            langs = {code: str(text) for code, text in entry.items()
                     if not code.startswith("_")}
            unknown = set(langs) - set(LANGS)
            if unknown:
                raise ValueError(f"{path.name}: «{key}» has {sorted(unknown)}, "
                                 f"which are not languages of this site")
            if DEFAULT_LANG not in langs:
                raise ValueError(f"{path.name}: «{key}» has no Spanish")
            if key in catalogue:
                raise ValueError(f"«{key}» is defined twice")
            catalogue[key] = {"langs": langs, "note": entry.get("_note", ""),
                              "area": area}
    return catalogue


_CATALOGUE: dict | None = None


def catalogue() -> dict:
    global _CATALOGUE
    if _CATALOGUE is None:
        _CATALOGUE = _load()
    return _CATALOGUE


def is_public(key: str) -> bool:
    return key.startswith(PUBLIC)


def areas() -> list[str]:
    """The files, in the order the screen offers them."""
    return sorted({entry["area"] for entry in catalogue().values()})


# --- what has been overridden ---------------------------------------------

def _row():
    return get_db().execute("SELECT * FROM strings WHERE id = 1").fetchone()


def stored() -> dict:
    """{key: {lang: text}} — only what somebody actually changed."""
    row = _row()
    try:
        value = json.loads(row["strings_json"]) if row else {}
    except (TypeError, ValueError):
        current_app.logger.warning("strings_json is not readable; using the catalogue")
        return {}
    return value if isinstance(value, dict) else {}


def overrides() -> dict:
    """`stored()`, once per request, and never raising.

    `base.html` asks for a word on every page, the error pages included — and on
    a 500 the thing that broke may well be the database. A page in the shipped
    wording is still a page; a second traceback inside the error handler is not.
    The same arrangement `brand.state()` has, for the same reason.
    """
    if "string_overrides" not in g:
        try:
            g.string_overrides = stored()
        except Exception:                       # pragma: no cover - defensive
            current_app.logger.warning("strings unreadable, using the catalogue",
                                       exc_info=True)
            g.string_overrides = {}
    return g.string_overrides


def state() -> dict:
    """What the screen and its Gestión card read."""
    row = _row()
    given = stored()
    return {
        "stored": given,
        "updated_at": row["updated_at"] if row else None,
        "published_at": row["published_at"] if row else None,
        "customised": bool(given),
    }


# --- the lookup -----------------------------------------------------------

def t(key: str, lang: str = DEFAULT_LANG, **values) -> str:
    """The word, overridden if somebody changed it, formatted if it takes a value.

    An unknown key returns itself. That is the rule `t.html` already uses on the
    public side and it is the right one: a typo renders as `nav.incio` on the
    page, which somebody notices, rather than as nothing, which nobody does.

    The order of the fallbacks matters on the public side. An override for the
    asked-for language wins; then the **shipped** word in that language, before
    the override in Spanish — somebody who reworded only the Spanish has said
    nothing about German, and showing a German reader Spanish would be reading
    an intention into silence.
    """
    entry = catalogue().get(key)
    if entry is None:
        return (overrides().get(key) or {}).get(lang) or key
    given = overrides().get(key) or {}
    text = (given.get(lang) or entry["langs"].get(lang)
            or given.get(DEFAULT_LANG) or entry["langs"][DEFAULT_LANG])
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, IndexError) as exc:
        # Loudly, in the log, and the unformatted text on the page. A label
        # somebody edited into `{nombre}` must not take the page down, and must
        # not render half a sentence either.
        current_app.logger.warning("string %s wants %s: %s", key, sorted(values), exc)
        return text


def public_langs(key: str) -> dict:
    """Every language of a public string, overrides applied — what gets written
    into `data/ui.yaml`."""
    entry = catalogue()[key]
    given = overrides().get(key) or {}
    return {lang: (given.get(lang) or "").strip() or entry["langs"].get(lang, "")
            for lang in LANGS}
