# DQ feedback POCs

Two proofs of the same idea: reviewer feedback on AI-generated artefacts is
**validated before it is stored**, then **replayed into every later run** without
being re-entered.

The problem both solve: feedback here is persistent. An accepted garbage entry is
not one bad response — it is permanent corruption replayed forever. Typing
`hygyt` used to be accepted and injected into every future generation.

| Folder | Artefact reviewed | Status |
|---|---|---|
| `anomaly_feedback/` | anomaly-detection rules | working, verified live |
| `description_feedback/` | column descriptions | working, verified live |

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # add a key from https://aistudio.google.com/apikey
```

## Running either one

Same three commands in both folders:

```bash
cd anomaly_feedback
../.venv/bin/python generate.py            # produce a version
../.venv/bin/python feedback.py ...        # comment on one item
../.venv/bin/python generate.py            # regenerate, feedback applied
../.venv/bin/python generate.py --control  # same inputs, feedback stripped
../.venv/bin/python report.py              # report.html
```

## The four checks

Feedback must clear all four before it is stored. Only stored feedback is ever
injected into a prompt.

| Gate | Cost | Catches |
|---|---|---|
| G0 coherence | ~1ms, no API call | `hygyt`, keyboard mash |
| G1 structural | parse / lookup | unknown columns, unsafe SQL, bad scope |
| G2 semantic | 1 small-model call | "make it better", off-topic |
| G3 consistency | DB read | contradicts earlier feedback → supersede |

Rejected submissions are recorded with a reason but stored where the injection
query cannot reach them — unreachable by construction, not by convention.

## Why every run has a control

The models reword themselves run to run, so "the output changed" proves nothing.
`--control` regenerates from identical inputs with the feedback block stripped.
A change is only attributable if it appears with feedback and not without.

This caught two false claims during development:

- a "keep under 10 words" rule scored 20/20 in the control — the model already
  did it unprompted, so compliance proved nothing
- a rejected anomaly rule vanished from the fed run, but appeared in **0 of 3**
  control runs, so its absence was not attributable either

Severity feedback on anomaly rules is the claim that holds up: medium or absent
across every control, high only when the feedback is present.

## Data you must supply

`anomaly_feedback/profiling.json` is **not in this repo** — it held real client
data and is gitignored. Supply a ydata-profiling / `build_lightweight_report`
payload with `table`, `variables` and `correlations.phi_k`.

> **Known upstream bug.** phi_k rows arrive with no row label. Production
> serialises with `phik_df.reset_index().rename(columns={"index": "column"})`
> (`sigma_dq_profiling_utils.py`), but phik's index is not named `index`, so the
> rename no-ops and the label column never reaches the JSON. `phik_row_labels()`
> recovers it from the diagonal. Do not index into `variables` — it has a
> different length and order, and mislabels every row.

## Mapping back to the platform

| POC | Production |
|---|---|
| `build_signature()` | verbatim copy of `celery_worker.py::build_signature` |
| feedback injection block | sibling of `{previous_rule_signatures}` in `USER_PROMPT` |
| SQLite tables | `sigmafeedbacklog` |

**The blocker to raise first:** `crud/sigma_table.py::generate_anomaly_rules`
returns the cached `AnomalyDetectionRules` row for a given profiling snapshot and
only fires the Celery task when nothing is cached. Any feedback feature built on
top of that endpoint will appear to do nothing until it can re-run against the
same snapshot.
