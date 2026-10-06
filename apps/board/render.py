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

def _renderer(breaks: bool) -> MarkdownIt:
    return (
        MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": breaks})
        .enable("linkify")
        .enable("table")
        .enable("strikethrough")
    )


# Two of them, because a line break means two different things depending on
# where the text is going.
#
# On the wall, in a private message, in a calendar entry — text this app both
# stores and renders — people write the way they write to each other, and a
# newline they typed is a newline they meant. `breaks=True`.
#
# A public post is a file in the repository rendered by Hugo, whose Goldmark
# has hardWraps off; and the files are hard-wrapped at about 78 characters,
# by the pipeline and by anything that has ever edited them. Rendering those
# with breaks on puts a line break in the middle of every sentence, so the
# editor would show something the site will never produce. `breaks=False`
# there, and a break somebody actually wants is written the way CommonMark
# writes one — two spaces at the end of the line — which both renderers honour.
_md = _renderer(True)
_md_site = _renderer(False)


def to_html(text: str, breaks: bool = True) -> Markup:
    """Markdown as HTML. `breaks=False` for anything bound for the public site."""
    return Markup((_md if breaks else _md_site).render(text or ""))


def excerpt(text: str, limit: int = 220) -> str:
    """A plain-text preview for the thread list — no markup, no truncated tags."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + " …"
