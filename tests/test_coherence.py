"""Gate 0 calibration harness.

Run: .venv/bin/python tests/test_coherence.py
Every MUST_PASS is legitimate feedback; every MUST_FAIL is what we're defending
against. Both lists matter equally — a gate that rejects everything is useless.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from feedback.coherence import check_coherence  # noqa: E402

SCHEMA_TERMS = {
    "orders", "order_id", "customer_id", "order_date", "ship_date", "quantity",
    "unit_price", "discount", "total_amount", "status", "country",
    "sales_transactions", "txn_id", "buyer_id", "purchase_dt", "dispatch_dt",
    "qty", "price_each", "rebate", "gross_total", "state", "region",
}

MUST_PASS = [
    "total_amount must subtract discount, not just quantity times unit_price",
    "ship_date can be null while the order is still pending, so this rule is wrong",
    "quantity should always be greater than zero, keep this rule",
    "the discount column is often null, coalesce it to 0 before subtracting",
    "wrong direction, ship_date comes after order_date not before",
    "use exactly quantity*unit_price - discount",
    "country has 38 distinct values so an in_list rule is not maintainable here",
    "this is a valid check but severity should be low not high",
    # typo tolerance — a real user typing fast must not be bounced
    "total_amount shoud subtract the discount amount",
    "ship_date is nullabe until the order ships, dont flag it",
]

MUST_FAIL = [
    "hygyt",
    "hygyt hygyt",
    "asdfgh qwerty",
    "hjkghtr dfghjk",
    "aaaaaa bbbbbb",
    "sdkfj sldkfj sdlkfj",
    "xzcvb nmqwe rtyui",
    "jjjj kkkk llll",
    "qqq www eee rrr",
    "zzzzz",
]


def main() -> int:
    failures = []

    print("=" * 78)
    print("MUST PASS — legitimate feedback")
    print("=" * 78)
    for text in MUST_PASS:
        r = check_coherence(text, SCHEMA_TERMS)
        ok = r["passed"]
        print(f"  [{'OK ' if ok else 'BAD'}] {text[:62]!r}")
        if not ok:
            print(f"         -> wrongly rejected: {r['reason']}")
            failures.append(("false_reject", text, r))

    print()
    print("=" * 78)
    print("MUST FAIL — gibberish")
    print("=" * 78)
    for text in MUST_FAIL:
        r = check_coherence(text, SCHEMA_TERMS)
        ok = not r["passed"]
        print(f"  [{'OK ' if ok else 'BAD'}] {text[:62]!r}")
        if ok:
            print(f"         -> {r['reason']}")
        else:
            print(f"         -> LEAKED THROUGH: signals={r['signals']} "
                  f"detail={r['detail'].get('mean_bigram')}")
            failures.append(("false_accept", text, r))

    print()
    print("=" * 78)
    total = len(MUST_PASS) + len(MUST_FAIL)
    print(f"{total - len(failures)}/{total} correct")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for kind, text, r in failures:
            print(f"  {kind}: {text!r}")
            print(f"    detail={r['detail']}")
        return 1
    print("Gate 0 calibrated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
