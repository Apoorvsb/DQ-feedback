"""Build the prompt-injection block.

Two rules learned the hard way and encoded here:
  1. Inject `normalized_directive`, never `raw_comment`. The raw text is kept
     for audit only. Anything that reaches the model has been through G2.
  2. Phrase imperatively ("do NOT emit", "use exactly"). Soft phrasing gets
     ignored; this block has to outrank the model's own judgement.
"""
from __future__ import annotations

from store import chroma_store, sqlite_store

HEADER = "### Prior reviewer feedback — honor these EXACTLY. They override your own judgement."

_VERB = {
    "reject": "REJECTED",
    "correct": "CORRECTED",
    "confirm": "CONFIRMED",
    "add": "MISSING",
}


def _line(row: dict) -> str:
    tag = _VERB.get(row["action"], row["action"].upper())
    text = row["normalized_directive"] or row["raw_comment"]

    # Anomaly dismissals are ALWAYS magnitude-bounded. Emitting a bare
    # "do not report X" here would silence the column at every severity,
    # which is the failure mode the bound exists to prevent.
    if row["entity_type"] == "anomaly":
        if row["action"] == "reject":
            bound = row["suppression_bound"]
            return (
                f"- SUPPRESSED: {text} Do NOT report this anomaly while its "
                f"magnitude is at or below {bound:.1f}%. "
                f"If it exceeds {bound:.1f}%, you MUST still report it."
            )
        if row["action"] == "confirm":
            return f"- CONFIRMED REAL: {text} Always report this anomaly."
        return f"- {tag}: {text}"

    if row["action"] == "add" and row["corrected_expression"]:
        return (
            f'- {tag}: {text} You MUST emit this rule. Use exactly: '
            f'`{row["corrected_expression"]}`'
        )
    if row["action"] == "correct" and row["corrected_expression"]:
        return f'- {tag}: {text} Use exactly: `{row["corrected_expression"]}`'
    return f"- {tag}: {text}"


def build_direct_block(table_name: str, entity_type: str = "rule") -> tuple[str, list[int]]:
    """Exact-signature feedback for this table. Handles same-table replay."""
    rows = sqlite_store.live_feedback(entity_type=entity_type, table_name=table_name)
    if not rows:
        return "", []
    lines = [HEADER] + [_line(r) for r in rows]
    return "\n".join(lines), [r["id"] for r in rows]


def build_transfer_block(
    profile: dict, entity_type: str = "rule", exclude_table: str | None = None
) -> tuple[str, list[int], list[dict]]:
    """Cross-table feedback found by semantic similarity.

    This is what carries the discount correction from orders.total_amount to
    sales_transactions.gross_total — a table that never received feedback.
    Every hit is above the distance threshold; below-threshold matches are
    dropped, not weakened, because a wrong transfer is worse than no transfer.
    """
    hits = chroma_store.query_for_profile(
        profile, entity_type=entity_type, exclude_table=exclude_table
    )
    if not hits:
        return "", [], []

    lines = [
        HEADER,
        f"(Transferred from prior review of other tables in the same domain. "
        f"Map the intent onto {profile['table_name']}'s column names.)",
    ]
    ids = []
    for h in hits:
        m = h["metadata"]
        tag = _VERB.get(m["action"], m["action"].upper())
        line = f'- {tag} (from {m["table_name"]}.{m["columns"]}): {h["directive"]}'
        if m.get("corrected_expression"):
            line += f' Original expression: `{m["corrected_expression"]}`'
        lines.append(line)
        ids.append(h["feedback_id"])

    return "\n".join(lines), ids, hits


def build_block(
    profile: dict, entity_type: str = "rule", include_transfer: bool = True
) -> tuple[str, list[int], list[dict]]:
    """Direct feedback first, then transferred. Returns (block, ids, transfer_hits)."""
    table_name = profile["table_name"]
    direct, direct_ids = build_direct_block(table_name, entity_type)

    transfer, transfer_ids, hits = ("", [], [])
    if include_transfer:
        transfer, transfer_ids, hits = build_transfer_block(
            profile, entity_type, exclude_table=table_name
        )

    if direct and transfer:
        # Strip the duplicate header from the transfer half.
        transfer_body = "\n".join(transfer.split("\n")[1:])
        block = f"{direct}\n{transfer_body}"
    else:
        block = direct or transfer

    return block, direct_ids + transfer_ids, hits
