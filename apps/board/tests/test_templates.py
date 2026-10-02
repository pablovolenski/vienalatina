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
