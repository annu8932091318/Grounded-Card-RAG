FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/app/.models

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Bake the local embedding model into the image so runtime never downloads anything.
RUN python -c "from src.modules.retrieval.embedder import embed_query; embed_query('warm up')"

RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 3000

# migrate + ingest are idempotent, so running them on every start is safe.
CMD ["sh", "-c", "python -m scripts.migrate && python -m scripts.ingest && python -m uvicorn src.app:app --host 0.0.0.0 --port 3000 --timeout-graceful-shutdown 30"]
