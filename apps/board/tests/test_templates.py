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
    deleted by accident. Use data-confirm, handled in static/board.js."""
    found = INLINE_HANDLER.findall(template.read_text(encoding="utf-8"))
    assert not found, f"{template.name} has inline handler(s): {found}"


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
