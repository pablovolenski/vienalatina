#!/usr/bin/env bash
# Download and convert the OPUS-MT models the pipeline needs.
#
# Run once on the server; the pipeline mounts the result read-only, so changing
# models later means re-running this rather than rebuilding a 1GB image.
#
#   bash scripts/fetch-models.sh            # -> /srv/mt-models
#   bash scripts/fetch-models.sh /tmp/mt    # -> somewhere else
#
# Conversion holds the fp32 model in RAM. These are 74M-237M parameter models,
# so each peaks well under the 4GB box's headroom — unlike M2M100 418M, whose
# conversion was OOM-killed here at a 3GB cap. One container per model keeps a
# failure contained to that model.

set -euo pipefail
DEST="${1:-/srv/mt-models}"

MODELS=(
  "Helsinki-NLP/opus-mt-es-de"
  "Helsinki-NLP/opus-mt-tc-big-de-es"
  "Helsinki-NLP/opus-mt-tc-big-itc-itc"
)

sudo mkdir -p "$DEST"
sudo chown "$(id -u):$(id -g)" "$DEST"

for REPO in "${MODELS[@]}"; do
  NAME="${REPO##*/}"
  if [ -f "$DEST/$NAME/model.bin" ]; then
    echo "== $NAME already converted, skipping"
    continue
  fi
  echo "== converting $NAME"
  docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp --memory=3g \
    -v "$DEST:/out" python:3.12-slim bash -c "
      set -e
      pip install --quiet 'transformers[torch]' ctranslate2 sentencepiece \
        --extra-index-url https://download.pytorch.org/whl/cpu
      ct2-transformers-converter --model $REPO --output_dir /out/$NAME \
        --quantization int8 --copy_files source.spm target.spm vocab.json tokenizer_config.json
    "
done

echo
du -sh "$DEST"/*
echo "Models ready in $DEST"
