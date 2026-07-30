"""Measure run-to-run churn with NO feedback.

This replaces temperature=0, which gemini-3.x no longer supports. If N runs of
the identical prompt produce a mean churn of ~0-1 signatures, then a v1->v2 diff
showing 3 targeted changes is demonstrably caused by feedback rather than by
sampling. Run this once before the demo and quote the number.
"""
from __future__ import annotations

from itertools import combinations

from pipelines import diff as diffmod
from pipelines import generate


def measure(table_name: str, runs: int = 3) -> dict:
    payloads = []
    for i in range(runs):
        p = generate.generate_rules(
            table_name, run_label=f"noise_{i+1}", use_feedback=False
        )
        payloads.append(p)
        print(f"  noise_{i+1}: {len(p['rules'])} rules")

    pairs = []
    for (i, a), (j, b) in combinations(list(enumerate(payloads)), 2):
        rows = diffmod.diff_runs(a, b)
        c = diffmod.churn(rows)
        pairs.append({
            "pair": f"noise_{i+1} vs noise_{j+1}",
            "churn": c,
            "counts": diffmod.summarize(rows),
        })

    churns = [p["churn"] for p in pairs]
    return {
        "table": table_name,
        "runs": runs,
        "pairs": pairs,
        "mean_churn": round(sum(churns) / len(churns), 2) if churns else 0.0,
        "max_churn": max(churns) if churns else 0,
    }


def render(result: dict) -> str:
    lines = [
        f"Noise floor for {result['table']} ({result['runs']} runs, no feedback):",
    ]
    for p in result["pairs"]:
        c = p["counts"]
        lines.append(
            f"  {p['pair']:<22} churn={p['churn']}  "
            f"(dropped={c['DROPPED']} added={c['ADDED']} changed={c['CHANGED']} kept={c['KEPT']})"
        )
    lines.append(f"  mean churn = {result['mean_churn']}   max churn = {result['max_churn']}")
    lines.append("")
    lines.append(
        "  Any v1->v2 change above this floor is attributable to feedback, not sampling."
    )
    return "\n".join(lines)
