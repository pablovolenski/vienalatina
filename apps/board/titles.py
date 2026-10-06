"""The name a post gets when nobody gives it one.

A title is not required anywhere a member writes. Most things people post have
one, and the ones that do not are usually the best writing on a wall: somebody
answering a question, somebody saying a thing happened. Demanding a headline
first is how a form stops a person mid-sentence.

So an untitled post is called «Sin Título», and the second one is «Sin Título
2». The number is there to keep them apart — in a list of twenty threads, three
rows reading the same four characters are three rows nobody can tell apart or
link to with any confidence.

Numbers are never reused. A deleted thread keeps its title in the database, and
counting it is what stops a new post inheriting the name of one somebody
remembers reading.
"""

from __future__ import annotations

import re
from typing import Iterable

UNTITLED = "Sin Título"

# «Sin Título», «Sin Título 2». Anchored, so a post somebody deliberately
# called «Sin Título ni ganas» is not read as a number and does not take one.
NUMBERED = re.compile(r"^Sin Título(?:\s+(\d+))?$")


def next_untitled(taken: Iterable[str]) -> str:
    """The first «Sin Título» name not in `taken`.

    `taken` is every title already in that place — the wall's threads, the
    queue's proposals — including deleted ones, because a number that comes
    back is a post that looks like a different post.
    """
    used = set()
    for title in taken:
        match = NUMBERED.match((title or "").strip())
        if match:
            used.add(int(match.group(1) or 1))

    number = 1
    while number in used:
        number += 1
    return UNTITLED if number == 1 else f"{UNTITLED} {number}"
