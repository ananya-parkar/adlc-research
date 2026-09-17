"""
Vector Store — embeds artifact text into knowledge_chunks for
semantic search, similarity-based dedup, and future RAG retrieval
(Spec Synthesizer will use this to pull relevant context when
drafting stories).

Uses a local embedding model (all-MiniLM-L6-v2, 384 dimensions —
matches the VECTOR(384) column in schema.sql) so there's no API
cost and no extra API key needed for embeddings.

This does NOT replace artifacts.content — that stays the canonical,
exact record of what a source said. This is a derived, chunked,
searchable index built FROM that same text. For short text (a Jira
ticket description), one chunk basically mirrors the content field.
For long text (an uploaded PDF, a long Confluence page), chunking
here is what actually preserves the full text — nothing gets
silently truncated or dropped.
"""

import logging
from typing import List, Optional

from substrate.db import get_connection

logger = logging.getLogger(__name__)

_model = None  # lazily loaded — importing sentence-transformers is slow, do it once


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed_text(text: str) -> List[float]:
    model = _get_model()
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """
    Simple word-based chunking with overlap, so a concept split
    across a chunk boundary isn't lost entirely in either chunk.
    chunk_size and overlap are in words, not characters.
    """
    words = text.split()
    if not words:
        return []

    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunks.append(" ".join(words[start:end]))
        start = end - overlap  # step back by the overlap amount
        if end >= len(words):
            break
    return chunks


def store_chunks(artifact_id: str, text: str, project_id: Optional[str] = None, metadata: Optional[dict] = None) -> int:
    """
    Chunks the given text, embeds each chunk, and stores them all in
    knowledge_chunks linked to artifact_id. Existing chunks for this
    artifact are cleared first, so re-running (e.g. on a content
    update) doesn't accumulate stale duplicates.

    Returns the number of chunks stored.
    """
    if not text or not text.strip():
        logger.warning("No text to chunk/embed for artifact %s", artifact_id)
        return 0

    chunks = chunk_text(text)
    if not chunks:
        return 0

    chunk_metadata = dict(metadata or {})
    if project_id:
        chunk_metadata["project_id"] = project_id

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Clear old chunks for this artifact before inserting fresh ones
            cur.execute("DELETE FROM knowledge_chunks WHERE artifact_id = %s", (artifact_id,))

            for i, chunk in enumerate(chunks):
                embedding = embed_text(chunk)
                cur.execute(
                    """
                    INSERT INTO knowledge_chunks (artifact_id, chunk_index, content, embedding, metadata)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (artifact_id, i, chunk, embedding, __import__("json").dumps(chunk_metadata)),
                )
        conn.commit()

    logger.info("Stored %d chunk(s) for artifact %s", len(chunks), artifact_id)
    return len(chunks)


def search_similar(
    query_text: str,
    top_k: int = 5,
    artifact_type: Optional[str] = None,
    project_id: Optional[str] = None,
) -> list:
    """
    Semantic similarity search — embeds the query and finds the
    closest chunks by cosine distance. Useful later for: dedup
    (does a similar CWI already exist?), or Spec Synthesizer RAG
    (pull relevant context for drafting a story).

    project_id filters to one project's chunks only — reads straight
    off knowledge_chunks.metadata (denormalized), no JOIN needed.
    Always pass this once multiple projects/clients share the DB,
    or search results will mix data across projects.
    """
    query_embedding = embed_text(query_text)

    sql = """
        SELECT kc.artifact_id, kc.content, kc.chunk_index,
               1 - (kc.embedding <=> %s::vector) AS similarity
        FROM knowledge_chunks kc
    """
    params: list = [query_embedding]
    where_clauses = []
    needs_artifact_join = bool(artifact_type)

    if needs_artifact_join:
        sql += " JOIN artifacts a ON kc.artifact_id = a.artifact_id"
        where_clauses.append("a.artifact_type = %s")
        params.append(artifact_type)

    if project_id:
        where_clauses.append("kc.metadata->>'project_id' = %s")
        params.append(project_id)

    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)

    sql += " ORDER BY kc.embedding <=> %s::vector LIMIT %s"
    params.extend([query_embedding, top_k])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [
        {"artifact_id": str(r[0]), "content": r[1], "chunk_index": r[2], "similarity": float(r[3])}
        for r in rows
    ]