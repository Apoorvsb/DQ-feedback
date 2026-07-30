"""Prompt construction.

Design note on placement: the feedback block goes LAST, immediately before the
output instruction. Instructions late in the prompt are followed more reliably,
and this block is the whole point of the system — it must not be buried above
the schema dump.
"""
from __future__ import annotations

import json

RULE_SYSTEM = """You are a data-quality engineer. You propose validation rules for a \
table given its schema and profiling statistics.

Rules:
- Emit ONLY rules justified by the schema or the statistics shown.
- `expression` must be a SQL boolean that is TRUE when a row PASSES the check.
- Use the exact column names given. Never invent columns.
- Prefer few, high-value rules over exhaustive coverage. Aim for 6-9 rules.
- If prior feedback is supplied, it OVERRIDES your own judgement without exception."""

HEAL_SYSTEM = """You are a data remediation engineer. Given a data-quality rule that is \
failing on a table, you propose how to repair the offending rows.

Rules:
- Every `update_sql` MUST be a single UPDATE with a WHERE clause that targets only failing rows.
- Never emit DROP, TRUNCATE, DELETE, ALTER or MERGE.
- Prefer evidence-based fills (mode/median from the same table) over constants.
- If prior feedback is supplied, it OVERRIDES your own judgement without exception."""

VALIDATOR_SYSTEM = """You judge whether a human's feedback on an automatically generated \
data-quality artefact is specific enough to act on. You do NOT decide whether the feedback \
is correct — only whether it is actionable.

Verdicts:
- ACTIONABLE: states a concrete change. Produce an imperative normalized_directive.
- VAGUE: on-topic but unactionable ("make it better", "this is wrong"). Ask one clarifying question.
- OFF_TOPIC: real language, unrelated to this artefact.
- INCOHERENT: not meaningful language.
- CONTRADICTS_SCHEMA: references columns or types that do not exist in the table.

Be strict. Feedback here is PERSISTENT — it is replayed into every future generation, so a \
bad entry causes permanent damage. When in doubt, prefer VAGUE over ACTIONABLE."""


ANOMALY_SYSTEM = """You review automated data-profiling alerts and decide which ones a \
data team should actually act on.

Rules:
- You are given parsed alerts from a profiling run. Report ONLY on alerts supplied \
to you. Never invent an anomaly.
- Copy `magnitude_pct` verbatim from the supplied alert. Do not recompute it.
- Set is_actionable=false for structural facts that are working as intended \
(a deliberately constant flag, an ID column that is unique by design). Say so in \
likely_cause rather than dropping the alert silently.
- A column that is 100% missing is a pipeline failure, not a data-quality nit. \
Severity should reflect that.
- Be selective. A wall of identical 'missing values' alerts helps nobody; group \
your reasoning and surface what matters.
- If prior reviewer feedback is supplied, it OVERRIDES your own judgement without \
exception, including any instruction to stop reporting an anomaly below a stated \
magnitude."""


def build_anomaly_prompt(
    table_name: str, report: dict, parsed_alerts: list[dict], feedback_block: str = ""
) -> str:
    t = report.get("table", {})
    header = (
        f"TABLE: {table_name}\n"
        f"ROWS: {t.get('no_of_rows'):,}   COLUMNS: {t.get('no_of_variables')}\n"
        f"DUPLICATE ROWS: {t.get('no_of_duplicates')} "
        f"({t.get('percent_of_duplicates')}%)\n"
        f"CELLS MISSING: {t.get('no_of_cells_missing'):,} "
        f"({round((t.get('percent_of_cells_with_missing_values') or 0) * 100, 2)}% of cells)\n"
        f"COLUMNS ENTIRELY EMPTY: {t.get('no_of_variables_all_missing_values')}\n"
        f"COLUMNS WITH ANY MISSING: {t.get('no_of_variables_with_missing_values')}\n"
        f"DATA TYPES: {t.get('data_types')}"
    )

    lines = []
    for a in parsed_alerts:
        mag = f"{a['magnitude_pct']:.1f}%" if a["magnitude_pct"] is not None else "n/a"
        extra = ""
        if a.get("distinct_values") is not None:
            extra = f", distinct={a['distinct_values']}"
        lines.append(
            f"  - column={a['column']} type={a['anomaly_type']} "
            f"magnitude={mag}{extra}"
        )

    parts = [
        header,
        f"PROFILING ALERTS ({len(parsed_alerts)}):\n" + "\n".join(lines),
        "Review these alerts. Return JSON matching the supplied schema.",
    ]
    if feedback_block:
        parts.append(feedback_block)
    return "\n\n".join(parts)


def _profile_block(profile: dict) -> str:
    cols = "\n".join(
        f"  - {c['name']} ({c['dtype']}): null {c['null_pct']}%, "
        f"{c['distinct']} distinct"
        + (f", min={c['min']}, max={c['max']}" if c.get("min") is not None else "")
        for c in profile["columns"]
    )
    notes = "\n".join(f"  - {n}" for n in profile.get("notes", []))
    return (
        f"TABLE: {profile['table_name']} ({profile['row_count']:,} rows)\n"
        f"DESCRIPTION: {profile['description']}\n"
        f"COLUMNS:\n{cols}\n"
        + (f"NOTES:\n{notes}\n" if notes else "")
    )


def build_rule_prompt(profile: dict, feedback_block: str = "") -> str:
    parts = [_profile_block(profile)]
    parts.append(
        "Propose data-quality rules for this table. "
        "Return JSON matching the supplied schema."
    )
    if feedback_block:
        parts.append(feedback_block)
    return "\n\n".join(parts)


def build_heal_prompt(profile: dict, case: dict, feedback_block: str = "") -> str:
    parts = [
        _profile_block(profile),
        (
            "FAILING RULE\n"
            f"  rule_type: {case['failing_rule_type']}\n"
            f"  column:    {case['target_column']}\n"
            f"  statement: {case['statement']}\n"
            f"  failing rows: {case['failing_row_count']:,} of {case['total_rows']:,}\n"
            f"  observed failing values: {json.dumps(case.get('sample_failing_values', []))}"
        ),
        (
            "Propose up to 3 remediation options, best first. "
            "Return JSON matching the supplied schema."
        ),
    ]
    if feedback_block:
        parts.append(feedback_block)
    return "\n\n".join(parts)


def build_validator_prompt(artefact: dict, comment: str, action: str, profile: dict) -> str:
    # Accepts either fixture shape: the rules profile ({"columns":[{"name":...}]})
    # or a ydata profiling report (columns live under "variables").
    if "columns" in profile:
        names = [c["name"] for c in profile["columns"]]
    else:
        names = list(profile.get("variables", {}))
    # A 108-column table would swamp the validator prompt; the validator only
    # needs enough context to spot a hallucinated column reference.
    shown = ", ".join(names[:60]) + (f" … (+{len(names)-60} more)" if len(names) > 60 else "")
    return (
        f"TABLE {profile['table_name']} has columns: {shown}\n\n"
        f"ARTEFACT UNDER REVIEW:\n{json.dumps(artefact, indent=2)}\n\n"
        f"THE USER'S STATED ACTION: {action}\n"
        f"THE USER'S COMMENT (verbatim, may be nonsense):\n"
        f'"""{comment}"""\n\n'
        "Judge the comment. Return JSON matching the supplied schema."
    )
