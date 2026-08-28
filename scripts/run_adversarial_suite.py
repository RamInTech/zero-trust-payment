"""Phase 6 — run the adversarial suite and generate the results evidence.

    uv run python scripts/run_adversarial_suite.py

Writes docs/adversarial-results.md and docs/adversarial-results.json from a
real run. Phase 8's README embeds the generated file rather than restating its
numbers, so the table cannot drift from reality by hand-copying.

Exits non-zero if any attack breached, so this is usable as a check and not
only as a report generator.
"""

import json
import sys
import tempfile
from pathlib import Path

from zerotrust.adversary import (
    MANDATE_CAP_PAISE,
    MANDATE_SKUS,
    MANDATE_VELOCITY,
    run_suite,
)

DOCS = Path(__file__).resolve().parent.parent / "docs"
MD_PATH = DOCS / "adversarial-results.md"
JSON_PATH = DOCS / "adversarial-results.json"


def main() -> int:
    print("=" * 78)
    print("  ADVERSARIAL SUITE — a hostile agent attacks the running system")
    print("=" * 78)
    print(f"\n  Mandate under attack:")
    print(f"    max per transaction : Rs.{MANDATE_CAP_PAISE / 100:,.2f}")
    print(f"    allowed items       : {', '.join(sorted(MANDATE_SKUS))}")
    print(f"    velocity            : {MANDATE_VELOCITY} purchases/hour")
    print("\n  Every attack below goes through the HTTP API, except where the")
    print("  row says otherwise.\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        report = run_suite(tmpdir)

    for i, outcome in enumerate(report.outcomes, 1):
        marker = ">>>" if outcome.defended else "!!!"
        print(f"  {marker} {i:2}. {outcome.name}")
        print(f"        attack   : {outcome.attack}")
        print(f"        result   : {outcome.status}")
        print(f"        stopped by: {outcome.defence}")
        print(f"        evidence : {outcome.evidence}")
        charges = f"{outcome.money_actions}"
        if outcome.money_actions != outcome.intended_actions:
            charges += f"  <-- EXPECTED {outcome.intended_actions}"
        print(f"        charges  : {charges}\n")

    DOCS.mkdir(exist_ok=True)
    MD_PATH.write_text(report.to_markdown() + "\n")
    JSON_PATH.write_text(report.to_json() + "\n")

    print("=" * 78)
    print(f"  {report.defended}/{len(report.outcomes)} attacks defended")
    print(f"  {report.unintended_charges} unintended charges")
    print("=" * 78)
    print(f"\n  wrote {MD_PATH.relative_to(DOCS.parent)}")
    print(f"  wrote {JSON_PATH.relative_to(DOCS.parent)}")

    if report.breached:
        print(f"\n  FAILED: {report.breached} attack(s) breached the system.")
        return 1
    print("\n  All attacks defended. No money moved that shouldn't have.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
