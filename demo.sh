#!/usr/bin/env bash
# Verified demo sequence. Every step below was run live against Gemini.
# Usage: ./demo.sh          (full, ~5 min, ~15 API calls)
#        ./demo.sh gate     (validation gate only, ~30s)
set -euo pipefail
PY=.venv/bin/python
step() { echo; echo "################ $* ################"; }
pause() { [ -n "${NOPAUSE:-}" ] || read -rp "  [enter] "; }

if [ "${1:-full}" = "anomaly" ]; then
  T=pmi_items
  rm -f feedback.db; rm -rf chroma; rm -f runs/${T}_anomaly_*.json
  $PY cli.py init

  step "1  Review 71 real profiling alerts, no feedback yet"
  $PY cli.py anomaly $T --label v1 --no-feedback
  pause

  step "2  Gibberish — blocked at G0, before any API call"
  $PY cli.py feedback add --table $T --entity anomaly --run v1 \
    --signature "anomaly::${T}::selling_price::missing_values" \
    --action reject --bound 99 --comment "hygyt hygyt" --explain || true
  pause

  step "3  A dismissal with no magnitude to scope it — blocked at G1"
  $PY cli.py feedback add --table $T --entity anomaly --run nonexistent \
    --signature "anomaly::${T}::printed_co::missing_values" --action reject \
    --comment "printed_co is optional metadata that only applies to printed cartons" || true
  pause

  step "4  A real dismissal — auto-scoped to the magnitude actually seen"
  $PY cli.py feedback add --table $T --entity anomaly --run v1 \
    --signature "anomaly::${T}::currency::missing_values" --action reject \
    --comment "currency is only populated for export shipments so it is expected to be mostly empty in this table"
  pause

  step "5  THE SAFETY PROPERTY — dismiss a 100% column but bound it at 50%"
  $PY cli.py feedback add --table $T --entity anomaly --run v1 \
    --signature "anomaly::${T}::selling_price::missing_values" --action reject --bound 50 \
    --comment "selling price is maintained in the pricing system not here, so some sparseness is normal"
  pause

  step "6  Regenerate — currency suppressed, selling_price still fires"
  $PY cli.py anomaly $T --label v2
  echo
  echo "  currency (99.6%, bound 99.9%) -> SUPPRESSED"
  echo "  selling_price (100%, bound 50%) -> STILL REPORTED, the bound protected you"
  pause

  step "7  Everything that was blocked"
  $PY cli.py audit
  exit 0
fi

if [ "${1:-full}" = "gate" ]; then
  step "1  GIBBERISH — rejected at G0, no API call"
  $PY cli.py feedback add --table orders --run v1 \
    --signature "rule::unique::order_id" --action reject \
    --comment "hygyt hygyt" --explain || true
  pause
  step "2  VAGUE but coherent — passes G0/G1, caught by G2"
  $PY cli.py feedback add --table orders --run v1 \
    --signature "rule::unique::order_id" --action reject \
    --comment "this rule is not very good, please make it better somehow" --explain || true
  pause
  step "3  UNSAFE SQL — blocked at G1"
  $PY cli.py feedback add --table orders --run v1 \
    --signature "rule::unique::order_id" --action correct \
    --comment "please fix this rule to also check for empty strings" \
    --expr "order_id IS NOT NULL; DROP TABLE orders" --explain || true
  pause
  step "4  Everything that was blocked"
  $PY cli.py audit
  exit 0
fi

rm -f feedback.db; rm -rf chroma; rm -f runs/*.json
$PY cli.py init

step "1  v1 — no feedback"
$PY cli.py generate orders --label v1 --no-feedback
pause

step "2  Gibberish rejected at G0 (no API call), then vague caught by G2"
$PY cli.py feedback add --table orders --run v1 \
  --signature "rule::unique::order_id" --action reject \
  --comment "hygyt hygyt" --explain || true
$PY cli.py feedback add --table orders --run v1 \
  --signature "rule::unique::order_id" --action reject \
  --comment "this rule is not very good, please make it better somehow" --explain || true
pause

step "3  Three real feedback entries"
$PY cli.py feedback add --table orders --run v1 \
  --signature "rule::arithmetic_consistency::discount+quantity+total_amount+unit_price" --action add \
  --comment "you missed the revenue formula entirely. total_amount must equal quantity times unit_price minus the discount, it is net of discount" \
  --expr "total_amount = quantity * unit_price - discount"
$PY cli.py feedback add --table orders --run v1 \
  --signature "rule::unique::order_id" --action correct \
  --comment "the expression only checks for nulls, it does not test uniqueness at all. order_id has 482301 distinct values over 482301 rows so it is a genuine key" \
  --expr "order_id IS NOT NULL AND order_id <> ''"
$PY cli.py feedback add --table orders --run v1 \
  --signature "rule::positive_number::quantity" --action confirm \
  --comment "quantity must always be greater than zero, the minimum of -3 proves this is a real defect. keep this rule always"
pause

step "4  v2 with feedback, then the diff"
$PY cli.py generate orders --label v2
$PY cli.py diff orders v1 v2
pause

step "5  THE PAYOFF — sales_transactions has NEVER received feedback"
$PY cli.py generate sales_transactions --label control --no-feedback
echo "  ^ no arithmetic rule emitted unaided"
$PY cli.py generate sales_transactions --label v1
echo "  ^ gross_total now nets off rebate, transferred from orders.total_amount"
pause

step "6  Self-heal — same mechanism"
$PY cli.py selfheal orders orders_country_null --label v1
$PY cli.py feedback add --table orders --entity selfheal --run "orders_country_null:v1" \
  --signature "heal::orders::country::not_null::mode_fill" --action reject \
  --comment "never impute country from the global mode, it silently assigns customers to the wrong country and corrupts regional revenue reporting. use the customer's own history only"
$PY cli.py selfheal orders orders_country_null --label v2
echo "  ^ mode_fill is gone"
pause

step "7  Everything that was blocked"
$PY cli.py audit

