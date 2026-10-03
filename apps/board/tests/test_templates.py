"""Guards on the templates themselves.

These check one thing that no request-level test can: the Content-Security-
Policy makes a whole category of markup silently inert rather than broken, so
nothing at runtime will ever fail to tell you about it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = sorted((Path(__file__).resolve().parents[1] / "templates").glob("*.html"))
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_inline_event_handlers(template):
    """`default-src 'self'` with no 'unsafe-inline' means the browser ignores
    an onsubmit="" attribute without complaining. A delete button written that
    way loses its confirmation dialog and nobody finds out until something is
    deleted by accident. Use data-confirm, handled in static/board.js.

    Comments stripped first, like the two guards below: base.html explains in a
    Jinja comment why its account menu is a <details> rather than a button with
    an onclick, and a guard that fails on its own reasoning gets deleted rather
    than obeyed."""
    found = INLINE_HANDLER.findall(
        JINJA_COMMENT.sub("", template.read_text(encoding="utf-8")))
    assert not found, f"{template.name} has inline handler(s): {found}"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_inline_style_blocks(template):
    """The same policy, the same silence, a different symptom.

    `default-src 'self'` covers style-src too, and 'self' does not permit an
    inline <style>. The browser drops the whole block without a word, and the
    page arrives in Times New Roman with blue links — which is exactly how the
    public profile page shipped. Put it in static/ and link it, where the
    policy allows it and the browser caches it.
    """
    # Comments stripped first: profile.html explains in a Jinja comment why it
    # has no <style> block, and a guard that trips on its own documentation is
    # a guard people delete.
    html = JINJA_COMMENT.sub("", template.read_text(encoding="utf-8"))
    assert "<style" not in html, (
        f"{template.name} has an inline <style> block, which the CSP ignores. "
        "Move it to apps/board/static/ and link it with url_for('static', …)."
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_table_cell_is_told_to_be_a_flex_container(template):
    """`.actions` is display:flex, and a <td> given it stops being a table cell.

    It leaves the row's layout, so its bottom border is drawn at its own content
    height rather than the row's and the columns' rules no longer meet. Nothing
    errors, nothing logs, the page just looks subtly broken — which is how it
    survived two rounds of somebody looking straight at it. The flex row belongs
    in a <div> inside the cell.
    """
    html = JINJA_COMMENT.sub("", template.read_text(encoding="utf-8"))
    offenders = re.findall(r"<td[^>]*class=[\"'][^\"']*\bactions\b[^\"']*[\"']", html)
    assert not offenders, (
        f"{template.name} puts a flex class on a <td>: {offenders}. "
        "Wrap the buttons in <div class=\"actions\"> inside the cell instead."
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_post_form_carries_a_csrf_token(template):
    """The hook in app.py rejects a POST without one, so a form that forgets it
    is a button that always fails — and fails with a 400 that reads like the
    page is broken rather than like a missing field."""
    html = template.read_text(encoding="utf-8")
    forms = re.findall(r"<form[^>]*method=[\"']post[\"'][^>]*>(.*?)</form>", html,
                       re.IGNORECASE | re.DOTALL)
    for form in forms:
        assert "csrf_token" in form, f"{template.name} has a POST form without a CSRF token"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_nothing_links_to_the_account_server_logout(template):
    """A link there is a GET, and that route is POST-only, so the browser gets
    a 404 and the session it was meant to end carries on. We shipped exactly
    that and it went unnoticed for a week, because a dead link on a page nobody
    reaches twice looks like nothing at all.

    It cannot be fixed by turning the link into a form either: the POST needs a
    CSRF token belonging to that other domain, which is unreadable from here by
    design. The page has to tell the member what to do instead."""
    html = JINJA_COMMENT.sub("", template.read_text(encoding="utf-8"))
    assert "/user/logout" not in html, (
        f"{template.name} links to /user/logout, which answers 404 to a GET"
    )


# --- the name of the software behind the login ---------------------------
#
# Members sign in through an OAuth provider that happens to be Gitea. They are
# never told so: as far as anyone using vienalatina.com is concerned there is
# one site, and a stray brand name in an error message is the seam showing.
# Comments and docstrings are exempt — the code has to stay honest about what
# it talks to, and neither reaches a browser.

JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_template_shows_the_name_of_the_account_server(template):
    body = JINJA_COMMENT.sub("", template.read_text(encoding="utf-8"))
    assert "Gitea" not in body, (
        f"{template.name} shows 'Gitea' to the member; call it el servidor de "
        "cuentas, or put the remark in a {# Jinja comment #}"
    )


def test_no_message_in_the_code_shows_it_either():
    """String literals only, docstrings excluded, and case-sensitive on
    purpose: `gitea_login` and `gitea_tokens` are column names nobody sees, so
    only the capitalised prose form is worth failing on."""
    import ast

    offenders = []
    for path in sorted(Path(__file__).resolve().parents[1].glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            doc for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef))
            and (doc := ast.get_docstring(node, clean=False))
        }
        offenders += [
            f"{path.name}:{node.lineno}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and "Gitea" in node.value and node.value not in docstrings
        ]
    assert not offenders, "\n".join(offenders)


# --- the writing boxes opt in ---------------------------------------------

PROSE_BOXES = {
    "thread_form.html": "body",
    "comment_form.html": "body",
    "thread.html": "body",
    "member.html": "body",
    "submission_form.html": "body",
    "content_form.html": "body",
    "profile_edit.html": "bio",
}


@pytest.mark.parametrize("name,box", sorted(PROSE_BOXES.items()))
def test_every_prose_box_has_the_markdown_toolbar(name, box):
    """`data-markdown` is what board.js attaches the toolbar and the preview to.
    A box that forgets it loses both silently — the page still works, it is just
    the plain textarea it always was, which nobody files as a bug."""
    html = (Path(__file__).resolve().parents[1] / "templates" / name).read_text(encoding="utf-8")
    opening = re.search(rf'<textarea id="{box}"[^>]*>', html)
    assert opening, f"{name} has no textarea #{box}"
    assert "data-markdown" in opening.group(0), (
        f"{name}: #{box} is prose and should carry data-markdown")


def test_the_fields_that_are_not_prose_stay_plain():
    """The links and FAQ boxes are parsed line by line, not rendered. A bold
    button there would put asterisks into a value, not into writing."""
    for name, box in (("profile_edit.html", "links"), ("content_form.html", "faq")):
        html = (Path(__file__).resolve().parents[1] / "templates" / name).read_text(encoding="utf-8")
        opening = re.search(rf'<textarea id="{box}"[^>]*>', html)
        assert opening and "data-markdown" not in opening.group(0), name


# --- the form controls are named by what they are not ---------------------

UNSTYLED_ON_PURPOSE = {"checkbox", "radio", "file", "submit",
                       "button", "reset", "image", "hidden"}


def test_the_form_rule_is_an_exclusion_and_not_a_list_of_accepted_types():
    """It used to be `input[type=text], input[type=email], input[type=date],
    input:not([type])` — an allowlist, which is wrong the moment anybody adds a
    field. It was: the password box on the sign-in page was a browser default
    sitting under a full-width username box, and the number box for a page's
    order was the same. Nothing errors; the form just looks unfinished, which
    only a person looking at it can catch.

    So the guard is on the shape of the rule rather than on any one type: it has
    to exclude the controls that are not text boxes and style everything else.
    """
    css = (Path(__file__).resolve().parents[1] / "static" / "board.css").read_text(encoding="utf-8")
    selector = [line for line in css.splitlines() if line.startswith("input:not(")]
    assert selector, (
        "board.css has no `input:not(...)` form rule. If it is back to listing "
        "accepted types, the next field somebody adds will be unstyled."
    )
    excluded = set(re.findall(r'\[type="([a-z]+)"\]', selector[0]))
    assert excluded == UNSTYLED_ON_PURPOSE, (
        f"the stylesheet excludes {sorted(excluded)}; expected "
        f"{sorted(UNSTYLED_ON_PURPOSE)}. Anything a person types in belongs in "
        "the rule, not outside it."
    )
    for typed_in in ("password", "number", "email", "date", "text", "search", "url"):
        assert typed_in not in excluded, f"{typed_in} is a box somebody types in"


# --- the theme's own chrome -----------------------------------------------
#
# These two read the Hugo theme rather than the members area, which no other
# test here does. They are here because the bug they guard against is this
# suite's recurring shape — CSS that expects markup nobody wrote — and because
# pytest is the only thing in this repository that runs on every commit.

THEME = Path(__file__).resolve().parents[3] / "themes" / "vienalatina"


def test_the_collapsed_menu_has_something_that_can_open_it():
    """The one that shipped: `main.css` hid `.site-bar__pages` below 768px and
    showed it again on a toggle, the hamburger's own styles were ported with
    it, and `header.html` never got the button. For three phases the public
    site had no navigation at all on a phone — every rule correct, every
    selector pointing at an element that did not exist.

    So: if the stylesheet hides the menu behind a state, the header has to carry
    that state and the control that flips it.
    """
    css = (THEME / "assets" / "css" / "main.css").read_text(encoding="utf-8")
    header = (THEME / "layouts" / "partials" / "header.html").read_text(encoding="utf-8")

    assert ".site-menu__state:checked ~ .site-bar__pages" in css, (
        "main.css no longer opens the mobile menu from the checkbox. If the "
        "mechanism changed, change this test with it — but check first that the "
        "menu still opens on a phone."
    )
    for needed in ('id="site-menu"', 'class="site-menu__state"', 'for="site-menu"'):
        assert needed in header, (
            f"header.html has no {needed}. The CSS hides the menu on a phone and "
            "only shows it again on that checkbox, so without it the site has no "
            "navigation below 768px."
        )


def test_the_way_into_the_members_area_is_in_the_bar_itself():
    """Not in the menu. It was a menu entry, which on a phone put it inside a
    menu that did not open — and even on a desktop made the one link that is not
    content look like one of the pages."""
    header = (THEME / "layouts" / "partials" / "header.html").read_text(encoding="utf-8")
    bar = header.split('<nav class="site-bar__pages"')[0]
    assert "site-bar__cta" in bar and "/comunidad/" in bar
