"""Column-description generation with a persistent feedback loop.

Three functions are the whole interface:

    generate()                          -> build the next version
    add_feedback(target, comment, ...)  -> record feedback on a column
    report()                            -> write report.html

Feedback is persistent. Every generation rebuilds its prompt from the full
feedback log, so a comment made before v2 is still in effect at v5. Nothing is
a delta.

Scope
-----
Feedback lands on the column it was given on. Widen it explicitly:

    add_feedback("orders.total_amount", "state the currency")                  # that column
    add_feedback("orders.total_amount", "state the currency", scope="table")   # all of orders
    add_feedback("orders.total_amount", "state the currency", scope="all")     # every column
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from pydantic import BaseModel, Field

# Reuse the Gemini wrapper and model config from the parent project.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config                       # noqa: E402
from llm import client as llm_client  # noqa: E402

DATA_PATH = HERE / "data.json"
FEEDBACK_PATH = HERE / "feedback.json"
RUNS_PATH = HERE / "runs.json"
REPORT_PATH = HERE / "report.html"

SCOPES = ("column", "table", "all")
MIN_COMMENT_CHARS = 8
MAX_EXAMPLES = 4


# --- schemas ---------------------------------------------------------------
class ColumnDescription(BaseModel):
    table: str
    column: str
    description: str = Field(description="One line. No trailing period needed.")


class DescriptionSet(BaseModel):
    items: list[ColumnDescription]


class FeedbackVerdict(BaseModel):
    """Gate 2. Is this comment specific enough to act on?"""

    actionable: bool
    normalized_rule: str = Field(
        description=(
            "The comment rewritten as a short imperative instruction, e.g. "
            "'Keep the description under 12 words.' Empty string if not actionable."
        )
    )
    clarifying_question: str = Field(
        description="If not actionable, the one question to ask. Otherwise empty string."
    )
    rationale: str = Field(description="One sentence.")


# --- state -----------------------------------------------------------------
def _load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2))


def load_data() -> dict:
    return json.loads(DATA_PATH.read_text())


def load_feedback() -> list[dict]:
    return _load(FEEDBACK_PATH, [])


def load_runs() -> dict:
    return _load(RUNS_PATH, {})


def current_version() -> str | None:
    runs = load_runs()
    return list(runs)[-1] if runs else None


def _next_version() -> str:
    return f"v{len(load_runs()) + 1}"


def reset() -> None:
    """Wipe feedback and runs. Leaves data.json alone."""
    for p in (FEEDBACK_PATH, RUNS_PATH, REPORT_PATH):
        p.unlink(missing_ok=True)
    print("reset: feedback.json, runs.json, report.html removed")


# --- feedback --------------------------------------------------------------
def _valid_target(target: str, data: dict) -> tuple[str, str]:
    if "." not in target:
        raise ValueError(f"target must be 'table.column', got {target!r}")
    table, column = target.split(".", 1)
    if table not in data:
        raise ValueError(f"unknown table {table!r}; have: {', '.join(data)}")
    names = [c["name"] for c in data[table]["columns"]]
    if column not in names:
        raise ValueError(f"unknown column {column!r} in {table}; have: {', '.join(names)}")
    return table, column


def _gate2(target: str, comment: str, current_description: str) -> FeedbackVerdict:
    prompt = (
        f"COLUMN: {target}\n"
        f'CURRENT DESCRIPTION: "{current_description or "(not generated yet)"}"\n\n'
        f'REVIEWER COMMENT (verbatim, may be nonsense):\n"""{comment}"""\n\n'
        "Judge the comment. Return JSON matching the supplied schema."
    )
    system = (
        "You judge whether a reviewer's comment on a generated column description is "
        "specific enough to act on. You do NOT judge whether it is correct — only "
        "whether it states a concrete change.\n\n"
        "actionable=true  -> it names a concrete change. Rewrite it as a short "
        "imperative rule.\n"
        "actionable=false -> vague ('make it better'), off-topic, or not meaningful "
        "language. Ask one clarifying question.\n\n"
        "Be strict. This feedback is persistent — it is replayed into every future "
        "generation, so a bad entry causes lasting damage. When in doubt, say "
        "actionable=false."
    )
    return llm_client.structured(
        prompt, FeedbackVerdict, model=config.VALIDATOR_MODEL, system=system
    )


def add_feedback(target: str, comment: str, scope: str = "column") -> dict:
    """Record feedback on a column. Returns the stored record.

    Two gates before it is accepted:
      1. length   — deterministic, no API call
      2. semantic — one flash-lite call; also normalizes the comment into an
                    imperative rule, which is what actually gets injected

    Rejected feedback is stored with status='rejected' and never reaches a prompt.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")

    data = load_data()
    _valid_target(target, data)

    feedback = load_feedback()
    runs = load_runs()
    version = current_version()
    prev = runs.get(version, {}) if version else {}

    record = {
        "id": len(feedback) + 1,
        "target": target,
        "scope": scope,
        "comment": comment,
        "rule": "",
        "status": "accepted",
        "gate_failed": None,
        "reason": "",
        "given_after_version": version or "v0",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # Gate 1: length.
    if len(comment.strip()) < MIN_COMMENT_CHARS:
        record.update(
            status="rejected",
            gate_failed="length",
            reason=f"only {len(comment.strip())} chars, minimum {MIN_COMMENT_CHARS}",
        )
    else:
        # Gate 2: semantic.
        verdict = _gate2(target, comment, prev.get(target, ""))
        if not verdict.actionable or not verdict.normalized_rule.strip():
            record.update(
                status="rejected",
                gate_failed="semantic",
                reason=verdict.rationale,
                clarifying_question=verdict.clarifying_question,
            )
        else:
            record["rule"] = verdict.normalized_rule.strip()
            record["reason"] = verdict.rationale

    feedback.append(record)
    _save(FEEDBACK_PATH, feedback)

    if record["status"] == "accepted":
        print(f"ACCEPTED  #{record['id']}  {target}  scope={scope}")
        print(f"  rule injected from now on: {record['rule']}")
    else:
        print(f"REJECTED  #{record['id']}  {target}  (gate: {record['gate_failed']})")
        print(f"  {record['reason']}")
        if record.get("clarifying_question"):
            print(f"  ask: {record['clarifying_question']}")
        print("  not stored as active — this will never reach a prompt.")
    return record


# --- prompt ----------------------------------------------------------------
def rules_for(table: str, column: str, feedback: list[dict]) -> list[dict]:
    """Accepted rules that apply to this column, oldest first.

    Resolution is column + its table's scope='table' entries. scope='all' entries
    are rendered once in their own section rather than repeated 20 times.
    """
    out = []
    for f in feedback:
        if f["status"] != "accepted":
            continue
        f_table, f_column = f["target"].split(".", 1)
        if f["scope"] == "table" and f_table == table:
            out.append(f)
        elif f["scope"] == "column" and f_table == table and f_column == column:
            out.append(f)
    return out


def _global_rules(feedback: list[dict]) -> list[dict]:
    return [f for f in feedback if f["status"] == "accepted" and f["scope"] == "all"]


def _examples(feedback: list[dict], runs: dict) -> list[str]:
    """before -> after pairs, only for widened feedback.

    Per-column feedback does not need an example: the rule sits directly next to
    the column and its current text in the prompt, so there is nothing to
    transfer. Widened feedback does, because the model has to carry the intent
    onto columns it was never raised on.
    """
    labels = list(runs)
    out = []
    for f in reversed(feedback):
        if f["status"] != "accepted" or f["scope"] == "column":
            continue
        given = f["given_after_version"]
        if given not in labels:
            continue
        i = labels.index(given)
        if i + 1 >= len(labels):
            continue
        before = runs[labels[i]].get(f["target"])
        after = runs[labels[i + 1]].get(f["target"])
        if not before or not after or before == after:
            continue
        out.append(
            f"  {f['target']}\n"
            f'    before: "{before}"\n'
            f'    after:  "{after}"\n'
            f"    why:    {f['rule']}"
        )
        if len(out) >= MAX_EXAMPLES:
            break
    return out


def build_prompt(data: dict, feedback: list[dict], prev: dict, revise_after: str | None) -> str:
    """Assemble the generation prompt.

    Columns split into two buckets:
      REVISE   — feedback arrived since the last version was generated
      ACCEPTED — no objection raised, so the content stands
    ACCEPTED does not mean frozen: a scope='all' rule can still restyle it.
    """
    parts = []

    for table, tinfo in data.items():
        block = [f"TABLE {table} — {tinfo['description']}"]
        for col in tinfo["columns"]:
            name = col["name"]
            key = f"{table}.{name}"
            samples = ", ".join(str(s) for s in col["samples"])
            block.append(f"  {name} ({col['dtype']}, e.g. {samples})")

            if key in prev:
                block.append(f'      current: "{prev[key]}"')

            scoped = rules_for(table, name, feedback)
            for n, f in enumerate(scoped, 1):
                block.append(f"      RULE {n}: {f['rule']}")

            is_revise = any(f["given_after_version"] == revise_after for f in scoped)
            if key in prev:
                if is_revise:
                    block.append("      REVISE — rewrite this to satisfy the rules above.")
                else:
                    block.append(
                        "      ACCEPTED — keep the meaning as-is; change the wording only "
                        "if a rule above or a global rule requires it."
                    )
        parts.append("\n".join(block))

    body = "\n\n".join(parts)

    tail = [
        body,
        "Write one short description for every column listed above. "
        "Return JSON matching the supplied schema, with one item per column.",
    ]

    globals_ = _global_rules(feedback)
    if globals_:
        lines = "\n".join(f"  - {f['rule']}" for f in globals_)
        tail.append(
            "RULES FOR EVERY COLUMN — these apply to all columns in all tables, "
            "including ones marked ACCEPTED. Later rules override earlier ones.\n" + lines
        )

    examples = _examples(feedback, load_runs())
    if examples:
        tail.append(
            "EXAMPLES OF PREVIOUSLY APPLIED FEEDBACK — match this style.\n"
            + "\n".join(examples)
        )

    return "\n\n".join(tail)


SYSTEM = """You write short, precise descriptions for database columns.

- One line per column. Plain language. No markdown.
- Describe what the column means to the business, not its storage type.
- Reviewer rules attached to a column OVERRIDE your own judgement, without exception.
- A column marked ACCEPTED has already been approved: preserve its meaning. Reword it
  only when a rule requires it."""


# --- generation ------------------------------------------------------------
def generate() -> str:
    """Build the next version. Returns its label ('v1', 'v2', ...)."""
    data = load_data()
    feedback = load_feedback()
    runs = load_runs()

    prev_label = current_version()
    prev = runs.get(prev_label, {}) if prev_label else {}
    label = _next_version()

    prompt = build_prompt(data, feedback, prev, revise_after=prev_label)
    result: DescriptionSet = llm_client.structured(
        prompt, DescriptionSet, model=config.GEN_MODEL, system=SYSTEM
    )

    out = {}
    got = {f"{i.table}.{i.column}": i.description.strip() for i in result.items}
    for table, tinfo in data.items():
        for col in tinfo["columns"]:
            key = f"{table}.{col['name']}"
            # If the model skipped a column, keep the previous text rather than
            # dropping the row out of the report.
            out[key] = got.get(key) or prev.get(key, "")

    runs[label] = out
    _save(RUNS_PATH, runs)

    n_changed = sum(1 for k, v in out.items() if prev.get(k) not in (None, v))
    print(f"{label}: {len(out)} descriptions" + (f", {n_changed} changed" if prev else ""))
    return label


# --- report ----------------------------------------------------------------
_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; padding: 32px; background: #fff; color: #16181d; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 32px 0 10px; text-transform: uppercase;
     letter-spacing: .06em; color: #6b7280; }
.sub { color: #6b7280; margin: 0 0 8px; }
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
.muted { color: #9ca3af; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 999px;
         font-size: 11px; font-weight: 600; }
.ok { background: #dcfce7; color: #166534; }
.no { background: #fee2e2; color: #991b1b; }
.scope { background: #e0e7ff; color: #3730a3; font-family: ui-monospace, monospace; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e6e8eb; }
  .wrap { border-color: #262b33; }
  th { background: #171a20; } th, td { border-bottom-color: #21252c; }
  tr.tablehead td { background: #1b1f26; color: #9aa4b2; }
  h2, .sub { color: #9aa4b2; }
  .changed { background: #2b2410; }
  .ok { background: #14361f; color: #86efac; }
  .no { background: #3b1618; color: #fca5a5; }
  .scope { background: #1e2447; color: #a5b4fc; }
}
"""


def report(path: Path | None = None) -> Path:
    """Write report.html: one row per column, one column per version."""
    path = path or REPORT_PATH
    data = load_data()
    runs = load_runs()
    feedback = load_feedback()
    labels = list(runs)

    h = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Column descriptions — feedback loop</title>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>Column descriptions</h1>",
        f"<p class='sub'>{len(data)} tables &middot; "
        f"{sum(len(t['columns']) for t in data.values())} columns &middot; "
        f"{len(labels)} version(s)</p>",
    ]

    if not labels:
        h.append("<p class='muted'>No versions yet — call generate().</p></body></html>")
        path.write_text("\n".join(h))
        return path

    h.append("<div class='wrap'><table><thead><tr><th>Column</th>")
    h += [f"<th>{escape(l)}</th>" for l in labels]
    h.append("</tr></thead><tbody>")

    span = len(labels) + 1
    for table, tinfo in data.items():
        h.append(f"<tr class='tablehead'><td colspan='{span}'>{escape(table)}</td></tr>")
        for col in tinfo["columns"]:
            key = f"{table}.{col['name']}"
            h.append(f"<tr><td class='col'>{escape(col['name'])}</td>")
            for i, label in enumerate(labels):
                text = runs[label].get(key, "")
                prior = runs[labels[i - 1]].get(key, "") if i else None
                cls = " class='changed'" if prior is not None and text != prior else ""
                h.append(f"<td{cls}>{escape(text)}</td>")
            h.append("</tr>")
    h.append("</tbody></table></div>")

    if feedback:
        h.append("<h2>Feedback log</h2><div class='wrap'><table><thead><tr>"
                 "<th>#</th><th>Column</th><th>Scope</th><th>Comment</th>"
                 "<th>Rule injected</th><th>Given after</th><th>Status</th>"
                 "</tr></thead><tbody>")
        for f in feedback:
            ok = f["status"] == "accepted"
            badge = (f"<span class='badge ok'>accepted</span>" if ok
                     else f"<span class='badge no'>rejected: {escape(f['gate_failed'] or '')}</span>")
            rule = escape(f["rule"]) if ok else f"<span class='muted'>{escape(f['reason'])}</span>"
            h.append(
                f"<tr><td>{f['id']}</td>"
                f"<td class='col'>{escape(f['target'])}</td>"
                f"<td><span class='badge scope'>{escape(f['scope'])}</span></td>"
                f"<td>{escape(f['comment'])}</td>"
                f"<td>{rule}</td>"
                f"<td class='col'>{escape(f['given_after_version'])}</td>"
                f"<td>{badge}</td></tr>"
            )
        h.append("</tbody></table></div>")

    h.append("</body></html>")
    path.write_text("\n".join(h))
    print(f"wrote {path}")
    return path


# --- convenience -----------------------------------------------------------
def show(version: str | None = None) -> None:
    """Print one version's descriptions to the terminal."""
    runs = load_runs()
    version = version or current_version()
    if not version or version not in runs:
        print("no such version")
        return
    print(f"--- {version} ---")
    for key, text in runs[version].items():
        print(f"  {key:<28} {text}")


def preview_prompt() -> str:
    """Return the prompt the next generate() would send. No API call."""
    prev_label = current_version()
    prev = load_runs().get(prev_label, {}) if prev_label else {}
    return build_prompt(load_data(), load_feedback(), prev, revise_after=prev_label)
