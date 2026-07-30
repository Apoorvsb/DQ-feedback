"""Signature-keyed diff.

Compares on the signature tuple, never on statement strings. Free-text
comparison would report every reworded statement as a change and drown the
signal the demo depends on.
"""
from __future__ import annotations

from typing import Iterable

DROPPED, ADDED, CHANGED, KEPT = "DROPPED", "ADDED", "CHANGED", "KEPT"


def _index(rules: Iterable[dict]) -> dict[str, dict]:
    return {r["signature"]: r for r in rules}


def diff_runs(before: dict, after: dict, key: str = "rules") -> list[dict]:
    a, b = _index(before[key]), _index(after[key])
    rows = []

    for sig in a.keys() - b.keys():
        rows.append({"status": DROPPED, "signature": sig, "before": a[sig], "after": None})

    for sig in b.keys() - a.keys():
        rows.append({"status": ADDED, "signature": sig, "before": None, "after": b[sig]})

    for sig in a.keys() & b.keys():
        ra, rb = a[sig], b[sig]
        changed_fields = [
            f for f in ("expression", "severity")
            if str(ra.get(f, "")).strip() != str(rb.get(f, "")).strip()
        ]
        rows.append({
            "status": CHANGED if changed_fields else KEPT,
            "signature": sig,
            "before": ra,
            "after": rb,
            "changed_fields": changed_fields,
        })

    order = {DROPPED: 0, CHANGED: 1, ADDED: 2, KEPT: 3}
    rows.sort(key=lambda r: (order[r["status"]], r["signature"]))
    return rows


def render(rows: list[dict], pinned: set[str] | None = None) -> str:
    pinned = pinned or set()
    out = []
    for r in rows:
        sig = r["signature"]
        tag = "  (pinned by feedback)" if sig in pinned else ""
        if r["status"] == DROPPED:
            out.append(f"DROPPED  {sig}\n           was: {r['before']['expression']}{tag}")
        elif r["status"] == ADDED:
            out.append(f"ADDED    {sig}\n           now: {r['after']['expression']}{tag}")
        elif r["status"] == CHANGED:
            out.append(
                f"CHANGED  {sig}\n"
                f"           {r['before']['expression']}\n"
                f"        -> {r['after']['expression']}{tag}"
            )
        else:
            out.append(f"KEPT     {sig}{tag}")
    return "\n".join(out)


def summarize(rows: list[dict]) -> dict[str, int]:
    counts = {DROPPED: 0, ADDED: 0, CHANGED: 0, KEPT: 0}
    for r in rows:
        counts[r["status"]] += 1
    return counts


def churn(rows: list[dict]) -> int:
    """Number of signatures that are not identical between the two runs."""
    return sum(1 for r in rows if r["status"] != KEPT)
