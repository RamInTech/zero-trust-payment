"""Phase 12 completion test — a stale key is verified before it is re-run.

A claim goes stale when its claimant stops answering. That is not the same as
failing: the claimant may have charged the customer and then died before it
could record the fact. Until this phase, the staleness timeout re-ran the
purchase in that case, and the customer paid twice (tests/test_chaos.py pinned
it). Now the retry asks the provider first, and each answer maps to exactly
one safe action:

  an order exists          -> complete the key from it, charge nothing (RECOVERED)
  no order, long enough    -> run the purchase (RECLAIMED)
  anything ambiguous       -> freeze for reconciliation (AWAITING_VERIFICATION)

What stays true: a stale key still never blocks forever. It is completed,
re-run, or frozen for reconciliation -- which resolves it.
"""

from __future__ import annotations

import threading

import pytest

from zerotrust.audit import AuditLog, EventType
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import (
    COMPLETED,
    FAILED,
    PENDING_VERIFICATION,
    IdempotencyStore,
    Outcome,
    StaleVerdict,
)
from zerotrust.ledger import REVENUE, SALE, Ledger
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest
from zerotrust.provider import ProviderTimeout, SimulatedProvider
from zerotrust.reconcile import Finding, Reconciler

HOUR = 3600.0
AGENT = "agent_1"
KEY = "key-1"
AMOUNT = 15_000
STALE = 30.0
GRACE = 300.0


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def receipt(request: PurchaseRequest) -> str:
    return f"rcpt_{request.idempotency_key}"


@pytest.fixture
def stack(db):
    clock = FakeClock()
    audit = AuditLog(db, clock=clock)
    engine = PolicyEngine(MandateStore(db, clock=clock), clock=clock)
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}), expires_at=clock() + 24 * HOUR,
        velocity_limit=100, velocity_window_secs=HOUR, created_at=clock()))
    store = IdempotencyStore(db, stale_after_seconds=STALE, clock=clock)
    ledger = Ledger(db, clock=clock)
    provider = SimulatedProvider()
    lookups = {"fail": False}

    def find_orders(request):
        if lookups["fail"]:
            raise ProviderTimeout("the provider did not answer")
        return provider.orders_for_receipt(receipt(request))

    def gateway(execute, verify=True):
        return PurchaseGateway(
            engine, store, execute, audit=audit, ledger=ledger,
            find_orders=find_orders if verify else None,
            not_found_grace_seconds=GRACE, clock=clock)

    def charge(request):
        return provider.create_order(request.amount_paise, receipt=receipt(request))

    return {"clock": clock, "audit": audit, "engine": engine, "store": store,
            "ledger": ledger, "provider": provider, "gateway": gateway,
            "charge": charge, "lookups": lookups}


def req(amount=AMOUNT):
    return PurchaseRequest(AGENT, "SKU-COFFEE", amount, KEY)


def stall(stack, *, charge_first: bool):
    """A claimant that takes the key and then never answers again.

    `charge_first` decides whether it reached the provider before it stalled
    -- the one fact the retry cannot see locally and has to ask about.
    """
    claimed = threading.Event()

    def hang(request):
        if charge_first:
            stack["charge"](request)
        claimed.set()
        threading.Event().wait()   # never returns: the process is gone

    ghost = stack["gateway"](hang)
    threading.Thread(target=lambda: ghost.submit(req()), daemon=True).start()
    assert claimed.wait(timeout=10)


def orders(stack):
    return stack["provider"].orders_for_receipt(f"rcpt_{KEY}")


def status(stack):
    return stack["store"].get(KEY, agent_id=AGENT)["status"]


def sales(stack):
    return [t for t in stack["ledger"].recent(100) if t["kind"] == SALE]


# -- the case this phase exists for ---------------------------------------

def test_a_stalled_attempt_that_charged_is_recovered_not_charged_again(stack):
    stall(stack, charge_first=True)
    stack["clock"].advance(STALE + 1)

    retry = stack["gateway"](stack["charge"])
    outcome = retry.submit(req())

    assert outcome.outcome is Outcome.RECOVERED
    assert len(orders(stack)) == 1, "the customer was charged a second time"
    assert outcome.response["id"] == orders(stack)[0]["id"]
    assert status(stack) == COMPLETED
    # The stalled attempt's money is booked, once.
    assert stack["ledger"].trial_balance()[REVENUE] == -AMOUNT
    assert len(sales(stack)) == 1
    assert stack["ledger"].verify().balanced


def test_a_recovery_is_explained_in_the_audit_log(stack):
    stall(stack, charge_first=True)
    stack["clock"].advance(STALE + 1)
    outcome = stack["gateway"](stack["charge"]).submit(req())

    entries = stack["audit"].for_request(outcome.request_id)
    types = [e.event_type for e in entries]
    assert EventType.IDEMPOTENCY_RECOVERED in types
    captured = next(e for e in entries if e.event_type is EventType.PAYMENT_CAPTURED)
    assert captured.details["recovered_from_stalled_attempt"] is True
    # No new provider attempt was logged, because none was made.
    assert EventType.PAYMENT_ATTEMPTED not in types


def test_a_second_retry_after_recovery_is_a_plain_replay(stack):
    stall(stack, charge_first=True)
    stack["clock"].advance(STALE + 1)
    retry = stack["gateway"](stack["charge"])
    retry.submit(req())

    again = retry.submit(req())
    assert again.outcome is Outcome.REPLAYED
    assert len(orders(stack)) == 1


# -- a stalled attempt that never charged ---------------------------------

def test_no_order_long_enough_after_the_stall_is_reclaimed_and_charged_once(stack):
    stall(stack, charge_first=False)
    stack["clock"].advance(GRACE + 1)

    outcome = stack["gateway"](stack["charge"]).submit(req())

    assert outcome.outcome is Outcome.RECLAIMED
    assert len(orders(stack)) == 1
    assert status(stack) == COMPLETED
    assert stack["ledger"].trial_balance()[REVENUE] == -AMOUNT


def test_no_order_yet_inside_the_lag_window_freezes_instead_of_charging(stack):
    """Stale after 30s, but the provider's list can lag by 300s: an empty
    answer at 31s is silence, not a denial. Re-running here is how a charge
    that simply was not listed yet becomes a second charge."""
    stall(stack, charge_first=False)
    stack["clock"].advance(STALE + 1)

    outcome = stack["gateway"](stack["charge"]).submit(req())

    assert outcome.outcome is Outcome.AWAITING_VERIFICATION
    assert "absence is not yet evidence" in outcome.reason
    assert orders(stack) == []
    assert status(stack) == PENDING_VERIFICATION
    # Frozen with suspense, so the exposure is on the books, not in silence.
    assert stack["ledger"].exposure() == AMOUNT


def test_a_frozen_stale_key_is_released_by_reconciliation(stack):
    """Freezing must not become blocking forever: reconciliation settles it,
    and a retry then goes through."""
    stall(stack, charge_first=False)
    stack["clock"].advance(STALE + 1)
    stack["gateway"](stack["charge"]).submit(req())
    assert status(stack) == PENDING_VERIFICATION

    stack["clock"].advance(GRACE + 1)
    reconciler = Reconciler(stack["provider"], stack["store"], audit=stack["audit"],
                            policy=stack["engine"], clock=stack["clock"],
                            ledger=stack["ledger"])
    result = reconciler.reconcile(KEY, f"rcpt_{KEY}", agent_id=AGENT)

    assert result.finding is Finding.CONFIRMED_NOT_EXECUTED
    assert status(stack) == FAILED
    assert stack["ledger"].exposure() == 0

    retry = stack["gateway"](stack["charge"]).submit(req())
    assert retry.outcome is Outcome.EXECUTED
    assert len(orders(stack)) == 1


# -- ambiguous answers freeze ---------------------------------------------

def test_an_unreachable_provider_freezes_the_key(stack):
    stall(stack, charge_first=True)
    stack["clock"].advance(STALE + 1)
    stack["lookups"]["fail"] = True

    outcome = stack["gateway"](stack["charge"]).submit(req())

    assert outcome.outcome is Outcome.AWAITING_VERIFICATION
    assert "could not ask the provider" in outcome.reason
    assert len(orders(stack)) == 1, "a failed check must not be read as 'no order'"
    assert status(stack) == PENDING_VERIFICATION


def test_two_existing_orders_are_left_for_a_human(stack):
    stall(stack, charge_first=True)
    stack["charge"](req())   # a second order already on this receipt
    stack["clock"].advance(STALE + 1)

    outcome = stack["gateway"](stack["charge"]).submit(req())

    assert outcome.outcome is Outcome.AWAITING_VERIFICATION
    assert "needs a human" in outcome.reason
    assert len(orders(stack)) == 2, "nothing further was charged"


def test_an_order_for_a_different_amount_is_not_trusted(stack):
    stall(stack, charge_first=False)
    stack["provider"].create_order(99, receipt=f"rcpt_{KEY}")
    stack["clock"].advance(STALE + 1)

    outcome = stack["gateway"](stack["charge"]).submit(req())

    assert outcome.outcome is Outcome.AWAITING_VERIFICATION
    assert "not completing from it" in outcome.reason
    assert status(stack) == PENDING_VERIFICATION


# -- the claim is fenced across the provider call -------------------------

def test_a_claim_taken_over_during_the_check_does_not_act(stack):
    """The provider is asked with no lock held, so the check can outlive the
    staleness window and someone else can take the key. The original retry
    must then stand down, not run a purchase on a claim it no longer holds."""
    store = stack["store"]
    ran = []

    def stale_claimant():
        claimed = threading.Event()
        threading.Thread(
            target=lambda: store.execute(KEY, {"a": 1},
                                         lambda: (claimed.set(), threading.Event().wait())),
            daemon=True).start()
        assert claimed.wait(timeout=10)

    stale_claimant()
    stack["clock"].advance(STALE + 1)

    def slow_check(claim):
        # Someone else reclaims the key while this check is in flight.
        with store.db.connection() as conn:
            conn.execute("UPDATE idempotency_records SET claimed_at = claimed_at + 1 "
                         "WHERE key = %s", (KEY,))
        return StaleVerdict.not_executed()

    result = store.execute(KEY, {"a": 1}, lambda: ran.append(1),
                           verify_stale=slow_check)

    assert result.outcome is Outcome.IN_PROGRESS
    assert ran == [], "a lost claim ran the action anyway"


def test_a_verifier_that_raises_is_treated_as_unknown(stack):
    store = stack["store"]
    claimed = threading.Event()
    threading.Thread(
        target=lambda: store.execute(KEY, {"a": 1},
                                     lambda: (claimed.set(), threading.Event().wait())),
        daemon=True).start()
    assert claimed.wait(timeout=10)
    stack["clock"].advance(STALE + 1)
    ran = []

    def broken(claim):
        raise RuntimeError("bug in the check")

    result = store.execute(KEY, {"a": 1}, lambda: ran.append(1), verify_stale=broken)

    assert result.outcome is Outcome.AWAITING_VERIFICATION
    assert ran == []


# -- concurrency ----------------------------------------------------------

@pytest.mark.parametrize("run", range(5))
def test_racing_retries_of_a_stalled_charged_key_never_charge_again(stack, run):
    stall(stack, charge_first=True)
    stack["clock"].advance(STALE + 1)
    retry = stack["gateway"](stack["charge"])

    threads_n = 16
    barrier = threading.Barrier(threads_n)
    outcomes, errors = [], []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            o = retry.submit(req())
            with lock:
                outcomes.append(o.outcome)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(orders(stack)) == 1, "a racing retry charged again"
    assert outcomes.count(Outcome.RECOVERED) == 1
    assert set(outcomes) <= {Outcome.RECOVERED, Outcome.IN_PROGRESS, Outcome.REPLAYED}
    assert len(sales(stack)) == 1


@pytest.mark.parametrize("run", range(5))
def test_racing_retries_of_a_stalled_uncharged_key_charge_exactly_once(stack, run):
    stall(stack, charge_first=False)
    stack["clock"].advance(GRACE + 1)
    retry = stack["gateway"](stack["charge"])

    threads_n = 16
    barrier = threading.Barrier(threads_n)
    outcomes = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        o = retry.submit(req())
        with lock:
            outcomes.append(o.outcome)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(orders(stack)) == 1
    assert outcomes.count(Outcome.RECLAIMED) == 1
    assert len(sales(stack)) == 1


# -- why the wiring matters -----------------------------------------------

def test_without_a_verifier_the_old_double_charge_is_still_possible(stack):
    """The fix is a question the gateway asks, so a gateway built without
    `find_orders` still behaves as before Phase 12. Kept as a test so that
    dropping the wiring from a real surface shows up as a named risk."""
    stall(stack, charge_first=True)
    stack["clock"].advance(STALE + 1)

    outcome = stack["gateway"](stack["charge"], verify=False).submit(req())

    assert outcome.outcome is Outcome.RECLAIMED
    assert len(orders(stack)) == 2
