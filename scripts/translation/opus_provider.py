"""OPUS-MT (Helsinki-NLP) via CTranslate2, on CPU.

Replaces M2M100 418M, which was measured producing unusable German on real
articles — "frijoles" became "Beeren" (berries), "rompe la idea" became
"breitet die Idee" (spreads it), and the site's own name came back mangled.

Not every pair exists as a published model, so routes are explicit rather than
assumed. Verified against Hugging Face:

    es -> de        opus-mt-es-de            (small, ~74M)
    de -> es        opus-mt-tc-big-de-es     (tc-big, ~237M)
    es <-> pt-br    opus-mt-tc-big-itc-itc   (Italic multilingual)
    de <-> pt-br    no direct model — pivots through Spanish

Marian models take the target language as a `>>xxx<<` token at the start of
the *source* text, unlike M2M100's decoder-side target_prefix.
"""

from __future__ import annotations

import os

from .provider import Provider

# (source, target) -> [(model directory, target-language token or None), ...]
# More than one hop means a pivot translation.
ROUTES: dict[tuple[str, str], list[tuple[str, str | None]]] = {
    ("es", "de"): [("opus-mt-es-de", None)],
    ("de", "es"): [("opus-mt-tc-big-de-es", None)],
    ("es", "pt-br"): [("opus-mt-tc-big-itc-itc", ">>pob<<")],
    ("pt-br", "es"): [("opus-mt-tc-big-itc-itc", ">>spa<<")],
    ("de", "pt-br"): [("opus-mt-tc-big-de-es", None),
                      ("opus-mt-tc-big-itc-itc", ">>pob<<")],
    ("pt-br", "de"): [("opus-mt-tc-big-itc-itc", ">>spa<<"),
                      ("opus-mt-es-de", None)],
}


class OpusMTProvider(Provider):
    def __init__(self, model_root: str | None = None, compute_type: str | None = None,
                 threads: int | None = None):
        self.model_root = model_root or os.environ.get("MT_MODEL_DIR", "/opt/mt/models")
        self.compute_type = compute_type or os.environ.get("MT_COMPUTE_TYPE", "int8")
        self.threads = threads or int(os.environ.get("MT_THREADS", "2"))
        self._name: str | None = None
        self._translator = None
        self._tokenizer = None

    def _load(self, name: str):
        """Keep exactly one model resident — three at once would not fit a 4GB box."""
        if self._name == name:
            return self._translator, self._tokenizer
        import ctranslate2
        import transformers

        self._translator = None  # free the previous model before allocating the next
        path = os.path.join(self.model_root, name)
        self._tokenizer = transformers.AutoTokenizer.from_pretrained(path)
        self._translator = ctranslate2.Translator(
            path, device="cpu", compute_type=self.compute_type, intra_threads=self.threads
        )
        self._name = name
        return self._translator, self._tokenizer

    def _hop(self, texts: list[str], model: str, token: str | None) -> list[str]:
        translator, tok = self._load(model)
        prepared = [f"{token} {t}" if token else t for t in texts]
        batch = [tok.convert_ids_to_tokens(tok.encode(t)) for t in prepared]
        results = translator.translate_batch(batch)
        return [
            tok.decode(tok.convert_tokens_to_ids(r.hypotheses[0]), skip_special_tokens=True)
            for r in results
        ]

    def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []
        route = ROUTES.get((src, tgt))
        if route is None:
            raise ValueError(f"no translation route from {src} to {tgt}")
        for model, token in route:
            texts = self._hop(texts, model, token)
        return texts
