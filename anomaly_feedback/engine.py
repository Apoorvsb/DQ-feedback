"""Anomaly-rule feedback loop.

Mirrors the production pipeline in backend_repo:
  celery_worker.py :: generate_anomaly_result_from_insights
  crud/sigma_table.py :: generate_anomaly_rules  (the /anomaly_insights endpoint)

Three things are deliberately copied from production rather than invented, so
anything proven here maps straight back:

  1. The rule shape — rule_name / severity / anomaly_type / identifier_columns /
     target_columns / business_impact / sql_query / ...
  2. build_signature() — production's own dedup key, reused here as the feedback
     key. It already exists; today it is only used to avoid emitting the same
     rule twice within one run.
  3. The inputs — correlations and column statistics from a real profiling
     report. The model never sees data rows, only statistics, which is exactly
     why it produces plausible-but-wrong rules and why feedback is needed.

The one thing production does that this deliberately does NOT: return a cached
answer. generate_anomaly_rules() short-circuits on the stored
AnomalyDetectionRules row, so in production feedback can never take effect
against the same profiling snapshot. See README.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Literal

import sqlglot
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlglot import exp

load_dotenv(Path(__file__).parent.parent / ".env")

ROOT = Path(__file__).parent
DB_PATH = ROOT / "anomaly.db"
PROFILING_PATH = ROOT / "profiling.json"
REPORT_PATH = ROOT / "report.html"
WORDLIST = Path("/usr/share/dict/american-english")

GEN_MODEL = os.getenv("GEN_MODEL", "gemini-3.6-flash")
VALIDATOR_MODEL = os.getenv("VALIDATOR_MODEL", "gemini-3.5-flash-lite")

TABLE_NAME = os.getenv("POC_TABLE_NAME", "pmi_items")
MAX_RULES = 8

ACTIONS = ("reject", "severity", "correct", "confirm")
SEVERITIES = ("high", "medium", "low")
CONFIDENCE_FLOOR = 0.70


# ─────────────────────────── production rule shape ──────────────────────────
AnomalyType = Literal[
    "formula_violation",
    "dependency_violation",
    "correlation_breakdown",
    "distribution_anomaly",
    "ratio_anomaly",
    "nullable_anomaly",
    "consistency_anomaly",
    "hierarchy_violation",
]


class AnomalyRule(BaseModel):
    rule_name: str
    severity: Literal["high", "medium", "low"]
    severity_reasoning: str
    identifier_columns: list[str]
    target_columns: list[str]
    anomaly_type: AnomalyType
    business_logic: str
    business_impact: str
    root_cause_hypothesis: str
    recommended_remediation: str
    sql_query: str = Field(description="Spark SQL SELECT against __TABLE_NAME__.")


class RuleSet(BaseModel):
    rules: list[AnomalyRule]


class FeedbackVerdict(BaseModel):
    verdict: Literal["ACTIONABLE", "VAGUE", "OFF_TOPIC", "INCOHERENT"]
    confidence: float = Field(ge=0.0, le=1.0)
    directive: str = Field(description="Imperative one-liner. Empty unless ACTIONABLE.")
    clarifying_question: str
    rationale: str


# ──────────────────────── production's own signature ────────────────────────
def build_signature(rule: dict) -> str:
    """Verbatim port of celery_worker.py::build_signature.

    Production already computes this for within-run dedup. Reusing it as the
    feedback key means feedback survives rewording: the model can rename a rule
    or rephrase its business_logic and the key is unchanged, because the key is
    (what kind of anomaly) x (which columns).
    """
    return "::".join([
        rule.get("anomaly_type", ""),
        "|".join(sorted(rule.get("target_columns", []))),
        "|".join(sorted(rule.get("identifier_columns", []))),
    ])


# ───────────────────────────────── storage ──────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL,
    is_control INTEGER NOT NULL DEFAULT 0,
    payload    TEXT NOT NULL,
    injected   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signature   TEXT NOT NULL,
    rule_name   TEXT,
    action      TEXT NOT NULL CHECK (action IN ('reject','severity','correct','confirm')),
    raw_comment TEXT NOT NULL,       -- what the human typed; never injected
    directive   TEXT,                -- cleaned imperative; THIS is injected
    new_severity TEXT,               -- action='severity'
    new_sql     TEXT,                -- action='correct'
    confidence  REAL,
    status      TEXT NOT NULL,       -- accepted | rejected
    gate_failed TEXT,
    reason      TEXT,
    given_after TEXT NOT NULL,
    superseded  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with db() as c:
        c.executescript(DDL)


def live_feedback() -> list[dict]:
    """Accepted and not superseded. The one query deciding what reaches a prompt.

    Rejected rows stay in the same table for the report, but can never match.
    """
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM feedback WHERE status='accepted' AND superseded=0 ORDER BY id"
        )]


def all_feedback() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback ORDER BY id")]


def versions() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM versions ORDER BY id")]


def next_label() -> str:
    return f"v{len([v for v in versions() if not v['is_control']]) + 1}"


def current_label() -> str:
    real = [v for v in versions() if not v["is_control"]]
    return real[-1]["label"] if real else "v0"


def load_run(label: str) -> list[dict]:
    for v in versions():
        if v["label"] == label:
            return json.loads(v["payload"])
    return []


# ───────────────────────── profiling input (stats only) ─────────────────────
def load_profiling() -> dict:
    return json.loads(PROFILING_PATH.read_text())


def column_stats(report: dict, limit: int = 45) -> list[dict]:
    """Columns worth reasoning about: mostly-populated ones with some variety.

    A 100%-missing column cannot participate in a relationship, so feeding all
    108 just dilutes the prompt.
    """
    out = []
    for name, v in report.get("variables", {}).items():
        miss = (v.get("percent_of_missing_values") or 0) * 100
        if miss > 60:
            continue
        out.append({
            "name": name,
            "missing_pct": round(miss, 1),
            "distinct": v.get("no_of_distinct_values"),
            "min": v.get("min_value"),
            "max": v.get("max_value"),
            "mean": v.get("mean"),
        })
    out.sort(key=lambda c: -(c["distinct"] or 0))
    return out[:limit]


def phik_row_labels(phik: list[dict]) -> list[str]:
    """Recover which column each phi_k row belongs to.

    The row label is MISSING from the payload. Production serialises with
    `phik_df.reset_index().rename(columns={"index": "column"})`
    (sigma_dq_profiling_utils.py), but phik's index is not named "index", so the
    rename silently no-ops and the label column never reaches the JSON. What
    lands is a bare 25x25 grid of numbers keyed only by column name.

    Do NOT fall back to `list(report["variables"])[i]` — variables has 108
    entries in a different order, so that mislabels every single row.

    phi_k of a column with itself is 1.0, so each row is identified by the key
    holding 1.0. Perfectly-dependent column groups produce several 1.0s in one
    row; those are resolved by elimination. Order within such a group does not
    matter numerically, because perfectly correlated columns have identical
    correlations to everything else.
    """
    candidates = []
    for row in phik:
        ones = [k for k, v in row.items()
                if isinstance(v, (int, float)) and abs(v - 1.0) < 1e-9]
        candidates.append(ones)

    labels: list[str | None] = [None] * len(phik)
    taken: set[str] = set()
    for _ in range(len(phik)):
        progressed = False
        for i, opts in enumerate(candidates):
            if labels[i] is not None:
                continue
            free = [o for o in opts if o not in taken]
            if len(free) == 1:
                labels[i] = free[0]
                taken.add(free[0])
                progressed = True
        if not progressed:
            break

    # Anything still ambiguous: assign any remaining candidate deterministically.
    for i, opts in enumerate(labels):
        if labels[i] is None:
            free = [o for o in candidates[i] if o not in taken]
            labels[i] = free[0] if free else f"phik_row_{i}"
            taken.add(labels[i])
    return [str(l) for l in labels]


def correlated_pairs(report: dict, floor: float = 0.50, limit: int = 30) -> list[dict]:
    """phi_k pairs above the floor — production's 'focus attributes'."""
    phik = report.get("correlations", {}).get("phi_k", [])
    if not phik:
        return []
    labels = phik_row_labels(phik)
    seen: set[tuple[str, str]] = set()
    pairs = []
    for i, row in enumerate(phik):
        a = labels[i]
        for b, val in row.items():
            if a == b or not isinstance(val, (int, float)):
                continue
            if not (floor <= val < 1.0):
                continue
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"a": key[0], "b": key[1], "phi_k": round(val, 3)})
    pairs.sort(key=lambda p: -p["phi_k"])
    return pairs[:limit]


def all_column_names(report: dict) -> set[str]:
    return {c.lower() for c in report.get("variables", {})}


# ─────────────────────────────── Gemini client ──────────────────────────────
_client: genai.Client | None = None


def _llm() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise SystemExit("GEMINI_API_KEY missing — check ../.env")
        _client = genai.Client(api_key=key)
    return _client


def _structured(prompt: str, schema, model: str, system: str):
    """gemini-3.x rejects temperature/top_p/top_k. Run-to-run variation is
    handled by the control version, not by sampling parameters."""
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        system_instruction=system,
    )
    r = _llm().models.generate_content(model=model, contents=prompt, config=cfg)
    p = getattr(r, "parsed", None)
    return p if isinstance(p, schema) else schema.model_validate_json(r.text)


# ──────────────────────── G0: coherence (no LLM call) ───────────────────────
VOWELS = set("aeiou")  # 'y' excluded — it let "hygyt" fake a healthy vowel ratio
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ALPHA_RE = re.compile(r"^[A-Za-z]+$")
MIN_CHARS = 8
DOMAIN_WORDS = {
    "sql", "null", "nulls", "pk", "fk", "id", "ids", "utc", "dq", "kpi", "uom",
    "col", "cols", "phi", "phik", "api", "json", "etl", "sku", "ts", "dtype",
}
_bg: dict | None = None


@lru_cache(maxsize=1)
def _words() -> frozenset[str]:
    if not WORDLIST.exists():
        return frozenset()
    return frozenset(
        w.strip().lower() for w in WORDLIST.read_text(errors="ignore").splitlines()
        if w.strip() and "'" not in w
    )


def _bigram_model() -> dict:
    global _bg
    if _bg is not None:
        return _bg
    counts: Counter = Counter()
    total = 0
    for w in _words():
        if not ALPHA_RE.match(w) or len(w) < 3:
            continue
        p = f"^{w}$"
        for i in range(len(p) - 1):
            counts[p[i:i + 2]] += 1
            total += 1
    vocab = 28 * 28
    _bg = {"logp": {b: math.log10((n + 1) / (total + vocab)) for b, n in counts.items()},
           "floor": math.log10(1 / (total + vocab)) if total else -6.0}
    return _bg


def _bigram(w: str) -> float:
    m = _bigram_model()
    if not m["logp"]:
        return 0.0
    p = f"^{w.lower()}$"
    s = [m["logp"].get(p[i:i + 2], m["floor"]) for i in range(len(p) - 1)]
    return sum(s) / len(s)


def gate0(text: str, terms: set[str]) -> tuple[bool, str]:
    """Is this even language? ~1ms, no API call.

    Runs first because feedback here is persistent — an accepted garbage entry
    is replayed into every future generation, not just one bad response.
    """
    t = text.strip()
    if len(t) < MIN_CHARS:
        return False, f"only {len(t)} chars, minimum {MIN_CHARS}"
    tokens = TOKEN_RE.findall(t)
    if len(tokens) < 2:
        return False, f"only {len(tokens)} word(s) — too terse to act on"
    alpha = [x for x in tokens if ALPHA_RE.match(x) and len(x) >= 3]
    if not alpha:
        return True, "coherent"

    known = _words()
    parts = set(terms)
    for term in terms:
        parts.update(p for p in term.split("_") if p)
    unknown = [x for x in alpha if x.lower() not in known
               and x.lower() not in DOMAIN_WORDS and x.lower() not in parts]

    if known and len(unknown) == len(alpha) and len(alpha) >= 2:
        return False, ("no recognisable words — none of "
                       + ", ".join(repr(x) for x in alpha[:4])
                       + " is a known word, a column on this table, or known jargon")

    garbage = []
    for x in unknown:
        fired = 0
        letters = [c for c in x.lower() if c.isalpha()]
        vr = sum(1 for c in letters if c in VOWELS) / len(letters) if letters else 0
        run = best = 0
        for c in x.lower():
            run = run + 1 if c.isalpha() and c not in VOWELS else 0
            best = max(best, run)
        if _bigram(x) < -2.90:
            fired += 1
        if best > 4:
            fired += 1
        if not (0.12 <= vr <= 0.70):
            fired += 1
        if len(x) >= 3 and len(set(x.lower())) <= 2:
            fired += 1
        if fired >= 2:
            garbage.append(x)

    if garbage and len(garbage) / len(alpha) >= 0.4:
        return False, "does not read as language — " + ", ".join(repr(x) for x in garbage[:4])
    return True, "coherent"


# ───────────────────── G1: structural, incl. SQL safety ─────────────────────
FORBIDDEN_SQL = {"DROP", "DELETE", "TRUNCATE", "ALTER", "INSERT", "UPDATE",
                 "MERGE", "CREATE", "GRANT", "REVOKE"}


def gate1(action: str, signature: str, known: set[str] | None,
          new_severity: str | None, new_sql: str | None,
          columns: set[str]) -> tuple[bool, str]:
    if action not in ACTIONS:
        return False, f"action must be one of {ACTIONS}, got {action!r}"

    if known is not None and signature not in known:
        return False, (f"no rule with signature {signature!r} in the run being "
                       f"reviewed — you cannot give feedback on a rule that was "
                       f"not generated")

    if action == "severity":
        if not new_severity:
            return False, "action=severity requires --severity high|medium|low"
        if new_severity not in SEVERITIES:
            return False, f"severity must be one of {SEVERITIES}, got {new_severity!r}"

    if action == "correct":
        if not new_sql or not new_sql.strip():
            return False, "action=correct requires --sql"
        # These rules EXECUTE against the warehouse. Descriptions and dismissals
        # cannot; this can. So the bar here is higher than in the other POCs.
        upper = new_sql.upper()
        hit = [k for k in FORBIDDEN_SQL if re.search(rf"\b{k}\b", upper)]
        if hit:
            return False, f"SQL contains forbidden keyword(s): {', '.join(sorted(hit))}"
        try:
            tree = sqlglot.parse_one(new_sql, dialect="spark")
        except Exception as e:
            return False, f"SQL does not parse as Spark SQL: {e}"
        if not isinstance(tree, exp.Select):
            return False, "SQL must be a SELECT — anomaly rules only read"
        refs = {c.name.lower() for c in tree.find_all(exp.Column)}
        unknown = sorted(r for r in refs if r and r not in columns)
        if unknown:
            return False, (f"SQL references columns not on this table: "
                           f"{', '.join(unknown)}")
        if "__TABLE_NAME__" not in new_sql:
            return False, "SQL must select from __TABLE_NAME__ (the placeholder production substitutes)"

    return True, "ok"


# ────────────────────────── G2: semantic (one call) ─────────────────────────
VALIDATOR_SYSTEM = """You judge whether a reviewer's comment about a generated \
anomaly-detection rule is specific enough to act on. You do NOT judge whether the \
reviewer is correct.

Verdicts:
- ACTIONABLE: states a concrete reason. Produce an imperative directive for the
  rule generator, e.g. "Do NOT generate nullable_anomaly rules on column X; it is
  populated only for export shipments."
- VAGUE: on-topic but unactionable ("this is wrong", "fix it"). Ask one question.
- OFF_TOPIC: real language, nothing to do with this rule.
- INCOHERENT: not meaningful language.

Be strict. This feedback is PERSISTENT — it is replayed into every future
generation, so a bad entry causes lasting damage. Prefer VAGUE when unsure."""


def gate2(rule: dict, comment: str, action: str) -> FeedbackVerdict:
    prompt = (
        f"RULE UNDER REVIEW:\n{json.dumps(rule, indent=2)[:2000]}\n\n"
        f"REVIEWER ACTION: {action}\n"
        f"REVIEWER COMMENT (verbatim, may be nonsense):\n\"\"\"{comment}\"\"\"\n\n"
        "Judge the comment. Return JSON matching the schema."
    )
    return _structured(prompt, FeedbackVerdict, VALIDATOR_MODEL, VALIDATOR_SYSTEM)


# ─────────────────────────────── record feedback ────────────────────────────
def record_feedback(signature: str, comment: str, action: str = "reject",
                    new_severity: str | None = None,
                    new_sql: str | None = None) -> dict:
    init_db()
    report = load_profiling()
    columns = all_column_names(report)
    given_after = current_label()

    latest = {build_signature(r): r for r in load_run(given_after)}
    known = set(latest) if latest else None
    rule = latest.get(signature, {})

    def reject(gate, reason):
        with db() as c:
            c.execute(
                "INSERT INTO feedback (signature,rule_name,action,raw_comment,status,"
                "gate_failed,reason,given_after) VALUES (?,?,?,?,?,?,?,?)",
                (signature, rule.get("rule_name"), action, comment,
                 "rejected", gate, reason, given_after),
            )
        return {"ok": False, "gate": gate, "reason": reason,
                "signature": signature, "action": action,
                "rule_name": rule.get("rule_name")}

    ok, why = gate1(action, signature, known, new_severity, new_sql, columns)
    if not ok:
        return reject("G1_STRUCTURAL", why)

    ok, why = gate0(comment, columns | {TABLE_NAME})
    if not ok:
        return reject("G0_COHERENCE", why)

    v = gate2(rule, comment, action)
    if v.verdict != "ACTIONABLE":
        return reject("G2_SEMANTIC", f"{v.verdict}: {v.rationale}")
    if v.confidence < CONFIDENCE_FLOOR:
        return reject("G2_SEMANTIC",
                      f"confidence {v.confidence:.2f} below the {CONFIDENCE_FLOOR:.2f} "
                      f"bar: {v.rationale}")

    # G3: same signature already ruled on -> newest wins, old retires, so the
    # prompt never carries two contradictory instructions about one rule.
    with db() as c:
        old = [r["id"] for r in c.execute(
            "SELECT id FROM feedback WHERE status='accepted' AND superseded=0 "
            "AND signature=?", (signature,))]
        for i in old:
            c.execute("UPDATE feedback SET superseded=1 WHERE id=?", (i,))
        cur = c.execute(
            "INSERT INTO feedback (signature,rule_name,action,raw_comment,directive,"
            "new_severity,new_sql,confidence,status,given_after) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (signature, rule.get("rule_name"), action, comment, v.directive,
             new_severity, new_sql, v.confidence, "accepted", given_after))
        fid = cur.lastrowid

    return {"ok": True, "id": fid, "signature": signature, "action": action,
            "directive": v.directive, "confidence": v.confidence,
            "given_after": given_after, "superseded": old,
            "rule_name": rule.get("rule_name")}


# ──────────────────────────────── generation ────────────────────────────────
GEN_SYSTEM = f"""You are an anomaly detection rule generator.

You are given column statistics and phi_k correlations for a single table. You
never see data rows — only statistics.

Generate anomaly rules that catch violations of business relationships implied by
the correlations. For each rule produce Spark SQL that SELECTs the offending rows.

STRICT REQUIREMENTS
- Spark SQL only. No window functions. No HAVING.
- Select from the literal placeholder __TABLE_NAME__.
- Use only columns given to you. Never invent a column.
- identifier_columns are the grain (what identifies a row/group).
- target_columns are what the rule actually tests.
- Generate at most {MAX_RULES} rules. Prefer few, high-value rules.
- Do not emit two rules with the same anomaly_type and the same target columns.

If reviewer feedback is supplied, it OVERRIDES your own judgement without exception."""


def build_injection(rules: list[dict]) -> str:
    """Placed LAST in the prompt on purpose — late instructions are followed more
    reliably, and this block is the entire point of the system.

    Production's USER_PROMPT already carries {previous_rule_signatures} for
    within-run dedup; this is the same idea, carrying reviewer intent across runs.
    """
    if not rules:
        return ""
    out = ["### Reviewer feedback — honor these EXACTLY. They override your own judgement."]
    for r in rules:
        sig = r["signature"]
        if r["action"] == "reject":
            out.append(f"- DO NOT GENERATE any rule with signature {sig}. {r['directive']}")
        elif r["action"] == "confirm":
            out.append(f"- ALWAYS GENERATE the rule with signature {sig}. {r['directive']}")
        elif r["action"] == "severity":
            out.append(f"- The rule with signature {sig} MUST have severity "
                       f"'{r['new_severity']}'. {r['directive']}")
        elif r["action"] == "correct":
            out.append(f"- For signature {sig}, use EXACTLY this SQL: {r['new_sql']}"
                       f"  ({r['directive']})")
    return "\n".join(out)


def generate(control: bool = False) -> dict:
    """control=True strips the feedback block, everything else identical.

    This is what lets a change in the rule list be attributed to feedback rather
    than to the model producing a slightly different list each run.
    """
    init_db()
    report = load_profiling()
    stats = column_stats(report)
    pairs = correlated_pairs(report)
    rules_live = [] if control else live_feedback()
    block = build_injection(rules_live)

    t = report.get("table", {})
    prompt = (
        f"TABLE: {TABLE_NAME}\n"
        f"ROWS: {t.get('no_of_rows'):,}   COLUMNS: {t.get('no_of_variables')}\n\n"
        f"COLUMN STATISTICS ({len(stats)} usable columns):\n"
        + "\n".join(
            f"  {c['name']}: missing={c['missing_pct']}% distinct={c['distinct']}"
            + (f" min={c['min']} max={c['max']}" if c["min"] is not None else "")
            for c in stats)
        + f"\n\nCORRELATED COLUMN PAIRS (phi_k, {len(pairs)} shown):\n"
        + "\n".join(f"  {p['a']} ~ {p['b']}  phi_k={p['phi_k']}" for p in pairs)
        + "\n\nGenerate anomaly rules. Return JSON matching the schema."
    )
    if block:
        prompt += "\n\n" + block

    result: RuleSet = _structured(prompt, RuleSet, GEN_MODEL, GEN_SYSTEM)

    payload = []
    seen = set()
    for r in result.rules:
        d = r.model_dump()
        d["signature"] = build_signature(d)
        if d["signature"] in seen:      # production dedups the same way
            continue
        seen.add(d["signature"])
        payload.append(d)

    # Control labels must be unique: consecutive --control runs share the same
    # next_label(), which previously produced two rows both called "v3_control"
    # and silently collapsed them in the report.
    if control:
        n = len([v for v in versions() if v["is_control"]]) + 1
        label = f"control_{n}"
    else:
        label = next_label()
    with db() as c:
        c.execute("INSERT INTO versions (label,is_control,payload,injected) VALUES (?,?,?,?)",
                  (label, 1 if control else 0, json.dumps(payload), block))

    return {"label": label, "control": control, "rules": payload,
            "injected": block, "feedback_applied": len(rules_live)}


# ────────────────────────────────── report ──────────────────────────────────
CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; padding: 32px; background: #fff; color: #16181d; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 32px 0 10px; text-transform: uppercase;
     letter-spacing: .06em; color: #6b7280; }
.sub { color: #6b7280; margin: 0 0 8px; }
.note { color: #6b7280; margin: 0 0 12px; max-width: 76ch; font-size: 13px; }
.wrap { overflow-x: auto; border: 1px solid #e3e6ea; border-radius: 8px; }
table { border-collapse: collapse; width: 100%; min-width: 760px; }
th, td { text-align: left; vertical-align: top; padding: 9px 12px;
         border-bottom: 1px solid #eef0f3; }
th { background: #f7f8fa; font-weight: 600; white-space: nowrap; }
td.col { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 999px;
         font-size: 11px; font-weight: 600; }
.ok { background: #dcfce7; color: #166534; }
.no { background: #fee2e2; color: #991b1b; }
.hi { background: #fee2e2; color: #991b1b; }
.me { background: #fef3c7; color: #92400e; }
.lo { background: #e0e7ff; color: #3730a3; }
.act { background: #e0e7ff; color: #3730a3; font-family: ui-monospace, monospace; }
.gone { background: #fff8e1; }
.muted { color: #9ca3af; }
.yes { color: #166534; font-weight: 600; }
.nope { color: #991b1b; font-weight: 600; }
pre.inj { margin: 0 0 14px; padding: 12px; background: #f7f8fa; border: 1px solid #e3e6ea;
          border-radius: 8px; overflow-x: auto; font-size: 12.5px; line-height: 1.5; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e6e8eb; }
  .wrap { border-color: #262b33; }
  th { background: #171a20; } th, td { border-bottom-color: #21252c; }
  h2, .sub, .note { color: #9aa4b2; }
  .ok { background: #14361f; color: #86efac; } .no { background: #3b1618; color: #fca5a5; }
  .hi { background: #3b1618; color: #fca5a5; } .me { background: #3a2c0c; color: #fcd34d; }
  .lo, .act { background: #1e2447; color: #a5b4fc; }
  .gone { background: #2b2410; }
  .yes { color: #86efac; } .nope { color: #fca5a5; }
  pre.inj { background: #171a20; border-color: #262b33; }
}
"""

SEV_CLS = {"high": "hi", "medium": "me", "low": "lo"}


def build_report() -> Path:
    vs = versions()
    if not vs:
        raise SystemExit("no versions yet — run generate.py first")
    fb = all_feedback()
    labels = [v["label"] for v in vs]
    runs = {v["label"]: json.loads(v["payload"]) for v in vs}
    real = [v["label"] for v in vs if not v["is_control"]]

    h = [f"<h1>Anomaly rules — feedback loop</h1>",
         f"<p class='sub'>{TABLE_NAME} &middot; {len(real)} version(s) "
         f"&middot; {len([f for f in fb if f['status']=='accepted'])} accepted feedback</p>"]

    # A — did each feedback item take effect?
    acc = [f for f in fb if f["status"] == "accepted"]
    h.append("<h2>Did the feedback take effect?</h2>")
    h.append("<p class='note'>Checked in Python against the stored runs, not judged by a model. "
             "A <code>control</code> version is the same profiling snapshot regenerated with the "
             "feedback block stripped — it is what separates a real effect from the model simply "
             "producing a different list each run.</p>")
    if not acc:
        h.append("<p class='muted'>No accepted feedback yet.</p>")
    else:
        h.append("<div class='wrap'><table><thead><tr><th>Feedback</th><th>Action</th>"
                 "<th>Given after</th>" + "".join(f"<th>{escape(l)}</th>" for l in labels)
                 + "</tr></thead><tbody>")
        for f in acc:
            cells = []
            for l in labels:
                present = any(r["signature"] == f["signature"] for r in runs[l])
                rule = next((r for r in runs[l] if r["signature"] == f["signature"]), None)
                if f["action"] == "reject":
                    cells.append(f"<td class='{'nope' if present else 'yes'}'>"
                                 f"{'still there' if present else 'gone'}</td>")
                elif f["action"] == "confirm":
                    cells.append(f"<td class='{'yes' if present else 'nope'}'>"
                                 f"{'present' if present else 'missing'}</td>")
                elif f["action"] == "severity":
                    got = rule["severity"] if rule else "—"
                    good = got == f["new_severity"]
                    cells.append(f"<td class='{'yes' if good else 'nope'}'>{escape(got)}</td>")
                else:
                    cells.append("<td class='muted'>—</td>")
            h.append(f"<tr><td>{escape(f['directive'] or f['raw_comment'])}</td>"
                     f"<td><span class='badge act'>{f['action']}</span></td>"
                     f"<td class='col'>{escape(f['given_after'])}</td>"
                     + "".join(cells) + "</tr>")
        h.append("</tbody></table></div>")

    # B — persistence receipt
    h.append("<h2>What was sent to the model</h2>")
    h.append("<p class='note'>Loaded from SQLite at generation time, never re-typed. Each run is a "
             "separate OS process, so the database file is the only route from an early version's "
             "feedback to a later version's rules.</p>")
    for v in vs:
        inj = (v["injected"] or "").strip()
        h.append(f"<p class='sub'><strong>{escape(v['label'])}</strong>"
                 + (" <span class='badge act'>control</span>" if v["is_control"] else "") + "</p>")
        h.append(f"<pre class='inj'>{escape(inj) if inj else '(no feedback in force)'}</pre>")

    # C — the rules per version
    h.append("<h2>Rules generated</h2>")
    for l in labels:
        rs = runs[l]
        h.append(f"<p class='sub'><strong>{escape(l)}</strong> — {len(rs)} rule(s)</p>")
        h.append("<div class='wrap'><table><thead><tr><th>Rule</th><th>Sev</th>"
                 "<th>Type</th><th>Targets</th><th>Signature</th></tr></thead><tbody>")
        for r in rs:
            sev = r["severity"]
            h.append(f"<tr><td>{escape(r['rule_name'])}"
                     f"<div class='muted' style='font-size:12px'>{escape(r['business_impact'][:110])}</div></td>"
                     f"<td><span class='badge {SEV_CLS.get(sev,'lo')}'>{sev}</span></td>"
                     f"<td class='col'>{escape(r['anomaly_type'])}</td>"
                     f"<td class='col'>{escape(', '.join(r['target_columns']))}</td>"
                     f"<td class='col muted'>{escape(r['signature'][:56])}</td></tr>")
        h.append("</tbody></table></div>")

    # D — feedback log
    h.append("<h2>Feedback log</h2><div class='wrap'><table><thead><tr><th>#</th>"
             "<th>Rule</th><th>Action</th><th>Comment</th><th>Injected / why refused</th>"
             "<th>Given after</th><th>Status</th></tr></thead><tbody>")
    for f in fb:
        if f["status"] == "accepted":
            rule = escape(f["directive"] or "")
            badge = "<span class='badge ok'>accepted</span>"
            if f["superseded"]:
                badge = "<span class='badge no'>superseded</span>"
        else:
            rule = f"<span class='muted'>{escape(f['reason'] or '')}</span>"
            gate = (f["gate_failed"] or "").split("_")[0].lower()
            badge = f"<span class='badge no'>rejected: {gate}</span>"
        h.append(f"<tr><td>{f['id']}</td><td>{escape(f['rule_name'] or '—')}</td>"
                 f"<td><span class='badge act'>{f['action']}</span></td>"
                 f"<td>{escape(f['raw_comment'])}</td><td>{rule}</td>"
                 f"<td class='col'>{escape(f['given_after'])}</td><td>{badge}</td></tr>")
    h.append("</tbody></table></div>")

    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Anomaly rules — feedback loop</title>"
            f"<style>{CSS}</style></head><body>" + "\n".join(h)
            + f"<p class='sub' style='margin-top:28px'>generated {datetime.now():%Y-%m-%d %H:%M}</p>"
            "</body></html>")
    REPORT_PATH.write_text(html)
    return REPORT_PATH
