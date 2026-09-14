"""M2M100 via CTranslate2, on CPU.

Replaces the DeepL HTTP call. No API key, no quota, no third-party request —
the model ships inside the pipeline image.
"""

from __future__ import annotations

import os

from .provider import Provider, SITE_TO_MODEL


class CTranslate2Provider(Provider):
    def __init__(
        self,
        model_dir: str | None = None,
        tokenizer: str | None = None,
        compute_type: str | None = None,
        threads: int | None = None,
    ):
        self.model_dir = model_dir or os.environ.get("MT_MODEL_DIR", "/opt/mt/model")
        self.tokenizer = tokenizer or os.environ.get("MT_TOKENIZER", "facebook/m2m100_418M")
        self.compute_type = compute_type or os.environ.get("MT_COMPUTE_TYPE", "int8")
        self.threads = threads or int(os.environ.get("MT_THREADS", "2"))
        self._translator = None
        self._tokenizer = None

    def _load(self) -> None:
        if self._translator is not None:
            return
        import ctranslate2
        import transformers

        self._tokenizer = transformers.AutoTokenizer.from_pretrained(self.tokenizer)
        self._translator = ctranslate2.Translator(
            self.model_dir,
            device="cpu",
            compute_type=self.compute_type,
            intra_threads=self.threads,
        )

    def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []
        self._load()
        tok = self._tokenizer
        tok.src_lang = SITE_TO_MODEL[src]
        target_token = tok.lang_code_to_token[SITE_TO_MODEL[tgt]]

        batch = [tok.convert_ids_to_tokens(tok.encode(t)) for t in texts]
        results = self._translator.translate_batch(
            batch, target_prefix=[[target_token]] * len(batch)
        )
        # hypotheses[0][0] is the target-language token we forced; drop it.
        return [
            tok.decode(tok.convert_tokens_to_ids(r.hypotheses[0][1:])) for r in results
        ]
