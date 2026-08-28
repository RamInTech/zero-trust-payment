"""Phase 4 completion test — Audit Log.

Three claims, each mapped to a completion-test bullet:
  1. every Phase 1 and Phase 3 outcome produces exactly ONE named entry;
  2. entries are immutable -- tampering fails at the database, not by policy;
  3. the log alone explains a decision, without reading the code.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from zerotrust.audit import Actor, AuditLog, AuditWriteError, EventType
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore, Outcome
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest, Rule

HOUR = 3600.0
MANDATE_TTL = 24 * HOUR


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def audit(tmp_path, clock):
    return AuditLog(str(tmp_path / "audit.db"), clock=clock)


@pytest.fixture
def engine(tmp_path, clock):
    return PolicyEngine(MandateStore(str(tmp_path / "policy.db"), clock=clock),
                        clock=clock)


@pytest.fixture
def mandate(engine, clock):
    return engine.mandates.issue(
        Mandate(
            agent_id="agent_1",
            max_amount_paise=50_000,
            allowed_skus=frozenset({"SKU-COFFEE"}),
            expires_at=clock() + MANDATE_TTL,
            velocity_limit=3,
            velocity_window_secs=HOUR,
            created_at=clock(),
        )
    )


@pytest.fixture
def gateway(engine, audit, tmp_path):
    calls = []
    store = IdempotencyStore(str(tmp_path / "idem.db"))

    def execute(request):
        calls.append(request)
        return {"order_id": f"order_{len(calls)}"}

    gw = PurchaseGateway(engine, store, execute, audit=audit)
    gw.calls = calls
    return gw


def req(**overrides):
    base = dict(agent_id="agent_1", sku="SKU-COFFEE", amount_paise=10_000,
                idempotency_key="key-1", currency="INR")
    base.update(overrides)
    return PurchaseRequest(**base)


# -- BULLET 1: exactly one entry per outcome ------------------------------

def test_approved_purchase_logs_the_full_story(gateway, audit, mandate):
    outcome = gateway.submit(req())
    types = [e.event_type for e in audit.for_request(outcome.request_id)]

    assert types == [
        EventType.PURCHASE_REQUESTED,
        EventType.POLICY_APPROVED,
        EventType.PAYMENT_ATTEMPTED,
        EventType.IDEMPOTENCY_EXECUTED,
        EventType.PAYMENT_CAPTURED,
    ]
    # Exactly one of each -- no duplicates, no omissions.
    assert len(types) == len(set(types))


@pytest.mark.parametrize(
    "overrides,expected_rule",
    [
        ({"amount_paise": 999_999}, Rule.AMOUNT_EXCEEDS_CAP),
        ({"sku": "SKU-YACHT"}, Rule.SKU_NOT_ALLOWED),
        ({"currency": "USD"}, Rule.CURRENCY_MISMATCH),
        ({"amount_paise": -1}, Rule.MALFORMED_REQUEST),
        ({"agent_id": "ghost"}, Rule.NO_ACTIVE_MANDATE),
    ],
)
def test_each_denial_logs_exactly_one_entry_citing_its_rule(
    gateway, audit, mandate, overrides, expected_rule
):
    outcome = gateway.submit(req(**overrides))
    entries = audit.for_request(outcome.request_id)
    denials = [e for e in entries if e.event_type is EventType.POLICY_DENIED]

    assert len(denials) == 1, "a denial must log exactly one POLICY_DENIED"
    assert denials[0].rule == expected_rule.value
    assert denials[0].reason
    assert denials[0].actor is Actor.POLICY_ENGINE

    # A denial must not log an approval or any money event.
    logged = {e.event_type for e in entries}
    assert EventType.POLICY_APPROVED not in logged
    assert EventType.PAYMENT_ATTEMPTED not in logged
    assert EventType.PAYMENT_CAPTURED not in logged


def test_expiry_denial_is_logged_with_its_own_rule(gateway, audit, mandate, clock):
    clock.advance(MANDATE_TTL + 1)
    outcome = gateway.submit(req())
    denial = audit.for_request(outcome.request_id)[-1]
    assert denial.rule == Rule.MANDATE_EXPIRED.value


def test_velocity_denial_is_logged_with_its_own_rule(gateway, audit, mandate):
    for i in range(3):
        gateway.submit(req(idempotency_key=f"k{i}"))
    outcome = gateway.submit(req(idempotency_key="k3"))

    denial = audit.for_request(outcome.request_id)[-1]
    assert denial.event_type is EventType.POLICY_DENIED
    assert denial.rule == Rule.VELOCITY_EXCEEDED.value
    assert denial.details["limit"] == 3


def test_replay_logs_replayed_not_executed(gateway, audit, mandate):
    first = gateway.submit(req())
    second = gateway.submit(req())

    assert second.outcome is Outcome.REPLAYED
    second_types = {e.event_type for e in audit.for_request(second.request_id)}
    assert EventType.IDEMPOTENCY_REPLAYED in second_types
    assert EventType.IDEMPOTENCY_EXECUTED not in second_types
    # A replay moves no money, so it logs no capture.
    assert EventType.PAYMENT_CAPTURED not in second_types

    assert audit.count_of(EventType.IDEMPOTENCY_EXECUTED) == 1
    assert audit.count_of(EventType.PAYMENT_CAPTURED) == 1
    assert len(gateway.calls) == 1


def test_conflict_logs_conflict(gateway, audit, mandate):
    gateway.submit(req())
    conflict = gateway.submit(req(amount_paise=20_000))

    assert conflict.outcome is Outcome.CONFLICT
    types = {e.event_type for e in audit.for_request(conflict.request_id)}
    assert EventType.IDEMPOTENCY_CONFLICT in types
    assert audit.count_of(EventType.IDEMPOTENCY_CONFLICT) == 1


def test_in_progress_logs_in_progress(engine, audit, tmp_path, mandate):
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    released, claimed = threading.Event(), threading.Event()

    def slow(request):
        claimed.set()
        released.wait(timeout=10)
        return {"order_id": "order_slow"}

    gw = PurchaseGateway(engine, store, slow, audit=audit)
    holder = threading.Thread(target=lambda: gw.submit(req()))
    holder.start()
    assert claimed.wait(timeout=10)

    blocked = gw.submit(req())
    released.set()
    holder.join(timeout=10)

    assert blocked.outcome is Outcome.IN_PROGRESS
    types = {e.event_type for e in audit.for_request(blocked.request_id)}
    assert EventType.IDEMPOTENCY_IN_PROGRESS in types


def test_reclaimed_logs_reclaimed(engine, audit, tmp_path, mandate, clock):
    store = IdempotencyStore(str(tmp_path / "idem.db"),
                             stale_after_seconds=30.0, clock=clock)
    claimed = threading.Event()

    def hang(request):
        claimed.set()
        threading.Event().wait()

    ghost_gw = PurchaseGateway(engine, store, hang, audit=audit)
    threading.Thread(target=lambda: ghost_gw.submit(req()), daemon=True).start()
    assert claimed.wait(timeout=10)

    clock.advance(31.0)
    gw = PurchaseGateway(engine, store, lambda r: {"order_id": "recovered"},
                         audit=audit)
    outcome = gw.submit(req())

    assert outcome.outcome is Outcome.RECLAIMED
    types = {e.event_type for e in audit.for_request(outcome.request_id)}
    assert EventType.IDEMPOTENCY_RECLAIMED in types
    assert audit.count_of(EventType.IDEMPOTENCY_RECLAIMED) == 1


def test_every_phase1_and_phase3_outcome_has_a_distinct_event(gateway, audit,
                                                              mandate):
    """The taxonomy covers every outcome, with no two sharing an event type."""
    from zerotrust.gateway import OUTCOME_EVENTS

    assert set(OUTCOME_EVENTS) == set(Outcome), "an outcome would go unlogged"
    assert len(set(OUTCOME_EVENTS.values())) == len(OUTCOME_EVENTS)


def test_failed_execution_logs_payment_failed(engine, audit, tmp_path, mandate):
    store = IdempotencyStore(str(tmp_path / "idem.db"))

    def boom(request):
        raise RuntimeError("provider unreachable")

    gw = PurchaseGateway(engine, store, boom, audit=audit)
    with pytest.raises(RuntimeError):
        gw.submit(req())

    failures = audit.of_type(EventType.PAYMENT_FAILED)
    assert len(failures) == 1
    assert "unreachable" in failures[0].reason
    assert failures[0].actor is Actor.PROVIDER


# -- BULLET 2: entries are immutable --------------------------------------

def test_update_is_rejected_by_the_database(audit):
    audit.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE,
                 request_id="r1", rule="AMOUNT_EXCEEDS_CAP", reason="over cap")

    conn = sqlite3.connect(audit.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            conn.execute("UPDATE audit_log SET reason = 'nothing happened'")
        assert "append-only" in str(exc.value)
    finally:
        conn.close()

    assert audit.all()[0].reason == "over cap"


def test_delete_is_rejected_by_the_database(audit):
    audit.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE, request_id="r1")

    conn = sqlite3.connect(audit.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            conn.execute("DELETE FROM audit_log")
        assert "append-only" in str(exc.value)
    finally:
        conn.close()

    assert len(audit.all()) == 1


def test_tampering_fails_even_targeting_one_row(audit):
    audit.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE, request_id="r1")
    audit.record(EventType.POLICY_APPROVED, Actor.POLICY_ENGINE, request_id="r2")

    conn = sqlite3.connect(audit.db_path)
    try:
        for sql in (
            "UPDATE audit_log SET rule = 'X' WHERE event_id = 1",
            "DELETE FROM audit_log WHERE event_id = 1",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    finally:
        conn.close()

    assert len(audit.all()) == 2


def test_the_log_has_no_update_or_delete_api(audit):
    """There is no supported way to mutate history, only to append."""
    for forbidden in ("update", "delete", "edit", "amend", "purge"):
        assert not hasattr(audit, forbidden), (
            f"AuditLog grew a {forbidden}() method -- the log must be append-only"
        )


def test_an_unwritable_log_blocks_the_money_action(engine, tmp_path, mandate):
    """Log-before-execute: a broken log stops the payment, never the reverse."""
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    calls = []

    class BrokenLog(AuditLog):
        def record(self, *args, **kwargs):
            raise AuditWriteError("disk full")

    gw = PurchaseGateway(engine, store,
                         lambda r: calls.append(r) or {"order_id": "x"},
                         audit=BrokenLog(str(tmp_path / "audit2.db")))

    with pytest.raises(AuditWriteError):
        gw.submit(req())
    assert len(calls) == 0, "money moved despite the audit log being unwritable"


# -- BULLET 3: the log alone explains the decision ------------------------

def test_a_reviewer_can_explain_a_denial_from_the_log_alone(gateway, audit,
                                                            mandate):
    outcome = gateway.submit(req(amount_paise=75_000))
    story = audit.timeline(outcome.request_id)

    # Everything needed to explain the refusal, without opening the code.
    assert "PURCHASE_REQUESTED" in story
    assert "POLICY_DENIED" in story
    assert "AMOUNT_EXCEEDS_CAP" in story
    assert "75000" in story and "50000" in story

    denial = audit.for_request(outcome.request_id)[-1]
    assert denial.details["requested_paise"] == 75_000
    assert denial.details["cap_paise"] == 50_000
    assert denial.mandate_id == mandate.mandate_id


def test_held_out_requests_are_each_explainable(gateway, audit, mandate):
    """A reviewer who didn't see these constructed can still classify them."""
    cases = [
        (req(sku="SKU-BANNED", idempotency_key="h1"), "SKU_NOT_ALLOWED"),
        (req(amount_paise=60_000, idempotency_key="h2"), "AMOUNT_EXCEEDS_CAP"),
        (req(currency="EUR", idempotency_key="h3"), "CURRENCY_MISMATCH"),
        (req(idempotency_key="h4"), None),  # approved
    ]
    for request, expected_rule in cases:
        outcome = gateway.submit(request)
        entries = audit.for_request(outcome.request_id)
        verdict = [
            e for e in entries
            if e.event_type in (EventType.POLICY_APPROVED, EventType.POLICY_DENIED)
        ]
        assert len(verdict) == 1
        assert verdict[0].rule == expected_rule
        if expected_rule:
            assert verdict[0].reason, "denial with no explanation"


def test_actor_distinguishes_who_decided(gateway, audit, mandate):
    """The agent proposes; only the policy engine authorises."""
    outcome = gateway.submit(req())
    by_type = {e.event_type: e.actor for e in audit.for_request(outcome.request_id)}

    assert by_type[EventType.PURCHASE_REQUESTED] is Actor.AGENT
    assert by_type[EventType.POLICY_APPROVED] is Actor.POLICY_ENGINE
    assert by_type[EventType.PAYMENT_CAPTURED] is Actor.PROVIDER
    # The agent never authorises anything.
    authorising = [
        e for e in audit.for_request(outcome.request_id)
        if e.event_type in (EventType.POLICY_APPROVED, EventType.POLICY_DENIED)
    ]
    assert all(e.actor is Actor.POLICY_ENGINE for e in authorising)


def test_entries_are_ordered_and_tied_to_one_request(gateway, audit, mandate):
    a = gateway.submit(req(idempotency_key="a"))
    b = gateway.submit(req(idempotency_key="b"))

    assert a.request_id != b.request_id
    for rid in (a.request_id, b.request_id):
        entries = audit.for_request(rid)
        assert entries
        assert all(e.request_id == rid for e in entries)
        assert [e.event_id for e in entries] == sorted(e.event_id for e in entries)


def test_entries_survive_reopening_the_database(tmp_path, clock):
    log = AuditLog(str(tmp_path / "a.db"), clock=clock)
    log.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE, request_id="r1",
               rule="SKU_NOT_ALLOWED", reason="not allowed")

    reopened = AuditLog(str(tmp_path / "a.db"), clock=clock)
    entries = reopened.for_request("r1")
    assert len(entries) == 1
    assert entries[0].rule == "SKU_NOT_ALLOWED"


# -- concurrency: no entries lost under load ------------------------------

@pytest.mark.parametrize("run", range(5))
def test_concurrent_writers_lose_no_entries(tmp_path, clock, run):
    log = AuditLog(str(tmp_path / f"a{run}.db"), clock=clock)
    threads_n, per_thread = 16, 10
    barrier = threading.Barrier(threads_n)

    def worker(i):
        barrier.wait()
        for j in range(per_thread):
            log.record(EventType.PURCHASE_REQUESTED, Actor.AGENT,
                       request_id=f"r{i}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(log.all()) == threads_n * per_thread
    ids = [e.event_id for e in log.all()]
    assert len(set(ids)) == len(ids), "duplicate event ids"
