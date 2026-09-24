"""Phase 11 completion test — the guarantees under injected failure.

Three claims:

  1. Rounds of real purchases survive latency, timeouts, crashes, terminated
     database backends and SIGKILLed processes, with the money still adding up
     afterwards.
  2. The harness can actually fail. A stack that charges money and books no
     sale is caught, so a green run means something.
  3. A claimant SIGKILLed after charging is recovered by the next retry, not
     charged again (Phase 12) -- and without that check, the harness fails.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from zerotrust.audit import AuditLog
from zerotrust.chaos import (
    BACKEND_KILL,
    PROCESS_KILL_AFTER,
    AGENT,
    ChaosHarness,
    ChaosProvider,
    receipt_for_key,
)
from zerotrust.idempotency import COMPLETED, PROCESSING, IdempotencyStore
from zerotrust.ledger import REVENUE, SALE, Ledger
from zerotrust.mandate import ANY_SKU, Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest
from zerotrust.reconcile import Finding, Reconciler

HOUR = 3600.0


def _mandate(db):
    engine = PolicyEngine(MandateStore(db))
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=10_000_000,
        allowed_skus=frozenset({ANY_SKU}), expires_at=time.time() + HOUR,
        velocity_limit=10_000, velocity_window_secs=HOUR, cooldown_denials=0))
    return engine


def _run_worker(db, key: str, amount: int, mode: str = PROCESS_KILL_AFTER):
    return subprocess.run(
        [sys.executable, "-m", "zerotrust.chaos_worker", db.dsn, db.schema,
         AGENT, key, str(amount), receipt_for_key(key), mode],
        capture_output=True, timeout=120)


# -- 1. the guarantees hold ------------------------------------------------

@pytest.mark.parametrize("seed", [11, 2026, 90210])
def test_the_money_still_adds_up_under_injected_failure(test_dsn, seed):
    report = ChaosHarness(test_dsn, rounds=2, purchases=12, process_kills=2,
                          seed=seed).run()

    assert report.survived_all, (
        f"CHAOS_SEED={seed}: " +
        "; ".join(v for r in report.failures for v in r.violations))
    totals = report.totals
    # A run that injected nothing would pass vacuously.
    assert totals["purchases"] == 24
    assert totals["charged_orders"] > 0
    assert totals["process_kills"] == 4, "the subprocesses were not really killed"
    assert totals["mid_run_reads"] > 0
    assert totals["revenue_paise"] == totals["charged_paise"]
    # Every killed claimant was retried and settled; none left hanging.
    assert totals["stalled_retried"] >= 4
    assert totals["recovered"] > 0, "no killed-after-charge key was recovered"
    assert totals["in_flight_records"] == 0


def test_a_killed_process_leaves_a_charge_that_reconciliation_books(db):
    """The case no in-process fault can produce: the claimant stops existing.

    SIGKILL runs no `except` and no `finally`, so unlike Phase 7's injected
    crash there is no FAILED record -- only a claimed key, a real charge, and
    nothing that knows about it.
    """
    engine = _mandate(db)
    audit, ledger = AuditLog(db), Ledger(db)
    store = IdempotencyStore(db)
    provider = ChaosProvider(db)

    killed = _run_worker(db, "k-killed", 4_200)

    assert killed.returncode < 0, "the worker exited instead of being killed"
    assert len(provider.orders_for_receipt(receipt_for_key("k-killed"))) == 1
    assert store.get("k-killed", agent_id=AGENT)["status"] == PROCESSING
    assert ledger.trial_balance()[REVENUE] == 0, "nothing should be booked yet"

    reconciler = Reconciler(provider, store, audit=audit, policy=engine,
                            ledger=ledger, not_found_grace_seconds=0)
    result = reconciler.reconcile("k-killed", receipt_for_key("k-killed"),
                                  agent_id=AGENT)

    assert result.finding is Finding.DIVERGED_REPAIRED
    assert store.get("k-killed", agent_id=AGENT)["status"] == COMPLETED
    assert ledger.trial_balance()[REVENUE] == -4_200
    assert ledger.verify().balanced
    assert ledger.discrepancies(store.records()) == []


def test_terminating_a_backend_mid_flight_never_books_money_twice(db, test_dsn):
    """A killed connection can lose a write. It must not duplicate one."""
    harness = ChaosHarness(test_dsn, rounds=1, purchases=10, process_kills=0,
                           seed=4242)
    report = harness.run()
    round_result = report.rounds[0]

    assert round_result.survived, round_result.violations
    assert round_result.faults.get(BACKEND_KILL, 0) > 0, "no backend was killed"
    assert round_result.backend_kills > 0


# -- 2. the harness can fail -----------------------------------------------

def test_the_harness_catches_a_double_charge(test_dsn):
    """Proof this suite has teeth.

    One receipt charged twice is the error the whole project exists to
    prevent, and it is the one nothing downstream can undo: reconciliation
    finds two orders on one receipt and refuses to choose between them. If the
    harness reported that as a survival, every green run above would be
    meaningless.
    """
    report = ChaosHarness(test_dsn, rounds=1, purchases=8, process_kills=0,
                          seed=7, inject_double_charge=True).run()

    assert not report.survived_all
    assert report.survival_rate == 0.0
    violations = " ".join(report.failures[0].violations)
    assert "charged more than once" in violations
    # The seed is printed so the failing round can be replayed exactly.
    assert report.failures[0].seed
    assert "FAILED" in report.to_markdown()


def test_a_gateway_that_books_nothing_is_repaired_before_the_round_ends(test_dsn):
    """The opposite case, and worth stating: money charged with no sale booked
    is a real gap, but a recoverable one. Reconciliation asks the provider and
    books what it finds, which is why this round still survives -- the books
    are wrong in the middle and right at the end."""
    report = ChaosHarness(test_dsn, rounds=1, purchases=8, process_kills=0,
                          seed=7, book_sales=False).run()

    assert report.survived_all
    round_result = report.rounds[0]
    assert round_result.repairs > 0
    assert round_result.revenue_paise == round_result.charged_paise


# -- 3. the gap Phase 12 closed ---------------------------------------------

def test_a_claimant_killed_after_charging_is_recovered_not_charged_again(db):
    """The case Phase 11 could only document, now closed.

    A real process charges and is SIGKILLed before recording it. Once its key
    goes stale, a retry reclaims it -- and before Phase 12, ran the purchase
    again. The retry now asks the provider, finds the dead claimant's order,
    and completes the key from it.
    """
    engine = _mandate(db)
    audit, ledger = AuditLog(db), Ledger(db)
    provider = ChaosProvider(db)
    store = IdempotencyStore(db, stale_after_seconds=0.05)

    assert _run_worker(db, "k-stale", 7_000).returncode < 0
    time.sleep(0.1)

    from zerotrust.gateway import PurchaseGateway
    retry = PurchaseGateway(
        engine, store,
        lambda r: provider.create_order(r.amount_paise,
                                        receipt=receipt_for_key("k-stale")),
        audit=audit, ledger=ledger,
        find_orders=lambda r: provider.orders_for_receipt(receipt_for_key("k-stale")),
        not_found_grace_seconds=0)
    outcome = retry.submit(PurchaseRequest(AGENT, "SKU-CHAOS", 7_000, "k-stale"))

    assert outcome.outcome.value == "RECOVERED"
    assert len(provider.orders_for_receipt(receipt_for_key("k-stale"))) == 1
    assert store.get("k-stale", agent_id=AGENT)["status"] == COMPLETED
    assert ledger.trial_balance()[REVENUE] == -7_000
    assert len([t for t in ledger.recent(50) if t["kind"] == SALE]) == 1
    assert ledger.discrepancies(store.records()) == []


def test_without_verification_the_harness_catches_the_second_charge(test_dsn):
    """The same rounds, with the Phase 12 check turned off, must fail --
    naming the receipt the retry charged twice. This is the proof that the
    green runs above depend on the fix, not on the faults happening to miss."""
    report = ChaosHarness(test_dsn, rounds=2, purchases=12, process_kills=2,
                          seed=31, verify_before_reclaim=False).run()

    assert not report.survived_all
    violations = " ".join(v for r in report.failures for v in r.violations)
    assert "charged more than once" in violations
    assert report.totals["recovered"] == 0


# -- the report ------------------------------------------------------------

def test_the_report_states_what_was_injected(test_dsn):
    report = ChaosHarness(test_dsn, rounds=1, purchases=8, process_kills=1,
                          seed=5).run()
    markdown = report.to_markdown()

    assert "GENERATED FILE" in markdown
    assert "do not edit by hand" in markdown
    assert "Processes SIGKILLed" in markdown
    assert str(report.rounds[0].seed) in markdown

    import json
    data = json.loads(report.to_json())
    assert data["totals"]["rounds"] == 1
    assert data["survival_rate"] == 1.0
