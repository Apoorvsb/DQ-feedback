"""Typed LLM contracts. Every call returns a parsed Pydantic object, never raw text.

Mirrors the prod convention in crud/llm_service.py (PROMPT | llm | PydanticOutputParser).
Optional fields use "" rather than None because Gemini's structured-output schema
conversion handles required-with-empty-default far more reliably than nullable.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RuleType = Literal[
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
]

Severity = Literal["high", "medium", "low"]

HealStrategy = Literal[
    "mode_fill",
    "mean_fill",
    "median_fill",
    "constant_fill",
    "reference_lookup",
    "derive_from_formula",
    "null_out",
    "drop_row",
]


# --- Rule generation -------------------------------------------------------
class GeneratedRule(BaseModel):
    columns: list[str] = Field(description="Columns this rule touches, exact names.")
    rule_type: RuleType
    statement: str = Field(description="One-line natural-language statement of the check.")
    expression: str = Field(description="SQL boolean expression that is TRUE when the row PASSES.")
    severity: Severity
    reason: str = Field(description="Why this rule is warranted, citing the profiling stats.")


class RuleSet(BaseModel):
    rules: list[GeneratedRule]


# --- Gate 2: semantic feedback validation ----------------------------------
class SemanticVerdict(BaseModel):
    """Is this feedback actionable, and what exactly is it asking for?"""

    verdict: Literal[
        "ACTIONABLE",         # clear, specific, can be turned into a directive
        "VAGUE",              # on-topic but not specific enough to act on
        "OFF_TOPIC",          # coherent English, nothing to do with this rule
        "INCOHERENT",         # not meaningful language
        "CONTRADICTS_SCHEMA", # asks for something the table cannot support
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    normalized_directive: str = Field(
        description=(
            "Imperative one-liner for prompt injection, e.g. "
            "'Do NOT emit a date_order rule on ship_date vs order_date.' "
            "Empty string if verdict is not ACTIONABLE."
        )
    )
    inferred_action: Literal["reject", "correct", "confirm", "none"]
    referenced_columns: list[str]
    clarifying_question: str = Field(
        description="If VAGUE, the single question to ask the user. Otherwise empty string."
    )
    rationale: str = Field(description="One sentence explaining the verdict.")


# --- Self-heal generation --------------------------------------------------
class HealSuggestion(BaseModel):
    target_column: str
    failing_rule_type: str
    strategy: HealStrategy
    replacement_expression: str = Field(description="The value/expression to write.")
    update_sql: str = Field(description="Single UPDATE statement. MUST include a WHERE clause.")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class HealSet(BaseModel):
    suggestions: list[HealSuggestion]


# --- Profiling anomaly insights -------------------------------------------
AnomalyType = Literal[
    "missing_values",
    "constant_value",
    "unique_values",
    "high_correlation",
    "high_cardinality",
    "imbalance",
    "skewed",
    "zeros",
    "uniform",
    "duplicates",
    "infinite",
    "unknown",
]


class AnomalyInsight(BaseModel):
    """One reviewed profiling anomaly.

    The model does NOT invent these — it is given the parsed ydata alerts and
    decides which are worth surfacing, how severe they are, and why. Anything
    it emits must trace back to a supplied alert.
    """

    column: str
    anomaly_type: AnomalyType
    magnitude_pct: float = Field(
        ge=0.0, le=100.0, description="Percent, 0-100. Copy from the supplied alert."
    )
    severity: Severity
    is_actionable: bool = Field(
        description="False for known-benign structural facts (e.g. an intentionally "
        "constant flag column) that should not page anyone."
    )
    insight: str = Field(description="One-line plain-language statement of the problem.")
    likely_cause: str
    recommended_action: str


class AnomalySet(BaseModel):
    insights: list[AnomalyInsight]
