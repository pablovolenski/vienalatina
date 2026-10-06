"""The look of both halves of the site, owned by the admin rather than by me.

Until this existed, the brand colours were two identical `:root` blocks — one in
`themes/vienalatina/assets/css/main.css`, one in `apps/board/static/board.css` —
with a comment in the second saying *"If the brand colours change, change them
in both."* There was no logo anywhere and no favicon in either `<head>`. Every
one of those is a change only somebody with the repository could make.

**The dictionary below is now the single source of truth for the palette**, and
`apps/board/tests/test_brand.py` parses both stylesheets and fails if either
drifts from it. That is what retires the comment: not by merging the two files —
they are served by different things and each has to stand on its own — but by
making a divergence fail the build instead of going unnoticed for a month.

### Whose screen this is

**The superadministrator's, not an admin's.** The look and the identity are the
platform's, and an admin is the association's committee — the same line that
makes an admin account the superadministrator's to grant. An admin who wants the
colours changed asks the person who installed this, which is the correct
conversation to be having.

### The two destinations

One form, saved to two places, because the two halves cannot read the same
store:

* **SQLite**, for the members area, which must render correctly when the git
  server is unreachable — the same rule the private calendar follows.
* **Git** (`data/brand.yaml` and `static/brand/…`), for the public site, which
  is files on disk built by Hugo and has no database to read.

SQLite is written first and the commits follow. If Gitea is down the change
still takes effect here, the row keeps `published_at` NULL, and the screen says
so with a button to try the publish again. The other order would let a git
outage block a change that does not need git.

### Why the custom CSS is a file and not a `<style>`

The Content-Security-Policy in `app.py` has no `'unsafe-inline'`, so an inline
style block is not an error anybody sees — the browser ignores it and the colours
simply do not apply. Both halves therefore serve a generated stylesheet:
`/comunidad/marca.css` here, a fingerprinted `resources.FromString` on the
public site.
"""

from __future__ import annotations

import colorsys
import json
import re
import secrets

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect,
                   render_template, request, send_from_directory, url_for)

from . import activity, gitea, uploads
from .db import get_db
from .security import superadmin_required

bp = Blueprint("brand", __name__)

# Every token, with the value it has had since the site was built. Copied from
# the two `:root` blocks, which the test above holds to this.
FACTORY = {
    "brand":        "#c0391c",
    "brand-dark":   "#9a2a0f",
    "brand-soft":   "#fbe2db",
    "brand-softer": "#fdf1ed",
    "bg":           "#f8f3ef",
    "surface":      "#ffffff",
    "surface-soft": "#f2ebe6",
    "border":       "#c2a89f",
    "border-light": "#e0cec8",
    "text":         "#1a0d09",
    "muted":        "#5c3d37",
    "subtle":       "#9a7870",
    "danger":       "#a11b1b",
}

# The six on the easy form, in the order they are drawn, with what each one is
# for in words an admin can act on. Six rather than thirteen because the other
# seven are shades of these, and a form of thirteen colour pickers is a form
# nobody finishes.
SIMPLE = (
    ("brand", "Color principal", "Botones, enlaces y la barra de arriba."),
    ("bg", "Fondo", "El color de la página detrás de todo."),
    ("surface", "Tarjetas", "El fondo de las tarjetas y los formularios."),
    ("text", "Texto", "El color de las letras."),
    ("muted", "Texto secundario", "Fechas, autores y notas al pie."),
    ("border", "Líneas", "Bordes y separadores."),
)

# Shades worked out from `brand` when they are not given explicitly. The factory
# palette has exactly this relationship, so an admin who picks one colour gets a
# coherent set rather than one red button on an orange background.
#
# Each entry is the lightness the shade gets: a multiplier of the brand's own
# for the dark one, an absolute value for the two tints. Hue and saturation are
# kept, which is what makes them read as the same colour.
DERIVED = {
    "brand-dark":   ("scale", 0.75),
    "brand-soft":   ("set", 0.92),
    "brand-softer": ("set", 0.96),
}

HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# --- the typeface ----------------------------------------------------------
#
# Four stacks of fonts that are already on the reader's machine, and nothing
# downloaded from anywhere. The site's freedom from Google Fonts is deliberate
# and a GDPR matter: a stylesheet that pulls a font from fonts.gstatic.com tells
# Google the IP address of everyone who reads this site, which is a disclosure
# an association cannot make on its visitors' behalf without asking them.
#
# A fifth option, `propia`, uses an uploaded .woff2 and falls back to the sans
# stack — so a font that fails to load leaves a readable site rather than a
# browser default.
TYPEFACES = {
    "sans": ("Sans (la de ahora)",
             '"Montserrat", -apple-system, BlinkMacSystemFont, "Segoe UI", '
             'Roboto, "Helvetica Neue", Arial, sans-serif'),
    "serif": ("Serif",
              'Georgia, Cambria, "Times New Roman", Times, serif'),
    "redonda": ("Redondeada",
                '"Trebuchet MS", "Segoe UI", Verdana, sans-serif'),
    "sistema": ("La del sistema",
                'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'),
}
DEFAULT_TYPEFACE = "sans"
FONT_STACK_FALLBACK = TYPEFACES["sans"][1]

# A .woff2 and nothing else. It is the web format — every browser in use takes
# it, it is the smallest, and it has a four-byte signature this can check
# without a parser. A .ttf or .otf would work in a browser and would also be
# two to five times the size for the same letters.
FONT_SIGNATURE = b"wOF2"
FONT_MAX_BYTES = 400_000

# How round everything is. One setting driving four tokens, because a site whose
# cards are round and whose buttons are square looks like two sites.
ROUNDNESS = {
    "suave": ("Suave (la de ahora)", {"radius-xl": "18px", "radius-lg": "12px",
                                      "radius-md": "8px", "radius-sm": "5px"}),
    "recta": ("Recta", {"radius-xl": "0", "radius-lg": "0",
                        "radius-md": "0", "radius-sm": "0"}),
    "redonda": ("Redonda", {"radius-xl": "28px", "radius-lg": "20px",
                            "radius-md": "14px", "radius-sm": "10px"}),
}
DEFAULT_ROUNDNESS = "suave"

# The free CSS box takes anything but these. `<` and `>` cannot appear in valid
# CSS outside a string, and they are the two characters that could end the
# stylesheet context in a consumer that mishandles it. Everything else is left
# alone on purpose: the point of the box is that an admin who knows CSS is not
# second-guessed.
CSS_FORBIDDEN = ("<", ">")
CSS_MAX = 20_000

LOGO_EXTENSIONS = ("png", "jpg", "webp")
FAVICON_EXTENSIONS = ("png",)

# Where the two pictures land in the content repository. Stable paths, so saving
# twice replaces the file rather than leaving the old one behind.
REPO_FOLDER = "static/brand"
DATA_FILE = "data/brand.yaml"


# --- colour arithmetic, all of it from the standard library ----------------

def _rgb(value: str) -> tuple[float, float, float]:
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    return tuple(int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def shade(value: str, token: str) -> str:
    """One of the three brand shades, from the brand colour.

    In HLS rather than by multiplying the channels: scaling RGB towards white
    washes the hue out, and the result of mixing #c0391c with white is pink
    rather than the warm tint the design uses.
    """
    how, amount = DERIVED[token]
    hue, lightness, saturation = colorsys.rgb_to_hls(*_rgb(value))
    lightness = lightness * amount if how == "scale" else amount
    return _hex(colorsys.hls_to_rgb(hue, lightness, saturation))


def _luminance(value: str) -> float:
    """WCAG 2.1 relative luminance."""
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(c) for c in _rgb(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(a: str, b: str) -> float:
    """The WCAG contrast ratio between two colours, 1.0 to 21.0."""
    first, second = _luminance(a), _luminance(b)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


# What has to be readable, and the ratio WCAG asks for. Body text is 4.5:1;
# 3:1 is the rule for large text and for the edge of a control, which is what a
# button in the brand colour is.
PAIRS = (
    ("text", "bg", 4.5, "el texto sobre el fondo"),
    ("text", "surface", 4.5, "el texto sobre las tarjetas"),
    ("muted", "surface", 4.5, "el texto secundario sobre las tarjetas"),
)


def warnings(tokens: dict) -> list[str]:
    """Pairs that are hard to read, named with their ratio.

    A warning and not a refusal. An admin may have a reason, and a form that
    argues is a form people work around; what this prevents is somebody
    shipping an unreadable site without once being told.
    """
    notes = []
    for first, second, needed, description in PAIRS:
        ratio = contrast(tokens[first], tokens[second])
        if ratio < needed:
            notes.append(
                f"Poco contraste en {description}: {ratio:.1f}:1, y hacen falta "
                f"{needed}:1. Se guarda igual, pero costará leerlo."
            )
    white = contrast("#ffffff", tokens["brand"])
    if white < 3.0:
        notes.append(
            f"El texto blanco sobre el color principal queda en {white:.1f}:1 "
            "(hacen falta 3:1). Los botones costarán de leer."
        )
    return notes


# --- the stored row --------------------------------------------------------

def _row():
    return get_db().execute("SELECT * FROM brand WHERE id = 1").fetchone()


def resolve(stored: dict) -> dict:
    """Every token's value, given what has been stored.

    Only differences from the factory are stored, which has two consequences
    worth the arrangement: a token added here later arrives with its default
    already in place for every existing install, and an admin who has changed
    one colour has one row of JSON rather than a snapshot of thirteen.

    The three shades are derived from `brand` **only when a brand colour has
    been stored**. Without one the factory hexes are used verbatim, so saving
    nothing cannot quietly shift the palette by a rounding error.
    """
    tokens = dict(FACTORY)
    tokens.update({k: v for k, v in stored.items() if k in FACTORY and v})
    if stored.get("brand"):
        for token in DERIVED:
            if not stored.get(token):
                tokens[token] = shade(stored["brand"], token)
    return tokens


def current() -> dict:
    """What both the form and the stylesheet read. Works with no row at all."""
    row = _row()
    try:
        stored = json.loads(row["tokens_json"]) if row else {}
    except (TypeError, ValueError):
        # A row we cannot parse must not take the site down. Factory colours
        # and a line in the log is the right failure here.
        current_app.logger.warning("brand.tokens_json is not readable; using factory")
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    def column(name):
        return (row[name] if row else None) or None

    typeface = column("typeface")
    if typeface not in TYPEFACES and typeface != "propia":
        typeface = DEFAULT_TYPEFACE
    roundness = column("roundness")
    if roundness not in ROUNDNESS:
        roundness = DEFAULT_ROUNDNESS

    return {
        "stored": stored,
        "tokens": resolve(stored),
        "custom_css": (row["custom_css"] if row else "") or "",
        "logo_name": column("logo_name"),
        "favicon_name": column("favicon_name"),
        "font_name": column("font_name"),
        "typeface": typeface,
        "roundness": roundness,
        "updated_at": row["updated_at"] if row else None,
        "published_at": row["published_at"] if row else None,
        "stamp": _stamp(row["updated_at"] if row else None),
        "customised": bool(stored or (row and (row["custom_css"] or row["logo_name"]
                                               or row["favicon_name"]
                                               or column("font_name")
                                               or typeface != DEFAULT_TYPEFACE
                                               or roundness != DEFAULT_ROUNDNESS))),
    }


def _blank() -> dict:
    """The factory look, as `current()` would describe it."""
    return {"stored": {}, "tokens": dict(FACTORY), "custom_css": "",
            "logo_name": None, "favicon_name": None, "font_name": None,
            "typeface": DEFAULT_TYPEFACE, "roundness": DEFAULT_ROUNDNESS,
            "updated_at": None, "published_at": None, "stamp": "0",
            "customised": False}


def state() -> dict:
    """`current()`, once per request, and never raising.

    `base.html` asks for this on every page, the error pages included — and on a
    500 the thing that broke may well be the database. A page in the factory
    colours is still a page; a second traceback inside the error handler is not.
    """
    if "brand_state" not in g:
        try:
            g.brand_state = current()
        except Exception:                       # pragma: no cover - defensive
            current_app.logger.warning("brand unreadable, using factory", exc_info=True)
            g.brand_state = _blank()
    return g.brand_state


def _stamp(updated_at: str | None) -> str:
    """A cache-buster safe to put in a query string.

    `updated_at` is SQLite's `datetime('now')` — spaces and colons — and both
    halves of the site paste this into a URL, so the digits alone are what
    travels.
    """
    return re.sub(r"\D", "", updated_at or "") or "0"


def font_stack(state: dict) -> str | None:
    """The `font-family` the site should use, or None to leave it alone.

    None rather than the default string when nothing was chosen, so a site that
    has not touched this serves no font rule at all and the stylesheets stay
    byte-identical to what they were.

    An uploaded face is named first and the sans stack follows it: a font that
    404s or arrives corrupt then leaves a readable site rather than whatever the
    browser falls back to on its own.
    """
    if state["typeface"] == "propia" and state["font_name"]:
        return f'"VLPropia", {FONT_STACK_FALLBACK}'
    if state["typeface"] in TYPEFACES and state["typeface"] != DEFAULT_TYPEFACE:
        return TYPEFACES[state["typeface"]][1]
    return None


def _radius_tokens(state: dict) -> dict:
    """The four radius values, or nothing when the roundness is the built-in."""
    if state["roundness"] == DEFAULT_ROUNDNESS:
        return {}
    return ROUNDNESS[state["roundness"]][1]


def font_face(state: dict, url: str) -> str:
    """The `@font-face` for an uploaded typeface, at whatever URL serves it.

    `font-display: swap` deliberately: the alternative shows nothing until the
    file arrives, and a blank article on a slow phone is worse than an article
    that changes typeface a moment after it appears.
    """
    return ("@font-face {\n"
            '\tfont-family: "VLPropia";\n'
            f"\tsrc: url({url}) format('woff2');\n"
            "\tfont-weight: 100 900;\n"
            "\tfont-display: swap;\n"
            "}\n")


def stylesheet(state: dict | None = None) -> str:
    """The `@font-face`, the `:root` overrides, then the custom CSS, in that
    order.

    Only what differs from the factory is written, so the file reads as what the
    superadministrator changed, and an install that has changed nothing serves a
    comment and no rules at all.
    """
    state = state or current()
    css = "/* Generado desde Gestión → Marca. No editar a mano. */\n"

    if state["typeface"] == "propia" and state["font_name"]:
        css += font_face(state, url_for("brand.picture",
                                        stored_name=state["font_name"]))

    lines = [f"\t--{name}: {value};"
             for name, value in state["tokens"].items()
             if value != FACTORY[name]]
    lines += [f"\t--{name}: {value};" for name, value in _radius_tokens(state).items()]
    if lines:
        css += ":root {\n" + "\n".join(lines) + "\n}\n"

    stack = font_stack(state)
    if stack:
        # On `body` rather than `:root`, because the rule it is overriding is on
        # `body` and a custom property would need every rule to opt in.
        css += f"body {{ font-family: {stack}; }}\n"

    if state["custom_css"]:
        # Last, so it wins over everything above it. That is the whole contract
        # of the advanced box.
        css += "\n/* CSS propio */\n" + state["custom_css"] + "\n"
    return css


# --- reading the form ------------------------------------------------------

def _read_tokens(form, errors: list[str]) -> dict:
    """Only what differs from the factory, and only if it is a colour.

    A colour field is not a place to accept arbitrary CSS: `url(...)` in a
    custom property is a request to another server on every page view, and
    there is a whole textarea below for people who want to write CSS.
    """
    stored = {}
    for name in FACTORY:
        raw = (form.get(f"token-{name}") or "").strip().lower()
        if not raw:
            continue
        if not HEX.match(raw):
            errors.append(f"«{raw}» no es un color. Usa la forma #rrggbb.")
            continue
        if raw != FACTORY[name].lower():
            stored[name] = raw
    return stored


def _read_css(form, errors: list[str]) -> str:
    css = (form.get("custom_css") or "").strip()
    if len(css) > CSS_MAX:
        errors.append(f"El CSS propio no puede pasar de {CSS_MAX // 1000}.000 caracteres.")
        return ""
    for character in CSS_FORBIDDEN:
        if character in css:
            errors.append(
                f"El CSS propio no puede contener «{character}». "
                "Eso no aparece en CSS válido y sí en una etiqueta HTML."
            )
            return ""
    return css


def _read_picture(field: str, allowed: tuple[str, ...], errors: list[str]):
    """One staged picture, or None if nothing was chosen.

    `uploads.stage` does the work that matters — it reads the bytes and decides
    the extension from them rather than from the name — so a GIF called
    `logo.png` is refused here by the same code that refuses it on the wall.
    """
    upload = request.files.get(field)
    if not upload or not upload.filename:
        return None
    try:
        staged = uploads.stage([upload])
    except uploads.RejectedUpload as exc:
        errors.append(str(exc))
        return None
    if not staged:
        return None
    item = staged[0]
    extension = item["stored_name"].rsplit(".", 1)[-1]
    if extension not in allowed:
        errors.append(
            f"«{item['original_name']}» es un {extension}. "
            f"Aquí hace falta {' o '.join(allowed)}."
        )
        return None
    return item


def _read_font(errors: list[str]):
    """The uploaded typeface, sniffed rather than trusted.

    Not through `uploads.stage`, which knows about pictures: this needs its own
    four-byte check and its own size limit. Same shape though, and the same
    rule — the name on the file decides nothing.

    A stored name that ends `.woff2` would not match `uploads.STORED_NAME`,
    which allows three or four letters; `.wof` would be a lie. So the file is
    stored as `.bin` and served with an explicit Content-Type, which is what the
    browser reads anyway.
    """
    upload = request.files.get("font")
    if not upload or not upload.filename:
        return None
    data = upload.read()
    if not data:
        return None
    if len(data) > FONT_MAX_BYTES:
        errors.append(
            f"La tipografía pesa {len(data) // 1024}KB y el máximo son "
            f"{FONT_MAX_BYTES // 1024}KB. Subconjunta la fuente a latín y "
            "latín extendido antes de subirla."
        )
        return None
    if not data.startswith(FONT_SIGNATURE):
        errors.append(
            f"«{upload.filename[:60]}» no es un .woff2. Es el único formato que "
            "se acepta: es el de la web, lo entienden todos los navegadores y "
            "es el más pequeño."
        )
        return None
    return {
        "data": data,
        "original_name": upload.filename[:200],
        "stored_name": f"tipografia-{secrets.token_hex(6)}.bin",
    }


def _read_choice(form, field: str, table: dict, default: str,
                 extra: tuple[str, ...] = ()) -> str:
    """One key from a table of our own, or the default. Never the raw value."""
    choice = (form.get(field) or "").strip()
    return choice if choice in table or choice in extra else default


# --- routes ----------------------------------------------------------------

@bp.route("/gestion/marca", methods=["GET", "POST"])
@superadmin_required
def edit():
    if request.method == "GET":
        state = current()
        return render_template("brand_form.html", state=state,
                               factory=FACTORY, simple=SIMPLE,
                               derived=sorted(DERIVED), advanced=_advanced(),
                               typefaces=TYPEFACES, roundness=ROUNDNESS,
                               notes=warnings(state["tokens"]))

    errors: list[str] = []
    stored = _read_tokens(request.form, errors)
    css = _read_css(request.form, errors)
    logo = _read_picture("logo", LOGO_EXTENSIONS, errors)
    favicon = _read_picture("favicon", FAVICON_EXTENSIONS, errors)
    font = _read_font(errors)
    typeface = _read_choice(request.form, "typeface", TYPEFACES,
                            DEFAULT_TYPEFACE, extra=("propia",))
    roundness = _read_choice(request.form, "roundness", ROUNDNESS, DEFAULT_ROUNDNESS)
    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(url_for("brand.edit"))

    state = current()
    if typeface == "propia" and not (font or state["font_name"]):
        # Chosen without ever uploading one. Refused rather than silently
        # falling back, because the site would look unchanged and the setting
        # would say otherwise.
        flash("Para usar una tipografía propia hay que subir un archivo .woff2.",
              "error")
        return redirect(url_for("brand.edit"))

    # Written before the commits: the members area must not depend on the git
    # server being up to change its own colours.
    _store(stored, css, logo, favicon, font, typeface, roundness, state)
    for note in warnings(resolve(stored)):
        flash(note, "error")

    activity.log("brand.saved")
    if _publish():
        flash("Guardado. El sitio público se reconstruye en un par de minutos.", "ok")
    return redirect(url_for("brand.edit"))


def _advanced() -> tuple:
    """Every token the easy form does not offer, for the fold-out."""
    simple = {name for name, _, _ in SIMPLE}
    return tuple(name for name in FACTORY if name not in simple)


def _store(stored: dict, css: str, logo, favicon, font, typeface: str,
           roundness: str, state: dict) -> None:
    """The row, and the three uploaded files on disk.

    Files before the row, as `uploads.save` does it and for the same reason: a
    row naming a file that is not there renders as a broken image on every
    future visit, while a file with no row is invisible.
    """
    names = {"logo_name": state["logo_name"],
             "favicon_name": state["favicon_name"],
             "font_name": state["font_name"]}
    uploaded = (("logo_name", logo), ("favicon_name", favicon), ("font_name", font))
    for field, item in uploaded:
        if item is None:
            continue
        uploads.directory().joinpath(item["stored_name"]).write_bytes(item["data"])
        names[field] = item["stored_name"]

    get_db().execute(
        """INSERT INTO brand (id, tokens_json, custom_css, logo_name, favicon_name,
                              font_name, typeface, roundness,
                              updated_by, updated_at, published_at)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), NULL)
           ON CONFLICT(id) DO UPDATE SET
               tokens_json = excluded.tokens_json,
               custom_css = excluded.custom_css,
               logo_name = excluded.logo_name,
               favicon_name = excluded.favicon_name,
               font_name = excluded.font_name,
               typeface = excluded.typeface,
               roundness = excluded.roundness,
               updated_by = excluded.updated_by,
               updated_at = excluded.updated_at,
               published_at = NULL""",
        (json.dumps(stored, sort_keys=True), css,
         names["logo_name"], names["favicon_name"], names["font_name"],
         typeface, roundness, g.member["id"]),
    )

    # The old files, once nothing points at them. After the row, so a rollback
    # cannot leave the row naming a file that has been deleted.
    for field, item in uploaded:
        if item is not None and state[field] and state[field] != names[field]:
            uploads.remove(state[field])


@bp.route("/gestion/marca/publicar", methods=["POST"])
@superadmin_required
def publish():
    """Try the commits again after the git server was unreachable."""
    if _publish():
        flash("Publicado. El sitio público se reconstruye en un par de minutos.", "ok")
    return redirect(url_for("brand.edit"))


@bp.route("/gestion/marca/restaurar", methods=["POST"])
@superadmin_required
def restore():
    """Back to the colours the site was built with.

    Reachable even when the custom CSS has broken every other page, because the
    Marca screen deliberately does not load it — see `brand_form.html`.
    """
    state = current()
    get_db().execute("DELETE FROM brand WHERE id = 1")
    for name in (state["logo_name"], state["favicon_name"], state["font_name"]):
        if name:
            uploads.remove(name)
    activity.log("brand.restored")
    flash("Restaurado lo de fábrica.", "ok")
    _publish()
    return redirect(url_for("brand.edit"))


@bp.route("/marca.css")
def stylesheet_file():
    """Loaded by every page in the members area, after board.css.

    No login: it carries no private data, and the sign-in page should look like
    the site it signs you in to. Cached hard and busted by `?v=`, which is why
    `base.html` appends the stamp.
    """
    response = Response(stylesheet(), mimetype="text/css")
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@bp.route("/marca/<stored_name>")
def picture(stored_name: str):
    """The logo and the favicon, for the members area.

    Its own route rather than `uploads.serve`, which is behind the login: these
    two are public by definition, and one of them is in the `<head>` of the
    sign-in page. Only the current pair is served — an old logo stops being
    reachable the moment it is replaced, which is also when the file goes.
    """
    if not uploads.STORED_NAME.match(stored_name):
        abort(404)
    state = current()
    if stored_name not in (state["logo_name"], state["favicon_name"],
                           state["font_name"]):
        abort(404)
    # The typeface is stored `.bin` — `.woff2` is five characters and
    # `STORED_NAME` allows three or four — so its type is stated here rather
    # than guessed from the name. A font served as application/octet-stream
    # loads anyway, but says nothing true in a network panel.
    if stored_name == state["font_name"]:
        return send_from_directory(uploads.directory(), stored_name,
                                   mimetype="font/woff2")
    return send_from_directory(uploads.directory(), stored_name)


# --- the public site's copy ------------------------------------------------

def _repo_path(stored_name: str, kind: str) -> str:
    """A stable path per picture: `static/brand/logo.png`.

    Stable rather than the stored name so that saving a new logo replaces the
    file instead of leaving every previous one in the repository, deployed and
    unreferenced.
    """
    # The typeface is stored locally as `.bin` — see `_read_font` — but the
    # repository is a web root, where the extension is what a reader's cache and
    # a CDN go by. It lands as `.woff2`, which is what it is.
    extension = "woff2" if kind == "font" else stored_name.rsplit(".", 1)[-1]
    return f"{REPO_FOLDER}/{kind}.{extension}"


def _yaml(state: dict, logo: str | None, favicon: str | None,
          font: str | None = None) -> str:
    """`data/brand.yaml`, written rather than dumped.

    By hand because the values are known: colours matched against a hex
    pattern, a stamp of digits, two paths this module built, and one block of
    CSS that goes in as a literal block scalar so nothing in it needs quoting.
    PyYAML is not a dependency of the members area and is not worth becoming
    one for twenty lines of output.
    """
    lines = ["# Generado desde Gestión → Marca en vienalatina.com/comunidad.",
             "# No editar a mano: el próximo guardado lo sobrescribe.",
             f"stamp: \"{state['stamp']}\"",
             "tokens:"]
    for name, value in state["tokens"].items():
        if value != FACTORY[name]:
            lines.append(f"  {name}: \"{value}\"")
    for name, value in _radius_tokens(state).items():
        lines.append(f"  {name}: \"{value}\"")
    stack = font_stack(state)
    if stack:
        # Single-quoted because every stack has double quotes inside it.
        lines.append(f"font_stack: '{stack}'")
    if font:
        lines.append(f"font: \"/{font.removeprefix('static/')}\"")
    if logo:
        lines.append(f"logo: \"/{logo.removeprefix('static/')}\"")
    if favicon:
        lines.append(f"favicon: \"/{favicon.removeprefix('static/')}\"")
    if state["custom_css"]:
        lines.append("custom_css: |")
        lines += [f"  {line}" for line in state["custom_css"].splitlines()]
    return "\n".join(lines) + "\n"


def _publish() -> bool:
    """Commit the brand into the content repository. True if it got there.

    Four commits at most, because the contents API writes one file per request.
    That is the same trade `content.commit_picture` makes: the git trees API
    would batch them, at the cost of noticeably more code to get wrong, for a
    benefit nobody sees beyond one fewer pipeline run.

    Returns False and says why rather than raising: the colours are already
    saved here, and an admin whose change took effect in the members area needs
    to know the public site is a step behind, not to meet a traceback.
    """
    state = current()
    try:
        token = gitea.content_token()
    except gitea.GiteaError as exc:
        flash(f"Guardado aquí. El sitio público no se ha actualizado: {exc}", "error")
        return False

    try:
        paths = {}
        for kind, name in (("logo", state["logo_name"]),
                           ("favicon", state["favicon_name"]),
                           ("font", state["font_name"])):
            if not name:
                continue
            path = _repo_path(name, kind)
            data = uploads.directory().joinpath(name).read_bytes()
            _write(path, data, f"marca: {kind}", token)
            _tidy(kind, path, token)
            paths[kind] = path

        _write(DATA_FILE,
               _yaml(state, paths.get("logo"), paths.get("favicon"),
                     paths.get("font")).encode("utf-8"),
               "marca: colores", token)
    except (gitea.GiteaError, OSError) as exc:
        current_app.logger.warning("brand publish failed: %s", exc)
        flash(f"Guardado aquí. El sitio público no se ha actualizado: {exc}", "error")
        return False

    get_db().execute(
        "UPDATE brand SET published_at = datetime('now') WHERE id = 1")
    return True


def _write(path: str, data: bytes, message: str, token: str) -> None:
    """Create or update, whichever this file needs.

    The contents API wants the current sha to replace a file and refuses one
    when creating, so the read comes first. A missing file is the normal case
    the first time.
    """
    try:
        _, sha = gitea.read_file(path, token)
    except gitea.GiteaError:
        sha = None
    gitea.write_file(path, data, message, token, sha=sha)


def _tidy(kind: str, keep: str, token: str) -> None:
    """Remove a previous picture of the same kind with another extension.

    A logo saved as a PNG and then as a WebP would otherwise leave
    `static/brand/logo.png` in the repository for good — deployed, unreferenced
    and confusing to the next person who looks. Failure here is logged and
    swallowed: the new logo is already committed, and a leftover file is not a
    reason to tell the admin their change did not work.
    """
    try:
        for item in gitea.list_directory(REPO_FOLDER, token):
            name = item.get("name", "")
            path = f"{REPO_FOLDER}/{name}"
            if path != keep and name.rsplit(".", 1)[0] == kind:
                gitea.delete_file(path, item.get("sha", ""),
                                  f"marca: retirar {name}", token)
    except (gitea.GiteaError, OSError) as exc:
        current_app.logger.info("brand tidy-up skipped: %s", exc)
