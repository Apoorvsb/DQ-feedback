"""Generation for both entity types.

On determinism: gemini-3.x removed temperature/top_p/top_k, so the usual
"temp 0 or the demo is unconvincing" trick is unavailable. Instead:
  - v1 is generated ONCE and frozen to disk + the runs table. Never regenerated.
  - noise_floor() generates N times with no feedback and diffs those runs
    against each other, establishing how much churn is sampling noise.
Any v1->v2 change larger than the noise floor is attributable to feedback.
That is a stronger claim than temp 0 ever supported, because it is measured.
"""
from __future__ import annotations

import json
from pathlib import Path

import config
from feedback import injector
from feedback.alerts import parse_report, summarize
from feedback.signature import make_anomaly_signature, signature_of_rule
from llm import client as llm_client
from llm import prompts
from llm.schemas import AnomalySet, HealSet, RuleSet
from store import sqlite_store


def load_profile(table_name: str) -> dict:
    return json.loads((config.DATA_DIR / f"{table_name}.json").read_text())


def load_heal_cases(table_name: str) -> list[dict]:
    cases = json.loads((config.DATA_DIR / "selfheal_cases.json").read_text())
    return cases.get(table_name, [])


def generate_rules(
    table_name: str,
    *,
    run_label: str,
    use_feedback: bool = True,
    include_transfer: bool = True,
) -> dict:
    profile = load_profile(table_name)

    block, fb_ids, hits = ("", [], [])
    if use_feedback:
        block, fb_ids, hits = injector.build_block(
            profile, entity_type="rule", include_transfer=include_transfer
        )

    prompt = prompts.build_rule_prompt(profile, block)
    result: RuleSet = llm_client.structured(
        prompt, RuleSet, model=config.GEN_MODEL, system=prompts.RULE_SYSTEM
    )

    payload = {
        "table_name": table_name,
        "run_label": run_label,
        "model": config.GEN_MODEL,
        "feedback_injected": bool(block),
        "feedback_ids": fb_ids,
        "transfer_hits": [
            {
                "feedback_id": h["feedback_id"],
                "similarity": h["similarity"],
                "distance": round(h["distance"], 4),
                "from_table": h["metadata"]["table_name"],
                "directive": h["directive"],
            }
            for h in hits
        ],
        "rules": [
            {**r.model_dump(), "signature": signature_of_rule(r.model_dump())}
            for r in result.rules
        ],
    }

    sqlite_store.save_run(
        run_label, "rule", table_name, config.GEN_MODEL, payload, fb_ids
    )
    _freeze(table_name, run_label, payload)
    return payload


def generate_selfheal(
    table_name: str, case_id: str, *, run_label: str, use_feedback: bool = True
) -> dict:
    profile = load_profile(table_name)
    cases = load_heal_cases(table_name)
    case = next((c for c in cases if c["case_id"] == case_id), None)
    if case is None:
        raise SystemExit(
            f"unknown case {case_id!r}; available: "
            f"{', '.join(c['case_id'] for c in cases)}"
        )

    block, fb_ids, hits = ("", [], [])
    if use_feedback:
        block, fb_ids, hits = injector.build_block(profile, entity_type="selfheal")

    prompt = prompts.build_heal_prompt(profile, case, block)
    result: HealSet = llm_client.structured(
        prompt, HealSet, model=config.GEN_MODEL, system=prompts.HEAL_SYSTEM
    )

    from feedback.signature import make_heal_signature

    payload = {
        "table_name": table_name,
        "case_id": case_id,
        "run_label": run_label,
        "model": config.GEN_MODEL,
        "feedback_injected": bool(block),
        "feedback_ids": fb_ids,
        "transfer_hits": [
            {
                "feedback_id": h["feedback_id"],
                "similarity": h["similarity"],
                "from_table": h["metadata"]["table_name"],
                "directive": h["directive"],
            }
            for h in hits
        ],
        "suggestions": [
            {
                **s.model_dump(),
                "signature": make_heal_signature(
                    table_name, s.target_column, s.failing_rule_type, s.strategy
                ),
            }
            for s in result.suggestions
        ],
    }

    sqlite_store.save_run(
        f"{case_id}:{run_label}", "selfheal", table_name, config.GEN_MODEL, payload, fb_ids
    )
    _freeze(table_name, f"{case_id}_{run_label}", payload)
    return payload


def load_profiling_report(name: str = "profiling_report") -> dict:
    return json.loads((config.DATA_DIR / f"{name}.json").read_text())


def generate_anomaly_insights(
    table_name: str,
    *,
    run_label: str,
    report_name: str = "profiling_report",
    use_feedback: bool = True,
    include_transfer: bool = False,
) -> dict:
    """Review profiling alerts, honouring prior reviewer feedback.

    Cross-table transfer defaults OFF here. A dismissal is scoped to a specific
    column on a specific table; semantically transferring "ignore missing values
    on printed_co" to another table's column is far more likely to hide a real
    outage than to save anyone time.
    """
    report = load_profiling_report(report_name)
    report["_table_name"] = table_name
    parsed = parse_report(report)

    block, fb_ids, hits = ("", [], [])
    if use_feedback:
        report_for_injector = dict(report, table_name=table_name)
        block, fb_ids, hits = injector.build_block(
            report_for_injector, entity_type="anomaly", include_transfer=include_transfer
        )

    prompt = prompts.build_anomaly_prompt(table_name, report, parsed, block)
    result: AnomalySet = llm_client.structured(
        prompt, AnomalySet, model=config.GEN_MODEL, system=prompts.ANOMALY_SYSTEM
    )

    payload = {
        "table_name": table_name,
        "run_label": run_label,
        "model": config.GEN_MODEL,
        "feedback_injected": bool(block),
        "feedback_ids": fb_ids,
        "transfer_hits": [],
        "alerts_supplied": len(parsed),
        "alert_type_counts": summarize(parsed),
        "insights": [
            {
                **i.model_dump(),
                "signature": make_anomaly_signature(
                    table_name, i.column, i.anomaly_type
                ),
            }
            for i in result.insights
        ],
    }

    sqlite_store.save_run(
        run_label, "anomaly", table_name, config.GEN_MODEL, payload, fb_ids
    )
    _freeze(table_name, f"anomaly_{run_label}", payload)
    return payload


def _freeze(table_name: str, run_label: str, payload: dict) -> Path:
    path = config.RUNS_DIR / f"{table_name}_{run_label}.json"
    path.write_text(json.dumps(payload, indent=2))
    return path
