"""Canonical feedback keys.

Feedback is keyed on the *meaning* of a rule, not on any id the LLM generated.
Two runs that phrase the same check differently must collide here, or the diff
in step 7 degenerates into free-text comparison and reports noise.

Production equivalent: crud/selfheal_ai_suggestions.py::make_rule_signature.
"""
from __future__ import annotations

import json
import re

# Canonical rule_type vocabulary. The generation prompt is constrained to emit
# only these, so the signature space stays closed and diffable.
RULE_TYPES = {
    "not_null",
    "positive_number",
    "non_negative",
    "date_order",
    "range_check",
    "in_list",
    "unique",
    "arithmetic_consistency",
    "length_check",
    "pattern_match",
    "cross_field_compare",
    "freshness",
}

HEAL_STRATEGIES = {
    "mode_fill",
    "mean_fill",
    "median_fill",
    "constant_fill",
    "reference_lookup",
    "derive_from_formula",
    "null_out",
    "drop_row",
}


def _norm(token: str) -> str:
    """lowercase, strip quoting/whitespace, collapse separators."""
    t = token.strip().strip('"').strip("`").strip("'").lower()
    return re.sub(r"[^a-z0-9_]+", "_", t).strip("_")


def make_rule_signature(columns: list[str], rule_type: str) -> str:
    """(sorted(columns), rule_type) -> stable string key.

    Sorted so ship_date/order_date and order_date/ship_date are one signature.
    """
    cols = sorted({_norm(c) for c in columns if _norm(c)})
    rt = _norm(rule_type)
    return f"rule::{rt}::{'+'.join(cols)}"


def make_heal_signature(
    table: str, target_column: str, failing_rule_type: str, strategy: str
) -> str:
    """Self-heal suggestions are keyed per (table, column, what failed, how we fix it).

    Table is included here (unlike rule signatures) because a remediation is a
    write against a specific table; the same fix on a different table is a
    different decision. Cross-table reuse for heals goes through the vector
    path, never through exact signature match.
    """
    return (
        f"heal::{_norm(table)}::{_norm(target_column)}"
        f"::{_norm(failing_rule_type)}::{_norm(strategy)}"
    )


def make_anomaly_signature(table: str, column: str, anomaly_type: str) -> str:
    """Key a profiling anomaly on meaning, never on magnitude or run date.

    ydata alerts carry no id at all, and embed their magnitude in the text
    ("[col] 5854 (64.1%) missing values"). If the magnitude entered the key,
    every profiling run would mint a brand-new signature as the percentage
    drifted and feedback would never replay. The magnitude is preserved
    separately as suppression_bound so dismissal can be scoped instead of
    permanent.
    """
    return f"anomaly::{_norm(table)}::{_norm(column)}::{_norm(anomaly_type)}"


def parse_signature(sig: str) -> dict:
    parts = sig.split("::")
    if parts[0] == "rule":
        return {
            "kind": "rule",
            "rule_type": parts[1],
            "columns": parts[2].split("+") if len(parts) > 2 and parts[2] else [],
        }
    if parts[0] == "heal":
        return {
            "kind": "heal",
            "table": parts[1],
            "target_column": parts[2],
            "failing_rule_type": parts[3],
            "strategy": parts[4],
        }
    if parts[0] == "anomaly":
        if len(parts) != 4:
            raise ValueError(
                f"anomaly signature must be anomaly::<table>::<column>::<type>, got {sig!r}"
            )
        return {
            "kind": "anomaly",
            "table": parts[1],
            "column": parts[2],
            "anomaly_type": parts[3],
        }
    raise ValueError(f"unrecognised signature: {sig}")


def signature_of_rule(rule: dict) -> str:
    """Signature for one rule dict emitted by the generation prompt."""
    return make_rule_signature(rule["columns"], rule["rule_type"])


def columns_json(columns: list[str]) -> str:
    return json.dumps(sorted({_norm(c) for c in columns if _norm(c)}))
