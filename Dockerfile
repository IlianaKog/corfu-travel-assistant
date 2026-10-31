# Single container: REST API + CLI + tests, with embedded retrieval storage.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBEDDING_CACHE_DIR=/app/.cache/fastembed \
    INDEX_PATH=/app/storage/poi_index.npz

WORKDIR /app

# Dependencies first, for layer caching. Exact tested versions.
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY . .

# Download the embedding model and build the vector index at build time, so the
# container starts instantly and never needs Hugging Face at runtime.
RUN python -m app.rag.index
ENV HF_HUB_OFFLINE=1

RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
