"""Translation provider interface.

The pipeline talks to a provider, never to a model directly, so the engine can
be swapped — a larger M2M100, OPUS-MT, or a hosted API — without touching
translate.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# Site language codes (Hugo, and the .<lang>.md filename suffix) -> model codes.
# M2M100 has no Brazilian variant, so pt-br translates as generic Portuguese.
SITE_TO_MODEL = {"es": "es", "de": "de", "pt-br": "pt"}

SITE_LANGS = tuple(SITE_TO_MODEL)


class Provider(ABC):
    @abstractmethod
    def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        """Translate plain-text strings between two site language codes.

        Input must carry no markup: callers mask it first (see markdown.py).
        Returns one string per input, in order.
        """
