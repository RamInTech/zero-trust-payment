"""Phase 4 completion test — Audit Log.

Three claims, each mapped to a completion-test bullet:
  1. every Phase 1 and Phase 3 outcome produces exactly ONE named entry;
  2. entries are immutable -- tampering fails at the database, not by policy;
  3. the log alone explains a decision, without reading the code.

Phase 10: every attack on the database now arrives through a separate
connection from outside the application's pool, and fails with Postgres's
`restrict_violation`, which psycopg reports as an `IntegrityError`.
"""

from __future__ import annotations

import threading

import psycopg
import pytest

from zerotrust.audit import (
    APPEND_ONLY_TRIGGERS,
    Actor,
    AuditLog,
    AuditWriteError,
    EventType,
)
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
def audit(db, clock):
    return AuditLog(db, clock=clock)


@pytest.fixture
def engine(db, clock):
    return PolicyEngine(MandateStore(db, clock=clock), clock=clock)


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
def gateway(engine, audit):
    calls = []
    store = IdempotencyStore(engine.db)

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


def test_in_progress_logs_in_progress(engine, audit, mandate):
    store = IdempotencyStore(engine.db)
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


def test_reclaimed_logs_reclaimed(engine, audit, mandate, clock):
    store = IdempotencyStore(engine.db, stale_after_seconds=30.0, clock=clock)
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


def test_failed_execution_logs_payment_failed(engine, audit, mandate):
    store = IdempotencyStore(engine.db)

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

    with audit.db.outside_connection() as conn:
        with pytest.raises(psycopg.IntegrityError) as exc:
            conn.execute("UPDATE audit_log SET reason = 'nothing happened'")
    assert "append-only" in str(exc.value)

    assert audit.all()[0].reason == "over cap"


def test_delete_is_rejected_by_the_database(audit):
    audit.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE, request_id="r1")

    with audit.db.outside_connection() as conn:
        with pytest.raises(psycopg.IntegrityError) as exc:
            conn.execute("DELETE FROM audit_log")
    assert "append-only" in str(exc.value)

    assert len(audit.all()) == 1


def test_truncate_is_rejected_by_the_database(audit):
    """TRUNCATE is a separate command in Postgres and needs its own trigger."""
    audit.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE, request_id="r1")

    with audit.db.outside_connection() as conn:
        with pytest.raises(psycopg.IntegrityError) as exc:
            conn.execute("TRUNCATE audit_log")
    assert "append-only" in str(exc.value)

    assert len(audit.all()) == 1


def test_without_its_own_trigger_truncate_would_wipe_the_log(audit):
    """The hole is real, not theoretical: row triggers do not see TRUNCATE.

    With only the UPDATE and DELETE triggers in place, TRUNCATE empties the
    table. That is why the migration to Postgres needed a third trigger rather
    than a straight copy of the SQLite two.
    """
    audit.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE, request_id="r1")

    with audit.db.outside_connection() as conn:
        conn.execute("DROP TRIGGER audit_log_no_truncate ON audit_log")
        with pytest.raises(psycopg.IntegrityError):
            conn.execute("DELETE FROM audit_log")   # row trigger still holds
        conn.execute("TRUNCATE audit_log")          # ...and TRUNCATE walks past it

    assert audit.all() == []


def test_tampering_fails_even_targeting_one_row(audit):
    audit.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE, request_id="r1")
    audit.record(EventType.POLICY_APPROVED, Actor.POLICY_ENGINE, request_id="r2")

    with audit.db.outside_connection() as conn:
        for sql in (
            "UPDATE audit_log SET rule = 'X' WHERE event_id = 1",
            "DELETE FROM audit_log WHERE event_id = 1",
        ):
            with pytest.raises(psycopg.IntegrityError):
                conn.execute(sql)

    assert len(audit.all()) == 2


def test_the_live_schema_carries_every_append_only_trigger(audit):
    """Read back from Postgres's catalog, not from this file."""
    definitions = audit.triggers()
    for name in APPEND_ONLY_TRIGGERS:
        assert any(name in d for d in definitions), f"{name} is missing"
    assert any("BEFORE UPDATE" in d for d in definitions)
    assert any("BEFORE DELETE" in d for d in definitions)
    assert any("BEFORE TRUNCATE" in d for d in definitions)


def test_the_log_has_no_update_or_delete_api(audit):
    """There is no supported way to mutate history, only to append."""
    for forbidden in ("update", "delete", "edit", "amend", "purge"):
        assert not hasattr(audit, forbidden), (
            f"AuditLog grew a {forbidden}() method -- the log must be append-only"
        )


def test_an_unwritable_log_blocks_the_money_action(engine, mandate):
    """Log-before-execute: a broken log stops the payment, never the reverse."""
    store = IdempotencyStore(engine.db)
    calls = []

    class BrokenLog(AuditLog):
        def record(self, *args, **kwargs):
            raise AuditWriteError("disk full")

    gw = PurchaseGateway(engine, store,
                         lambda r: calls.append(r) or {"order_id": "x"},
                         audit=BrokenLog(engine.db))

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


def test_entries_survive_reopening_the_database(db, clock):
    log = AuditLog(db, clock=clock)
    log.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE, request_id="r1",
               rule="SKU_NOT_ALLOWED", reason="not allowed")

    reopened = AuditLog(db, clock=clock)
    entries = reopened.for_request("r1")
    assert len(entries) == 1
    assert entries[0].rule == "SKU_NOT_ALLOWED"


# -- concurrency: no entries lost under load ------------------------------

@pytest.mark.parametrize("run", range(5))
def test_concurrent_writers_lose_no_entries(db, clock, run):
    log = AuditLog(db, clock=clock)
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


# -- the hash chain --------------------------------------------------------
#
# The triggers stop tampering THROUGH the database. These cover tampering that
# goes AROUND it: dropping the triggers, restoring a doctored backup. Every
# case here first proves the tampering SUCCEEDED at the SQL level, so the
# assertion is that the chain detected a real edit rather than that something
# else blocked it.

def _fill(log, n=4):
    for i in range(n):
        log.record(EventType.POLICY_APPROVED, Actor.POLICY_ENGINE,
                   request_id=f"req_{i}", reason="all mandate rules satisfied")


def _unlocked(log):
    """An outside connection with the append-only triggers removed.

    Dropping them is allowed -- that is precisely the gap the chain exists to
    cover, and a test that could not drop them would be testing the triggers
    again instead of the chain. The caller closes it.
    """
    conn = log.db.outside_connection()
    for name in APPEND_ONLY_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name} ON audit_log")
    return conn


def test_a_clean_log_verifies(db):
    log = AuditLog(db)
    _fill(log)
    report = log.verify()
    assert report.intact is True
    assert report.checked == 4
    assert report.broken_at is None


def test_editing_a_row_breaks_the_chain(db):
    log = AuditLog(db)
    _fill(log)
    with _unlocked(log) as conn:
        conn.execute("UPDATE audit_log SET reason = 'nothing to see here' "
                     "WHERE event_id = 2")
        # The edit really landed -- otherwise this would re-test the triggers.
        assert conn.execute(
            "SELECT reason FROM audit_log WHERE event_id = 2"
        ).fetchone()["reason"] == "nothing to see here"

    report = log.verify()
    assert report.intact is False
    assert report.broken_at == 2
    assert "contents were altered" in report.detail


def test_deleting_a_row_breaks_the_chain(db):
    log = AuditLog(db)
    _fill(log)
    with _unlocked(log) as conn:
        conn.execute("DELETE FROM audit_log WHERE event_id = 2")
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 3

    report = log.verify()
    assert report.intact is False
    # Entry 3 is where the break shows: it points at a predecessor that is gone.
    assert report.broken_at == 3
    assert "removed, reordered, or inserted" in report.detail


def test_renumbering_a_row_breaks_the_chain(db):
    """The event_id is inside the hash, so resequencing is detected too."""
    log = AuditLog(db)
    _fill(log)
    with _unlocked(log) as conn:
        conn.execute("UPDATE audit_log SET event_id = 99 WHERE event_id = 4")

    report = log.verify()
    assert report.intact is False
    assert report.broken_at == 99


def test_a_forged_entry_appended_by_hand_is_rejected(db):
    """Inserting a row without recomputing its hash does not pass."""
    log = AuditLog(db)
    _fill(log, 2)
    with _unlocked(log) as conn:
        conn.execute(
            "INSERT INTO audit_log (event_id, event_type, actor, occurred_at, "
            "request_id, details, prev_hash, entry_hash) "
            "VALUES (3, 'POLICY_APPROVED', 'POLICY_ENGINE', 1.0, 'req_forged', "
            "'{}', %s, 'deadbeef')",
            (log.head(),))

    report = log.verify()
    assert report.intact is False
    assert report.broken_at == 3


def test_an_entry_without_a_hash_is_refused_by_the_schema(db):
    """Phase 10 started fresh, so every row is chained from the first.

    The SQLite log allowed NULL hashes for rows written before the chain
    existed, and reported them as unverifiable. There is no such history now,
    so the columns are NOT NULL: an unchained row cannot be written at all,
    even by someone who has dropped the append-only triggers.
    """
    log = AuditLog(db)
    with _unlocked(log) as conn:
        with pytest.raises(psycopg.IntegrityError):
            conn.execute(
                "INSERT INTO audit_log (event_id, event_type, actor, "
                "occurred_at, details, prev_hash, entry_hash) VALUES "
                "(1, 'POLICY_APPROVED', 'POLICY_ENGINE', 1.0, '{}', NULL, NULL)")


def test_the_chain_survives_a_reopen(db):
    """Reopening must continue the chain, not restart it from genesis."""
    log = AuditLog(db)
    _fill(log, 2)
    head_before = log.head()

    reopened = AuditLog(db)
    reopened.record(EventType.POLICY_DENIED, Actor.POLICY_ENGINE,
                    request_id="req_x", rule="AMOUNT_EXCEEDS_CAP")
    assert reopened.verify().intact is True
    assert reopened.verify().checked == 3
    # The new entry linked to the old head rather than to GENESIS.
    with db.outside_connection() as conn:
        assert conn.execute(
            "SELECT prev_hash FROM audit_log WHERE event_id = 3"
        ).fetchone()["prev_hash"] == head_before


@pytest.mark.parametrize("run", range(5))
def test_concurrent_appends_produce_one_unbroken_chain(db, run):
    """Sixteen threads appending at once must not fork the chain.

    Each link names its predecessor, so two writers reading the same tail would
    produce two rows sharing one prev_hash. The fork verifies cleanly from
    either side, which is why this needs its own test rather than being assumed
    from the single-threaded case. Repeated, because a race that happens to
    lose once proves nothing.
    """
    log = AuditLog(db)
    threads_n = 16
    barrier = threading.Barrier(threads_n)
    errors = []

    def append(i):
        barrier.wait()
        try:
            log.record(EventType.POLICY_APPROVED, Actor.POLICY_ENGINE,
                       request_id=f"req_{i}", reason=f"thread {i}")
        except Exception as exc:  # pragma: no cover - surfaced via `errors`
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    report = log.verify()
    assert report.intact is True
    assert report.checked == threads_n

    with db.outside_connection() as conn:
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT prev_hash) AS n FROM audit_log").fetchone()["n"]
    assert distinct == threads_n, "two entries share a predecessor: the chain forked"


def test_an_empty_log_does_not_report_itself_as_protected(db):
    """`intact` is vacuously True with nothing in the log; the summary is not.

    If a UI renders that boolean as "chain intact", it tells a viewer their
    entries are protected when there are none -- the exact overclaim this
    project exists to avoid (JOURNAL.md Entry 21).
    """
    log = AuditLog(db)
    empty = log.verify()
    assert empty.intact is True and empty.checked == 0
    assert empty.summary == "no entries yet"
    assert "intact" not in empty.summary

    log.record(EventType.POLICY_APPROVED, Actor.POLICY_ENGINE, request_id="r")
    assert log.verify().summary == "intact — 1 verified"


def test_a_broken_chain_says_so_in_the_summary(db):
    log = AuditLog(db)
    _fill(log, 3)
    with _unlocked(log) as conn:
        conn.execute("UPDATE audit_log SET reason = 'edited' WHERE event_id = 2")
    assert log.verify().summary == "BROKEN at entry 2"
