"""Turning what members type into HTML.

`html=False` is the whole security model, and it is worth understanding rather
than copying. With raw HTML disabled, markdown-it never passes a fragment of
the input through untouched: it emits only the tags its own rules produce, and
everything else is escaped as text. A `<script>` in a post comes out as visible
characters, not as a tag.

That is why there is no sanitiser here. A sanitiser is what you need when you
have decided to allow *some* HTML and must then decide which — a judgement with
a long history of near misses. Allowing none is a smaller thing to get right.

Anyone tempted to set `html=True` later to embed a video: that single flag
turns this file into an XSS hole, and re-enabling it means adding a sanitiser
and owning its allowlist forever.
"""

from __future__ import annotations

from markdown_it import MarkdownIt
from markupsafe import Markup

_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": True})
    .enable("linkify")
    .enable("table")
    .enable("strikethrough")
)


def to_html(text: str) -> Markup:
    return Markup(_md.render(text or ""))


def excerpt(text: str, limit: int = 220) -> str:
    """A plain-text preview for the thread list — no markup, no truncated tags."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + " …"
