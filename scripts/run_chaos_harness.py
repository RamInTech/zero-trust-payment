"""Phase 11 — run the chaos harness and generate the results evidence.

    uv run python scripts/run_chaos_harness.py
    uv run python scripts/run_chaos_harness.py --rounds 60 --purchases 32
    CHAOS_SEED=12345 uv run python scripts/run_chaos_harness.py   # replay

Writes docs/chaos-results.md and docs/chaos-results.json from a real run.
README embeds the generated file rather than restating its numbers, so the
survival rate cannot drift from reality by hand-copying -- the same rule as
the adversarial suite.

Exits non-zero if any round violated an invariant, so this is usable as a
check and not only as a report generator. A failing round prints its seed;
passing that seed back reproduces the same plan of faults exactly.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from zerotrust.chaos import DEFAULT_DSN, ChaosHarness

DOCS = Path(__file__).resolve().parent.parent / "docs"
MD_PATH = DOCS / "chaos-results.md"
JSON_PATH = DOCS / "chaos-results.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=40)
    parser.add_argument("--purchases", type=int, default=24)
    parser.add_argument("--kills", type=int, default=2,
                        help="processes SIGKILLed per round")
    parser.add_argument("--seed", type=int,
                        default=int(os.environ["CHAOS_SEED"])
                        if os.environ.get("CHAOS_SEED") else None)
    args = parser.parse_args()

    load_dotenv()
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    harness = ChaosHarness(dsn, rounds=args.rounds, purchases=args.purchases,
                           process_kills=args.kills, seed=args.seed)

    print("=" * 78)
    print("  CHAOS HARNESS — real purchases, with the ground removed underneath")
    print("=" * 78)
    print(f"\n  master seed : {harness.seed}   (CHAOS_SEED={harness.seed} replays this run)")
    print(f"  rounds      : {args.rounds} x {args.purchases} purchases, "
          f"{args.kills} process kill(s) each")
    print(f"  database    : {dsn} (one throwaway schema per round)")
    print("\n  Injected per round: slow and timing-out provider calls, crashes")
    print("  between the charge and the record, Postgres backends terminated")
    print("  mid-transaction, and processes SIGKILLed after the money moved.\n")

    report = harness.run()

    for i, result in enumerate(report.rounds, 1):
        marker = ">>>" if result.survived else "!!!"
        print(f"  {marker} round {i:3} seed {result.seed:<12} "
              f"charged {result.charged_orders:3}  "
              f"repaired {result.repairs:3}  "
              f"in flight {result.in_flight_records:2}  "
              f"{result.duration_seconds:5.2f}s")
        for violation in result.violations:
            print(f"        VIOLATION: {violation}")

    DOCS.mkdir(exist_ok=True)
    MD_PATH.write_text(report.to_markdown())
    JSON_PATH.write_text(report.to_json() + "\n")

    totals = report.totals
    print("\n" + "=" * 78)
    print(f"  {report.summary}")
    print(f"  {totals['charged_orders']} orders charged, "
          f"Rs.{totals['charged_paise'] / 100:,.2f} taken, "
          f"Rs.{totals['revenue_paise'] / 100:,.2f} booked")
    print(f"  {totals['backend_kills']} database backends killed, "
          f"{totals['process_kills']} processes SIGKILLed, "
          f"{totals['repairs']} divergences repaired")
    print("=" * 78)
    print(f"\n  wrote {MD_PATH.relative_to(DOCS.parent)}")
    print(f"  wrote {JSON_PATH.relative_to(DOCS.parent)}")

    if not report.survived_all:
        failed = report.failures
        print(f"\n  FAILED: {len(failed)} round(s) violated an invariant.")
        print(f"  Replay one with: CHAOS_SEED={failed[0].seed} "
              f"uv run python scripts/run_chaos_harness.py --rounds 1")
        return 1
    print("\n  Every round stayed correct. No money moved that shouldn't have.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
