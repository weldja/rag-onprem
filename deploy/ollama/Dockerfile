# Ollama with the LLM and embedding model baked in, so a customer install
# needs no internet. The build machine downloads the models; the running
# container never does. Adapted from rag-claude/ollama/Dockerfile.
# 0.35.1 matches rag-claude and supports LLAMA_ARG_FIT_TARGET (see docker-compose.gpu.yml).
FROM ollama/ollama:0.35.1@sha256:292ee7945dfc3d5840a181f3ab86fedb1e66703e02c8af98b50f4da56b7e278c

ARG LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M
ARG EMBED_MODEL=bge-m3
# Other answer models to bake in (comma-separated), so a customer can switch
# LLM_MODEL in .env without a new image. The release bundle adds the 7B here.
ARG LLM_EXTRA_MODELS=

# Models live in /models, not /root/.ollama: a volume mounted over the default
# directory would hide the baked-in models. Nothing is ever mounted at /models.
ENV OLLAMA_MODELS=/models \
    OLLAMA_HOST=0.0.0.0:11434

# Start a temporary server, pull the models, list them. The build fails here
# if a model name is wrong or the download fails.
RUN ollama serve & \
    for i in $(seq 1 60); do ollama list >/dev/null 2>&1 && break; sleep 1; done; \
    ollama pull "${LLM_MODEL}" && ollama pull "${EMBED_MODEL}" && \
    for m in $(echo "${LLM_EXTRA_MODELS}" | tr ',' ' '); do ollama pull "$m" || exit 1; done && \
    ollama list

# Defaults for the warm-up in entrypoint.sh; compose passes the LLM_MODEL
# chosen in .env, which may be one of LLM_EXTRA_MODELS.
ENV LLM_MODEL=${LLM_MODEL} \
    EMBED_MODEL=${EMBED_MODEL}

# Added after the model layer so editing the script doesn't re-download models.
# The sed strips CRLF in case of a Windows checkout without .gitattributes.
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD []

HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=5 \
    CMD ollama list || exit 1
