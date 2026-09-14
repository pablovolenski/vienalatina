"""Markdown-safe translation.

DeepL preserved markup server-side with tag_handling=html. A self-hosted NMT
model has no equivalent, so structure is protected here instead: non-prose
blocks pass through untouched, and inline constructs are masked with opaque
placeholders whose survival is verified after the round trip.

Placeholders use OpenNMT's protected-sequence convention (U+FF5F/U+FF60).
SentencePiece keeps these atomic; ``{{x}}``, ``<x>`` and ``%s`` get fragmented
by BPE and dropped by the model.
"""

from __future__ import annotations

import re

from .provider import SITE_TO_MODEL, Provider

OPEN, CLOSE = "｟", "｠"

# Community vocabulary that must reach readers unchanged. Not inherited from
# the WordPress plugin, which had no glossary at all — edit freely.
PROTECTED_TERMS = [
    "Viena Latina",
    "Grätzl",
    "empanadas de viento",
    "Naschmarkt",
]

_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HEADING = re.compile(r"^(#{1,6}\s+)(.*)$")
_LIST = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
_QUOTE = re.compile(r"^(\s*>\s?)(.*)$")
_HTML_BLOCK = re.compile(r"^\s*<")
_PREFIXED = (_HEADING, _LIST, _QUOTE)

# Inline spans that must never reach the model. Order matters: inline code is
# taken first so a URL inside backticks is masked once, not twice.
_INLINE = (
    re.compile(r"`[^`]*`"),        # inline code
    re.compile(r"\]\([^)]*\)"),    # link/image target — the label stays translatable
    re.compile(r"<[^>\s][^>]*>"),  # raw HTML tags, autolinks
    re.compile(r"https?://\S+"),   # bare URLs
)

_PLACEHOLDER = re.compile(re.escape(OPEN) + r"\s*(\d+)\s*" + re.escape(CLOSE))


class PlaceholderError(RuntimeError):
    """A masked span did not survive translation intact."""


class _Masker:
    def __init__(self) -> None:
        self.spans: list[str] = []

    def _take(self, match: re.Match) -> str:
        self.spans.append(match.group(0))
        return f"{OPEN}{len(self.spans) - 1}{CLOSE}"

    def mask(self, text: str) -> str:
        for pattern in _INLINE:
            text = pattern.sub(self._take, text)
        for term in PROTECTED_TERMS:
            text = re.sub(re.escape(term), self._take, text, flags=re.IGNORECASE)
        return text

    def restore(self, text: str) -> str:
        # Models pad and reorder placeholders; normalise spacing before matching.
        text = _PLACEHOLDER.sub(lambda m: f"{OPEN}{m.group(1)}{CLOSE}", text)
        for index, span in enumerate(self.spans):
            token = f"{OPEN}{index}{CLOSE}"
            seen = text.count(token)
            if seen != 1:
                raise PlaceholderError(
                    f"masked span {span!r} came back {seen} times, expected once"
                )
            text = text.replace(token, span)
        return text


def _sentences(text: str, lang: str) -> list[str]:
    from sentencex import segment

    return [s.strip() for s in segment(SITE_TO_MODEL[lang], text) if s.strip()]


def translate_text(text: str, src: str, tgt: str, provider: Provider) -> str:
    """Translate one prose string, protecting inline markup and fixed terms."""
    if not text.strip():
        return text
    masker = _Masker()
    pieces = _sentences(masker.mask(text), src)
    if not pieces:
        return text
    return masker.restore(" ".join(provider.translate(pieces, src, tgt)))


def _is_prose(line: str) -> bool:
    return bool(
        line.strip()
        and not _FENCE.match(line)
        and not _HTML_BLOCK.match(line)
        and not any(p.match(line) for p in _PREFIXED)
    )


def translate_markdown(body: str, src: str, tgt: str, provider: Provider) -> str:
    """Translate a markdown body, leaving every non-prose construct intact."""
    lines = body.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if _FENCE.match(line):
            out.append(line)
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                out.append(lines[i])
                i += 1
            if i < len(lines):
                out.append(lines[i])
                i += 1
            continue

        if not line.strip() or _HTML_BLOCK.match(line):
            out.append(line)
            i += 1
            continue

        prefixed = next((m for m in (p.match(line) for p in _PREFIXED) if m), None)
        if prefixed:
            out.append(prefixed.group(1) + translate_text(prefixed.group(2), src, tgt, provider))
            i += 1
            continue

        # A soft-wrapped paragraph: rejoin it so sentences are translated whole.
        para: list[str] = []
        while i < len(lines) and _is_prose(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(translate_text(" ".join(para), src, tgt, provider))

    return "\n".join(out)
