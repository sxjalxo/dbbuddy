# DB Buddy backend + engine.
#
# Deliberately boring: no compiler stage, because every dependency that would
# need one ships a manylinux wheel (psycopg2-binary, pymssql, onnxruntime).
# Adding build-essential would roughly double the image for nothing.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the whole tree. The
# engine pulls chromadb + onnxruntime, which are the slow part of this layer.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# The package metadata, then the source. `pip install -e .` needs pyproject and
# the package directory present, and dbbuddy/__init__.py carries the version.
COPY pyproject.toml ./
COPY dbbuddy/ ./dbbuddy/
COPY dbbuddy_core/ ./dbbuddy_core/
RUN pip install -e . --no-deps

COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY docker/entrypoint.sh docker/bootstrap.py ./docker/
RUN chmod +x ./docker/entrypoint.sh

# The app is launched from backend/ (its imports assume that), with the repo root
# on the path so dbbuddy_core resolves.
ENV PYTHONPATH=/app
WORKDIR /app/backend

EXPOSE 8000

# Where the engine persists its vector index. The prepared-context cache is not
# configurable — context_store.CACHE_DIR is fixed at <repo root>/.dbbuddy_cache —
# so compose mounts a volume over /app/.dbbuddy_cache instead of setting a var.
# Both are mounted as named volumes so an Analyze survives a container restart.
ENV CHROMADB_PERSIST_DIRECTORY=/data/chroma

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
