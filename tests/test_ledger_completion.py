"""Phase 9 completion test — the books balance under real concurrency.

Hundreds of purchases go through a real `PurchaseGateway` from many threads.
Each one is assigned, from a seeded plan, one of the ways a payment actually
goes: it works, it times out before reaching the provider, it times out AFTER
the provider charged, or the process crashes after the provider charged. A
reconciler runs against the same stores *while* the purchases are happening,
so the gateway and the reconciler genuinely race to write the books. A reader
re-adds the books over and over the whole time.

What must hold:

- on EVERY read, the books net to zero, and so do each account pair;
- once everything is resolved, revenue equals exactly what the provider
  charged -- no sale missed, none counted twice;
- nothing is left in suspense, and the books agree with the idempotency store.

The plan is random but seeded. Set LEDGER_SEED to reproduce a run; a failure
prints the seed it used.
"""

from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from zerotrust.audit import AuditLog
from zerotrust.faults import InjectedCrash
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import COMPLETED, FAILED, IdempotencyStore
from zerotrust.ledger import (
    CLEARING,
    REVENUE,
    SALE,
    SUSPENSE_CLEARING,
    SUSPENSE_REVENUE,
    Ledger,
)
from zerotrust.mandate import ANY_SKU, Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest
from zerotrust.provider import ProviderTimeout, SimulatedProvider
from zerotrust.reconcile import Reconciler, _unscope

AGENTS = ("agent_a", "agent_b", "agent_c")
PURCHASES = 300
THREADS = 16

OK = "OK"
TIMEOUT_BEFORE_CALL = "TIMEOUT_BEFORE_CALL"
TIMEOUT_AFTER_CALL = "TIMEOUT_AFTER_CALL"
CRASH_AFTER_CALL = "CRASH_AFTER_CALL"
OUTCOMES = [OK] * 50 + [TIMEOUT_BEFORE_CALL] * 15 + \
    [TIMEOUT_AFTER_CALL] * 15 + [CRASH_AFTER_CALL] * 20


def receipt_for(scoped: str) -> str:
    agent_id, key = _unscope(scoped)
    return f"rcpt_{agent_id}_{key}"


def seeds():
    fixed = [11, 2024, 90210]
    env = os.environ.get("LEDGER_SEED")
    return fixed + [int(env)] if env else fixed


@pytest.mark.parametrize("seed", seeds())
def test_the_books_balance_under_concurrent_failures_and_races(db, seed):
    rng = random.Random(seed)
    audit = AuditLog(db)
    engine = PolicyEngine(MandateStore(db))
    for agent in AGENTS:
        # Wide limits on purpose: this test is about the books, so no request
        # should be refused by the mandate, and no denial should trip cooldown.
        engine.mandates.issue(Mandate(
            agent_id=agent, max_amount_paise=10_000_000,
            allowed_skus=frozenset({ANY_SKU}), expires_at=time.time() + 3600,
            velocity_limit=10_000, velocity_window_secs=3600,
            cooldown_denials=0))
    # One Database for everything: the purchase record and its posting are
    # written in one transaction, which is only possible inside one database.
    store = IdempotencyStore(db)
    ledger = Ledger(db)
    provider = SimulatedProvider()

    requests = []
    plan = {}
    for i in range(PURCHASES):
        agent = AGENTS[i % len(AGENTS)]
        key = f"key-{i:04d}"
        requests.append(PurchaseRequest(agent, "SKU-COFFEE",
                                        rng.randint(100, 50_000), key))
        plan[(agent, key)] = rng.choice(OUTCOMES)

    def execute(request: PurchaseRequest) -> dict:
        outcome = plan[(request.agent_id, request.idempotency_key)]
        if outcome == TIMEOUT_BEFORE_CALL:
            raise ProviderTimeout("timed out before reaching the provider")
        order = provider.create_order(
            request.amount_paise,
            receipt=f"rcpt_{request.agent_id}_{request.idempotency_key}")
        if outcome == TIMEOUT_AFTER_CALL:
            raise ProviderTimeout("timed out after the provider charged")
        if outcome == CRASH_AFTER_CALL:
            raise InjectedCrash("died after the provider charged")
        return order

    gateway = PurchaseGateway(engine, store, execute, audit=audit, ledger=ledger)
    reconciler = Reconciler(provider, store, audit=audit, policy=engine,
                            ledger=ledger, not_found_grace_seconds=0)

    stop = threading.Event()
    bad_reads, reads, background_errors = [], [0], []

    def reader():
        while not stop.is_set():
            tb = ledger.trial_balance()
            reads[0] += 1
            checks = (sum(tb.values()),
                      tb[CLEARING] + tb[REVENUE],
                      tb[SUSPENSE_CLEARING] + tb[SUSPENSE_REVENUE])
            if any(checks):
                bad_reads.append((reads[0], tb))

    def background_reconciler():
        # Racing the gateway is the point: it may settle a key between the
        # store freezing it and the gateway posting suspense.
        while not stop.is_set():
            try:
                reconciler.sweep(receipt_for)
                reconciler.repair_ledger(receipt_for)
            except Exception as exc:  # noqa: BLE001
                background_errors.append(repr(exc))

    def submit(request: PurchaseRequest):
        try:
            gateway.submit(request)
        except (ProviderTimeout, InjectedCrash):
            pass

    watchers = [threading.Thread(target=reader),
                threading.Thread(target=background_reconciler)]
    for w in watchers:
        w.start()
    try:
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            list(pool.map(submit, requests))
    finally:
        stop.set()
        for w in watchers:
            w.join()

    # A crash after the charge leaves a FAILED record that no sweep visits,
    # so settle every key directly -- the documented limitation, not a bypass.
    for request in requests:
        reconciler.reconcile(
            request.idempotency_key,
            f"rcpt_{request.agent_id}_{request.idempotency_key}",
            agent_id=request.agent_id)

    msg = f"LEDGER_SEED={seed}"
    tb = ledger.trial_balance()
    charged = provider.orders
    receipts = [o["receipt"] for o in charged]

    assert background_errors == [], msg
    assert reads[0] > 0, msg
    assert bad_reads == [], f"{msg}: books unbalanced mid-run at {bad_reads[:3]}"
    assert len(receipts) == len(set(receipts)), f"{msg}: a receipt was charged twice"

    assert tb[REVENUE] == -sum(o["amount"] for o in charged), msg
    assert tb[CLEARING] == sum(o["amount"] for o in charged), msg
    assert tb[SUSPENSE_CLEARING] == 0 and tb[SUSPENSE_REVENUE] == 0, msg
    assert ledger.exposure() == 0, msg
    assert [t["kind"] for t in ledger.recent(10_000)].count(SALE) == len(charged), msg

    records = {r["key"]: r["status"] for r in store.records()}
    # Mapped through the requests, not parsed out of the receipt: agent ids
    # contain underscores, so splitting "rcpt_agent_a_key-0001" is ambiguous.
    scoped_by_receipt = {
        f"rcpt_{r.agent_id}_{r.idempotency_key}": f"{r.agent_id}:{r.idempotency_key}"
        for r in requests}
    charged_keys = {scoped_by_receipt[o["receipt"]] for o in charged}
    for scoped, status in records.items():
        expected = COMPLETED if scoped in charged_keys else FAILED
        assert status == expected, f"{msg}: {scoped} is {status}"

    assert ledger.discrepancies(store.records()) == [], msg
    report = ledger.verify()
    assert report.balanced, f"{msg}: {report.summary}"
