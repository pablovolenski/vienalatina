"""The writing box: what comes back out of it.

The editor shows formatting as it is typed, but markdown is still the only
thing stored — so the one piece of new logic that can lose somebody's work is
the walk from the document back to markdown. It is a pure function, and this
runs it for real, in node, against the same `static/board.js` the browser
loads. No second implementation, no mock of the thing under test.

The last test is the one that matters most: markdown → HTML → markdown has to
mean the same thing at both ends, for every document a member can produce.
Exact string equality is the wrong assertion there (escaping is a choice, not a
meaning), so it compares what the two render to.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from apps.board.render import to_html

HARNESS = Path(__file__).resolve().parent / "serialize_harness.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is not installed here; the serializer is JavaScript")


def serialize(*nodes) -> str:
    """The markdown the editor would put in the textarea for these nodes."""
    done = subprocess.run([NODE, str(HARNESS)], input=json.dumps(list(nodes)),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def tag(name, *children, **attrs):
    return {"tag": name, "attrs": attrs, "children": list(children)}


class _Tree(HTMLParser):
    """HTML into the same shape `tag()` builds, for the round-trip test."""

    VOID = {"br", "hr", "img", "input"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = [{"tag": "root", "attrs": {}, "children": []}]

    def handle_starttag(self, name, attrs):
        node = {"tag": name, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if name not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, name):
        if len(self.stack) > 1 and self.stack[-1]["tag"] == name:
            self.stack.pop()

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


def parse(html: str) -> list:
    tree = _Tree()
    tree.feed(html)
    return tree.stack[0]["children"]


# --- one document at a time ------------------------------------------------

def test_a_paragraph_keeps_its_emphasis():
    assert serialize(tag("p", "Hola ", tag("strong", "mundo"), ".")) == "Hola **mundo**."


def test_the_three_heading_levels():
    assert serialize(tag("h2", "Título"), tag("h3", "Subtítulo"),
                     tag("h4", "Apartado")) == "## Título\n\n### Subtítulo\n\n#### Apartado"


def test_a_pasted_h1_does_not_compete_with_the_post_title():
    """The page's own heading is the title. Two h1s is the one hierarchy
    mistake a screen reader cannot work around."""
    assert serialize(tag("h1", "Pegado")) == "## Pegado"


def test_a_space_inside_the_markers_moves_outside_them():
    """«** negrita **» is not bold in CommonMark, and a double click almost
    always takes the space after the word with it."""
    assert serialize(tag("p", tag("strong", "negrita "), "y más")) == "**negrita** y más"


def test_a_line_break_is_a_commonmark_hard_break():
    """Two spaces then a newline, which is a break in the members area and on
    the public site alike — a bare newline is a break in one and a space in
    the other."""
    assert serialize(tag("p", "uno", tag("br"), "dos")) == "uno  \ndos"


def test_lists_keep_their_order_and_their_nesting():
    assert serialize(tag("ul", tag("li", "uno"), tag("li", "dos"))) == "- uno\n- dos"
    assert serialize(tag("ol", tag("li", "uno"), tag("li", "dos"))) == "1. uno\n2. dos"
    nested = tag("ul", tag("li", "fuera", tag("ul", tag("li", "dentro"))))
    assert serialize(nested) == "- fuera\n  - dentro"


def test_a_quote_marks_every_line():
    assert serialize(tag("blockquote", tag("p", "uno"), tag("p", "dos"))) == "> uno\n>\n> dos"


def test_a_link_keeps_its_address():
    link = tag("a", "la agenda", href="https://vienalatina.com/page/agenda/")
    assert serialize(tag("p", "Mira ", link)) == "Mira [la agenda](https://vienalatina.com/page/agenda/)"


def test_a_link_that_could_run_something_loses_its_address():
    """markdown-it refuses `javascript:` on its own. This is the second lock,
    and it is here because the href is written by whatever the browser's
    execCommand did with what somebody typed into a prompt."""
    link = tag("a", "pulsa", href="javascript:alert(1)")
    assert serialize(tag("p", link)) == "pulsa"


def test_a_line_that_looks_like_a_marker_is_escaped():
    """«- 20 € por persona» is a sentence, and it would come back as a bullet
    the next time the post was opened."""
    assert serialize(tag("p", "- 20 € por persona")) == "\\- 20 € por persona"
    assert serialize(tag("p", "1. nunca más")) == "1\\. nunca más"
    assert serialize(tag("p", "# no es un título")) == "\\# no es un título"


def test_what_a_paste_leaves_behind_contributes_its_words_and_nothing_else():
    """A paste arrives as plain text, so this should never happen — but an
    engine's own execCommand leaves spans and fonts behind, and they must not
    reach the file."""
    span = tag("span", "texto", style="font-weight: 700; color: red")
    assert serialize(tag("p", span)) == "texto"


def test_a_script_or_a_stylesheet_leaves_nothing():
    assert serialize(tag("p", tag("script", "alert(1)"), "hola")) == "hola"
    assert serialize(tag("style", "body { display: none }")) == ""


def test_an_empty_box_is_an_empty_string():
    assert serialize(tag("p")) == ""
    assert serialize(tag("p", tag("br"))) == ""


def test_a_div_around_blocks_is_a_container_and_not_a_paragraph():
    """What a contenteditable does constantly: pressing Enter inside a list
    wrapped the whole list in a <div>, and reading that as a paragraph ran
    «uno» and «dos» together into «unodos». Found by typing in a browser —
    no fixture would have predicted the wrapper."""
    wrapped = tag("div", tag("ul", tag("li", "uno"), tag("li", "dos")))
    assert serialize(wrapped) == "- uno\n- dos"

    mixed = tag("div", tag("h2", "Título"), tag("p", "Texto."))
    assert serialize(mixed) == "## Título\n\nTexto."


def test_a_div_around_words_is_still_a_paragraph():
    assert serialize(tag("div", "una línea")) == "una línea"


def test_a_table_keeps_its_words():
    """The buttons do not make tables; the markdown view does. A pasted one
    loses its grid, which is a loss — losing the text would be worse."""
    table = tag("table", tag("tr", tag("td", "uno"), tag("td", "dos")))
    assert "uno" in serialize(table) and "dos" in serialize(table)


# --- and the whole way round -----------------------------------------------

DOCUMENTS = [
    "Un párrafo corriente, sin nada dentro.",
    "Con **negrita**, _cursiva_ y ~~tachado~~ en la misma línea.",
    "## Un título\n\nY su párrafo.\n\n### Y un subtítulo\n\nCon más texto.",
    "- uno\n- dos\n- tres",
    "1. primero\n2. segundo",
    "> Lo que alguien dijo.\n\nY la respuesta.",
    "Mira [la agenda](https://vienalatina.com/page/agenda/) antes de venir.",
    "Una línea  \ny otra debajo.",
    "Texto con un `trozo de código` dentro.",
    "## Reunión\n\n- 20 € por persona\n- Traer el acta\n\n> Y puntuales.",
]


@pytest.mark.parametrize("document", DOCUMENTS, ids=range(len(DOCUMENTS)))
def test_markdown_survives_the_round_trip(document):
    """Open a post, change nothing, save it: it must still mean what it meant.

    Rendered, serialized, rendered again — and the two renderings compared,
    rather than the two markdown strings, because where a backslash goes is a
    choice and what the reader sees is not.
    """
    once = to_html(document)
    back = serialize(*parse(once))
    assert to_html(back) == once, f"{document!r} became {back!r}"
