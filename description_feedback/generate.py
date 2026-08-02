"""Run 1 — produce the next version of the descriptions.

    python generate.py              next version, feedback applied
    python generate.py --control    same version, feedback block stripped

The control run is what makes the report's "changed" highlighting mean anything:
same schema, same model, one difference.
"""
import sys

import engine

control = "--control" in sys.argv
r = engine.generate(control=control)

tag = "  [CONTROL — feedback stripped]" if control else ""
print(f"\n{r['label']}{tag}   {len(r['payload'])} columns   "
      f"{r['rules_applied']} rule(s) in force")
print("-" * 78)

current = None
for d in r["payload"]:
    if d["table"] != current:
        current = d["table"]
        print(f"\n{current}")
    print(f"  {d['column']:<16} {d['description']}")

if r["injected"]:
    print("\n" + "-" * 78)
    print("loaded from the database and sent with this run:")
    print(r["injected"])

print(f"\nnext: python feedback.py <table.column> \"<comment>\" [--table|--all]")
