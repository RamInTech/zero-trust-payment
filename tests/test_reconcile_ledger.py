"""Phase 9 — what reconciliation does to the books, one finding at a time.

`test_reconcile.py` proves each finding is reached correctly. This file proves
each finding leaves the ledger correct: a sale booked once whichever path
recognises it, suspense reversed rather than edited, and nothing booked for a
case that needs a human.

Phase 10 adds the other half: a status change and the posting that mirrors it
commit in ONE transaction. A purchase can no longer be COMPLETED in the store
with no sale in the books because the second write failed -- both roll back.
"""

from __future__ import annotations

import pytest

from zerotrust.audit import AuditLog, EventType
from zerotrust.faults import Fault, FaultInjector, InjectedCrash
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import (
    COMPLETED,
    PENDING_VERIFICATION,
    PROCESSING,
    IdempotencyStore,
)
from zerotrust.ledger import (
    CLEARING,
    REVENUE,
    SALE,
    Ledger,
    LedgerError,
)
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest
from zerotrust.provider import ProviderTimeout, SimulatedProvider
from zerotrust.reconcile import (
    DEFAULT_NOT_FOUND_GRACE_SECONDS,
    Finding,
    ReconciliationScheduler,
    Reconciler,
)

HOUR = 3600.0
AGENT = "agent_1"
RECEIPT = "rcpt_phase9"
AMOUNT = 15_000


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FlakyLedger(Ledger):
    """A ledger whose next sale or suspense posting fails, once."""

    fail_next_sale = False
    fail_next_suspense = False

    def record_sale(self, *args, **kwargs):
        if self.fail_next_sale:
            self.fail_next_sale = False
            raise LedgerError("simulated: the sale posting failed")
        return super().record_sale(*args, **kwargs)

    def record_unverified(self, *args, **kwargs):
        if self.fail_next_suspense:
            self.fail_next_suspense = False
            raise LedgerError("simulated: the suspense posting failed")
        return super().record_unverified(*args, **kwargs)


@pytest.fixture
def books(db):
    clock = FakeClock()
    provider = SimulatedProvider()
    faults = FaultInjector()
    timeout_after_call = {"armed": False}
    audit = AuditLog(db, clock=clock)
    engine = PolicyEngine(MandateStore(db, clock=clock), clock=clock)
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}),
        expires_at=clock() + 24 * HOUR, velocity_limit=5,
        velocity_window_secs=HOUR, created_at=clock()))
    store = IdempotencyStore(db, clock=clock)
    ledger = FlakyLedger(db, clock=clock)

    def execute(request: PurchaseRequest) -> dict:
        if faults.fire_once(Fault.PROVIDER_TIMEOUT):
            raise ProviderTimeout("timed out before reaching the provider")
        order = provider.create_order(request.amount_paise, receipt=RECEIPT)
        if timeout_after_call["armed"]:
            timeout_after_call["armed"] = False
            raise ProviderTimeout("timed out after the provider charged")
        if faults.fire_once(Fault.CRASH_AFTER_PROVIDER_CALL):
            raise InjectedCrash("died after the provider charged")
        return order

    gateway = PurchaseGateway(engine, store, execute, audit=audit, ledger=ledger)
    reconciler = Reconciler(provider, store, audit=audit, policy=engine,
                            clock=clock, ledger=ledger)
    return {"gateway": gateway, "reconciler": reconciler, "ledger": ledger,
            "store": store, "provider": provider, "faults": faults,
            "clock": clock, "timeout_after_call": timeout_after_call,
            "audit": audit, "engine": engine, "execute": execute, "db": db}


def req(key="key-1"):
    return PurchaseRequest(AGENT, "SKU-COFFEE", AMOUNT, key)


def reconcile(books, key="key-1"):
    return books["reconciler"].reconcile(key, RECEIPT, agent_id=AGENT)


def status(books, key="key-1"):
    return books["store"].get(key, agent_id=AGENT)["status"]


def problems(books):
    return [d["problem"] for d in books["ledger"].discrepancies(
        books["store"].records())]


def sales(books):
    return [t for t in books["ledger"].recent(100) if t["kind"] == SALE]


def assert_sold_once(books):
    tb = books["ledger"].trial_balance()
    assert tb[REVENUE] == -AMOUNT and tb[CLEARING] == AMOUNT
    assert books["ledger"].exposure() == 0
    assert len(sales(books)) == 1
    assert books["ledger"].verify().balanced


def complete_without_books(books, key="key-1"):
    """A purchase COMPLETED by a gateway that had no ledger configured --
    the one way left to produce a completed record with no sale."""
    plain = PurchaseGateway(books["engine"], books["store"], books["execute"],
                            audit=books["audit"])
    plain.submit(req(key))
    assert status(books, key) == COMPLETED


def test_a_successful_purchase_books_one_sale(books):
    books["gateway"].submit(req())
    assert_sold_once(books)


def test_a_timeout_books_suspense_and_no_revenue(books):
    books["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        books["gateway"].submit(req())
    assert books["ledger"].exposure() == AMOUNT
    assert books["ledger"].trial_balance()[REVENUE] == 0


def test_a_timeout_that_never_reached_the_provider_is_reversed(books):
    books["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        books["gateway"].submit(req())
    books["clock"].advance(DEFAULT_NOT_FOUND_GRACE_SECONDS + 1)

    assert reconcile(books).finding is Finding.CONFIRMED_NOT_EXECUTED
    assert all(v == 0 for v in books["ledger"].trial_balance().values())
    assert books["ledger"].verify().balanced


def test_a_timeout_after_the_charge_becomes_a_sale(books):
    books["timeout_after_call"]["armed"] = True
    with pytest.raises(ProviderTimeout):
        books["gateway"].submit(req())
    assert books["ledger"].exposure() == AMOUNT

    assert reconcile(books).finding is Finding.DIVERGED_REPAIRED
    assert_sold_once(books)


def test_a_crash_after_the_charge_is_booked_when_repaired(books):
    books["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    with pytest.raises(InjectedCrash):
        books["gateway"].submit(req())
    # The money moved and nothing is booked: the limitation stated in the
    # module docstring, visible here rather than hidden.
    assert books["ledger"].trial_balance()[REVENUE] == 0

    assert reconcile(books).finding is Finding.DIVERGED_REPAIRED
    assert_sold_once(books)


def test_an_agreeing_record_does_not_book_a_second_sale(books):
    books["gateway"].submit(req())
    assert reconcile(books).finding is Finding.CONSISTENT
    assert reconcile(books).finding is Finding.CONSISTENT
    assert_sold_once(books)


def test_a_case_needing_a_human_books_nothing(books):
    books["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        books["gateway"].submit(req())
    # Two orders now share the receipt: no repair can choose between them.
    books["provider"].create_order(AMOUNT, receipt=RECEIPT)
    books["provider"].create_order(AMOUNT, receipt=RECEIPT)

    assert reconcile(books).finding is Finding.NEEDS_HUMAN_REVIEW
    assert books["ledger"].exposure() == AMOUNT
    assert books["ledger"].trial_balance()[REVENUE] == 0


# -- Phase 10: one transaction ----------------------------------------------

def test_a_failed_sale_posting_leaves_no_completed_record_without_a_sale(books):
    """In Phase 9 this left COMPLETED-with-no-sale, visible only to the repair
    pass. Now the record and the sale roll back together; the key is frozen
    because the charge happened, and suspense says the money may have moved."""
    books["ledger"].fail_next_sale = True
    with pytest.raises(LedgerError):
        books["gateway"].submit(req())

    assert status(books) == PENDING_VERIFICATION
    assert books["ledger"].trial_balance()[REVENUE] == 0
    assert books["ledger"].exposure() == AMOUNT
    assert problems(books) == []
    assert len(books["provider"].orders) == 1

    # A retry is refused rather than charging again.
    retry = books["gateway"].submit(req())
    assert retry.outcome.name == "AWAITING_VERIFICATION"
    assert len(books["provider"].orders) == 1

    cycle = ReconciliationScheduler(books["reconciler"],
                                    receipt_for=lambda scoped: RECEIPT).run_once()
    assert cycle.error is None
    assert cycle.findings == {Finding.DIVERGED_REPAIRED.value: 1}
    assert status(books) == COMPLETED
    assert_sold_once(books)
    assert problems(books) == []


def test_a_failed_suspense_posting_still_freezes_the_record(books):
    """The freeze is what stops a double charge, so it must not depend on the
    posting that normally commits with it."""
    books["ledger"].fail_next_suspense = True
    books["timeout_after_call"]["armed"] = True
    with pytest.raises(ProviderTimeout):
        books["gateway"].submit(req())

    assert status(books) == PENDING_VERIFICATION
    assert status(books) != PROCESSING
    assert books["ledger"].exposure() == 0
    assert problems(books) == ["pending_without_suspense"]
    pending = books["audit"].of_type(EventType.PAYMENT_PENDING_VERIFICATION)
    assert "failed" in pending[-1].details["suspense"]

    assert reconcile(books).finding is Finding.DIVERGED_REPAIRED
    assert_sold_once(books)
    assert problems(books) == []


def test_a_failed_repair_posting_leaves_the_record_unrepaired(books):
    """The reconciler's status change and its sale also commit together: if
    the sale fails, the record stays pending, and the next sweep retries."""
    books["timeout_after_call"]["armed"] = True
    with pytest.raises(ProviderTimeout):
        books["gateway"].submit(req())

    books["ledger"].fail_next_sale = True
    with pytest.raises(LedgerError):
        reconcile(books)
    assert status(books) == PENDING_VERIFICATION
    assert books["ledger"].trial_balance()[REVENUE] == 0

    assert reconcile(books).finding is Finding.DIVERGED_REPAIRED
    assert_sold_once(books)


def test_a_ledger_in_a_different_database_is_refused(books, db_factory):
    """Handing one schema's connection to a ledger in another would not fail;
    it would write the posting into the wrong schema. So it is refused up front."""
    elsewhere = Ledger(db_factory())
    with pytest.raises(ValueError, match="share one Database"):
        PurchaseGateway(books["engine"], books["store"], books["execute"],
                        ledger=elsewhere)
    with pytest.raises(ValueError, match="share one Database"):
        Reconciler(books["provider"], books["store"], ledger=elsewhere)


# -- the repair pass --------------------------------------------------------

def test_a_completed_purchase_with_no_sale_is_repaired_by_the_sweep(books):
    complete_without_books(books)
    assert problems(books) == ["missing_sale"]

    scheduler = ReconciliationScheduler(books["reconciler"],
                                        receipt_for=lambda scoped: RECEIPT)
    cycle = scheduler.run_once()

    assert cycle.error is None
    assert cycle.findings == {Finding.CONSISTENT.value: 1}
    assert_sold_once(books)
    assert problems(books) == []


def test_a_key_sent_for_human_review_is_not_retried_by_the_repair_pass(books):
    """Each retry that still cannot decide writes another permanent audit
    entry. Once flagged, the repair pass leaves the key alone; the gap stays
    visible in the books."""
    complete_without_books(books)
    books["provider"].create_order(AMOUNT, receipt=RECEIPT)  # two orders now

    first = books["reconciler"].repair_ledger(lambda scoped: RECEIPT)
    second = books["reconciler"].repair_ledger(lambda scoped: RECEIPT)

    assert [r.finding for r in first] == [Finding.NEEDS_HUMAN_REVIEW]
    assert second == []
    assert books["audit"].count_of(EventType.DIVERGENCE_DETECTED) == 1
    assert problems(books) == ["missing_sale"]


def test_freezing_a_record_reports_which_attempt_it_froze(books):
    """Suspense is keyed on this number, so it is read in the same statement
    that froze the record rather than looked up afterwards."""
    books["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        books["gateway"].submit(req())
    books["clock"].advance(DEFAULT_NOT_FOUND_GRACE_SECONDS + 1)
    reconcile(books)  # settled as not executed -> FAILED, retry allowed

    books["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        books["gateway"].submit(req())

    attempt = books["store"].mark_pending_verification(
        "key-1", "still unknown", agent_id=AGENT)
    assert attempt == 2
    unverified = [t for t in books["ledger"].recent(100) if t["kind"] == "UNVERIFIED"]
    assert sorted(t["attempt"] for t in unverified) == [1, 2]


def test_repair_does_nothing_when_the_books_already_agree(books):
    books["gateway"].submit(req())
    assert books["reconciler"].repair_ledger(lambda scoped: RECEIPT) == []
