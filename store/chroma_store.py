"""Chroma index over feedback directives.

Derived from SQLite, never authoritative. Rebuildable at any time from
feedback_log via rebuild().

Embeddings are supplied explicitly rather than letting Chroma pick a default
embedder, because task_type asymmetry is the whole point: stored directives are
embedded as RETRIEVAL_DOCUMENT, incoming candidate rules as RETRIEVAL_QUERY.
"""
from __future__ import annotations

import chromadb

import config
from llm import client as llm_client

_COLLECTION = "feedback_directives"
_client = None


def _coll():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    return _client.get_or_create_collection(
        name=_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def add(feedback_id: int, directive: str, metadata: dict) -> None:
    vec = llm_client.embed([directive], task_type=config.EMBED_TASK_DOC)[0]
    _coll().upsert(
        ids=[str(feedback_id)],
        embeddings=[vec],
        documents=[directive],
        metadatas=[metadata],
    )


def remove(feedback_id: int) -> None:
    """Superseded feedback must leave the index or it keeps being retrieved."""
    try:
        _coll().delete(ids=[str(feedback_id)])
    except Exception:
        pass


def query(
    text: str, n_results: int = 5, where: dict | None = None
) -> list[dict]:
    coll = _coll()
    if coll.count() == 0:
        return []
    vec = llm_client.embed([text], task_type=config.EMBED_TASK_QUERY)[0]
    res = coll.query(
        query_embeddings=[vec],
        n_results=min(n_results, coll.count()),
        where=where,
    )
    out = []
    for i, doc in enumerate(res["documents"][0]):
        dist = res["distances"][0][i]
        out.append({
            "feedback_id": int(res["ids"][0][i]),
            "directive": doc,
            "metadata": res["metadatas"][0][i],
            "distance": dist,
            "similarity": round(1.0 - dist, 4),
        })
    return out


def query_for_profile(
    profile: dict,
    entity_type: str = "rule",
    exclude_table: str | None = None,
    n_results: int = 5,
) -> list[dict]:
    """Find prior feedback relevant to a table we are about to generate for.

    The query text is a natural-language description of the table, so semantic
    match happens on *meaning* — which is what lets orders.total_amount feedback
    reach sales_transactions.gross_total despite zero column-name overlap.
    """
    cols = ", ".join(c["name"] for c in profile["columns"])
    q = (
        f"Data quality rules for {profile['table_name']}: {profile['description']} "
        f"Columns: {cols}."
    )

    where: dict = {"entity_type": entity_type}
    if exclude_table:
        where = {
            "$and": [
                {"entity_type": entity_type},
                {"table_name": {"$ne": exclude_table}},
            ]
        }

    hits = query(q, n_results=n_results, where=where)
    # Hard threshold. A wrong transfer is worse than no transfer, so
    # below-threshold matches are dropped rather than down-weighted.
    return [h for h in hits if h["distance"] <= config.VECTOR_DISTANCE_THRESHOLD]


def rebuild() -> int:
    """Re-index every valid+active row from SQLite. Chroma is disposable."""
    from store import sqlite_store

    try:
        _client_ = _client or chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        _client_.delete_collection(_COLLECTION)
    except Exception:
        pass

    rows = sqlite_store.live_feedback()
    for r in rows:
        add(
            feedback_id=r["id"],
            directive=r["normalized_directive"] or r["raw_comment"],
            metadata={
                "signature": r["signature"],
                "entity_type": r["entity_type"],
                "table_name": r["table_name"],
                "action": r["action"],
                "rule_type": r["rule_type"],
                "columns": ",".join(__import__("json").loads(r["columns_json"])),
                "corrected_expression": r["corrected_expression"] or "",
            },
        )
    return len(rows)


def count() -> int:
    return _coll().count()
