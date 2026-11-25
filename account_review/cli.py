"""routing-account-review command line.

  python -m account_review.cli synth --week week1 --out exports/week1.ndjson
  python -m account_review.cli pull --since 2026-11-02T00:00:00Z --until 2026-11-09T00:00:00Z --out exports/live.ndjson
  python -m account_review.cli review exports/week1.ndjson [--note replay|record|live] [--json out.json]
  python -m account_review.cli shadow [--record]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import export, note, review, savings, shadow, synth
from .pricing import PriceBook


def _print_review(r: dict) -> None:
    o = r["opt_in"]
    s = o["savings"]
    print(f"Decision: {r['decision']}  ({'; '.join(r['gate']['reasons'])})")
    print(f"Opt-in group: {o['engineers']} engineers, {o['main_loop_turns']} main-loop turns, {o['actions']} actions")
    print(f"Savings vs baseline: ${s['savings_usd']:.2f} ({s['savings_pct']}%) on ${s['baseline_usd']:.2f}")
    print(f"Docs query would say: ${o['docs_sql_savings']['savings_usd']:.2f}")
    for e in r["engineers"]:
        fails = [k for k, v in (e["checks"] or {}).items() if v == "fail"]
        print(f"  {e['user_id']} {e['client_app']:<11} {'ok' if not fails else 'FAIL ' + ', '.join(fails)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="account-review")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("synth", help="write a synthetic export week")
    a.add_argument("--week", choices=["week1", "week2"], required=True)
    a.add_argument("--out", required=True)

    a = sub.add_parser("pull", help="pull the live export with WEAVE_ANALYTICS_KEY")
    a.add_argument("--since", required=True)
    a.add_argument("--until")
    a.add_argument("--out", required=True)
    a.add_argument("--base-url", default=os.environ.get("WEAVE_ROUTER_URL", "https://router.weaveos.com"))

    a = sub.add_parser("review", help="review an export file")
    a.add_argument("ndjson")
    a.add_argument("--note", choices=["off", "replay", "record", "live"], default="replay")
    a.add_argument("--json")

    a = sub.add_parser("shadow", help="send the shadow sample to POST /v1/route with WEAVE_ROUTER_KEY")
    a.add_argument("--record", action="store_true")
    a.add_argument("--base-url", default=os.environ.get("WEAVE_ROUTER_URL", "https://router.weaveos.com"))

    args = ap.parse_args(argv)
    if args.cmd == "synth":
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        export.write_ndjson(synth.generate(args.week), args.out)
        print(f"wrote {args.out}")
    elif args.cmd == "pull":
        client = export.ExportClient(args.base_url, os.environ["WEAVE_ANALYTICS_KEY"])
        rows = client.decisions(args.since, args.until)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        export.write_ndjson(rows, args.out)
        print(f"wrote {len(rows)} rows to {args.out}")
    elif args.cmd == "review":
        rows = export.read_ndjson(args.ndjson)
        problems = export.validate(rows)
        if problems:
            print("schema problems:", *problems[:10], sep="\n  ")
            return 2
        book = PriceBook.load()
        r = review.review(rows, book)
        r["bridge"] = savings.bridge([x for x in rows if x.get("blind_experiment_arm") == "router_on"], book)
        if args.note != "off":
            r["note"] = note.draft(r, mode=args.note)
        _print_review(r)
        if r.get("note", {}).get("text"):
            print("\nNote (" + r["note"]["status"] + "):\n" + r["note"]["text"])
        if args.json:
            Path(args.json).write_text(json.dumps(r, indent=1))
    elif args.cmd == "shadow":
        out = shadow.run(args.base_url, os.environ["WEAVE_ROUTER_KEY"])
        print(json.dumps(out["by_kind"], indent=1))
        if args.record:
            shadow.RECORDING.parent.mkdir(exist_ok=True)
            shadow.RECORDING.write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
