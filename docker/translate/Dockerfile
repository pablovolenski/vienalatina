# Pipeline image for the translate step.
#
# Model and tokenizer are baked in, so a publish makes zero network calls and
# needs no API key. torch is deliberately absent: it is only required to
# *convert* a model, and the default PyPI wheel drags in ~2.5GB of CUDA
# libraries this CPU-only box will never use.
#
# Build on the server (once, and again only when changing models):
#   docker build -t vienalatina/translate:1 docker/translate
#
# The default model is a pre-converted CTranslate2 build, which avoids running
# ct2-transformers-converter on a 4GB box — it gets OOM-killed there.

FROM python:3.12-slim

ARG MT_MODEL_REPO=michaelfeil/ct2fast-m2m100_418M
ARG MT_TOKENIZER_REPO=facebook/m2m100_418M

# translate.py shells out to git to diff the push and commit the siblings back.
RUN apt-get update -qq \
 && apt-get install -qq -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      ctranslate2 \
      transformers \
      sentencepiece \
      sentencex \
      pyyaml \
      huggingface_hub

RUN python -c "from huggingface_hub import snapshot_download; \
snapshot_download('${MT_MODEL_REPO}', local_dir='/opt/mt/model')" \
 && python -c "import transformers; \
transformers.AutoTokenizer.from_pretrained('${MT_TOKENIZER_REPO}') \
  .save_pretrained('/opt/mt/tokenizer')"

# The published artifact is float16; CTranslate2 quantises to int8 on load.
ENV MT_MODEL_DIR=/opt/mt/model \
    MT_TOKENIZER=/opt/mt/tokenizer \
    MT_COMPUTE_TYPE=int8 \
    MT_THREADS=2
