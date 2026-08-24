"""Vector store module using ChromaDB for semantic retrieval.

This module replaces the EmbeddingEngine with ChromaDB-based semantic retrieval,
providing schema-aware semantic search with improved performance and accuracy.
"""

from typing import Dict, List, Optional, Any

from dbbuddy_core.config import CHROMADB_PERSIST_DIRECTORY
from dbbuddy_core.logger import get_logger

logger = get_logger()

try:
    import chromadb
except ImportError:  # pragma: no cover - optional dependency
    chromadb = None


# Bump when the embedding model or the document-construction logic changes, so
# stale collections (built with a different embedding scheme) are not reused.
# It is part of the collection name, so old collections are simply ignored.
#
# v2: collections are now created with an explicitly named embedding function
# (see get_embedding_function). The model is unchanged, but Chroma records the
# function's *name* in the collection config and refuses to open a collection
# stamped "default" with one stamped "onnx_mini_lm_l6_v2" — so v1 collections
# would silently fall back to the slow per-call path forever. A new name lets
# them be rebuilt once, correctly.
EMBEDDINGS_VERSION = "2"


# Process-wide singleton ChromaDB client. Creating a client is expensive (it
# opens the on-disk SQLite store and loads the embedding model), so it must be
# built once and shared — never per request.
_chroma_client = None

# Process-wide singleton embedding function.
#
# Chroma will happily default one in for you, but it constructs a *fresh*
# instance for the call — and that instance builds a new onnxruntime
# InferenceSession, ~150 ms, before a single token is embedded. Every schema
# search the user's question was not already cached for paid it. Holding one
# instance drops an uncached search to ~17 ms; the model itself is the same one
# Chroma would have picked, so existing collections stay readable and
# EMBEDDINGS_VERSION does not need to move.
_embedding_fn = None
_embedding_fn_failed = False


def get_embedding_function():
    """Return the shared ONNX embedding function, or None to let Chroma default.

    Returning None is a supported outcome: the caller omits the argument and
    Chroma falls back to its own per-call construction — slower, but working.
    """
    global _embedding_fn, _embedding_fn_failed
    if chromadb is None or _embedding_fn_failed:
        return None
    if _embedding_fn is None:
        try:
            from chromadb.utils import embedding_functions
            _embedding_fn = embedding_functions.ONNXMiniLM_L6_V2()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Shared embedding function unavailable, using Chroma default: {exc}")
            _embedding_fn_failed = True
            return None
    return _embedding_fn


def get_chroma_client():
    """Return the shared persistent ChromaDB client, or None if unavailable.

    Uses PersistentClient so embeddings survive process restarts — the schema
    index is computed once per schema, not recomputed on every run.
    """
    global _chroma_client
    if chromadb is None:
        return None
    if _chroma_client is None:
        try:
            _chroma_client = chromadb.PersistentClient(path=CHROMADB_PERSIST_DIRECTORY)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Failed to create persistent ChromaDB client: {exc}")
            _chroma_client = None
    return _chroma_client


class VectorStore:
    """ChromaDB-based vector store for semantic schema retrieval."""

    def __init__(self, collection_name: str = "schema_index", cache=None, schema_hash: str = None):
        """Initialize vector store with ChromaDB.

        Args:
            collection_name: Name of the ChromaDB collection
            cache: Optional Redis cache instance
            schema_hash: Optional schema hash for schema-aware caching
        """
        # Share the process-wide persistent client instead of building one per
        # instance (the per-request client creation was a major latency cost).
        self.client = get_chroma_client()

        # Scope the collection to the embeddings version + schema so different
        # schemas (or a changed embedding scheme) don't collide, and a populated
        # collection can be reused across restarts.
        # Without a schema_hash the name is still versioned: a collection left
        # behind by an older embedding scheme would otherwise be reopened forever,
        # and Chroma refuses to bind the shared embedder to it — so the caller
        # would silently keep paying the slow per-call path.
        if schema_hash:
            collection_name = f"schema_v{EMBEDDINGS_VERSION}_{schema_hash[:24]}"
        else:
            collection_name = f"{collection_name}-v{EMBEDDINGS_VERSION}"

        self.collection = self._open_collection(collection_name)
        self.indexed = False
        self._cache = {}
        self.redis_cache = cache
        self.schema_hash = schema_hash
        self._documents = []
        self._metadatas = []

    def _open_collection(self, collection_name: str):
        """Get or create the collection, bound to the shared embedding function.

        A collection persisted by an earlier version was created without an
        explicit embedding function; if Chroma ever rejects the pairing, fall
        back to letting it supply its own rather than losing the index.
        """
        if self.client is None:
            return None

        ef = get_embedding_function()
        if ef is not None:
            try:
                return self.client.get_or_create_collection(name=collection_name, embedding_function=ef)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"Collection rejected the shared embedder, falling back: {exc}")
        return self.client.get_or_create_collection(name=collection_name)

    def index_schema(self, schema: Dict[str, List[str]], semantic_layer: Optional[Dict] = None,
                     force: bool = False) -> None:
        """Index schema into ChromaDB vector store.

        This replaces the semantic layer usage by creating embeddings for all
        tables and columns in the schema.

        Args:
            schema: Database schema (table -> columns mapping)
            semantic_layer: Optional semantic layer with term mappings
            force: Re-embed even if the schema-scoped collection is already
                populated (used by Analyze Schema so embeddings reflect updated
                semantic terms).
        """
        if self.indexed:
            logger.debug("Schema already indexed, skipping")
            return  # Already indexed

        logger.info(f"Indexing schema with {len(schema)} tables...")

        documents = []
        metadatas = []
        ids = []

        idx = 0

        for table, columns in schema.items():
            for column in columns:
                # Get semantic term if available
                term = column
                if semantic_layer and table in semantic_layer:
                    if column in semantic_layer[table]:
                        term = semantic_layer[table][column].get("term", column)

                # Create document text for embedding
                text = f"{table} {column} {term}"

                documents.append(text)
                metadatas.append({
                    "table": table,
                    "column": column,
                    "type": "column"
                })
                ids.append(f"col_{idx}")
                idx += 1

            # Also index table itself
            documents.append(table)
            metadatas.append({
                "table": table,
                "type": "table"
            })
            ids.append(f"tbl_{idx}")
            idx += 1

        self._documents = documents
        self._metadatas = metadatas

        # Add to ChromaDB collection when available. Otherwise the in-memory
        # fallback below keeps retrieval functional without the optional package.
        if self.collection is not None and documents:
            # The collection is schema-scoped, so a populated one was already
            # embedded for this exact schema (possibly in a previous run).
            # Skip the expensive re-embedding in that case.
            try:
                existing = self.collection.count()
            except Exception:
                existing = 0

            if not force and existing >= len(documents) and existing > 0:
                logger.info(
                    f"Vector index already populated ({existing} docs) for this schema — skipping embedding"
                )
            else:
                # upsert is idempotent on id, so a partial/stale index is safely
                # completed without raising on duplicate ids.
                self.collection.upsert(
                    documents=documents,
                    metadatas=metadatas,
                    ids=ids
                )
                logger.info(f"Indexed {len(documents)} documents into ChromaDB")

        self.indexed = True
        logger.info("Schema indexing complete")

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Search for schema elements matching the query.

        Args:
            query: User query text
            top_k: Number of top results to return

        Returns:
            List of matches with table, column, type, and score
        """
        # Guard: ensure vector store is indexed before search
        if not self.indexed:
            raise RuntimeError("VectorStore not indexed. Call index_schema() first.")

        # NEW: Check Redis cache first with normalization and schema awareness
        if self.redis_cache:
            cache_hit = self.redis_cache.get("retrieval", f"{query}_{top_k}", normalize=True, schema_hash=self.schema_hash)
            if cache_hit:
                return cache_hit

        # Check local cache
        cache_key = f"{query}_{top_k}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if self.collection is not None:
            results = self.collection.query(
                query_texts=[query],
                n_results=top_k
            )

            matches = []

            for i in range(len(results["documents"][0])):
                # Convert distance to similarity score
                distance = results["distances"][0][i]
                # ChromaDB uses cosine distance by default (range [0, 2])
                # Convert to similarity score in [0, 1] range
                similarity_score = max(0.0, 1 - distance)

                matches.append({
                    "text": results["documents"][0][i],
                    "table": results["metadatas"][0][i].get("table"),
                    "column": results["metadatas"][0][i].get("column"),
                    "type": results["metadatas"][0][i].get("type"),
                    "score": similarity_score
                })
        else:
            matches = self._search_in_memory(query, top_k)

        # Cache results locally
        self._cache[cache_key] = matches

        # NEW: Cache results in Redis with normalization and schema awareness
        if self.redis_cache:
            self.redis_cache.set("retrieval", f"{query}_{top_k}", matches, ttl=600, normalize=True, schema_hash=self.schema_hash)

        return matches

    def _search_in_memory(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """Fallback schema search used when ChromaDB is not installed."""
        import re

        def normalize_token(token: str) -> str:
            if token.endswith("ies"):
                return token[:-3] + "y"
            if token.endswith("es"):
                return token[:-2]
            if token.endswith("s") and len(token) > 3:
                return token[:-1]
            return token

        query_tokens = {
            normalize_token(token)
            for token in re.findall(r"\b\w+\b", query.lower().replace("_", " "))
        }
        scored = []

        for document, metadata in zip(self._documents, self._metadatas):
            normalized_document = document.lower().replace("_", " ")
            doc_tokens = {
                normalize_token(token)
                for token in re.findall(r"\b\w+\b", normalized_document)
            }
            if not doc_tokens:
                continue

            overlap = query_tokens & doc_tokens
            substring_hits = sum(
                1 for token in query_tokens
                if len(token) >= 3 and token in normalized_document
            )
            doc_overlap = len(overlap) / len(doc_tokens)
            query_overlap = len(overlap) / max(len(query_tokens), 1)
            score = max(doc_overlap, query_overlap) + (0.1 * substring_hits)

            if score <= 0:
                continue

            scored.append({
                "text": document,
                "table": metadata.get("table"),
                "column": metadata.get("column"),
                "type": metadata.get("type"),
                "score": min(score, 1.0)
            })

        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:top_k]

    def delete_collection(self) -> None:
        """Delete the ChromaDB collection."""
        if self.client is not None and self.collection is not None:
            self.client.delete_collection(name=self.collection.name)
        self.indexed = False
        self._cache.clear()
        self._documents.clear()
        self._metadatas.clear()
