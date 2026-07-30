"""SQLite access. This is the source of truth; Chroma is a derived index."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import config


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with connect() as conn:
        conn.executescript(schema)


# --- writes ----------------------------------------------------------------
def insert_feedback(**kw) -> int:
    cols = ", ".join(kw)
    marks = ", ".join("?" for _ in kw)
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO feedback_log ({cols}) VALUES ({marks})", list(kw.values())
        )
        return cur.lastrowid


def insert_audit(**kw) -> int:
    cols = ", ".join(kw)
    marks = ", ".join("?" for _ in kw)
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO validation_audit ({cols}) VALUES ({marks})", list(kw.values())
        )
        return cur.lastrowid


def supersede(old_id: int, new_id: int, signature: str, kind: str, detail: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE feedback_log SET status='superseded', superseded_by=? WHERE id=?",
            (new_id, old_id),
        )
        conn.execute(
            "INSERT INTO feedback_conflicts (signature, old_id, new_id, kind, detail) "
            "VALUES (?,?,?,?,?)",
            (signature, old_id, new_id, kind, detail),
        )


def save_run(
    run_label: str,
    entity_type: str,
    table_name: str,
    model: str,
    payload: dict,
    feedback_ids: list[int],
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (run_label, entity_type, table_name, model, "
            "feedback_ids, payload_json) VALUES (?,?,?,?,?,?)",
            (
                run_label,
                entity_type,
                table_name,
                model,
                json.dumps(feedback_ids),
                json.dumps(payload),
            ),
        )
        return cur.lastrowid


# --- reads -----------------------------------------------------------------
def live_feedback(entity_type: str | None = None, table_name: str | None = None) -> list[dict]:
    """Only valid + active + unexpired rows.

    The expiry clause is what stops a dismissal made months ago from silently
    suppressing an anomaly whose distribution has since drifted. Rules and
    self-heal store expires_at = NULL and are unaffected.
    """
    q = (
        "SELECT * FROM feedback_log "
        "WHERE validation_status='valid' AND status='active' "
        "AND (expires_at IS NULL OR expires_at > datetime('now'))"
    )
    params: list = []
    if entity_type:
        q += " AND entity_type=?"
        params.append(entity_type)
    if table_name:
        q += " AND table_name=?"
        params.append(table_name)
    q += " ORDER BY created_at"
    with connect() as conn:
        return [dict(r) for r in conn.execute(q, params)]


def active_for_signature(signature: str) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM feedback_log WHERE signature=? "
                "AND validation_status='valid' AND status='active'",
                (signature,),
            )
        ]


def total_dismissals(signature: str) -> int:
    """How many times this anomaly has ever been dismissed, including expired
    and superseded rows. Repeated dismissal means the detector is wrong, and
    that fact must survive both expiry and supersession to be visible."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(dismiss_count),0) AS n FROM feedback_log "
            "WHERE signature=? AND action='reject' AND validation_status='valid'",
            (signature,),
        ).fetchone()
    return row["n"]


def load_run(table_name: str, run_label: str, entity_type: str = "rule") -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE table_name=? AND run_label=? AND entity_type=? "
            "ORDER BY id DESC LIMIT 1",
            (table_name, run_label, entity_type),
        ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def all_feedback() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM feedback_log ORDER BY id")]


def all_audit() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM validation_audit ORDER BY id")]


def next_iteration(table_name: str, entity_type: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(iteration),0)+1 AS n FROM feedback_log "
            "WHERE table_name=? AND entity_type=?",
            (table_name, entity_type),
        ).fetchone()
    return row["n"]
