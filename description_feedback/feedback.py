"""Run 2 — give feedback on a column.

    python feedback.py orders.total_amount "must state this is in INR and includes tax"
    python feedback.py orders.order_id "always name the granularity" --table
    python feedback.py products.sku "never start a description with 'This column'" --all

Scope defaults to the single column. --table widens it to every column in that
table; --all widens it to every column in the schema. You declare the scope; the
system does not infer it, because inferring wrong is the dangerous case.
"""
import sys

import engine

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) < 2:
    raise SystemExit(__doc__)

scope = "all" if "--all" in sys.argv else "table" if "--table" in sys.argv else "column"
target, comment = args[0], args[1]

r = engine.record_feedback(target, comment, scope)

BAR = "=" * 78
print(BAR)
print(("ACCEPTED" if r["ok"] else f"REJECTED  ({r['gate']})") + f"   scope={r['scope']}")
print(BAR)
print(f"  target:  {r['target']}")
print(f"  comment: {comment!r}")

if r["ok"]:
    print(f"  stored:  feedback id={r['id']}   confidence={r['confidence']:.2f}")
    print(f"  shape:   {r['shape']}")
    print(f"  check:   {r['check']}")
    print(f"  given after {r['given_after']} — applies to every version from here on")
    if r["superseded"]:
        print(f"  superseded earlier feedback: {r['superseded']}")
    print(f"\n  injected from now on:\n    {r['directive']}")
else:
    print(f"  reason:  {r['reason']}")
    print("\n  NOT stored as active feedback. It will never reach a prompt.")
