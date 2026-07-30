#!/usr/bin/env python
"""DQ feedback POC — command line.

  python cli.py init
  python cli.py generate orders --label v1 --no-feedback
  python cli.py feedback add --table orders --signature rule::date_order::order_date+ship_date \
                             --action reject --comment "..." --explain
  python cli.py diff orders v1 v2
  python cli.py noise-floor orders --runs 3
  python cli.py selfheal orders orders_country_null --label v1
  python cli.py audit
"""
from __future__ import annotations

import argparse
import json
import sys

import config
from feedback import recorder, validator
from pipelines import diff as diffmod
from pipelines import generate, noise_floor
from store import chroma_store, sqlite_store

BAR = "=" * 78


def _print_rules(payload: dict) -> None:
    print(f"\n{payload['table_name']}  [{payload['run_label']}]  "
          f"{len(payload['rules'])} rules  "
          f"feedback_injected={payload['feedback_injected']}")
    if payload.get("transfer_hits"):
        print("  cross-table transfers applied:")
        for h in payload["transfer_hits"]:
            print(f"    similarity={h['similarity']:.3f}  from {h['from_table']}: "
                  f"{h['directive'][:70]}")
    print("-" * 78)
    for r in payload["rules"]:
        print(f"  [{r['severity']:<6}] {r['signature']}")
        print(f"           {r['expression']}")


def cmd_init(args) -> int:
    sqlite_store.init_db()
    print(f"initialised {config.DB_PATH}")
    return 0


def cmd_generate(args) -> int:
    payload = generate.generate_rules(
        args.table,
        run_label=args.label,
        use_feedback=not args.no_feedback,
        include_transfer=not args.no_transfer,
    )
    _print_rules(payload)
    print(f"\nfrozen to runs/{args.table}_{args.label}.json")
    return 0


def cmd_selfheal(args) -> int:
    payload = generate.generate_selfheal(
        args.table, args.case_id, run_label=args.label,
        use_feedback=not args.no_feedback,
    )
    print(f"\n{args.table}.{args.case_id}  [{args.label}]  "
          f"feedback_injected={payload['feedback_injected']}")
    print("-" * 78)
    for s in payload["suggestions"]:
        print(f"  [{s['confidence']:.2f}] {s['strategy']} on {s['target_column']}")
        print(f"          {s['update_sql']}")
        print(f"          why: {s['reason'][:90]}")
    return 0


def cmd_anomaly(args) -> int:
    payload = generate.generate_anomaly_insights(
        args.table, run_label=args.label,
        report_name=args.report, use_feedback=not args.no_feedback,
    )
    print(f"\n{args.table}  [anomaly {args.label}]  "
          f"{payload['alerts_supplied']} alerts in -> "
          f"{len(payload['insights'])} insights out  "
          f"feedback_injected={payload['feedback_injected']}")
    print(f"  alert types: {payload['alert_type_counts']}")
    print("-" * 78)
    for n, i in enumerate(payload["insights"], 1):
        flag = "" if i["is_actionable"] else "  [not actionable]"
        print(f"  {n}. [{i['severity']:<6}] {i['column']} — {i['anomaly_type']} "
              f"@ {i['magnitude_pct']:.1f}%{flag}")
        print(f"        {i['insight'][:86]}")
        print(f"        id: {i['signature']}")
    print(f"\nfrozen to runs/{args.table}_anomaly_{args.label}.json")
    print(
        "\nTo give feedback on one of these, copy its id:\n"
        f"  python cli.py feedback add --table {args.table} --entity anomaly "
        f"--run {args.label} \\\n"
        "      --signature \"<id from above>\" --action reject \\\n"
        "      --comment \"why this is not a problem\"        # add --bound N to widen scope"
    )
    return 0


def cmd_feedback_add(args) -> int:
    if args.entity == "anomaly":
        profile = generate.load_profiling_report(args.report)
        profile["table_name"] = args.table
    else:
        profile = generate.load_profile(args.table)

    # Resolve the artefact under review from the frozen run, so G1 can verify
    # the signature actually exists and G2 can see what is being critiqued.
    run = sqlite_store.load_run(args.table, args.run, args.entity)
    artefact, known, observed = {}, None, None
    if run:
        key = {"rule": "rules", "selfheal": "suggestions", "anomaly": "insights"}[
            args.entity
        ]
        known = {r["signature"] for r in run[key]}
        artefact = next(
            (r for r in run[key] if r["signature"] == args.signature), {}
        )
        observed = artefact.get("magnitude_pct")
    else:
        print(f"! no frozen run {args.run!r} for {args.table}; "
              f"G1 signature-existence check will be skipped", file=sys.stderr)

    outcome, fid = recorder.record(
        comment=args.comment,
        action=args.action,
        signature=args.signature,
        artefact=artefact,
        profile=profile,
        entity_type=args.entity,
        corrected_expression=args.expr,
        known_signatures=known,
        skip_llm=args.skip_llm,
        suppression_bound=args.bound,
        observed_magnitude=observed,
    )

    status_line = {
        "valid": "ACCEPTED",
        "needs_clarification": "NEEDS CLARIFICATION",
        "rejected": "REJECTED",
    }[outcome.status]

    print(BAR)
    print(f"{status_line}  ({outcome.gate_failed or 'all gates passed'})")
    print(BAR)
    print(f"  comment: {args.comment!r}")
    print(f"  reason:  {outcome.reason}")
    if outcome.clarifying_question:
        print(f"  ask:     {outcome.clarifying_question}")
    if outcome.accepted:
        print(f"  stored:  feedback_log id={fid}")
        p = outcome.detail.get("_persisted", {})
        if p.get("suppression_bound") is not None:
            print(f"  scope:   suppressed only at or below "
                  f"{p['suppression_bound']:.1f}% — above that it still fires")
        if p.get("expires_at"):
            print(f"  expires: {p['expires_at']} "
                  f"({config.ANOMALY_FEEDBACK_TTL_DAYS}d — distributions drift)")
        if p.get("escalate"):
            print(f"  ! dismissed {p['dismiss_count']}x — the detector is likely "
                  f"miscalibrated, not the data")
        print(f"  directive injected from now on:")
        print(f"    {outcome.normalized_directive}")
    else:
        print("  NOT stored in feedback_log — logged to validation_audit only.")
        print("  This feedback will never reach a prompt.")

    if args.explain:
        print("\n  gate trace:")
        print(validator.format_trace(outcome))

    return 0 if outcome.accepted else 1


def cmd_feedback_list(args) -> int:
    rows = sqlite_store.all_feedback()
    if not rows:
        print("no feedback recorded")
        return 0
    print(f"{'id':<4} {'status':<20} {'act':<8} {'live':<10} signature")
    print("-" * 78)
    for r in rows:
        live = r["status"] if r["validation_status"] == "valid" else "-"
        print(f"{r['id']:<4} {r['validation_status']:<20} {r['action']:<8} "
              f"{live:<10} {r['signature']}")
        if r["normalized_directive"]:
            print(f"     -> {r['normalized_directive'][:70]}")
    return 0


def cmd_audit(args) -> int:
    rows = sqlite_store.all_audit()
    if not rows:
        print("no rejected submissions")
        return 0
    print(f"{'id':<4} {'gate':<16} comment / reason")
    print("-" * 78)
    for r in rows:
        print(f"{r['id']:<4} {r['gate_failed']:<16} {r['raw_comment'][:52]!r}")
        print(f"     {r['reason'][:100]}")
    return 0


def cmd_diff(args) -> int:
    before = sqlite_store.load_run(args.table, args.before, args.entity)
    after = sqlite_store.load_run(args.table, args.after, args.entity)
    if not before or not after:
        missing = args.before if not before else args.after
        print(f"run {missing!r} not found for {args.table}", file=sys.stderr)
        return 2

    key = "rules" if args.entity == "rule" else "suggestions"
    rows = diffmod.diff_runs(before, after, key=key)
    pinned = {
        r["signature"] for r in sqlite_store.live_feedback(args.entity, args.table)
        if r["action"] == "confirm"
    }

    print(BAR)
    print(f"DIFF  {args.table}  {args.before} -> {args.after}")
    print(BAR)
    print(diffmod.render(rows, pinned))
    print("-" * 78)
    c = diffmod.summarize(rows)
    print(f"  dropped={c['DROPPED']}  changed={c['CHANGED']}  "
          f"added={c['ADDED']}  kept={c['KEPT']}   churn={diffmod.churn(rows)}")
    return 0


def cmd_noise_floor(args) -> int:
    print(f"generating {args.runs} unfed runs of {args.table} ...")
    result = noise_floor.measure(args.table, runs=args.runs)
    print()
    print(noise_floor.render(result))
    (config.RUNS_DIR / f"{args.table}_noise_floor.json").write_text(
        json.dumps(result, indent=2)
    )
    return 0


def cmd_reindex(args) -> int:
    n = chroma_store.rebuild()
    print(f"re-embedded {n} live feedback rows into Chroma")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("reindex").set_defaults(fn=cmd_reindex)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)

    g = sub.add_parser("generate")
    g.add_argument("table")
    g.add_argument("--label", default="v1")
    g.add_argument("--no-feedback", action="store_true")
    g.add_argument("--no-transfer", action="store_true",
                   help="use direct feedback only, skip cross-table vector retrieval")
    g.set_defaults(fn=cmd_generate)

    an = sub.add_parser("anomaly", help="review profiling alerts into insights")
    an.add_argument("table")
    an.add_argument("--label", default="v1")
    an.add_argument("--report", default="profiling_report")
    an.add_argument("--no-feedback", action="store_true")
    an.set_defaults(fn=cmd_anomaly)

    sh = sub.add_parser("selfheal")
    sh.add_argument("table")
    sh.add_argument("case_id")
    sh.add_argument("--label", default="v1")
    sh.add_argument("--no-feedback", action="store_true")
    sh.set_defaults(fn=cmd_selfheal)

    fb = sub.add_parser("feedback")
    fbs = fb.add_subparsers(dest="sub", required=True)

    fa = fbs.add_parser("add")
    fa.add_argument("--table", required=True)
    fa.add_argument("--signature", required=True)
    fa.add_argument("--action", required=True, choices=["reject", "correct", "confirm", "add"])
    fa.add_argument("--comment", required=True)
    fa.add_argument("--expr", default=None, help="corrected expression (action=correct)")
    fa.add_argument("--entity", default="rule", choices=["rule", "selfheal", "anomaly"])
    fa.add_argument("--run", default="v1", help="frozen run the artefact came from")
    fa.add_argument("--report", default="profiling_report",
                    help="profiling report fixture (anomaly entity)")
    fa.add_argument("--bound", type=float, default=None,
                    help="suppression bound as a percent 0-100; required to dismiss "
                         "an anomaly. Below this it stays silent, above it still fires.")
    fa.add_argument("--explain", action="store_true", help="print the four-gate trace")
    fa.add_argument("--skip-llm", action="store_true",
                    help="bypass G2 (offline testing of G0/G1/G3 only)")
    fa.set_defaults(fn=cmd_feedback_add)

    fl = fbs.add_parser("list")
    fl.set_defaults(fn=cmd_feedback_list)

    d = sub.add_parser("diff")
    d.add_argument("table")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--entity", default="rule", choices=["rule", "selfheal", "anomaly"])
    d.set_defaults(fn=cmd_diff)

    nf = sub.add_parser("noise-floor")
    nf.add_argument("table")
    nf.add_argument("--runs", type=int, default=3)
    nf.set_defaults(fn=cmd_noise_floor)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
