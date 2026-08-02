"""Run 2 — give feedback on one generated rule.

    python feedback.py "<signature>" "this flags normal data because ..." --reject
    python feedback.py "<signature>" "not high, nobody acts on this" --severity low
    python feedback.py "<signature>" "this one is genuinely useful" --confirm
    python feedback.py "<signature>" "query misses the null case" --sql "SELECT ..."

The signature is printed under every rule by generate.py. It is production's own
build_signature() — anomaly_type plus the columns involved — so feedback survives
the model renaming or rewording the rule.
"""
import sys

import engine

argv = sys.argv[1:]
plain = [a for a in argv if not a.startswith("--")]
if len(plain) < 2:
    raise SystemExit(__doc__)

signature, comment = plain[0], plain[1]


def flag_value(name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
    return None


new_severity = flag_value("--severity")
new_sql = flag_value("--sql")

if new_severity:
    action = "severity"
elif new_sql:
    action = "correct"
elif "--confirm" in argv:
    action = "confirm"
else:
    action = "reject"

r = engine.record_feedback(signature, comment, action, new_severity, new_sql)

BAR = "=" * 78
print(BAR)
print(("ACCEPTED" if r["ok"] else f"REJECTED  ({r['gate']})") + f"   action={r['action']}")
print(BAR)
print(f"  rule:    {r.get('rule_name') or '(not in the reviewed run)'}")
print(f"  id:      {r['signature']}")
print(f"  comment: {comment!r}")

if r["ok"]:
    print(f"  stored:  feedback id={r['id']}   confidence={r['confidence']:.2f}")
    print(f"  given after {r['given_after']} — applies to every version from here on")
    if r["superseded"]:
        print(f"  superseded earlier feedback on this rule: {r['superseded']}")
    print(f"\n  injected from now on:\n    {r['directive']}")
else:
    print(f"  reason:  {r['reason']}")
    print("\n  NOT stored as active feedback. It will never reach a prompt.")
