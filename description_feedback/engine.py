"""Column-description feedback loop.

Three runs, three scripts: generate.py -> feedback.py -> report.py.
This file is the whole mechanism; the runners are ~20 lines each.

The claim this POC has to support is that feedback PERSISTS: typed once, stored
in SQLite, replayed into every later generation without re-entry. Every design
decision below serves that, and the report is built to prove it rather than
assert it:

  - each generate.py is a separate OS process, so the only route from v1's
    feedback to v4's output is the database file
  - a control version regenerates with the feedback block stripped, so
    "the description changed" can be attributed to feedback and not to the
    model rewording itself
  - compliance is scored in PYTHON, never by a model, so the numbers in the
    report are not the AI grading its own homework
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

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

ROOT = Path(__file__).parent
DB_PATH = ROOT / "descriptions.db"
SCHEMA_PATH = ROOT / "schema.json"
REPORT_PATH = ROOT / "report.html"
WORDLIST = Path("/usr/share/dict/american-english")

GEN_MODEL = os.getenv("GEN_MODEL", "gemini-3.6-flash")
VALIDATOR_MODEL = os.getenv("VALIDATOR_MODEL", "gemini-3.5-flash-lite")

SCOPES = ("column", "table", "all")

# Blast radius drives the bar. A bad column rule spoils one cell; a bad `all`
# rule degrades all 20 descriptions at once, and nothing tests a description,
# so it just quietly rots the dictionary that semantic_role feeds downstream.
CONFIDENCE_FLOOR = {"column": 0.70, "table": 0.80, "all": 0.90}


# ─────────────────────────────── LLM contracts ──────────────────────────────
SemanticRole = Literal[
    "identifier", "measure", "dimension", "timestamp", "flag", "free_text"
]


class ColumnDescription(BaseModel):
    table: str
    column: str
    description: str
    semantic_role: SemanticRole


class DescriptionSet(BaseModel):
    descriptions: list[ColumnDescription]


class FeedbackVerdict(BaseModel):
    verdict: Literal["ACTIONABLE", "VAGUE", "OFF_TOPIC", "INCOHERENT"]
    confidence: float = Field(ge=0.0, le=1.0)
    directive: str = Field(
        description="Imperative one-liner to inject. Empty unless ACTIONABLE."
    )
    rule_shape: Literal["fact", "convention"] = Field(
        description=(
            "'fact' asserts something true of one specific column (e.g. 'this is "
            "in INR'). 'convention' is a way of writing that holds regardless of "
            "which column it is applied to (e.g. 'keep under 10 words')."
        )
    )
    check_type: Literal[
        "none", "max_words", "forbidden_prefix", "must_contain", "forbidden_substring"
    ] = Field(
        description=(
            "If the rule can be verified mechanically on the description text, say "
            "how. 'none' if it needs human judgement."
        )
    )
    check_value: str = Field(description="Number for max_words, else the literal text.")
    clarifying_question: str
    rationale: str


# ─────────────────────────────── Gemini client ──────────────────────────────
_client: genai.Client | None = None


def _llm() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise SystemExit("GEMINI_API_KEY missing — add it to .env")
        _client = genai.Client(api_key=key)
    return _client


def _structured(prompt: str, schema, model: str, system: str):
    """gemini-3.x rejects temperature/top_p/top_k — they are removed parameters,
    not ignored ones. Run-to-run stability is handled by the control version."""
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        system_instruction=system,
    )
    resp = _llm().models.generate_content(model=model, contents=prompt, config=cfg)
    parsed = getattr(resp, "parsed", None)
    return parsed if isinstance(parsed, schema) else schema.model_validate_json(resp.text)


# ───────────────────────────────── storage ──────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    is_control  INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,          -- JSON: list of descriptions
    injected    TEXT NOT NULL,          -- the feedback block actually sent
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name   TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('column','table','all')),
    raw_comment  TEXT NOT NULL,         -- what the human typed; never injected
    directive    TEXT,                  -- cleaned imperative; THIS is injected
    rule_shape   TEXT,
    check_type   TEXT NOT NULL DEFAULT 'none',
    check_value  TEXT NOT NULL DEFAULT '',
    confidence   REAL,
    status       TEXT NOT NULL,         -- accepted | rejected
    gate_failed  TEXT,
    reason       TEXT,
    given_after  TEXT NOT NULL,         -- version label current when typed
    superseded   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(DDL)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def all_columns(schema: dict) -> list[tuple[str, str]]:
    return [(t, c["name"]) for t, v in schema.items() for c in v["columns"]]


def live_feedback() -> list[dict]:
    """Accepted, not superseded. The ONE query that decides what reaches a prompt.

    Rejected feedback lives in the same table with status='rejected' so the
    report can show it, but it can never match this filter.
    """
    with db() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM feedback WHERE status='accepted' AND superseded=0 "
                "ORDER BY id"
            )
        ]


def all_feedback() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback ORDER BY id")]


def versions() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM versions ORDER BY id")]


def next_version_label() -> str:
    real = [v for v in versions() if not v["is_control"]]
    return f"v{len(real) + 1}"


def current_version_label() -> str:
    real = [v for v in versions() if not v["is_control"]]
    return real[-1]["label"] if real else "v0"


# ────────────────────────── G0: coherence (no LLM) ──────────────────────────
VOWELS = set("aeiou")  # 'y' excluded deliberately — it let "hygyt" fake a vowel ratio
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ALPHA_RE = re.compile(r"^[A-Za-z]+$")
MIN_CHARS = 8
DOMAIN_WORDS = {
    "sql", "null", "nulls", "pk", "fk", "sku", "id", "ids", "utc", "inr", "usd",
    "col", "cols", "dq", "kpi", "uom", "ts", "dtype", "api", "json", "csv",
}
_bigrams: dict | None = None


@lru_cache(maxsize=1)
def _words() -> frozenset[str]:
    if not WORDLIST.exists():
        return frozenset()
    return frozenset(
        w.strip().lower()
        for w in WORDLIST.read_text(errors="ignore").splitlines()
        if w.strip() and "'" not in w
    )


def _bigram_model() -> dict:
    global _bigrams
    if _bigrams is not None:
        return _bigrams
    counts: Counter = Counter()
    total = 0
    for w in _words():
        if not ALPHA_RE.match(w) or len(w) < 3:
            continue
        p = f"^{w}$"
        for i in range(len(p) - 1):
            counts[p[i : i + 2]] += 1
            total += 1
    vocab = 28 * 28
    _bigrams = {
        "logp": {b: math.log10((n + 1) / (total + vocab)) for b, n in counts.items()},
        "floor": math.log10(1 / (total + vocab)) if total else -6.0,
    }
    return _bigrams


def _bigram(word: str) -> float:
    m = _bigram_model()
    if not m["logp"]:
        return 0.0
    p = f"^{word.lower()}$"
    s = [m["logp"].get(p[i : i + 2], m["floor"]) for i in range(len(p) - 1)]
    return sum(s) / len(s)


def gate0(text: str, schema_terms: set[str]) -> tuple[bool, str]:
    """Is this even language? Deterministic, ~1ms, no API call.

    Exists because feedback here is persistent: an accepted garbage entry is not
    one bad response, it is permanent corruption replayed into every future run.
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
    parts = set(schema_terms)
    for term in schema_terms:
        parts.update(p for p in term.split("_") if p)

    unknown = [
        x for x in alpha
        if x.lower() not in known
        and x.lower() not in DOMAIN_WORDS
        and x.lower() not in parts
    ]

    # Nothing recognisable at all -> nothing to act on, whatever the statistics say.
    if known and len(unknown) == len(alpha) and len(alpha) >= 2:
        return False, (
            "no recognisable words — none of "
            + ", ".join(repr(x) for x in alpha[:4])
            + " is a known word, a column in this schema, or known jargon"
        )

    # A token is garbage once two independent signals agree on it.
    garbage = []
    for x in unknown:
        fired = 0
        letters = [ch for ch in x.lower() if ch.isalpha()]
        vr = sum(1 for ch in letters if ch in VOWELS) / len(letters) if letters else 0
        run = best = 0
        for ch in x.lower():
            run = run + 1 if ch.isalpha() and ch not in VOWELS else 0
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
        return False, "does not read as language — " + ", ".join(
            repr(x) for x in garbage[:4]
        )
    return True, "coherent"


# ───────────────────────── G1: structural (no LLM) ──────────────────────────
def gate1(table: str, column: str, scope: str, schema: dict) -> tuple[bool, str]:
    if scope not in SCOPES:
        return False, f"scope must be one of {SCOPES}, got {scope!r}"
    if table not in schema:
        return False, f"table {table!r} is not in the schema"
    cols = {c["name"] for c in schema[table]["columns"]}
    if column not in cols:
        return False, f"column {column!r} is not in {table} (has: {', '.join(sorted(cols))})"
    return True, "ok"


def gate1_shape(scope: str, shape: str, table: str, column: str, schema: dict) -> tuple[bool, str]:
    """A fact can never widen.

    'must state INR' scoped to `all` would try to apply to products.title — the
    scope is declared by the human, and this is where the system checks that
    declaration is sane rather than guessing it from the wording.
    """
    if shape == "fact" and scope in ("table", "all"):
        others = [
            f"{t}.{c['name']}"
            for t, v in schema.items()
            for c in v["columns"]
            if not (t == table and c["name"] == column)
        ]
        example = next(
            (o for o in others if not o.startswith(f"{table}.")), others[0]
        ) if others else "another column"
        return False, (
            f"this states a fact specific to {table}.{column}, so it cannot be "
            f"scoped to '{scope}' — it would also be applied to {example}. "
            f"Use column scope, or rephrase as a general convention."
        )
    return True, "ok"


# ────────────────────────── G2: semantic (one LLM call) ─────────────────────
VALIDATOR_SYSTEM = """You judge whether a reviewer's comment about a generated column \
description is specific enough to act on. You do NOT judge whether it is correct.

Verdicts:
- ACTIONABLE: states a concrete change. Produce an imperative directive.
- VAGUE: on-topic but unactionable ("make it better"). Ask one clarifying question.
- OFF_TOPIC: real language, nothing to do with describing this column.
- INCOHERENT: not meaningful language.

Also classify rule_shape:
- "fact" asserts something true of this specific column only.
- "convention" is a writing rule that holds for any column.

Also extract check_type if the rule can be verified mechanically against the
description text — max_words (check_value = the number), forbidden_prefix,
must_contain, forbidden_substring. Use "none" when it needs human judgement.

Be strict. This feedback is PERSISTENT — it is replayed into every future
generation, so a bad entry causes lasting damage. Prefer VAGUE when unsure."""


def gate2(table: str, column: str, dtype: str, samples, current: str, comment: str) -> FeedbackVerdict:
    prompt = (
        f"COLUMN: {table}.{column}\n"
        f"DECLARED TYPE: {dtype}\n"
        f"SAMPLE VALUES: {json.dumps(samples)}\n"
        f"CURRENT GENERATED DESCRIPTION: {current!r}\n\n"
        f"REVIEWER COMMENT (verbatim, may be nonsense):\n\"\"\"{comment}\"\"\"\n\n"
        "Judge the comment. Return JSON matching the schema."
    )
    return _structured(prompt, FeedbackVerdict, VALIDATOR_MODEL, VALIDATOR_SYSTEM)


# ───────────────────────────── record feedback ──────────────────────────────
def record_feedback(target: str, comment: str, scope: str = "column") -> dict:
    """target is 'table.column'. Returns a result dict for the runner to print."""
    init_db()
    schema = load_schema()

    if "." not in target:
        return {"ok": False, "gate": "INPUT", "reason": "target must be table.column"}
    table, column = target.split(".", 1)
    given_after = current_version_label()

    def reject(gate, reason):
        with db() as c:
            c.execute(
                "INSERT INTO feedback (table_name,column_name,scope,raw_comment,"
                "status,gate_failed,reason,given_after) VALUES (?,?,?,?,?,?,?,?)",
                (table, column, scope, comment, "rejected", gate, reason, given_after),
            )
        return {"ok": False, "gate": gate, "reason": reason,
                "target": target, "scope": scope}

    ok, why = gate1(table, column, scope, schema)
    if not ok:
        return reject("G1_STRUCTURAL", why)

    terms = {t for t in schema} | {c["name"] for _, c in
                                   [(t, c) for t, v in schema.items() for c in v["columns"]]}
    ok, why = gate0(comment, terms)
    if not ok:
        return reject("G0_COHERENCE", why)

    col_meta = next(c for c in schema[table]["columns"] if c["name"] == column)
    latest = _latest_descriptions()
    current = latest.get(f"{table}.{column}", {}).get("description", "(not yet generated)")

    v = gate2(table, column, col_meta["dtype"], col_meta["samples"], current, comment)

    if v.verdict != "ACTIONABLE":
        return reject("G2_SEMANTIC", f"{v.verdict}: {v.rationale}")

    floor = CONFIDENCE_FLOOR[scope]
    if v.confidence < floor:
        return reject(
            "G2_SEMANTIC",
            f"confidence {v.confidence:.2f} below the {floor:.2f} bar required for "
            f"'{scope}' scope: {v.rationale}",
        )

    ok, why = gate1_shape(scope, v.rule_shape, table, column, schema)
    if not ok:
        return reject("G1_STRUCTURAL", why)

    # G3: same column + same scope already ruled on -> newest wins, old retires,
    # so the prompt never carries two contradictory instructions at once.
    superseded = []
    with db() as c:
        rows = c.execute(
            "SELECT id FROM feedback WHERE status='accepted' AND superseded=0 "
            "AND table_name=? AND column_name=? AND scope=?",
            (table, column, scope),
        ).fetchall()
        superseded = [r["id"] for r in rows]
        for rid in superseded:
            c.execute("UPDATE feedback SET superseded=1 WHERE id=?", (rid,))
        cur = c.execute(
            "INSERT INTO feedback (table_name,column_name,scope,raw_comment,directive,"
            "rule_shape,check_type,check_value,confidence,status,given_after) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (table, column, scope, comment, v.directive, v.rule_shape,
             v.check_type, v.check_value, v.confidence, "accepted", given_after),
        )
        fid = cur.lastrowid

    return {
        "ok": True, "id": fid, "target": target, "scope": scope,
        "directive": v.directive, "shape": v.rule_shape,
        "check": f"{v.check_type}={v.check_value}" if v.check_type != "none" else "not mechanically checkable",
        "confidence": v.confidence, "given_after": given_after,
        "superseded": superseded,
    }


# ──────────────────────────────── generation ────────────────────────────────
GEN_SYSTEM = """You write data-dictionary descriptions for database columns.

Rules:
- One clear sentence per column. No filler.
- Describe what the column MEANS in business terms, not its data type.
- You are given the column name, type and a few sample values. Nothing else.
- Return one entry for EVERY column given, using the exact table and column names.
- If reviewer feedback is supplied, it OVERRIDES your own judgement without exception."""


def build_injection(rules: list[dict]) -> str:
    """Group live rules by reach. Placed LAST in the prompt on purpose —
    instructions late in a prompt are followed more reliably, and this block is
    the entire point of the system."""
    if not rules:
        return ""
    glob = [r for r in rules if r["scope"] == "all"]
    tbl: dict[str, list[dict]] = {}
    col: dict[str, list[dict]] = {}
    for r in rules:
        if r["scope"] == "table":
            tbl.setdefault(r["table_name"], []).append(r)
        elif r["scope"] == "column":
            col.setdefault(f"{r['table_name']}.{r['column_name']}", []).append(r)

    out = ["### Reviewer feedback — honor these EXACTLY. They override your own judgement."]
    if glob:
        out.append("APPLIES TO EVERY COLUMN:")
        out += [f"  - {r['directive']}" for r in glob]
    for t, rs in sorted(tbl.items()):
        out.append(f"APPLIES TO EVERY COLUMN IN TABLE '{t}':")
        out += [f"  - {r['directive']}" for r in rs]
    for key, rs in sorted(col.items()):
        out.append(f"APPLIES ONLY TO {key}:")
        out += [f"  - {r['directive']}" for r in rs]
    return "\n".join(out)


def generate(control: bool = False) -> dict:
    """One call produces all descriptions for the whole schema.

    control=True regenerates with the feedback block stripped and everything else
    identical. That is what lets the report attribute a changed description to
    feedback rather than to the model rewording itself.
    """
    init_db()
    schema = load_schema()
    rules = [] if control else live_feedback()
    block = build_injection(rules)

    lines = []
    for t, v in schema.items():
        lines.append(f"TABLE {t} — {v['description']}")
        for c in v["columns"]:
            lines.append(
                f"  {c['name']} ({c['dtype']}) samples={json.dumps(c['samples'])}"
            )
    prompt = "\n".join(lines) + "\n\nWrite a description for every column above."
    if block:
        prompt += "\n\n" + block

    result: DescriptionSet = _structured(prompt, DescriptionSet, GEN_MODEL, GEN_SYSTEM)

    wanted = all_columns(schema)
    got = {(d.table, d.column): d for d in result.descriptions}
    payload = [
        {
            "table": t, "column": c,
            "description": got[(t, c)].description if (t, c) in got else "(missing)",
            "semantic_role": got[(t, c)].semantic_role if (t, c) in got else "free_text",
        }
        for t, c in wanted
    ]

    base = next_version_label()
    label = f"{base}_control" if control else base
    with db() as conn:
        conn.execute(
            "INSERT INTO versions (label,is_control,payload,injected) VALUES (?,?,?,?)",
            (label, 1 if control else 0, json.dumps(payload), block),
        )

    return {"label": label, "control": control, "rules_applied": len(rules),
            "payload": payload, "injected": block}


def _latest_descriptions() -> dict:
    vs = [v for v in versions() if not v["is_control"]]
    if not vs:
        return {}
    return {f"{d['table']}.{d['column']}": d for d in json.loads(vs[-1]["payload"])}


# ──────────────────────── compliance (pure Python) ──────────────────────────
def in_scope(rule: dict, table: str, column: str) -> bool:
    if rule["scope"] == "all":
        return True
    if rule["scope"] == "table":
        return rule["table_name"] == table
    return rule["table_name"] == table and rule["column_name"] == column


def check_one(rule: dict, description: str) -> bool | None:
    """True/False if mechanically checkable, None if not.

    Deliberately no model in this path — the compliance numbers in the report
    have to be something the AI cannot influence.
    """
    kind, val = rule["check_type"], (rule["check_value"] or "").strip()
    d = description.strip()
    if kind == "none" or not d:
        return None
    if kind == "max_words":
        try:
            return len(d.split()) <= int(float(val))
        except ValueError:
            return None
    if kind == "forbidden_prefix":
        return not d.lower().startswith(val.lower())
    if kind == "must_contain":
        return val.lower() in d.lower()
    if kind == "forbidden_substring":
        return val.lower() not in d.lower()
    return None


def compliance(rule: dict, payload: list[dict]) -> tuple[int, int] | None:
    """(passing, applicable) for one rule against one version's descriptions."""
    if rule["check_type"] == "none":
        return None
    hits = [d for d in payload if in_scope(rule, d["table"], d["column"])]
    if not hits:
        return None
    passing = sum(1 for d in hits if check_one(rule, d["description"]))
    return passing, len(hits)


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
.note { color: #6b7280; margin: 0 0 12px; max-width: 70ch; font-size: 13px; }
.wrap { overflow-x: auto; border: 1px solid #e3e6ea; border-radius: 8px; }
table { border-collapse: collapse; width: 100%; min-width: 720px; }
th, td { text-align: left; vertical-align: top; padding: 9px 12px;
         border-bottom: 1px solid #eef0f3; }
th { background: #f7f8fa; font-weight: 600; white-space: nowrap;
     position: sticky; top: 0; }
td.col { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px;
         white-space: nowrap; }
tr.tablehead td { background: #f0f2f5; font-weight: 600; font-size: 12px;
                  text-transform: uppercase; letter-spacing: .05em; color: #4b5563; }
.changed { background: #fff8e1; }
.changed::after { content: " changed"; font-size: 10px; color: #a16207;
                  text-transform: uppercase; letter-spacing: .05em; }
.ctrl { background: #f4f5f7; }
.muted { color: #9ca3af; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 999px;
         font-size: 11px; font-weight: 600; }
.ok { background: #dcfce7; color: #166534; }
.no { background: #fee2e2; color: #991b1b; }
.scope { background: #e0e7ff; color: #3730a3; font-family: ui-monospace, monospace; }
.pass { color: #166534; font-weight: 600; }
.fail { color: #991b1b; font-weight: 600; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
pre.inj { margin: 0; padding: 12px; background: #f7f8fa; border: 1px solid #e3e6ea;
          border-radius: 8px; overflow-x: auto; font-size: 12.5px; line-height: 1.5; }
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e6e8eb; }
  .wrap { border-color: #262b33; }
  th { background: #171a20; } th, td { border-bottom-color: #21252c; }
  tr.tablehead td { background: #1b1f26; color: #9aa4b2; }
  h2, .sub, .note { color: #9aa4b2; }
  .changed { background: #2b2410; }
  .ctrl { background: #16181d; }
  .ok { background: #14361f; color: #86efac; }
  .no { background: #3b1618; color: #fca5a5; }
  .scope { background: #1e2447; color: #a5b4fc; }
  .pass { color: #86efac; } .fail { color: #fca5a5; }
  pre.inj { background: #171a20; border-color: #262b33; }
}
"""


def build_report() -> Path:
    schema = load_schema()
    vs = versions()
    if not vs:
        raise SystemExit("no versions yet — run generate.py first")

    fb = all_feedback()
    accepted = [f for f in fb if f["status"] == "accepted"]
    payloads = {v["label"]: json.loads(v["payload"]) for v in vs}
    labels = [v["label"] for v in vs]
    real = [v["label"] for v in vs if not v["is_control"]]

    h = ["<h1>Column descriptions</h1>",
         f"<p class='sub'>{len(schema)} tables &middot; {len(all_columns(schema))} columns "
         f"&middot; {len(real)} version(s)</p>"]

    # ── A. compliance: the provable part ────────────────────────────────────
    checkable = [f for f in accepted if f["check_type"] != "none"]
    h.append("<h2>Compliance</h2>")
    h.append(
        "<p class='note'>Scored in Python, never by a model. A <code>control</code> "
        "column is the same version regenerated with the feedback block stripped — "
        "it is what lets a change be attributed to feedback rather than to the "
        "model rewording itself.</p>"
    )
    if not checkable:
        h.append("<p class='muted'>No mechanically checkable rules recorded yet.</p>")
    else:
        h.append("<div class='wrap'><table><thead><tr><th>Rule</th><th>Scope</th>"
                 "<th>Given after</th>"
                 + "".join(f"<th>{escape(l)}</th>" for l in labels)
                 + "</tr></thead><tbody>")
        for f in checkable:
            cells = []
            for l in labels:
                r = compliance(f, payloads[l])
                if r is None:
                    cells.append("<td class='muted'>—</td>")
                else:
                    p, n = r
                    cls = "pass" if p == n else "fail"
                    cells.append(f"<td class='{cls}'>{p}/{n}</td>")
            h.append(
                f"<tr><td>{escape(f['directive'] or f['raw_comment'])}</td>"
                f"<td><span class='badge scope'>{f['scope']}</span></td>"
                f"<td class='col'>{escape(f['given_after'])}</td>"
                + "".join(cells) + "</tr>"
            )
        h.append("</tbody></table></div>")

    # ── B. what was injected: proof of persistence ──────────────────────────
    h.append("<h2>What was sent to the model</h2>")
    h.append(
        "<p class='note'>Loaded from SQLite at generation time, not re-entered. "
        "Each run is a separate OS process, so the only route from an early "
        "version's feedback to a later version's output is the database file.</p>"
    )
    for v in vs:
        inj = v["injected"].strip()
        h.append(f"<p class='sub'><strong>{escape(v['label'])}</strong>"
                 + (" <span class='badge scope'>control</span>" if v["is_control"] else "")
                 + "</p>")
        h.append(f"<pre class='inj'>{escape(inj) if inj else '(no feedback in force)'}</pre>")

    # ── C. the description matrix ───────────────────────────────────────────
    h.append("<h2>Descriptions by version</h2>")
    h.append("<div class='wrap'><table><thead><tr><th>Column</th>"
             + "".join(f"<th>{escape(l)}</th>" for l in labels)
             + "</tr></thead><tbody>")
    for t, v in schema.items():
        h.append(f"<tr class='tablehead'><td colspan='{len(labels)+1}'>{escape(t)}</td></tr>")
        for c in v["columns"]:
            key = (t, c["name"])
            h.append(f"<tr><td class='col'>{escape(c['name'])}</td>")
            prev = None
            for l in labels:
                d = next((x["description"] for x in payloads[l]
                          if (x["table"], x["column"]) == key), "")
                is_ctrl = any(z["label"] == l and z["is_control"] for z in vs)
                cls = "ctrl" if is_ctrl else ("changed" if prev is not None and d != prev else "")
                h.append(f"<td class='{cls}'>{escape(d)}</td>")
                if not is_ctrl:
                    prev = d
            h.append("</tr>")
    h.append("</tbody></table></div>")

    # ── D. feedback log ─────────────────────────────────────────────────────
    h.append("<h2>Feedback log</h2><div class='wrap'><table><thead><tr><th>#</th>"
             "<th>Column</th><th>Scope</th><th>Comment</th><th>Rule injected</th>"
             "<th>Given after</th><th>In force</th><th>Status</th></tr></thead><tbody>")
    for f in fb:
        if f["status"] == "accepted":
            try:
                since = real.index(f["given_after"]) + 1 if f["given_after"] in real else 0
                held = len(real) - since
            except ValueError:
                held = 0
            force = f"{held} version(s)" if not f["superseded"] else "superseded"
            rule = escape(f["directive"] or "")
            badge = "<span class='badge ok'>accepted</span>"
        else:
            force = "—"
            rule = f"<span class='muted'>{escape(f['reason'] or '')}</span>"
            gate = (f["gate_failed"] or "").split("_")[0].lower()
            badge = f"<span class='badge no'>rejected: {gate}</span>"
        h.append(
            f"<tr><td>{f['id']}</td>"
            f"<td class='col'>{escape(f['table_name'])}.{escape(f['column_name'])}</td>"
            f"<td><span class='badge scope'>{f['scope']}</span></td>"
            f"<td>{escape(f['raw_comment'])}</td><td>{rule}</td>"
            f"<td class='col'>{escape(f['given_after'])}</td>"
            f"<td class='muted'>{force}</td><td>{badge}</td></tr>"
        )
    h.append("</tbody></table></div>")

    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Column descriptions — feedback loop</title>"
        f"<style>{CSS}</style></head><body>" + "\n".join(h) +
        f"<p class='sub' style='margin-top:28px'>generated {datetime.now():%Y-%m-%d %H:%M}</p>"
        "</body></html>"
    )
    REPORT_PATH.write_text(html)
    return REPORT_PATH
