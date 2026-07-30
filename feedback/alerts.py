"""Parse ydata-profiling alerts into structured, signable anomalies.

Built against a real `build_lightweight_report` payload (data/profiling_report.json,
108 columns / 71 alerts). Alerts arrive as bare strings with no id of any kind:

    "[primary_consumer_unit_weight_uom] 5854 (64.1%) missing values"
    "[classification] has a constant value"
    "[pmi_item_code] has unique values"

Two consequences drive the whole design:

1. NO IDS EXIST. There is nothing stable to key feedback on except meaning, so a
   derived signature is mandatory, not merely preferable.
2. MAGNITUDE IS EMBEDDED IN THE TEXT. "5854 (64.1%)" must be stripped out of the
   signature or every profiling run produces "new" anomalies as the percentage
   drifts, and feedback never replays. But it must be KEPT as a number, because
   bounded suppression is the difference between "stop flagging this" and going
   permanently blind to that column.

UNIT WARNING: the alert text carries percent (64.1), while
`variables[col].percent_of_missing_values` carries a fraction (0.641). Anything
comparing the two must normalise. Everything here is percent, 0-100.
"""
from __future__ import annotations

import re

# ydata's standard alert vocabulary. Only the first three appear in the
# lightweight report, but the full set is recognised so a switch to the complete
# profiler does not silently produce UNKNOWN types.
ALERT_PATTERNS: list[tuple[str, str]] = [
    (r"missing values", "missing_values"),
    (r"has a constant value", "constant_value"),
    (r"has unique values", "unique_values"),
    (r"is highly correlated", "high_correlation"),
    (r"high cardinality", "high_cardinality"),
    (r"is highly imbalanced", "imbalance"),
    (r"is highly skewed", "skewed"),
    (r"zeros", "zeros"),
    (r"is uniformly distributed", "uniform"),
    (r"duplicate rows", "duplicates"),
    (r"infinite values", "infinite"),
]

ANOMALY_TYPES = {t for _, t in ALERT_PATTERNS} | {"unknown"}

_ALERT_RE = re.compile(r"^\[(?P<col>.+?)\]\s*(?P<body>.*)$")
# "5854 (64.1%)" -> count 5854, pct 64.1
_MAG_RE = re.compile(r"(?P<count>[\d,]+)\s*\((?P<pct>[\d.]+)%\)")


def parse_alert(alert: str) -> dict:
    """One alert string -> structured anomaly.

    Returns column, anomaly_type, magnitude_pct (0-100 or None), raw.
    """
    m = _ALERT_RE.match(alert.strip())
    if not m:
        return {
            "column": None,
            "anomaly_type": "unknown",
            "magnitude_pct": None,
            "affected_rows": None,
            "raw": alert,
        }

    col, body = m.group("col"), m.group("body")

    anomaly_type = "unknown"
    for pattern, name in ALERT_PATTERNS:
        if re.search(pattern, body, re.IGNORECASE):
            anomaly_type = name
            break

    magnitude, count = None, None
    mm = _MAG_RE.search(body)
    if mm:
        magnitude = float(mm.group("pct"))
        count = int(mm.group("count").replace(",", ""))
    elif anomaly_type in {"constant_value", "unique_values"}:
        # Structural alerts are absolute, not proportional. Treat them as 100%
        # so a bounded suppression rule still has a number to compare against.
        magnitude = 100.0

    return {
        "column": col,
        "anomaly_type": anomaly_type,
        "magnitude_pct": magnitude,
        "affected_rows": count,
        "raw": alert,
    }


def parse_report(report: dict) -> list[dict]:
    """Every alert in a profiling report, structured and enriched."""
    variables = report.get("variables", {})
    rows = report.get("table", {}).get("no_of_rows")
    out = []
    for alert in report.get("alerts", []):
        a = parse_alert(alert)
        var = variables.get(a["column"], {})
        # percent_of_missing_values is a FRACTION here; convert to percent.
        pmv = var.get("percent_of_missing_values")
        a["profiled_missing_pct"] = round(pmv * 100, 3) if pmv is not None else None
        a["distinct_values"] = var.get("no_of_distinct_values")
        a["total_rows"] = rows
        out.append(a)
    return out


def summarize(parsed: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in parsed:
        counts[a["anomaly_type"]] = counts.get(a["anomaly_type"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
