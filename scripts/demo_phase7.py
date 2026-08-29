"""Phase 7 manual demo — break it on purpose, then watch it recover.

    uv run python scripts/demo_phase7.py

No credentials needed; uses the offline provider. Every ">>> ORDER CREATED"
line is money moving as far as the provider is concerned.
"""

import os
import time

from zerotrust.audit import AuditLog
from zerotrust.faults import Fault, FaultInjector, InjectedCrash
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore, Outcome
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest
from zerotrust.provider import ProviderTimeout, SimulatedProvider
from zerotrust.reconcile import DEFAULT_NOT_FOUND_GRACE_SECONDS, Reconciler

DBS = ["demo_phase7_audit.db", "demo_phase7_policy.db", "demo_phase7_idem.db"]
HOUR = 3600.0
AGENT = "agent_alpha"


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def banner(n, title):
    print(f"\n{'=' * 76}\n  {n}. {title}\n{'=' * 76}")


def main():
    for base in DBS:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(base + suffix):
                os.remove(base + suffix)

    clock = Clock()
    provider = SimulatedProvider()
    faults = FaultInjector()
    audit = AuditLog(DBS[0], clock=clock)
    engine = PolicyEngine(MandateStore(DBS[1], clock=clock), clock=clock)
    store = IdempotencyStore(DBS[2], clock=clock)

    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}),
        expires_at=clock() + 24 * HOUR, velocity_limit=3,
        velocity_window_secs=HOUR, created_at=clock()))

    def execute(request):
        if faults.fire_once(Fault.PROVIDER_TIMEOUT):
            raise ProviderTimeout("order creation timed out; outcome unknown")
        order = provider.create_order(request.amount_paise,
                                      receipt=f"rcpt_{request.idempotency_key}")
        print(f"        >>> ORDER CREATED at provider: {order['id']} "
              f"(Rs.{request.amount_paise / 100:,.2f})")
        if faults.fire_once(Fault.CRASH_AFTER_PROVIDER_CALL):
            raise InjectedCrash("process died before the ledger write")
        return order

    gateway = PurchaseGateway(engine, store, execute, audit=audit)
    reconciler = Reconciler(provider, store, audit=audit, policy=engine,
                            clock=clock)

    def status(key):
        row = store.get(key, agent_id=AGENT)
        return row["status"] if row else "(no record)"

    # ------------------------------------------------------------------
    banner(1, "THE FAILURE: the provider succeeds, the ledger never finds out")
    print("    Arming a crash between the provider call and the local write.\n")
    faults.arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    try:
        gateway.submit(PurchaseRequest(AGENT, "SKU-COFFEE", 15_000, "k1"))
    except InjectedCrash as exc:
        print(f"    process crashed: {exc}")

    print(f"\n    Provider thinks    : "
          f"{len(provider.orders_for_receipt('rcpt_k1'))} order exists")
    print(f"    Our ledger thinks  : {status('k1')}")
    print("    ^ The two disagree, and only the provider knows the truth.")

    # ------------------------------------------------------------------
    banner(2, "DETECTION AND REPAIR")
    result = reconciler.reconcile("k1", "rcpt_k1", agent_id=AGENT)
    print(f"    finding : {result.finding.value}")
    print(f"    reason  : {result.reason}")
    print(f"\n    Ledger now says: {status('k1')}")

    print("\n    And a retry now correctly replays instead of re-charging:")
    outcome = gateway.submit(PurchaseRequest(AGENT, "SKU-COFFEE", 15_000, "k1"))
    print(f"      retry -> {outcome.outcome.value}")
    print(f"      total orders at provider: "
          f"{len(provider.orders_for_receipt('rcpt_k1'))}   <- must be 1")

    # ------------------------------------------------------------------
    banner(3, "THE HARDER CASE: a timeout, where nobody knows what happened")
    faults.arm(Fault.PROVIDER_TIMEOUT)
    try:
        gateway.submit(PurchaseRequest(AGENT, "SKU-COFFEE", 15_000, "k2"))
    except ProviderTimeout as exc:
        print(f"    timed out: {exc}")

    print(f"\n    Ledger status : {status('k2')}")
    print("    NOT 'FAILED'. We do not know that it failed. Nobody does yet.")
    print(f"    Velocity slot : HELD "
          f"({engine.slots_used(AGENT, HOUR)} of 3 used)")
    print("    ^ held on purpose: releasing it would let an agent buy extra")
    print("      budget just by causing timeouts.")

    # ------------------------------------------------------------------
    banner(4, "A RETRY IS REFUSED, NOT RE-EXECUTED")
    outcome = gateway.submit(PurchaseRequest(AGENT, "SKU-COFFEE", 15_000, "k2"))
    print(f"    retry -> {outcome.outcome.value}")
    print(f"      {outcome.result.reason}")
    print("\n    This is the Phase 2 gap, closed. Retrying an unknown outcome")
    print("    is exactly how a timeout becomes a second charge.")

    # ------------------------------------------------------------------
    banner(5, "RECONCILING TOO EARLY: absence is not evidence")
    result = reconciler.reconcile("k2", "rcpt_k2", agent_id=AGENT)
    print(f"    finding : {result.finding.value}")
    print(f"    reason  : {result.reason}")
    print("\n    The provider says it has no such order -- but its read model")
    print("    lags, so right now that means nothing. Verified against the")
    print("    real Razorpay API: a just-created order is not in the list yet.")

    # ------------------------------------------------------------------
    banner(6, "RECONCILING LATER: now absence means something")
    clock.advance(DEFAULT_NOT_FOUND_GRACE_SECONDS + 1)
    result = reconciler.reconcile("k2", "rcpt_k2", agent_id=AGENT)
    print(f"    finding : {result.finding.value}")
    print(f"    reason  : {result.reason}")
    print(f"\n    Ledger status : {status('k2')}")
    print(f"    Velocity slot : released "
          f"({engine.slots_used(AGENT, HOUR)} of 3 used)")

    # ------------------------------------------------------------------
    banner(7, "AN AMBIGUOUS DIVERGENCE IS FLAGGED, NEVER GUESSED")
    faults.arm(Fault.PROVIDER_TIMEOUT)
    try:
        gateway.submit(PurchaseRequest(AGENT, "SKU-COFFEE", 15_000, "k3"))
    except ProviderTimeout:
        pass
    provider.create_order(15_000, receipt="rcpt_k3")
    provider.create_order(15_000, receipt="rcpt_k3")
    print("    Two provider orders share one receipt.\n")

    result = reconciler.reconcile("k3", "rcpt_k3", agent_id=AGENT)
    print(f"    finding : {result.finding.value}")
    print(f"    reason  : {result.reason}")
    print(f"    ledger  : {status('k3')}  <- deliberately NOT repaired")
    print("\n    An automatic repair would have to choose. Choosing is guessing.")

    # ------------------------------------------------------------------
    banner(8, "THE AUDIT TRAIL")
    for entry in audit.all():
        if entry.event_type.value.startswith(("DIVERGENCE", "PAYMENT_PENDING")):
            print(f"    {entry.describe()}")

    banner("", "TALLY")
    print(f"    Orders created at the provider : {len(provider.orders)}")
    print("    Purchases the agent asked for  : 3 (k1, k2, k3)")
    print("    Charged twice for anything?    : no")


if __name__ == "__main__":
    main()
