"""Phase 1 completion test — Idempotency Core.

Each test maps to one bullet of Phase 1's completion test in RAZORPAY.md.
The processor underneath has no idempotency of its own, so anything these
tests prove is a property of the wrapper alone.
"""

from __future__ import annotations

import threading
import time

import pytest

from zerotrust.idempotency import (
    PROCESSING,
    IdempotencyStore,
    Outcome,
)
from zerotrust.processor import MockPaymentProcessor


class FakeClock:
    """Deterministic time, so staleness tests never depend on wall-clock luck."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def store(tmp_path):
    return IdempotencyStore(str(tmp_path / "idem.db"))


@pytest.fixture
def processor():
    return MockPaymentProcessor()


def payload(order_id="order_1", amount=50_000):
    return {"order_id": order_id, "amount_paise": amount, "currency": "INR"}


def charge_action(processor, p):
    return lambda: processor.charge(p["order_id"], p["amount_paise"], p["currency"])


# -- 1. sequential retry, same key ---------------------------------------

def test_sequential_retry_charges_once_and_replays(store, processor):
    p = payload()
    first = store.execute("key-1", p, charge_action(processor, p))
    second = store.execute("key-1", p, charge_action(processor, p))

    assert first.outcome is Outcome.EXECUTED
    assert second.outcome is Outcome.REPLAYED
    assert processor.charge_count == 1
    # The replay returns the *original* result, not a fresh one.
    assert second.response == first.response


def test_many_sequential_retries_still_charge_once(store, processor):
    p = payload()
    outcomes = [
        store.execute("key-1", p, charge_action(processor, p)).outcome
        for _ in range(25)
    ]
    assert outcomes[0] is Outcome.EXECUTED
    assert all(o is Outcome.REPLAYED for o in outcomes[1:])
    assert processor.charge_count == 1


# -- 2. concurrent storm, identical key -----------------------------------

@pytest.mark.parametrize("run", range(20))
def test_concurrent_identical_key_charges_exactly_once(tmp_path, run):
    """Repeated 20x: a single green run is not proof for a race."""
    store = IdempotencyStore(str(tmp_path / f"idem_{run}.db"))
    # Latency widens the window between claim and completion, so racing
    # threads genuinely land mid-flight rather than after the fact.
    processor = MockPaymentProcessor(latency_seconds=0.02)
    p = payload()

    threads_n = 32
    barrier = threading.Barrier(threads_n)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        r = store.execute("race-key", p, charge_action(processor, p))
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == threads_n
    executed = [r for r in results if r.executed]
    assert len(executed) == 1, f"expected 1 executor, got {len(executed)}"
    assert processor.charge_count == 1

    # Every other caller got a safe answer -- never a second charge.
    for r in results:
        assert r.outcome in {
            Outcome.EXECUTED,
            Outcome.REPLAYED,
            Outcome.IN_PROGRESS,
        }
    # Replays hand back the same charge the executor made.
    for r in results:
        if r.outcome is Outcome.REPLAYED:
            assert r.response == executed[0].response


# -- 3. same key, different payload ---------------------------------------

def test_same_key_different_payload_is_rejected(store, processor):
    original = payload(amount=50_000)
    tampered = payload(amount=5_000_000)

    first = store.execute("key-1", original, charge_action(processor, original))
    conflict = store.execute("key-1", tampered, charge_action(processor, tampered))

    assert first.outcome is Outcome.EXECUTED
    assert conflict.outcome is Outcome.CONFLICT
    assert conflict.reason and "different payload" in conflict.reason
    # The original charge is untouched: one charge, at the original amount.
    assert processor.charge_count == 1
    assert processor.charges[0]["amount_paise"] == 50_000


def test_conflict_against_an_in_flight_key_is_also_rejected(tmp_path):
    """A tampered replay must be rejected even before the original finishes."""
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    processor = MockPaymentProcessor()
    original = payload(amount=50_000)
    tampered = payload(amount=5_000_000)

    released = threading.Event()
    claimed = threading.Event()

    def slow_action():
        claimed.set()
        released.wait(timeout=10)
        return processor.charge(original["order_id"], original["amount_paise"])

    holder = threading.Thread(
        target=lambda: store.execute("key-1", original, slow_action)
    )
    holder.start()
    assert claimed.wait(timeout=10)

    conflict = store.execute("key-1", tampered, charge_action(processor, tampered))
    assert conflict.outcome is Outcome.CONFLICT

    released.set()
    holder.join(timeout=10)
    assert processor.charge_count == 1


# -- 4. different keys are independent ------------------------------------

def test_different_keys_charge_independently(store, processor):
    a = payload(order_id="order_A")
    b = payload(order_id="order_B")

    ra = store.execute("key-A", a, charge_action(processor, a))
    rb = store.execute("key-B", b, charge_action(processor, b))

    assert ra.outcome is Outcome.EXECUTED
    assert rb.outcome is Outcome.EXECUTED
    assert processor.charge_count == 2
    assert ra.response["charge_id"] != rb.response["charge_id"]


# -- 5. many concurrent orders, retries each, no cross-contamination -------

@pytest.mark.parametrize("run", range(5))
def test_concurrent_distinct_orders_do_not_contaminate(tmp_path, run):
    store = IdempotencyStore(str(tmp_path / f"idem_{run}.db"))
    processor = MockPaymentProcessor(latency_seconds=0.01)

    orders = [f"order_{i}" for i in range(8)]
    retries_per_order = 6
    barrier = threading.Barrier(len(orders) * retries_per_order)
    results = {o: [] for o in orders}
    lock = threading.Lock()

    def worker(order_id):
        p = payload(order_id=order_id, amount=1_000 + int(order_id.split("_")[1]))
        barrier.wait()
        r = store.execute(f"key-{order_id}", p, charge_action(processor, p))
        with lock:
            results[order_id].append(r)

    threads = [
        threading.Thread(target=worker, args=(o,))
        for o in orders
        for _ in range(retries_per_order)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert processor.charge_count == len(orders)
    for i, order_id in enumerate(orders):
        charges = processor.charges_for(order_id)
        assert len(charges) == 1, f"{order_id} charged {len(charges)} times"
        # Each order was charged its own amount -- no payload bleed between keys.
        assert charges[0]["amount_paise"] == 1_000 + i
        assert len(results[order_id]) == retries_per_order
        assert sum(1 for r in results[order_id] if r.executed) == 1


# -- 6. stale record is reclaimed -----------------------------------------

def test_stale_processing_record_is_reclaimed(tmp_path):
    """A claimant that crashed leaves PROCESSING behind; it must not block forever."""
    clock = FakeClock()
    store = IdempotencyStore(
        str(tmp_path / "idem.db"), stale_after_seconds=30.0, clock=clock
    )
    processor = MockPaymentProcessor()
    p = payload()

    stuck = threading.Event()
    claimed = threading.Event()

    def crashing_action():
        # Claims the key, then hangs forever without ever charging --
        # exactly what a process killed mid-flight leaves behind.
        claimed.set()
        stuck.wait()
        return processor.charge(p["order_id"], p["amount_paise"])

    ghost = threading.Thread(
        target=lambda: store.execute("key-1", p, crashing_action), daemon=True
    )
    ghost.start()
    assert claimed.wait(timeout=10)
    assert store.get("key-1")["status"] == PROCESSING
    assert processor.charge_count == 0

    clock.advance(31.0)  # past the staleness timeout
    recovered = store.execute("key-1", p, charge_action(processor, p))

    assert recovered.outcome is Outcome.RECLAIMED
    assert recovered.attempts == 2
    assert recovered.response is not None
    assert processor.charge_count == 1  # still exactly once

    # And once reclaimed and completed, further retries replay as normal.
    again = store.execute("key-1", p, charge_action(processor, p))
    assert again.outcome is Outcome.REPLAYED
    assert processor.charge_count == 1


def test_stale_reclaim_works_on_a_real_clock(tmp_path):
    """Same property without the fake clock, to prove it isn't a test artifact."""
    store = IdempotencyStore(str(tmp_path / "idem.db"), stale_after_seconds=0.3)
    processor = MockPaymentProcessor()
    p = payload()

    claimed = threading.Event()
    ghost = threading.Thread(
        target=lambda: store.execute(
            "key-1", p, lambda: (claimed.set(), threading.Event().wait())
        ),
        daemon=True,
    )
    ghost.start()
    assert claimed.wait(timeout=10)

    blocked = store.execute("key-1", p, charge_action(processor, p))
    assert blocked.outcome is Outcome.IN_PROGRESS

    time.sleep(0.4)
    recovered = store.execute("key-1", p, charge_action(processor, p))
    assert recovered.outcome is Outcome.RECLAIMED
    assert processor.charge_count == 1


@pytest.mark.parametrize("run", range(10))
def test_concurrent_reclaim_of_a_stale_key_charges_once(tmp_path, run):
    """Many callers spotting the same stale record must not all reclaim it."""
    clock = FakeClock()
    store = IdempotencyStore(
        str(tmp_path / f"idem_{run}.db"), stale_after_seconds=30.0, clock=clock
    )
    processor = MockPaymentProcessor(latency_seconds=0.01)
    p = payload()

    claimed = threading.Event()
    ghost = threading.Thread(
        target=lambda: store.execute(
            "key-1", p, lambda: (claimed.set(), threading.Event().wait())
        ),
        daemon=True,
    )
    ghost.start()
    assert claimed.wait(timeout=10)
    clock.advance(31.0)

    threads_n = 16
    barrier = threading.Barrier(threads_n)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        r = store.execute("key-1", p, charge_action(processor, p))
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sum(1 for r in results if r.executed) == 1
    assert processor.charge_count == 1


# -- 7. a genuinely in-flight key is NOT reclaimed -------------------------

def test_fresh_in_flight_key_is_not_reclaimed(tmp_path):
    """The reclaim path must not weaken the core guarantee."""
    clock = FakeClock()
    store = IdempotencyStore(
        str(tmp_path / "idem.db"), stale_after_seconds=30.0, clock=clock
    )
    processor = MockPaymentProcessor()
    p = payload()

    released = threading.Event()
    claimed = threading.Event()

    def slow_action():
        claimed.set()
        released.wait(timeout=10)
        return processor.charge(p["order_id"], p["amount_paise"])

    holder = threading.Thread(target=lambda: store.execute("key-1", p, slow_action))
    holder.start()
    assert claimed.wait(timeout=10)

    # Well inside the staleness window: still in flight, must block.
    clock.advance(29.0)
    blocked = store.execute("key-1", p, charge_action(processor, p))
    assert blocked.outcome is Outcome.IN_PROGRESS
    assert blocked.reason and "in flight" in blocked.reason
    assert processor.charge_count == 0

    released.set()
    holder.join(timeout=10)
    assert processor.charge_count == 1


# -- failed attempts do not permanently burn a key ------------------------

def test_action_that_raises_leaves_the_key_retryable(store, processor):
    p = payload()
    boom = RuntimeError("processor unreachable")

    def failing_action():
        raise boom

    with pytest.raises(RuntimeError):
        store.execute("key-1", p, failing_action)
    assert store.get("key-1")["status"] == "FAILED"
    assert processor.charge_count == 0

    retry = store.execute("key-1", p, charge_action(processor, p))
    assert retry.outcome is Outcome.EXECUTED
    assert retry.attempts == 2
    assert processor.charge_count == 1

    assert store.execute("key-1", p, charge_action(processor, p)).outcome is Outcome.REPLAYED
    assert processor.charge_count == 1
