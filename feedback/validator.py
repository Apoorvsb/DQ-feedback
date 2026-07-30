"""The validation gate. Four checks, cheapest first.

Why this exists: feedback in this system is persistent. It is replayed into
every future generation, so an accepted garbage entry is not a one-off bad
response — it is permanent corruption of the loop. That asymmetry (cheap to
reject, expensive to accept wrongly) is what justifies four gates instead of
trusting the model to cope.

  G0 COHERENCE  deterministic, ~1ms, no API call   -> catches "hygyt"
  G1 STRUCTURAL deterministic, sqlglot parse       -> catches broken/unsafe SQL
  G2 SEMANTIC   one LLM call, flash-lite           -> catches "make it better"
  G3 CONSISTENCY deterministic, DB read            -> catches contradictions

Only feedback that clears all four is stored valid+active, and only valid+active
rows are ever injected into a prompt or embedded into Chroma.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

import config
from feedback.coherence import check_coherence
from feedback.alerts import ANOMALY_TYPES
from feedback.signature import HEAL_STRATEGIES, RULE_TYPES, parse_signature
from llm import client as llm_client
from llm import prompts
from llm.schemas import SemanticVerdict
from store import sqlite_store

# Never allowed in a user-supplied expression, in either entity type.
FORBIDDEN_SQL = {
    "DROP", "TRUNCATE", "DELETE", "ALTER", "GRANT", "REVOKE",
    "CREATE", "INSERT", "MERGE", "ATTACH", "PRAGMA", "EXEC",
}


@dataclass
class GateResult:
    passed: bool
    gate: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationOutcome:
    status: str                    # valid | needs_clarification | rejected
    gate_failed: str | None
    reason: str
    normalized_directive: str = ""
    inferred_action: str = ""
    confidence: float = 0.0
    clarifying_question: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status == "valid"


# --- G1: structural --------------------------------------------------------
def _columns_of(profile: dict) -> set[str]:
    """Column names from either fixture shape.

    Rule/self-heal fixtures use {"columns": [{"name": ...}]}; a ydata profiling
    report keys them under "variables".
    """
    if "columns" in profile:
        return {c["name"].lower() for c in profile["columns"]}
    return {c.lower() for c in profile.get("variables", {})}


def _gate1_anomaly(
    sig: dict,
    action: str,
    suppression_bound: float | None,
    table_cols: set[str],
    detail: dict,
) -> GateResult:
    """Anomaly-specific structural checks.

    The load-bearing one is the bound requirement on dismissal. Without it a
    single "not a problem" silences that anomaly class on that column forever,
    so a 64%-missing dismissal would also hide the column at 100% — i.e. the
    exact pipeline failure you most need to see.
    """
    if sig["column"] not in table_cols:
        return GateResult(
            False, "G1_STRUCTURAL",
            f"column {sig['column']!r} is not in the profiling report", detail,
        )

    if action == "add":
        return GateResult(
            False, "G1_STRUCTURAL",
            "action=add is not meaningful for anomalies — they are emitted by the "
            "profiler, not chosen by the model. Adjust the profiler instead.",
            detail,
        )

    if action == "reject":
        if suppression_bound is None:
            return GateResult(
                False, "G1_STRUCTURAL",
                "dismissing an anomaly requires a suppression bound (--bound). "
                "An unbounded dismissal permanently hides this anomaly on this "
                "column at every magnitude, including a future 100% outage.",
                detail,
            )
        if not (0.0 <= suppression_bound <= 100.0):
            return GateResult(
                False, "G1_STRUCTURAL",
                f"suppression bound must be a percent 0-100, got {suppression_bound}",
                detail,
            )
        if suppression_bound >= 100.0:
            return GateResult(
                False, "G1_STRUCTURAL",
                "a bound of 100% suppresses the anomaly unconditionally, which is "
                "what the bound exists to prevent; use a value below 100",
                detail,
            )
        detail["suppression_bound"] = suppression_bound

    return GateResult(True, "G1_STRUCTURAL", "ok", detail)


def gate1_structural(
    action: str,
    signature: str,
    corrected_expression: str | None,
    profile: dict,
    known_signatures: set[str] | None,
    entity_type: str,
    suppression_bound: float | None = None,
) -> GateResult:
    detail: dict[str, Any] = {}

    if action not in {"reject", "correct", "confirm", "add"}:
        return GateResult(False, "G1_STRUCTURAL",
                          f"action must be reject/correct/confirm/add, got {action!r}")

    try:
        parsed_sig = parse_signature(signature)
    except ValueError as e:
        return GateResult(False, "G1_STRUCTURAL", str(e))
    detail["signature"] = parsed_sig

    _vocab = {
        "rule": (RULE_TYPES, "rule_type"),
        "heal": (HEAL_STRATEGIES, "strategy"),
        "anomaly": (ANOMALY_TYPES, "anomaly_type"),
    }
    vocab, key = _vocab[parsed_sig["kind"]]
    if parsed_sig[key] not in vocab:
        return GateResult(False, "G1_STRUCTURAL",
                          f"unknown {key} {parsed_sig[key]!r}; expected one of {sorted(vocab)}")

    # Feedback must attach to something that actually exists in the run —
    # except 'add', which asserts the opposite: the rule is MISSING.
    if known_signatures is not None:
        present = signature in known_signatures
        if action == "add" and present:
            return GateResult(
                False, "G1_STRUCTURAL",
                f"action=add says this rule is missing, but {signature!r} was already "
                "emitted in the run under review — use correct or confirm instead",
                detail,
            )
        if action != "add" and not present:
            return GateResult(
                False, "G1_STRUCTURAL",
                f"signature {signature!r} is not present in the run being reviewed — "
                "you cannot give feedback on an artefact that was not generated "
                "(use action=add if the rule is missing and should exist)",
                detail,
            )

    table_cols = _columns_of(profile)

    # Columns named in the signature must exist (rule signatures only; heal
    # signatures are already table-scoped and validated above).
    if parsed_sig["kind"] == "rule":
        missing = [c for c in parsed_sig["columns"] if c not in table_cols]
        if missing:
            return GateResult(
                False, "G1_STRUCTURAL",
                f"columns not in {profile['table_name']}: {', '.join(missing)}",
                detail,
            )

    if parsed_sig["kind"] == "anomaly":
        return _gate1_anomaly(parsed_sig, action, suppression_bound, table_cols, detail)

    # reject/confirm carry no SQL, so there is nothing further to check.
    if action not in {"correct", "add"}:
        return GateResult(True, "G1_STRUCTURAL", "ok", detail)

    # --- correct/add: the supplied expression must be real, safe SQL ---
    if not corrected_expression or not corrected_expression.strip():
        return GateResult(False, "G1_STRUCTURAL",
                          f"action={action} requires an expression", detail)

    upper = corrected_expression.upper()
    hit = [kw for kw in FORBIDDEN_SQL if _has_keyword(upper, kw)]
    if hit:
        return GateResult(False, "G1_STRUCTURAL",
                          f"expression contains forbidden keyword(s): {', '.join(sorted(hit))}",
                          detail)

    try:
        tree = sqlglot.parse_one(corrected_expression)
    except Exception as e:  # sqlglot raises several types
        return GateResult(False, "G1_STRUCTURAL",
                          f"expression does not parse as SQL: {e}", detail)
    if tree is None:
        return GateResult(False, "G1_STRUCTURAL", "expression parsed to nothing", detail)

    refs = {c.name.lower() for c in tree.find_all(exp.Column)}
    detail["referenced_columns"] = sorted(refs)
    unknown = sorted(refs - table_cols)
    if unknown:
        return GateResult(
            False, "G1_STRUCTURAL",
            f"expression references columns that do not exist in "
            f"{profile['table_name']}: {', '.join(unknown)}",
            detail,
        )

    # Self-heal writes rows, so the bar is higher.
    if entity_type == "selfheal":
        heal = _check_heal_sql(corrected_expression, profile)
        if heal:
            return GateResult(False, "G1_STRUCTURAL", heal, detail)

    return GateResult(True, "G1_STRUCTURAL", "ok", detail)


def _has_keyword(sql_upper: str, keyword: str) -> bool:
    import re
    return re.search(rf"\b{keyword}\b", sql_upper) is not None


def _check_heal_sql(sql: str, profile: dict) -> str | None:
    """Self-heal corrections that are full statements must be a guarded UPDATE."""
    upper = sql.upper()
    if "UPDATE" not in upper:
        return None  # a bare replacement expression, not a statement — fine
    try:
        tree = sqlglot.parse_one(sql)
    except Exception as e:
        return f"self-heal SQL does not parse: {e}"
    if not isinstance(tree, exp.Update):
        return "self-heal SQL must be a single UPDATE statement"
    if tree.args.get("where") is None:
        return (
            "self-heal UPDATE has no WHERE clause — this would rewrite every row "
            "in the table"
        )
    target = tree.find(exp.Table)
    if target and target.name.lower() != profile["table_name"].lower():
        return (
            f"self-heal UPDATE targets {target.name!r} but the artefact under review "
            f"is on {profile['table_name']!r}"
        )
    return None


# --- G2: semantic ----------------------------------------------------------
def gate2_semantic(artefact: dict, comment: str, action: str, profile: dict) -> GateResult:
    prompt = prompts.build_validator_prompt(artefact, comment, action, profile)
    verdict: SemanticVerdict = llm_client.structured(
        prompt,
        SemanticVerdict,
        model=config.VALIDATOR_MODEL,
        system=prompts.VALIDATOR_SYSTEM,
    )
    d = verdict.model_dump()

    if verdict.verdict != "ACTIONABLE":
        return GateResult(False, "G2_SEMANTIC",
                          f"{verdict.verdict}: {verdict.rationale}", d)

    if verdict.confidence < config.G2_MIN_CONFIDENCE:
        d["_below_threshold"] = config.G2_MIN_CONFIDENCE
        return GateResult(
            False, "G2_SEMANTIC",
            f"confidence {verdict.confidence:.2f} is below the "
            f"{config.G2_MIN_CONFIDENCE:.2f} bar: {verdict.rationale}",
            d,
        )

    if not verdict.normalized_directive.strip():
        return GateResult(False, "G2_SEMANTIC",
                          "model judged it actionable but produced no directive", d)

    return GateResult(True, "G2_SEMANTIC", verdict.rationale, d)


# --- G3: consistency -------------------------------------------------------
def gate3_consistency(signature: str, action: str, corrected_expression: str | None) -> GateResult:
    """Detect contradiction with existing live feedback on the same signature.

    This does not reject. Newest feedback wins — a user is allowed to change
    their mind — but the conflict is recorded and the old row is superseded so
    the injection block never carries both instructions at once.
    """
    existing = sqlite_store.active_for_signature(signature)
    if not existing:
        return GateResult(True, "G3_CONSISTENCY", "no prior feedback on this signature")

    conflicts = []
    for row in existing:
        if row["action"] != action:
            conflicts.append({
                "old_id": row["id"], "kind": "action_contradiction",
                "detail": f"{row['action']} -> {action}",
            })
        elif action == "correct" and row["corrected_expression"] != corrected_expression:
            conflicts.append({
                "old_id": row["id"], "kind": "expression_change",
                "detail": f"{row['corrected_expression']!r} -> {corrected_expression!r}",
            })

    return GateResult(
        True, "G3_CONSISTENCY",
        f"{len(conflicts)} prior entr{'y' if len(conflicts)==1 else 'ies'} will be superseded"
        if conflicts else "consistent with prior feedback",
        {"conflicts": conflicts},
    )


# --- orchestration ---------------------------------------------------------
def validate(
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
) -> ValidationOutcome:
    """Run all four gates. Short-circuits on the first hard failure."""
    schema_terms = _columns_of(profile) | {profile.get("table_name", "")}
    trace: dict[str, Any] = {}

    # G0
    g0 = check_coherence(comment, schema_terms)
    trace["G0_COHERENCE"] = g0
    if not g0["passed"]:
        return ValidationOutcome(
            status="rejected", gate_failed="G0_COHERENCE",
            reason=g0["reason"], detail=trace,
        )

    # G1
    g1 = gate1_structural(
        action, signature, corrected_expression, profile, known_signatures,
        entity_type, suppression_bound,
    )
    trace["G1_STRUCTURAL"] = {"passed": g1.passed, "reason": g1.reason, **g1.detail}
    if not g1.passed:
        return ValidationOutcome(
            status="rejected", gate_failed="G1_STRUCTURAL",
            reason=g1.reason, detail=trace,
        )

    # G2
    if skip_llm:
        trace["G2_SEMANTIC"] = {"skipped": True}
        directive, inferred, conf = comment.strip(), action, 1.0
    else:
        g2 = gate2_semantic(artefact, comment, action, profile)
        trace["G2_SEMANTIC"] = {"passed": g2.passed, "reason": g2.reason, **g2.detail}
        if not g2.passed:
            vague = g2.detail.get("verdict") == "VAGUE" or "confidence" in g2.reason
            return ValidationOutcome(
                status="needs_clarification" if vague else "rejected",
                gate_failed="G2_SEMANTIC",
                reason=g2.reason,
                clarifying_question=g2.detail.get("clarifying_question", ""),
                confidence=g2.detail.get("confidence", 0.0),
                detail=trace,
            )
        directive = g2.detail["normalized_directive"]
        inferred = g2.detail.get("inferred_action", action)
        conf = g2.detail.get("confidence", 0.0)

    # G3
    g3 = gate3_consistency(signature, action, corrected_expression)
    trace["G3_CONSISTENCY"] = {"passed": g3.passed, "reason": g3.reason, **g3.detail}

    return ValidationOutcome(
        status="valid", gate_failed=None, reason=g3.reason,
        normalized_directive=directive, inferred_action=inferred,
        confidence=conf, detail=trace,
    )


def format_trace(outcome: ValidationOutcome) -> str:
    """Human-readable gate trace for `cli.py feedback add --explain`."""
    order = ["G0_COHERENCE", "G1_STRUCTURAL", "G2_SEMANTIC", "G3_CONSISTENCY"]
    lines = []
    for gate in order:
        info = outcome.detail.get(gate)
        if info is None:
            lines.append(f"  {gate:<16} —      not reached")
            continue
        if info.get("skipped"):
            lines.append(f"  {gate:<16} SKIP")
            continue
        ok = info.get("passed", True)
        mark = "PASS" if ok else "FAIL"
        reason = info.get("reason", "")
        lines.append(f"  {gate:<16} {mark}   {reason}")
    return "\n".join(lines)
