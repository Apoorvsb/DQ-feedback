"""Run 1 — generate anomaly rules from the profiling statistics.

    python generate.py              next version, feedback applied
    python generate.py --control    same snapshot, feedback block stripped

The control run is what makes any change attributable. Production cannot do this
today: /anomaly_insights returns the cached AnomalyDetectionRules row for a given
profiling snapshot, so re-running never re-runs.
"""
import sys

import engine

control = "--control" in sys.argv
r = engine.generate(control=control)

tag = "   [CONTROL — feedback stripped]" if control else ""
print(f"\n{r['label']}{tag}   {len(r['rules'])} rule(s)   "
      f"{r['feedback_applied']} feedback item(s) in force")
print("=" * 78)

for i, rule in enumerate(r["rules"], 1):
    print(f"\n{i}. [{rule['severity']:<6}] {rule['rule_name']}")
    print(f"   type:    {rule['anomaly_type']}")
    print(f"   targets: {', '.join(rule['target_columns'])}")
    print(f"   grain:   {', '.join(rule['identifier_columns']) or '(none)'}")
    print(f"   impact:  {rule['business_impact'][:88]}")
    print(f"   id:      {rule['signature']}")

if r["injected"]:
    print("\n" + "=" * 78)
    print("loaded from the database and sent with this run:")
    print(r["injected"])

print('\nnext:  python feedback.py "<id from above>" "<comment>" [--reject|--severity X|--confirm]')
