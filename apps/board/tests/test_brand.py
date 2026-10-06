"""The admin-editable look: colours, logo, favicon, custom CSS.

The first test in this file is the point of the phase. The brand colours were
two identical `:root` blocks in two stylesheets, with a comment saying *"If the
brand colours change, change them in both"* — an instruction, which is the
weakest kind of guarantee there is. `brand.FACTORY` is the one list now, and
this file fails the build if either stylesheet drifts from it.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from apps.board import brand

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64

BOARD_CSS = Path(brand.__file__).resolve().parent / "static" / "board.css"
THEME_CSS = (Path(brand.__file__).resolve().parents[2]
             / "themes" / "vienalatina" / "assets" / "css" / "main.css")


def tokens_in(path: Path) -> dict:
    """The `--name: value` pairs from a stylesheet's first `:root` block."""
    css = path.read_text(encoding="utf-8")
    block = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert block, f"{path.name} has no :root block"
    return {name: value.strip().lower() for name, value in
            re.findall(r"--([a-z-]+):\s*([^;]+);", block.group(1))}


# --- the one list ---------------------------------------------------------

@pytest.mark.parametrize("path", [BOARD_CSS, THEME_CSS], ids=["board", "theme"])
def test_the_stylesheet_agrees_with_the_factory_palette(path):
    """Both files keep their own `:root` — they are served by different things
    and each has to stand alone — but neither may disagree with brand.py.

    Only the names both sides share are compared: the theme has no `--danger`
    and the members area has no `--accent` aliases, and neither absence is a
    drift."""
    declared = tokens_in(path)
    shared = set(declared) & set(brand.FACTORY)
    assert len(shared) > 8, f"{path.name}: only {len(shared)} tokens matched — is the block still there?"
    for name in sorted(shared):
        assert declared[name] == brand.FACTORY[name].lower(), (
            f"{path.name}: --{name} is {declared[name]} and brand.FACTORY says "
            f"{brand.FACTORY[name]}. One of the two is wrong."
        )


# --- colour arithmetic ----------------------------------------------------

def test_the_three_shades_come_out_of_the_brand_colour():
    """Derived in HLS so the hue survives. A green brand must not produce the
    pink tints that mixing #c0391c with white would."""
    tokens = brand.resolve({"brand": "#1b5e20"})

    assert tokens["brand"] == "#1b5e20"
    for name in ("brand-dark", "brand-soft", "brand-softer"):
        assert tokens[name] != brand.FACTORY[name], name
    # Darker, then two tints, in that order.
    assert (brand._luminance(tokens["brand-dark"])
            < brand._luminance(tokens["brand"])
            < brand._luminance(tokens["brand-soft"])
            < brand._luminance(tokens["brand-softer"]))


def test_an_explicit_shade_stops_being_derived():
    tokens = brand.resolve({"brand": "#1b5e20", "brand-dark": "#000000"})
    assert tokens["brand-dark"] == "#000000"


def test_storing_nothing_leaves_the_factory_hexes_untouched():
    """Not "close to" the factory palette — the same strings. Deriving from an
    unchanged brand colour would shift the three shades by a rounding error the
    first time anybody opened the screen."""
    assert brand.resolve({}) == brand.FACTORY


def test_contrast_matches_the_known_extremes():
    assert round(brand.contrast("#ffffff", "#000000"), 1) == 21.0
    assert round(brand.contrast("#777777", "#777777"), 1) == 1.0


def test_an_unreadable_pair_warns_and_a_readable_one_does_not():
    assert brand.warnings(brand.FACTORY) == []

    notes = brand.warnings(brand.resolve({"text": "#f2f2f2"}))
    assert notes and "contraste" in notes[0]


# --- the form -------------------------------------------------------------

@pytest.fixture
def admin(superadmin_id, sign_in):
    """The superadministrator. Named `admin` because every test below reads
    "signed in as the person who may change this", and that person is no longer
    an administrator: the look and the identity are the platform's."""
    sign_in(superadmin_id)
    return superadmin_id


# Arguments for the test client rather than fields of the form. Separated by
# name because the form has a field per token and the two would otherwise be
# posted together — `follow_redirects` as a form value is a quiet way for a
# test to assert against a redirect page it never followed.
CLIENT_ARGS = ("follow_redirects", "content_type")


def save(post, **fields):
    """Post the Marca form with every colour left blank but the ones given.

    Blank rather than absent because that is what the real form sends: a text
    field nobody filled in arrives as an empty string, and the handler has to
    read that as "leave it at the factory value".
    """
    sent = {name: fields.pop(name) for name in CLIENT_ARGS if name in fields}
    data = {f"token-{name}": "" for name in brand.FACTORY}
    data.update(fields)
    return post("/comunidad/gestion/marca", data, **sent)


def test_only_the_superadmin_reaches_it(client, post, make_member, sign_in):
    """An administrator is refused too, which is the change: the colours and the
    identity belong to whoever runs the platform, not to the association's
    committee."""
    for role in ("user", "moderator", "admin", "owner"):
        sign_in(make_member(f"persona-{role}", role=role))
        assert client.get("/comunidad/gestion/marca").status_code == 403
        assert save(post, **{"token-brand": "#1b5e20"}).status_code == 403
        assert post("/comunidad/gestion/marca/restaurar").status_code == 403
        assert post("/comunidad/gestion/marca/publicar").status_code == 403


def test_a_colour_is_saved_and_served(client, post, admin):
    save(post, **{"token-brand": "#1B5E20"})

    css = client.get("/comunidad/marca.css")

    assert css.mimetype == "text/css"
    assert "--brand: #1b5e20;" in css.get_data(as_text=True)
    # Lower-cased on the way in, so the same colour is one string in the row.
    assert "#1B5E20" not in css.get_data(as_text=True)


def test_an_untouched_colour_is_not_written_at_all(client, db, post, admin):
    """Only differences are stored. A snapshot of all thirteen would freeze a
    token added later at whatever this version thought it should be."""
    save(post, **{"token-brand": brand.FACTORY["brand"], "token-text": "#111111"})

    stored = json.loads(db.execute("SELECT tokens_json FROM brand").fetchone()[0])

    assert stored == {"text": "#111111"}


def test_a_bad_colour_is_refused_field_by_field(client, db, post, admin):
    for bad in ("red", "#xyz", "url(https://otro.sitio/x.png)", "#12345"):
        response = save(post, **{"token-brand": bad}, follow_redirects=True)
        assert "no es un color" in response.get_data(as_text=True), bad
    assert db.execute("SELECT COUNT(*) AS n FROM brand").fetchone()["n"] == 0


def test_the_custom_css_is_last_so_it_wins(client, post, admin):
    save(post, **{"token-brand": "#1b5e20", "custom_css": ".site-bar { height: 4rem; }"})

    css = client.get("/comunidad/marca.css").get_data(as_text=True)

    assert css.index(":root") < css.index(".site-bar")


def test_an_html_bracket_in_the_custom_css_is_refused(client, db, post, admin):
    response = save(post, custom_css="</style><script>alert(1)</script>",
                    follow_redirects=True)

    assert "no puede contener" in response.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) AS n FROM brand").fetchone()["n"] == 0


def test_an_enormous_css_box_is_refused(client, db, post, admin):
    response = save(post, custom_css="a{}" * 20_000, follow_redirects=True)

    assert "caracteres" in response.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) AS n FROM brand").fetchone()["n"] == 0


def test_the_screen_never_loads_the_custom_css(client, post, admin):
    """The reason an admin can always undo a mistake: `* { display: none }` in
    the box breaks every page except this one."""
    save(post, custom_css="* { display: none }")

    page = client.get("/comunidad/gestion/marca").get_data(as_text=True)
    other = client.get("/comunidad/gestion").get_data(as_text=True)

    assert "marca.css" not in page
    assert "marca.css" in other
    assert "Restaurar lo de fábrica" in page


# --- the two pictures -----------------------------------------------------

def test_a_logo_is_sniffed_not_trusted(client, db, post, admin):
    """The same check as every other upload: the extension comes from the
    bytes. A GIF called logo.png is a GIF."""
    response = save(post, logo=(io.BytesIO(GIF), "logo.png"),
                    content_type="multipart/form-data", follow_redirects=True)

    assert "es un gif" in response.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) AS n FROM brand").fetchone()["n"] == 0


def test_a_png_logo_is_kept_and_served(app, client, db, post, admin):
    save(post, logo=(io.BytesIO(PNG), "vienalatina.png"),
         content_type="multipart/form-data")

    name = db.execute("SELECT logo_name FROM brand").fetchone()["logo_name"]
    assert name and name.endswith(".png")
    with app.app_context():
        from apps.board import uploads
        assert uploads.directory().joinpath(name).exists()

    # Public on purpose: it is in the <head> of the sign-in page.
    client.get("/comunidad/logout")
    assert client.get(f"/comunidad/marca/{name}").status_code == 200


def test_a_picture_that_is_not_the_current_one_is_not_served(client, db, post, admin):
    save(post, logo=(io.BytesIO(PNG), "uno.png"), content_type="multipart/form-data")
    first = db.execute("SELECT logo_name FROM brand").fetchone()["logo_name"]

    save(post, logo=(io.BytesIO(PNG), "dos.png"), content_type="multipart/form-data")

    assert client.get(f"/comunidad/marca/{first}").status_code == 404


def test_a_replaced_logo_goes_off_disk(app, client, db, post, admin):
    save(post, logo=(io.BytesIO(PNG), "uno.png"), content_type="multipart/form-data")
    first = db.execute("SELECT logo_name FROM brand").fetchone()["logo_name"]

    save(post, logo=(io.BytesIO(PNG), "dos.png"), content_type="multipart/form-data")

    with app.app_context():
        from apps.board import uploads
        assert not uploads.directory().joinpath(first).exists()


def test_the_favicon_is_png_only(client, db, post, admin):
    """ICO and SVG are both refused, for different reasons: the sniffer
    identifies bytes by a prefix and an SVG is XML that can carry script."""
    response = save(post, favicon=(io.BytesIO(GIF), "icono.gif"),
                    content_type="multipart/form-data", follow_redirects=True)

    assert "hace falta png" in response.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) AS n FROM brand").fetchone()["n"] == 0


def test_the_logo_and_the_favicon_reach_the_chrome(client, post, admin):
    save(post, logo=(io.BytesIO(PNG), "logo.png"),
         favicon=(io.BytesIO(PNG), "icono.png"),
         content_type="multipart/form-data")

    body = client.get("/comunidad/").get_data(as_text=True)

    assert "site-bar__logo" in body
    assert 'rel="icon"' in body
    assert "site-bar__brand-text" not in body


# --- publishing to the public site ----------------------------------------

@pytest.fixture(autouse=True)
def unreachable(monkeypatch):
    """No test in this file talks to a real git server.

    Autouse and not optional: every save tries to publish, so without this each
    one would open a socket to git.vienalatina.com and wait for it to fail. The
    tests that care about the commit ask for `repo` as well, whose patches are
    applied after these and therefore win.
    """
    def refuse(*args, **kwargs):
        raise brand.gitea.GiteaError("No se pudo contactar con el servidor de git.")
    for name in ("list_directory", "read_file", "write_file", "delete_file"):
        monkeypatch.setattr(brand.gitea, name, refuse)


def test_the_commit_carries_the_colours_and_the_pictures(db, post, admin, repo):
    save(post, **{"token-brand": "#1b5e20", "custom_css": ".x { color: red }"},
         logo=(io.BytesIO(PNG), "vienalatina.png"),
         favicon=(io.BytesIO(PNG), "icono.png"),
         content_type="multipart/form-data")

    assert set(repo.files) == {"data/brand.yaml",
                               "static/brand/logo.png",
                               "static/brand/favicon.png"}
    yaml = repo.files["data/brand.yaml"].decode("utf-8")
    assert '  brand: "#1b5e20"' in yaml
    assert 'logo: "/brand/logo.png"' in yaml        # the path Hugo serves it at
    assert 'favicon: "/brand/favicon.png"' in yaml
    assert "custom_css: |\n  .x { color: red }" in yaml
    # Only the differences, exactly as the stylesheet does it.
    assert "surface:" not in yaml
    assert db.execute("SELECT published_at FROM brand").fetchone()["published_at"]


def test_a_logo_in_another_format_does_not_leave_the_old_one_behind(post, admin, repo):
    """`static/brand/logo.png` and `logo.webp` would otherwise both sit in the
    repository, deployed, with only one of them referenced."""
    save(post, logo=(io.BytesIO(PNG), "uno.png"), content_type="multipart/form-data")
    assert "static/brand/logo.png" in repo.files

    webp = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
    save(post, logo=(io.BytesIO(webp), "dos.webp"), content_type="multipart/form-data")

    assert "static/brand/logo.webp" in repo.files
    assert "static/brand/logo.png" not in repo.files


def test_a_save_survives_the_git_server_being_down(client, db, post, admin):
    """The order this is written in. SQLite first, so a change that does not
    need git is not blocked by git — and the screen says the public site is a
    step behind rather than pretending it is not."""
    response = save(post, **{"token-brand": "#1b5e20"}, follow_redirects=True)

    row = db.execute("SELECT tokens_json, published_at FROM brand").fetchone()
    assert json.loads(row["tokens_json"]) == {"brand": "#1b5e20"}
    assert row["published_at"] is None
    assert "Pendiente de publicar" in response.get_data(as_text=True)
    assert "--brand: #1b5e20;" in client.get("/comunidad/marca.css").get_data(as_text=True)


def test_the_retry_publishes_what_was_already_saved(db, post, admin, monkeypatch, repo):
    """What the *Reintentar publicar* button is for: the colours are in SQLite
    and the commit never happened."""
    def refuse(*args, **kwargs):
        raise brand.gitea.GiteaError("caído")

    # Only the write, and put it back by hand rather than with
    # `monkeypatch.undo()` — which would also undo the `repo` fixture's own
    # patches and leave the retry talking to the real git server.
    working = brand.gitea.write_file
    monkeypatch.setattr(brand.gitea, "write_file", refuse)
    save(post, **{"token-brand": "#1b5e20"})
    assert db.execute("SELECT published_at FROM brand").fetchone()["published_at"] is None

    monkeypatch.setattr(brand.gitea, "write_file", working)
    post("/comunidad/gestion/marca/publicar")

    assert '  brand: "#1b5e20"' in repo.files["data/brand.yaml"].decode("utf-8")
    assert db.execute("SELECT published_at FROM brand").fetchone()["published_at"]


# --- restoring ------------------------------------------------------------

def test_restoring_empties_the_row_and_the_disk(app, client, db, post, admin):
    save(post, **{"token-brand": "#1b5e20"}, )
    save(post, logo=(io.BytesIO(PNG), "logo.png"), content_type="multipart/form-data")
    name = db.execute("SELECT logo_name FROM brand").fetchone()["logo_name"]

    post("/comunidad/gestion/marca/restaurar")

    assert db.execute("SELECT COUNT(*) AS n FROM brand").fetchone()["n"] == 0
    with app.app_context():
        from apps.board import uploads
        assert not uploads.directory().joinpath(name).exists()
    css = client.get("/comunidad/marca.css").get_data(as_text=True)
    assert ":root" not in css                   # nothing to override any more
    assert "site-bar__brand-text" in client.get("/comunidad/").get_data(as_text=True)


# --- erasure --------------------------------------------------------------

def test_erasing_whoever_set_the_brand_leaves_it_standing(client, db, post, admin,
                                                          make_member):
    """The colours belong to the association, like an event in the calendar.
    Whoever set them leaving is not a reason for the site to look like a default
    install again.

    The row erased is a former superadministrator who has since handed the
    platform on and is an admin again — which is the only way a row that set the
    brand can become erasable at all, since neither chair can be erased."""
    former = make_member("anterior", role="admin")
    save(post, **{"token-brand": "#1b5e20"})
    db.execute("UPDATE brand SET updated_by = ? WHERE id = 1", (former,))

    post(f"/comunidad/miembros/{former}/eliminar")

    row = db.execute("SELECT tokens_json, updated_by FROM brand").fetchone()
    assert json.loads(row["tokens_json"]) == {"brand": "#1b5e20"}
    assert row["updated_by"] is None
