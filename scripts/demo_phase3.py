"""Phase 3 manual demo — watch the policy engine refuse things.

    uv run python scripts/demo_phase3.py

No credentials needed; the executor is a local stub. Every ">>> EXECUTED" line
is the money action actually running. Count them: a denial must produce none.
"""

import os
import threading
import time

from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest

DB = "demo_phase3.db"
HOUR = 3600.0


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def banner(n, title):
    print(f"\n{'=' * 74}\n  {n}. {title}\n{'=' * 74}")


def main():
    for f in (DB, f"{DB}-wal", f"{DB}-shm", "demo_phase3_idem.db"):
        if os.path.exists(f):
            os.remove(f)

    clock = Clock()
    mandates = MandateStore(DB, clock=clock)
    policy = PolicyEngine(mandates, clock=clock)
    store = IdempotencyStore("demo_phase3_idem.db")

    executed = []

    def execute(request):
        executed.append(request)
        print(f"        >>> EXECUTED: {request.sku} for "
              f"Rs.{request.amount_paise / 100:,.2f}")
        return {"order_id": f"order_{len(executed):04d}"}

    gateway = PurchaseGateway(policy, store, execute)

    mandate = mandates.issue(
        Mandate(
            agent_id="agent_alpha",
            max_amount_paise=50_000,
            allowed_skus=frozenset({"SKU-COFFEE", "SKU-CAKE"}),
            expires_at=clock() + 24 * HOUR,
            velocity_limit=3,
            velocity_window_secs=HOUR,
            created_at=clock(),
        )
    )

    print("THE MANDATE (agreed in advance, before the agent may spend anything)")
    print(f"    agent        : {mandate.agent_id}")
    print(f"    max per txn  : Rs.{mandate.max_amount_paise / 100:,.2f}")
    print(f"    allowed items: {sorted(mandate.allowed_skus)}")
    print(f"    velocity     : {mandate.velocity_limit} purchases per hour")
    print(f"    expires      : in 24 hours")

    def submit(label, **kw):
        outcome = gateway.submit(PurchaseRequest(agent_id="agent_alpha", **kw))
        if outcome.approved:
            print(f"    {label:<34} -> APPROVED ({outcome.outcome.value})")
        else:
            print(f"    {label:<34} -> DENIED [{outcome.rule.value}]")
            print(f"       reason: {outcome.reason}")
        return outcome

    # ------------------------------------------------------------------
    banner(1, "A compliant purchase")
    submit("coffee, Rs.100", sku="SKU-COFFEE", amount_paise=10_000,
           idempotency_key="k1")

    # ------------------------------------------------------------------
    banner(2, "Over the per-transaction cap")
    before = len(executed)
    submit("coffee, Rs.900 (cap is Rs.500)", sku="SKU-COFFEE",
           amount_paise=90_000, idempotency_key="k2")
    print(f"\n    Executions caused: {len(executed) - before}   <- must be 0")

    # ------------------------------------------------------------------
    banner(3, "An item the mandate never allowed")
    before = len(executed)
    submit("a yacht", sku="SKU-YACHT", amount_paise=10_000, idempotency_key="k3")
    print(f"\n    Executions caused: {len(executed) - before}   <- must be 0")

    # ------------------------------------------------------------------
    banner(4, "Velocity limit: 3 per hour")
    print("    Two more purchases fit. The fourth does not.\n")
    submit("cake #2", sku="SKU-CAKE", amount_paise=5_000, idempotency_key="k4")
    submit("cake #3", sku="SKU-CAKE", amount_paise=5_000, idempotency_key="k5")
    before = len(executed)
    submit("cake #4 (over the limit)", sku="SKU-CAKE", amount_paise=5_000,
           idempotency_key="k6")
    print(f"\n    Executions caused by the 4th: {len(executed) - before}   <- 0")

    # ------------------------------------------------------------------
    banner(5, "Retries don't eat the velocity budget")
    print("    Re-submitting an ALREADY APPROVED request 4 more times.\n")
    before = len(executed)
    for i in range(4):
        o = gateway.submit(PurchaseRequest("agent_alpha", "SKU-COFFEE",
                                           10_000, "k1"))
        print(f"    retry {i + 1} -> {o.outcome.value}")
    print(f"\n    Executions caused: {len(executed) - before}   <- must be 0")
    print(f"    Velocity slots used: {policy.slots_used('agent_alpha', HOUR)}"
          f"   <- still 3, not 7")

    # ------------------------------------------------------------------
    banner(6, "A concurrent burst cannot overshoot the cap")
    print("    20 threads, all firing at once, against a fresh agent (cap 3).\n")
    mandates.issue(
        Mandate(agent_id="agent_burst", max_amount_paise=50_000,
                allowed_skus=frozenset({"SKU-COFFEE"}),
                expires_at=clock() + 24 * HOUR, velocity_limit=3,
                velocity_window_secs=HOUR, created_at=clock())
    )
    barrier = threading.Barrier(20)
    results, lock = [], threading.Lock()

    def worker(i):
        barrier.wait()
        d = policy.evaluate(
            PurchaseRequest("agent_burst", "SKU-COFFEE", 10_000, f"burst-{i}"))
        with lock:
            results.append(d)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    approved = sum(1 for d in results if d.approved)
    print(f"    20 simultaneous requests -> {approved} approved, "
          f"{20 - approved} denied")
    print(f"    Approved count: {approved}   <- must be exactly 3")

    # ------------------------------------------------------------------
    banner(7, "Mandate expiry")
    clock.advance(25 * HOUR)
    before = len(executed)
    submit("coffee, a day later", sku="SKU-COFFEE", amount_paise=10_000,
           idempotency_key="k7")
    print(f"\n    Executions caused: {len(executed) - before}   <- must be 0")

    banner("", "TALLY")
    print(f"    Total money actions executed: {len(executed)}")
    print("    From 8 submissions by agent_alpha + 20 by agent_burst.")
    print("    Every denial named the exact rule it broke -- never just 'denied'.")


if __name__ == "__main__":
    main()
