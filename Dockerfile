FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV RAG_HOST=0.0.0.0
ENV RAG_PORT=8090
ENV CHROMA_PERSIST_DIRECTORY=/app/.rag-data/chroma

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-kor \
  && rm -rf /var/lib/apt/lists/*

RUN addgroup --system rag \
  && adduser --system --ingroup rag rag

COPY pyproject.toml README.md ./
COPY app ./app
COPY docker ./docker

RUN pip install . \
  && mkdir -p /app/.rag-data/chroma \
  && chmod +x /app/docker/start-rag-combined.sh \
  && chown -R rag:rag /app

USER rag

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"RAG_PORT\", \"8090\")}/health', timeout=3).read()" || exit 1

CMD ["/app/docker/start-rag-combined.sh"]
