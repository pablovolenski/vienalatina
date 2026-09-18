# Pipeline image for the translate step.
#
# Models are NOT baked in — they live on the host at /srv/mt-models (see
# scripts/fetch-models.sh) and are mounted read-only by .woodpecker.yml. That
# keeps this image small and lets a model change be a directory swap instead of
# a 1GB image rebuild, which matters because model choice turned out to need
# iteration.
#
# torch is deliberately absent: it is only needed to *convert* a model, and the
# default PyPI wheel drags in ~2.5GB of CUDA libraries this CPU-only box will
# never use. Conversion happens in fetch-models.sh, not here.
#
# Build on the server:
#   docker build -t vienalatina/translate:2 docker/translate

FROM python:3.12-slim

# translate.py shells out to git to diff the push and commit the siblings back.
RUN apt-get update -qq \
 && apt-get install -qq -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      ctranslate2 \
      transformers \
      sentencepiece \
      sentencex \
      pyyaml

ENV MT_PROVIDER=opus \
    MT_MODEL_DIR=/opt/mt/models \
    MT_COMPUTE_TYPE=int8 \
    MT_THREADS=2
