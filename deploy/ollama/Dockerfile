# Ollama with the LLM and embedding model baked in, so a customer install
# needs no internet. The build machine downloads the models; the running
# container never does. Adapted from rag-claude/ollama/Dockerfile.
FROM ollama/ollama:0.12.3

ARG LLM_MODEL=qwen2.5:7b-instruct
ARG EMBED_MODEL=bge-m3

# Models live in /models, not /root/.ollama: a volume mounted over the default
# directory would hide the baked-in models. Nothing is ever mounted at /models.
ENV OLLAMA_MODELS=/models \
    OLLAMA_HOST=0.0.0.0:11434

# Start a temporary server, pull both models, list them. The build fails here
# if a model name is wrong or the download fails.
RUN ollama serve & \
    for i in $(seq 1 60); do ollama list >/dev/null 2>&1 && break; sleep 1; done; \
    ollama pull "${LLM_MODEL}" && ollama pull "${EMBED_MODEL}" && ollama list

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
