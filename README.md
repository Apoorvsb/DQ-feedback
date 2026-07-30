# DQ Feedback POC

Persistent, validated feedback on LLM-generated data-quality rules and self-heal
suggestions. Feedback is stored, replayed into every future generation, and
transferred across tables by semantic similarity.

The problem this solves beyond the original plan: **feedback here is persistent**,
so an accepted garbage entry is not one bad response — it is permanent corruption
of the loop. Typing `hygyt` used to be accepted and injected forever. Now it is
rejected at the front door with a reason, before any API call is made.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # add your key from https://aistudio.google.com/apikey
.venv/bin/python cli.py init
```

### You must supply your own profiling report

`data/profiling_report.json` is **not in this repo** — it contained real client
data (schema and data-quality state) and is gitignored deliberately.

To run the anomaly demo, drop a ydata-profiling / `build_lightweight_report`
payload at `data/profiling_report.json`. The parser needs these keys:

```jsonc
{
  "table":     { "no_of_rows": 9128, "no_of_variables": 108, ... },
  "alerts":    [ "[column_name] 5854 (64.1%) missing values",
                 "[column_name] has a constant value" ],
  "variables": { "column_name": { "percent_of_missing_values": 0.641,
                                  "no_of_distinct_values": 5, ... } }
}
```

`alerts` is the load-bearing one — see `feedback/alerts.py` for the grammar it
expects and the alert-type vocabulary it recognises.

## The validation gate

Four checks, cheapest first. Only feedback clearing all four is stored as
`valid`+`active`, and only `valid`+`active` rows are ever injected into a prompt
or embedded into Chroma.

| Gate | Cost | Catches |
|---|---|---|
| **G0 coherence** | ~1ms, no API call | `hygyt`, `asdfgh qwerty`, keyboard mash |
| **G1 structural** | sqlglot parse | nonexistent columns, `DROP`, unparseable SQL, `UPDATE` with no `WHERE` |
| **G2 semantic** | 1 call, flash-lite | "make it better", off-topic, low-confidence |
| **G3 consistency** | DB read | contradiction with existing feedback → supersede |

G0 is calibrated against measured data, not guesswork: the bigram threshold
(−2.90) sits at the ~1st percentile of 4,000 sampled dictionary words, and a
token must trip **two** independent signals to count as garbage. Verified 20/20
on `tests/test_coherence.py` — 10 legitimate comments (including typo'd ones)
pass, 10 gibberish strings fail.

```bash
.venv/bin/python tests/test_coherence.py
```

Rejected submissions go to `validation_audit`, never to `feedback_log`. They are
unreachable by the injection query by construction, not by convention.

## Profiling anomaly feedback (the main path)

Built against a **real** `build_lightweight_report` payload —
`data/profiling_report.json`, 108 columns, 9,128 rows, 71 alerts.

```bash
.venv/bin/python cli.py anomaly pmi_items --label v1 --no-feedback
.venv/bin/python cli.py feedback add --table pmi_items --entity anomaly --run v1 \
  --signature "anomaly::pmi_items::currency::missing_values" --action reject \
  --comment "currency is only populated for export shipments so it is expected to be mostly empty"
.venv/bin/python cli.py anomaly pmi_items --label v2      # currency is gone
```

### Why anomalies need machinery rules don't

ydata alerts arrive as bare strings with **no id of any kind**, and with the
magnitude baked into the text:

```
[primary_consumer_unit_weight_uom] 5854 (64.1%) missing values
[classification] has a constant value
```

Two consequences drive the design:

**1. Signatures are mandatory, not preferable.** There is no id to key feedback
on. `anomaly::<table>::<column>::<alert_type>` strips the magnitude out, so
feedback replays across runs instead of looking brand-new every time the
percentage drifts.

**2. Unbounded dismissal is dangerous.** Rules are time-invariant; anomalies are
not. "Stop flagging missing values on this column" would also hide that column
at 100% — the exact pipeline failure you most need to see. So every dismissal
carries a `suppression_bound` (percent). Below it, silence. Above it, the alert
still fires. If the reviewer gives no bound, one is derived from the magnitude
they actually saw plus 5 points headroom — never "always".

**Verified live.** A reviewer dismissed `selling_price` as "sparseness is
normal" with a 50% bound. Its real magnitude is 100% missing, so the system
**refused to suppress it** and reported it anyway. A free-text feedback field
would have silenced an entirely empty column permanently.

Two further anomaly-only mechanisms:

- **Expiry** — anomaly feedback dies after `ANOMALY_FEEDBACK_TTL_DAYS` (90),
  because distributions drift. `live_feedback()` filters on it. Rule and
  self-heal feedback store `expires_at = NULL` and never expire.
- **Dismissal counting** — `dismiss_count` survives both supersession and
  expiry. Three dismissals of the same anomaly means the detector is
  miscalibrated, not that the reviewer needs asking a fourth time.

### Measured on the real payload

| | |
|---|---|
| alerts in / insights out | 71 → 6 |
| alert types | `missing_values` 64, `constant_value` 6, `unique_values` 1 |
| parse rate | 71/71, zero unknowns |
| missing-rate median / p75 | 89.0% / 99.7% |
| alerts below 50% missing | 11 of 64 |

That distribution is the argument for bounded suppression: 64 near-identical
missing-value alerts at a median of 89% is exactly the wall of noise a reviewer
dismisses wholesale — and exactly where an unbounded dismissal does real damage.

> **Unit trap.** `variables[col].percent_of_missing_values` is a **fraction**
> (0.641); the alert text and `missing_corr_col` use **percent** (64.1, 42.48).
> Everything in `feedback/alerts.py` is normalised to percent 0-100.

## Feedback actions

| Action | Meaning | Requires |
|---|---|---|
| `reject` | this rule is wrong, stop emitting it | — |
| `correct` | right idea, wrong expression | `--expr` |
| `confirm` | this is right, always keep it | — |
| `add` | **you failed to emit this rule at all** | `--expr` |

`add` is the only action whose signature must be *absent* from the run under
review; G1 enforces that inversion in both directions. It was added after the
first live run showed the model silently omitting the revenue formula — "you
missed X" is among the most common real review comments, and without it the
loop cannot express the most valuable feedback there is.

## Demo (~3 min)

```bash
./demo.sh          # full sequence, ~15 API calls
./demo.sh gate     # validation gate only, ~30s, no generation
```

Or step by step:

```bash
# 0. Establish the noise floor FIRST — this is what makes the v1->v2 diff provable.
.venv/bin/python cli.py noise-floor orders --runs 3

# 1. v1, no feedback. Point at the bad rules.
.venv/bin/python cli.py generate orders --label v1 --no-feedback

# 2. Gibberish — rejected at G0, before any API call, nothing persisted.
.venv/bin/python cli.py feedback add --table orders --run v1 \
  --signature "rule::unique::order_id" --action reject \
  --comment "hygyt hygyt" --explain

# 2b. Coherent but vague — passes G0/G1, caught by G2 with a clarifying question.
.venv/bin/python cli.py feedback add --table orders --run v1 \
  --signature "rule::unique::order_id" --action reject \
  --comment "this rule is not very good, please make it better somehow" --explain

# 3. Three real feedback entries.
#    NOTE: signatures must match YOUR v1 run. Check runs/orders_v1.json first —
#    generation varies, and G1 rejects feedback on a signature that isn't there.
.venv/bin/python cli.py feedback add --table orders --run v1 \
  --signature "rule::arithmetic_consistency::discount+quantity+total_amount+unit_price" --action add \
  --comment "you missed the revenue formula entirely. total_amount must equal quantity times unit_price minus the discount, it is net of discount" \
  --expr "total_amount = quantity * unit_price - discount"

.venv/bin/python cli.py feedback add --table orders --run v1 \
  --signature "rule::unique::order_id" --action correct \
  --comment "the expression only checks for nulls, it does not test uniqueness at all. order_id has 482301 distinct values over 482301 rows so it is a genuine key" \
  --expr "order_id IS NOT NULL AND order_id <> ''"

.venv/bin/python cli.py feedback add --table orders --run v1 \
  --signature "rule::positive_number::quantity" --action confirm \
  --comment "quantity must always be greater than zero, the minimum of -3 proves this is a real defect. keep this rule always"

# 4. v2 with feedback injected -> the diff.
.venv/bin/python cli.py generate orders --label v2
.venv/bin/python cli.py diff orders v1 v2

# 5. THE PAYOFF — control first, then the fed run (see Attribution below).
.venv/bin/python cli.py generate sales_transactions --label control --no-feedback
.venv/bin/python cli.py generate sales_transactions --label v1

# 6. Self-heal, same mechanism.
.venv/bin/python cli.py selfheal orders orders_country_null --label v1
.venv/bin/python cli.py feedback add --table orders --entity selfheal \
  --run "orders_country_null:v1" \
  --signature "heal::orders::country::not_null::mode_fill" --action reject \
  --comment "never impute country from the global mode, it silently assigns customers to the wrong country and corrupts regional revenue reporting. use the customer's own history only"
.venv/bin/python cli.py selfheal orders orders_country_null --label v2

# 7. Show what was blocked.
.venv/bin/python cli.py audit
```

> **Signatures are run-specific.** The model does not emit the same rule set
> every time. Before scripting feedback, read `runs/orders_v1.json` and use the
> signatures actually in it — G1 will (correctly) reject anything else.

## Attribution: what this demo can and cannot prove

`gemini-3.6-flash` and `gemini-3.5-flash-lite` **removed** `temperature`,
`top_p` and `top_k` — passing them is an API error, not a no-op. The original
plan's "temp 0 or the demo is unconvincing" lever does not exist.

The replacement is measurement. **Measured live, do not skip this:**

```
noise floor for orders (3 unfed runs):  mean churn 5.0, max 7
observed v1 -> v2 churn with feedback:  8
```

**Aggregate churn therefore proves nothing** — 8 is inside the noise band. Do
not stand up and claim "8 rules changed, so feedback worked." Someone will ask,
and they will be right.

What *is* provable is per-signature behaviour, verified against the unfed runs:

| Feedback | Result | Attribution |
|---|---|---|
| `add` arithmetic_consistency | appeared in v2, exact expression | **Strong** — appears in 0/3 unfed runs |
| `correct` unique::order_id | v2 matches requested expression exactly | **Strong** — present in 3/3 unfed runs but *never* with this expression |
| `confirm` positive_number::quantity | survived into v2 | **None** — emitted in 3/3 unfed runs anyway |

The `confirm` case is honest-but-weak: the model always emits `quantity > 0`, so
its survival is not evidence of anything. Lead with `add` and `correct`.

For the cross-table transfer, always run the control first:

```bash
.venv/bin/python cli.py generate sales_transactions --label control --no-feedback  # no arithmetic rule
.venv/bin/python cli.py generate sales_transactions --label v1                     # rule appears
```

Measured: unaided, the model emits **no** arithmetic rule for
`sales_transactions`. With feedback transferred from `orders.total_amount` (zero
column-name overlap), it emits
`gross_total = qty * price_each - COALESCE(rebate, 0)` at similarity 0.644.

> **Fixture warning.** `sales_transactions.json` deliberately does *not* state the
> rebate/gross_total relationship. An earlier version did, and the model then
> derived the formula unaided — the control produced the same rule as the fed run
> and attribution collapsed. If you edit the fixtures, re-run the control.

## Design decisions worth knowing

- **Signatures, not ids.** `(sorted(columns), rule_type)`. Diffing on statement
  strings would report every rewording as a change and bury the signal.
- **`normalized_directive` is injected; `raw_comment` never is.** The raw text is
  audit-only. Everything reaching a model has passed G2.
- **`gemini-embedding-001`, not `-2`.** Only `-001` supports `task_type`, giving
  asymmetric retrieval (`RETRIEVAL_DOCUMENT` on write, `RETRIEVAL_QUERY` on read),
  which tightens the distance distribution and makes the threshold tunable.
- **Hard distance threshold, no soft weighting.** A wrong cross-table transfer is
  worse than no transfer, so below-threshold matches are dropped.
- **SQLite is authoritative, Chroma is disposable.** An embedding failure never
  costs the durable write; `cli.py reindex` rebuilds the index.
- **Newest feedback wins.** Users may change their mind. G3 supersedes the old
  row and logs the conflict so the injection block never carries both.

## Mapping back to the platform

| POC | Production |
|---|---|
| SQLite `feedback_log` | the `sigmafeedbacklog` table |
| Chroma | pgvector over the accepted-history corpus |
| `make_rule_signature` | `make_rule_signature` in `selfheal_ai_suggestions.py` |
| injection block | `_build_rule_feedback_context` in `llm_service.py` |
| G1 self-heal guards | the `apply_selfheal_ai_suggestions` safety filter, which today blocks only `DROP`/`TRUNCATE`/`DELETE` and does **not** require a `WHERE` clause |

That last row is the one worth raising: the POC's G1 is stricter than production.
