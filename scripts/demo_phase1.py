"""Phase 1 manual demo — watch the idempotency core work, step by step.

    uv run python scripts/demo_phase1.py

Leaves demo_phase1.db behind on purpose so you can inspect it with sqlite3
afterwards. Every "CHARGE" line printed below is real money moving, as far as
the mock processor is concerned.
"""

import os
import threading
import time

from zerotrust.idempotency import IdempotencyStore, Outcome
from zerotrust.processor import MockPaymentProcessor

DB = "demo_phase1.db"


class LoudProcessor(MockPaymentProcessor):
    """Same mock, but it announces every charge so you can count them yourself."""

    def charge(self, order_id, amount_paise, currency="INR"):
        result = super().charge(order_id, amount_paise, currency)
        print(f"        >>> CHARGE #{self.charge_count}: "
              f"{order_id} for Rs.{amount_paise / 100:,.2f} "
              f"[{result['charge_id']}]")
        return result


def banner(n, title):
    print(f"\n{'=' * 70}\n  {n}. {title}\n{'=' * 70}")


def show(label, result):
    extra = f" — {result.reason}" if result.reason else ""
    print(f"    {label:<28} -> {result.outcome.value}{extra}")


def main():
    if os.path.exists(DB):
        os.remove(DB)

    store = IdempotencyStore(DB, stale_after_seconds=2.0)
    p = LoudProcessor()
    payload = {"order_id": "order_coffee", "amount_paise": 50_000, "currency": "INR"}
    action = lambda: p.charge(payload["order_id"], payload["amount_paise"])

    # ------------------------------------------------------------------
    banner(1, "Retry the same key 5 times — expect ONE charge")
    print("    The user double-taps 'Pay'. Same intent, same key, 5 times over.\n")
    for i in range(5):
        show(f"attempt {i + 1}", store.execute("key-coffee", payload, action))
    print(f"\n    Charges so far: {p.charge_count}   <- must be 1")

    # ------------------------------------------------------------------
    banner(2, "Same key, TAMPERED amount — expect rejection, no charge")
    print("    An attacker replays the key but changes Rs.500 to Rs.50,000.\n")
    tampered = {**payload, "amount_paise": 5_000_000}
    show("tampered replay", store.execute("key-coffee", tampered, action))
    print(f"\n    Charges so far: {p.charge_count}   <- still 1, original untouched")
    print(f"    Original charge was Rs.{p.charges[0]['amount_paise'] / 100:,.2f}")

    # ------------------------------------------------------------------
    banner(3, "A DIFFERENT key — expect an independent charge")
    print("    A genuinely new purchase must not be blocked by the old one.\n")
    p2 = {"order_id": "order_cake", "amount_paise": 25_000, "currency": "INR"}
    show("new key", store.execute(
        "key-cake", p2, lambda: p.charge(p2["order_id"], p2["amount_paise"])))
    print(f"\n    Charges so far: {p.charge_count}   <- now 2, as it should be")

    # ------------------------------------------------------------------
    banner(4, "32 threads firing the SAME key at once — expect ONE charge")
    print("    This is the case a naive 'check then charge' gets wrong.\n")
    racy = LoudProcessor(latency_seconds=0.05)
    p3 = {"order_id": "order_race", "amount_paise": 99_900, "currency": "INR"}
    barrier = threading.Barrier(32)
    results, lock = [], threading.Lock()

    def worker():
        barrier.wait()  # all 32 threads leave the gate together
        r = store.execute(
            "key-race", p3,
            lambda: racy.charge(p3["order_id"], p3["amount_paise"]))
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tally = {}
    for r in results:
        tally[r.outcome.value] = tally.get(r.outcome.value, 0) + 1
    print(f"\n    32 threads returned: {tally}")
    print(f"    Charges for order_race: {racy.charge_count}   <- must be 1")

    # ------------------------------------------------------------------
    banner(5, "A crashed claimant — expect the key to unjam, then charge once")
    print("    A process grabs the key and dies before charging. Without a")
    print("    staleness timeout this key would be wedged forever.\n")
    p4 = {"order_id": "order_stuck", "amount_paise": 12_300, "currency": "INR"}
    claimed = threading.Event()

    def never_returns():
        claimed.set()
        threading.Event().wait()  # simulates a process killed mid-flight

    threading.Thread(
        target=lambda: store.execute("key-stuck", p4, never_returns),
        daemon=True,
    ).start()
    claimed.wait()
    print("    (claimant has taken the key and died)\n")

    show("retry immediately", store.execute(
        "key-stuck", p4, lambda: p.charge(p4["order_id"], p4["amount_paise"])))
    print("      ^ correctly refuses: it can't yet tell 'dead' from 'still working'")

    print("\n    ...waiting 2.5s for the staleness timeout to expire...\n")
    time.sleep(2.5)

    show("retry after timeout", store.execute(
        "key-stuck", p4, lambda: p.charge(p4["order_id"], p4["amount_paise"])))
    show("retry once more", store.execute(
        "key-stuck", p4, lambda: p.charge(p4["order_id"], p4["amount_paise"])))
    print(f"\n    Charges for order_stuck: {len(p.charges_for('order_stuck'))}"
          f"   <- must be 1")

    # ------------------------------------------------------------------
    banner("", "TALLY")
    print(f"    Main processor total charges: {p.charge_count}")
    for c in p.charges:
        print(f"      {c['charge_id']}  {c['order_id']:<14} "
              f"Rs.{c['amount_paise'] / 100:,.2f}")
    print(f"    Race processor total charges: {racy.charge_count}")
    print(f"\n    Expected: 3 main (coffee, cake, stuck) + 1 race = 4 total.")
    print(f"    Actual:   {p.charge_count + racy.charge_count}")
    print(f"\n    Inspect the ledger yourself:  sqlite3 {DB} "
          f"'select key, status, attempts from idempotency_records;'")


if __name__ == "__main__":
    main()
