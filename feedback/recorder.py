"""Persist validated feedback. The only writer to feedback_log."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import config
from feedback import validator
from feedback.signature import columns_json, parse_signature
from store import chroma_store, sqlite_store


def record(
    *,
    comment: str,
    action: str,
    signature: str,
    artefact: dict,
    profile: dict,
    entity_type: str = "rule",
    corrected_expression: str | None = None,
    known_signatures: set[str] | None = None,
    skip_llm: bool = False,
    suppression_bound: float | None = None,
    observed_magnitude: float | None = None,
) -> tuple[validator.ValidationOutcome, int | None]:
    """Validate, then persist. Returns (outcome, feedback_id or None).

    Rejected and needs-clarification submissions go to validation_audit, NOT to
    feedback_log — they must never be reachable by the injection query.
    """
    # A dismissal with no explicit bound is scoped to what the reviewer actually
    # saw, plus headroom — never to "always". This is the difference between
    # "stop nagging me about 64% missing" and going blind to a 100% outage.
    if (
        entity_type == "anomaly"
        and action == "reject"
        and suppression_bound is None
        and observed_magnitude is not None
    ):
        suppression_bound = min(
            99.9, observed_magnitude + config.ANOMALY_DEFAULT_BOUND_HEADROOM
        )

    outcome = validator.validate(
        comment=comment,
        action=action,
        signature=signature,
        artefact=artefact,
        profile=profile,
        entity_type=entity_type,
        corrected_expression=corrected_expression,
        known_signatures=known_signatures,
        skip_llm=skip_llm,
        suppression_bound=suppression_bound,
    )

    table_name = profile.get("table_name") or profile.get("_table_name", "unknown")

    if not outcome.accepted:
        sqlite_store.insert_audit(
            signature=signature,
            entity_type=entity_type,
            table_name=table_name,
            raw_comment=comment,
            gate_failed=outcome.gate_failed or "UNKNOWN",
            reason=outcome.reason,
            detail=json.dumps(outcome.detail, default=str),
        )
        return outcome, None

    sig = parse_signature(signature)
    if sig["kind"] == "rule":
        cols, rule_type = sig["columns"], sig["rule_type"]
    elif sig["kind"] == "heal":
        cols, rule_type = [sig["target_column"]], sig["failing_rule_type"]
    else:
        cols, rule_type = [sig["column"]], sig["anomaly_type"]

    expires_at = None
    dismiss_count = 1
    if entity_type == "anomaly":
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(days=config.ANOMALY_FEEDBACK_TTL_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        if action == "reject":
            # Carry the running total forward so supersession and expiry never
            # reset the "you have dismissed this N times" signal.
            dismiss_count = sqlite_store.total_dismissals(signature) + 1

    fid = sqlite_store.insert_feedback(
        signature=signature,
        entity_type=entity_type,
        table_name=table_name,
        columns_json=columns_json(cols),
        rule_type=rule_type,
        action=action,
        raw_comment=comment,
        normalized_directive=outcome.normalized_directive,
        corrected_expression=corrected_expression,
        validation_status="valid",
        validation_reason=outcome.reason,
        validation_detail=json.dumps(outcome.detail, default=str),
        confidence=outcome.confidence,
        suppression_bound=suppression_bound,
        expires_at=expires_at,
        dismiss_count=dismiss_count,
        iteration=sqlite_store.next_iteration(table_name, entity_type),
    )
    outcome.detail["_persisted"] = {
        "suppression_bound": suppression_bound,
        "expires_at": expires_at,
        "dismiss_count": dismiss_count,
        "escalate": dismiss_count >= config.ANOMALY_DISMISS_ESCALATION,
    }

    # G3 found contradictions: newest wins, older rows are retired so the
    # injection block can never carry both instructions at once.
    for c in outcome.detail.get("G3_CONSISTENCY", {}).get("conflicts", []):
        sqlite_store.supersede(c["old_id"], fid, signature, c["kind"], c["detail"])
        chroma_store.remove(c["old_id"])

    # Indexing is best-effort on purpose. SQLite is the source of truth and
    # Chroma is rebuildable with `cli.py reindex`, so an embedding failure must
    # never cost us the durable write we just made.
    try:
        chroma_store.add(
            feedback_id=fid,
            directive=outcome.normalized_directive,
            metadata={
                "signature": signature,
                "entity_type": entity_type,
                "table_name": table_name,
                "action": action,
                "rule_type": rule_type,
                "columns": ",".join(cols),
                "corrected_expression": corrected_expression or "",
            },
        )
    except Exception as e:
        print(
            f"! chroma index failed for feedback id={fid}: {e}\n"
            f"  stored in SQLite; run `python cli.py reindex` once the API is reachable",
            file=sys.stderr,
        )

    return outcome, fid
