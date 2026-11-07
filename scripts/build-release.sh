#!/usr/bin/env bash
# Build an offline installation bundle (needs internet on THIS machine only).
#   bash scripts/build-release.sh VERSION
# Model choice defaults to .env.example (the customer defaults), NOT your local
# .env. Override for a test build with, e.g.:
#   RELEASE_LLM_MODEL=qwen2.5:3b-instruct bash scripts/build-release.sh 0.5.0-test
#
# Output: release/rag-onprem-VERSION/ (git-ignored), ready to copy to a
# customer server; with PACK=1 also release/rag-onprem-VERSION.tar.
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
native_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -am "$1"; else realpath "$1"; fi; }
from_example() { sed -n "s/^$1=//p" .env.example; }

VERSION="${1:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$ ]] || {
    echo "usage: $0 VERSION   (e.g. 1.0.0 or 1.0.0-rc1)" >&2
    exit 2
}

LLM_MODEL="${RELEASE_LLM_MODEL:-$(from_example LLM_MODEL)}"
EMBED_MODEL="${RELEASE_EMBED_MODEL:-$(from_example EMBED_MODEL)}"
EMBED_DIM="${RELEASE_EMBED_DIM:-$(from_example EMBED_DIM)}"
RERANKER_MODEL="${RELEASE_RERANKER_MODEL:-$(from_example RERANKER_MODEL)}"
PGVECTOR_IMAGE="$(sed -n 's/^ *image: \(pgvector\/pgvector:.*@sha256:.*\)$/\1/p' deploy/docker-compose.yml)"
[ -n "$PGVECTOR_IMAGE" ] || { echo "pgvector image (pinned by digest) not found in deploy/docker-compose.yml" >&2; exit 1; }

commit="$(git rev-parse --short HEAD)"
if [ -n "$(git status --porcelain)" ]; then
    echo "WARNING: working tree has uncommitted changes; the bundle won't match commit $commit" >&2
    commit="$commit-dirty"
fi

out="release/rag-onprem-$VERSION"
[ ! -e "$out" ] || { echo "$out already exists; remove it first" >&2; exit 1; }

echo "==> Building rag-onprem $VERSION ($commit)"
echo "    LLM=$LLM_MODEL  embeddings=$EMBED_MODEL ($EMBED_DIM)  reranker=${RERANKER_MODEL:-none}"

OCR_IMAGE_LANGUAGES="${RELEASE_OCR_LANGUAGES:-$(from_example OCR_IMAGE_LANGUAGES)}"
docker build --target runtime --build-arg "RERANKER_MODEL=$RERANKER_MODEL" \
    --build-arg "OCR_LANGUAGES=$OCR_IMAGE_LANGUAGES" \
    -t "rag-onprem/api:$VERSION" "$(native_path backend)"
docker build -t "rag-onprem/web:$VERSION" "$(native_path frontend)"
docker build --build-arg "LLM_MODEL=$LLM_MODEL" --build-arg "EMBED_MODEL=$EMBED_MODEL" \
    -t "rag-onprem/ollama:$VERSION" "$(native_path deploy/ollama)"
docker pull "$PGVECTOR_IMAGE"
docker tag "$PGVECTOR_IMAGE" "rag-onprem/pgvector:$VERSION"

echo "==> Assembling $out"
mkdir -p "$out/nginx" "$out/certs"
cp deploy/release/*.sh deploy/release/docker-compose*.yml "$out/"
cp -r deploy/nginx/templates "$out/nginx/templates"
cp deploy/certs/README.md "$out/certs/README.md"
cp docs/operations.md "$out/README.md"
sed -e "s|^LLM_MODEL=.*|LLM_MODEL=$LLM_MODEL|" \
    -e "s|^EMBED_MODEL=.*|EMBED_MODEL=$EMBED_MODEL|" \
    -e "s|^EMBED_DIM=.*|EMBED_DIM=$EMBED_DIM|" \
    -e "s|^RERANKER_MODEL=.*|RERANKER_MODEL=$RERANKER_MODEL|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=set-by-install-sh|" \
    .env.example >"$out/.env.example"
{
    echo
    echo "# --- Release (set by the bundle; don't edit) ---"
    echo "RAG_VERSION=$VERSION"
} >>"$out/.env.example"
{
    echo "rag-onprem $VERSION"
    echo "commit=$commit"
    echo "built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "llm_model=$LLM_MODEL"
    echo "embed_model=$EMBED_MODEL"
    echo "reranker_model=${RERANKER_MODEL:-}"
    echo "ocr_languages=${OCR_IMAGE_LANGUAGES}"
    echo "pgvector=$PGVECTOR_IMAGE"
    for name in api web ollama pgvector; do
        echo "image.$name=$(docker image inspect --format '{{.Id}}' "rag-onprem/$name:$VERSION")"
    done
} >"$out/VERSION"

echo "==> Saving images (compressed; this takes a while)"
docker save "rag-onprem/api:$VERSION" "rag-onprem/web:$VERSION" \
    "rag-onprem/ollama:$VERSION" "rag-onprem/pgvector:$VERSION" | gzip -1 >"$out/images.tar.gz"

(cd "$out" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | sort | xargs sha256sum >SHA256SUMS)

echo
echo "Bundle: $out ($(du -sh "$out" | cut -f1))"
if [ "${PACK:-0}" = 1 ]; then
    # A single file is easier to move; it needs as much disk again as the bundle.
    tar -C release -cf "release/rag-onprem-$VERSION.tar" "rag-onprem-$VERSION"
    echo "Archive: release/rag-onprem-$VERSION.tar"
    echo "On the server: tar -xf rag-onprem-$VERSION.tar && cd rag-onprem-$VERSION && bash install.sh"
else
    echo "Copy the folder to the server, then: cd rag-onprem-$VERSION && bash install.sh"
    echo "(PACK=1 also writes a single .tar for transfer.)"
fi
