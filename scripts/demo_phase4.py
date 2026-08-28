"""Phase 4 manual demo — read the audit log and explain what happened.

    uv run python scripts/demo_phase4.py

Runs a handful of purchases, then prints the log and tries to tamper with it.
The point: everything below is reconstructed from the log alone.
"""

import os
import sqlite3
import time

from zerotrust.audit import AuditLog, EventType
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest

AUDIT_DB = "demo_phase4_audit.db"
POLICY_DB = "demo_phase4_policy.db"
IDEM_DB = "demo_phase4_idem.db"
HOUR = 3600.0


def banner(title):
    print(f"\n{'=' * 76}\n  {title}\n{'=' * 76}")


def main():
    for base in (AUDIT_DB, POLICY_DB, IDEM_DB):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(base + suffix):
                os.remove(base + suffix)

    audit = AuditLog(AUDIT_DB)
    mandates = MandateStore(POLICY_DB)
    policy = PolicyEngine(mandates)
    store = IdempotencyStore(IDEM_DB)
    executed = []

    mandates.issue(Mandate(
        agent_id="agent_alpha", max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}),
        expires_at=time.time() + 24 * HOUR,
        velocity_limit=3, velocity_window_secs=HOUR))

    gw = PurchaseGateway(
        policy, store,
        lambda r: (executed.append(r), {"order_id": f"order_{len(executed)}"})[1],
        audit=audit)

    scenarios = [
        ("a normal coffee",        dict(sku="SKU-COFFEE", amount_paise=10_000, idempotency_key="k1")),
        ("the same one, retried",  dict(sku="SKU-COFFEE", amount_paise=10_000, idempotency_key="k1")),
        ("too expensive",          dict(sku="SKU-COFFEE", amount_paise=90_000, idempotency_key="k2")),
        ("an item not allowed",    dict(sku="SKU-YACHT",  amount_paise=10_000, idempotency_key="k3")),
        ("a tampered retry",       dict(sku="SKU-COFFEE", amount_paise=25_000, idempotency_key="k1")),
    ]

    ids = []
    banner("RUNNING FIVE REQUESTS")
    for label, kw in scenarios:
        outcome = gw.submit(PurchaseRequest(agent_id="agent_alpha", **kw))
        ids.append((label, outcome.request_id))
        verdict = "APPROVED" if outcome.approved else f"DENIED [{outcome.rule.value}]"
        print(f"    {label:<24} -> {verdict}")

    banner("THE LOG, AS A REVIEWER WOULD READ IT")
    for label, rid in ids:
        print(f"\n  -- {label} --")
        print(audit.timeline(rid))

    banner("EXPLAINING A DENIAL FROM THE LOG ALONE")
    _, rid = ids[2]
    denial = [e for e in audit.for_request(rid)
              if e.event_type is EventType.POLICY_DENIED][0]
    print(f"    request      : {rid}")
    print(f"    verdict      : DENIED")
    print(f"    rule broken  : {denial.rule}")
    print(f"    why          : {denial.reason}")
    print(f"    evidence     : {denial.details}")
    print(f"    decided by   : {denial.actor.value}")
    print("\n    Note: none of that required reading the source code.")

    banner("TRYING TO TAMPER WITH HISTORY")
    conn = sqlite3.connect(AUDIT_DB)
    for sql in (
        "UPDATE audit_log SET reason = 'looked fine to me'",
        "UPDATE audit_log SET rule = NULL WHERE rule IS NOT NULL",
        "DELETE FROM audit_log WHERE event_type = 'POLICY_DENIED'",
        "DELETE FROM audit_log",
    ):
        try:
            conn.execute(sql)
            print(f"    !!! SUCCEEDED (should not happen): {sql}")
        except sqlite3.IntegrityError as exc:
            print(f"    blocked: {sql[:46]:<48} -> {exc}")
    conn.close()

    print(f"\n    Entries still present: {len(audit.all())}")
    print("    The database refuses, not the application. Even raw SQL cannot")
    print("    rewrite what happened.")

    banner("TALLY")
    print(f"    Money actions executed : {len(executed)}")
    print(f"    Audit entries written  : {len(audit.all())}")
    print(f"    POLICY_DENIED entries  : {audit.count_of(EventType.POLICY_DENIED)}")
    print(f"    PAYMENT_CAPTURED       : {audit.count_of(EventType.PAYMENT_CAPTURED)}")
    print("\n    5 submissions, 1 charge, and a complete record of all five.")


if __name__ == "__main__":
    main()
