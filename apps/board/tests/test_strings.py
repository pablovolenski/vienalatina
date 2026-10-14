"""The words the software says, and the screen that changes them.

The first test in this file is the one that matters most, and it is the reason
extracting five hundred strings is mechanical rather than risky: every `t("…")`
used anywhere must exist in the catalogue, and every key in the catalogue must
be used somewhere. A typo and an orphan are the only two failure modes of a
change this shape — one renders `nav.incio` on a page, the other grows a field
in Gestión that changes nothing — and both are a red test here instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from apps.board import strings

ROOT = Path(__file__).resolve().parents[3]
BOARD = Path(__file__).resolve().parents[1]
THEME = ROOT / "themes" / "vienalatina"

# `t('nav.inicio')` and `strings.t("nav.inicio", ...)`, in templates and Python.
USED = re.compile(r"""\bt\(\s*['"]([a-z][\w.]*)['"]""")


def _keys_used() -> dict[str, set[str]]:
    """Every key referenced, and the files that reference it."""
    where: dict[str, set[str]] = {}
    for path in [*BOARD.glob("*.py"), *(BOARD / "templates").glob("*.html")]:
        if path.name.startswith("test_"):
            continue
        for key in USED.findall(path.read_text(encoding="utf-8")):
            where.setdefault(key, set()).add(path.name)
    return where


def test_every_key_used_exists_in_the_catalogue():
    """A typo in a key renders as the key itself, which is the right failure on
    a page and a silent one in a review."""
    unknown = {key: sorted(files) for key, files in _keys_used().items()
               if key not in strings.catalogue()}
    assert not unknown, f"used but not defined: {unknown}"


def test_every_key_in_the_catalogue_is_used():
    """The other direction, and the one that keeps the screen honest: an
    orphaned key is a box in Gestión that somebody fills in and that changes
    nothing on any page."""
    used = set(_keys_used())
    # The public half is used by the theme, in Go templates, not by the app.
    for path in THEME.rglob("*.html"):
        for key in re.findall(r'"key"\s+"([\w.]+)"', path.read_text(encoding="utf-8")):
            used.add(strings.PUBLIC + key)
    orphans = sorted(set(strings.catalogue()) - used)
    assert not orphans, f"defined but never shown: {orphans}"


def test_every_theme_call_site_passes_the_page_explicitly():
    """`$.Site.Language.Lang`, never `.Site.Language.Lang`.

    Inside a `{{ with }}` the dot is whatever that block holds — `.Params.author`
    is a string, `.Params.faq` is a slice — and `.Site` on a string is an
    execution error, not a wrong word. Two call sites shipped exactly that way,
    and the build only went red on a post that happened to carry an `author`:
    every post in the test fixture had neither, so a clean build proved nothing.

    `$` is the page in every context, so the rule is "always `$.`" rather than
    "think about where you are", and this is what holds it.
    """
    offenders = []
    for path in THEME.rglob("*"):
        if not path.is_file() or path.suffix in (".css", ".js", ".toml"):
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if 't.html"' in line and re.search(r'"lang"\s+\.', line):
                offenders.append(f"{path.relative_to(THEME)}:{line_no}")
    assert not offenders, (
        "these pass `.Site…` to t.html instead of `$.Site…`, which fails the "
        f"build inside a `with`: {offenders}")


def test_the_theme_and_the_catalogue_agree_on_the_public_defaults():
    """Two copies, deliberately — Hugo reads the repository and the members area
    reads its own image, and neither can read the other's file. The same trade
    `brand.FACTORY` makes with the two `:root` blocks, held together the same
    way: by a test that fails rather than by a comment that asks nicely.
    """
    source = (THEME / "layouts" / "partials" / "t.html").read_text(encoding="utf-8")
    block = source.split("$words := dict", 1)[1].split("\n-}}", 1)[0]
    theme = {}
    for key, body in re.findall(r'"([\w-]+)"\s+\(dict((?:\s*"[\w-]+"\s*"[^"]*")+)\s*\)',
                                block):
        theme[key] = dict(re.findall(r'"([\w-]+)"\s*"([^"]*)"', body))

    assert len(theme) > 15, f"only parsed {len(theme)} keys out of t.html — did it move?"
    for key, entry in sorted(strings.catalogue().items()):
        if not strings.is_public(key):
            continue
        bare = key[len(strings.PUBLIC):]
        assert bare in theme, f"{key} is in the catalogue and not in t.html"
        for lang, text in entry["langs"].items():
            assert theme[bare].get(lang) == text, (
                f"{key} [{lang}]: t.html says {theme[bare].get(lang)!r} and the "
                f"catalogue says {text!r}. One of the two is wrong.")
    extra = sorted(set(theme) - {k[len(strings.PUBLIC):] for k in strings.catalogue()})
    assert not extra, f"t.html has keys the screen cannot reach: {extra}"


# --- the lookup -----------------------------------------------------------

def test_the_catalogue_is_what_the_software_says(app):
    with app.test_request_context():
        assert strings.t("nav.inicio") == "Inicio"
        assert strings.t("site.share", "de") == "Teilen"


def test_an_unknown_key_renders_as_itself(app):
    """`t.html` has done this since it was written, and for the same reason: a
    typo on the page is a typo somebody reports."""
    with app.test_request_context():
        assert strings.t("nav.incio") == "nav.incio"


def test_an_override_wins_and_a_blank_one_falls_back(app, db, superadmin_id):
    db.execute("INSERT INTO strings (id, strings_json) VALUES (1, ?)",
               (json.dumps({"nav.inicio": {"es": "Escritorio"}}),))
    with app.test_request_context():
        assert strings.t("nav.inicio") == "Escritorio"
        assert strings.t("nav.muro") == "Muro"


def test_rewording_spanish_says_nothing_about_german(app, db):
    """The fallback order, and it is a judgement rather than an accident: an
    override for the asked-for language wins, then the *shipped* word in that
    language, before the Spanish override. Somebody who reworded only the
    Spanish has said nothing about German, and showing a German reader Spanish
    would be reading an intention into silence."""
    db.execute("INSERT INTO strings (id, strings_json) VALUES (1, ?)",
               (json.dumps({"site.share": {"es": "Pasar a alguien"}}),))
    with app.test_request_context():
        assert strings.t("site.share") == "Pasar a alguien"
        assert strings.t("site.share", "de") == "Teilen"


def test_a_string_that_takes_a_value_formats_it(app):
    with app.test_request_context():
        assert strings.t("nav.inicio", nombre="x") == "Inicio"


def test_a_string_edited_into_a_placeholder_does_not_take_the_page_down(app, db):
    """Somebody types «{nombre}» into a box. The page must still render, and it
    must not render half a sentence either."""
    db.execute("INSERT INTO strings (id, strings_json) VALUES (1, ?)",
               (json.dumps({"nav.inicio": {"es": "Hola {quien}"}}),))
    with app.test_request_context():
        assert strings.t("nav.inicio", nombre="Pablo") == "Hola {quien}"


def test_the_catalogue_refuses_a_language_this_site_does_not_have(tmp_path, monkeypatch):
    """Caught at load, which is start-up, rather than as a missing word months
    later."""
    (tmp_path / "x.yaml").write_text('a.b:\n  es: "Hola"\n  fr: "Bonjour"\n',
                                     encoding="utf-8")
    monkeypatch.setattr(strings, "CATALOGUE_DIR", tmp_path)
    monkeypatch.setattr(strings, "_CATALOGUE", None)
    with pytest.raises(ValueError, match="not languages"):
        strings.catalogue()


def test_the_catalogue_refuses_an_entry_with_no_spanish(tmp_path, monkeypatch):
    (tmp_path / "x.yaml").write_text('a.b:\n  de: "Hallo"\n', encoding="utf-8")
    monkeypatch.setattr(strings, "CATALOGUE_DIR", tmp_path)
    monkeypatch.setattr(strings, "_CATALOGUE", None)
    with pytest.raises(ValueError, match="no Spanish"):
        strings.catalogue()


# --- the screen -----------------------------------------------------------

@pytest.fixture
def boss(superadmin_id, sign_in):
    sign_in(superadmin_id)
    return superadmin_id


def test_a_renamed_section_shows_up_everywhere_at_once(client, post, boss, repo):
    """The navigation, and the strip the public site draws from `sesion.json`.
    Those were two copies of six words until this phase; a rename that reached
    one and not the other is the failure this is shaped around."""
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Escritorio"})

    assert "Escritorio" in client.get("/comunidad/").get_data(as_text=True)
    session = client.get("/comunidad/sesion.json").get_json()
    assert session["sections"][0]["label"] == "Escritorio"


def test_only_what_was_typed_is_stored(client, db, post, boss, repo):
    """The form posts every box it drew, almost all of them empty. Saving the
    shipped wording back into the row would freeze it — a later version's better
    sentence would never arrive."""
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Escritorio",
                                                "nav.muro:es": ""})

    stored = json.loads(db.execute("SELECT strings_json FROM strings").fetchone()[0])
    assert stored == {"nav.inicio": {"es": "Escritorio"}}


def test_typing_the_default_back_in_stores_nothing(client, db, post, boss, repo):
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Inicio"})

    stored = json.loads(db.execute("SELECT strings_json FROM strings").fetchone()[0])
    assert stored == {}


def test_clearing_a_box_goes_back_to_the_shipped_word(client, db, post, boss, repo):
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Escritorio"})
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": ""})

    stored = json.loads(db.execute("SELECT strings_json FROM strings").fetchone()[0])
    assert stored == {}
    assert "Inicio" in client.get("/comunidad/").get_data(as_text=True)


def test_saving_one_area_does_not_blank_another(client, db, post, boss, repo):
    """The screen is one page per area, so a save sees a fraction of the keys.
    What it did not draw it must leave alone."""
    post("/comunidad/gestion/textos?area=sitio", {"site.share:es": "Pasar"})
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Escritorio"})

    stored = json.loads(db.execute("SELECT strings_json FROM strings").fetchone()[0])
    assert stored == {"site.share": {"es": "Pasar"},
                      "nav.inicio": {"es": "Escritorio"}}


def test_a_label_is_flattened_to_one_line(client, db, post, boss, repo):
    """A newline in a menu entry is a menu that renders raggedly and nobody can
    see why. Long explanations keep theirs — see `row.long`."""
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Escri\n\ntorio"})

    stored = json.loads(db.execute("SELECT strings_json FROM strings").fetchone()[0])
    assert stored["nav.inicio"]["es"] == "Escri torio"


def test_the_public_half_is_written_for_hugo(client, post, boss, repo):
    post("/comunidad/gestion/textos?area=sitio",
         {"site.home:es": "Escritorio", "site.home:de": "Schreibtisch",
          "site.share:es": "Pasar a alguien"})

    written = repo.files["data/ui.yaml"].decode("utf-8")
    assert 'home:\n  es: "Escritorio"\n  de: "Schreibtisch"' in written
    assert '  pt-br:' not in written, "an empty language must be left for the pipeline"
    assert 'share:\n  es: "Pasar a alguien"' in written


def test_the_members_area_half_is_not_written_for_hugo(client, post, boss, repo):
    """`nav.*` is this software's own chrome. Committing it would put words the
    public site never says into the repository it builds from."""
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Escritorio"})

    written = repo.files["data/ui.yaml"].decode("utf-8")
    assert "inicio" not in written and "Escritorio" not in written


def test_a_quote_in_a_word_cannot_break_the_file(client, post, boss, repo):
    post("/comunidad/gestion/textos?area=sitio",
         {"site.home:es": 'El "Escritorio"\\'})

    written = repo.files["data/ui.yaml"].decode("utf-8")
    assert r'es: "El \"Escritorio\"\\"' in written


def test_a_save_survives_the_git_server_being_down(client, db, post, boss):
    """SQLite first, so the members area's own words are never blocked by git —
    and the screen says the public site is a step behind rather than pretending
    it is not. No `repo` fixture here, so the git layer refuses."""
    response = post("/comunidad/gestion/textos?area=nav",
                    {"nav.inicio:es": "Escritorio"}, follow_redirects=True)

    row = db.execute("SELECT strings_json, published_at FROM strings").fetchone()
    assert json.loads(row["strings_json"]) == {"nav.inicio": {"es": "Escritorio"}}
    assert row["published_at"] is None
    assert "Pendiente de publicar" in response.get_data(as_text=True)
    assert "Escritorio" in client.get("/comunidad/").get_data(as_text=True)


def test_the_retry_publishes_what_was_already_saved(db, post, boss, monkeypatch, repo):
    """What the *Reintentar publicar* button is for: the words are in SQLite
    and the commit never happened."""
    from apps.board import gitea, ui

    def boom(*args, **kwargs):
        raise gitea.GiteaError("caído")

    # Only the write, and put it back by hand rather than with
    # `monkeypatch.undo()` — which would also undo the `repo` fixture's own
    # patches and leave the retry talking to the real git server.
    working = ui.gitea.write_file
    monkeypatch.setattr(ui.gitea, "write_file", boom)
    post("/comunidad/gestion/textos?area=sitio", {"site.home:es": "Escritorio"})
    assert db.execute("SELECT published_at FROM strings").fetchone()["published_at"] is None

    monkeypatch.setattr(ui.gitea, "write_file", working)
    post("/comunidad/gestion/textos/publicar")

    assert 'es: "Escritorio"' in repo.files["data/ui.yaml"].decode("utf-8")
    assert db.execute("SELECT published_at FROM strings").fetchone()["published_at"]


def test_restoring_puts_every_word_back(client, db, post, boss, repo):
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Escritorio"})
    post("/comunidad/gestion/textos/restaurar")

    assert db.execute("SELECT COUNT(*) AS n FROM strings").fetchone()["n"] == 0
    assert "Inicio" in client.get("/comunidad/").get_data(as_text=True)


def test_the_search_finds_a_word_across_areas(client, boss, repo):
    page = client.get("/comunidad/gestion/textos?area=todo&q=compartir").get_data(as_text=True)
    assert "site.share" in page
    assert "nav.inicio" not in page


def test_erasing_whoever_chose_the_words_leaves_them_standing(client, db, post, boss,
                                                              make_member, repo):
    """The words belong to the association, like the colours and the calendar."""
    former = make_member("anterior", role="admin")
    post("/comunidad/gestion/textos?area=nav", {"nav.inicio:es": "Escritorio"})
    db.execute("UPDATE strings SET updated_by = ? WHERE id = 1", (former,))

    post(f"/comunidad/miembros/{former}/eliminar")

    row = db.execute("SELECT strings_json, updated_by FROM strings").fetchone()
    assert json.loads(row["strings_json"]) == {"nav.inicio": {"es": "Escritorio"}}
    assert row["updated_by"] is None
