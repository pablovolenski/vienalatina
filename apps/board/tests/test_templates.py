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
