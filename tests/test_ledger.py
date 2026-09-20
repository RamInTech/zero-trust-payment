"""Phase 9 — the double-entry ledger.

The property under test is not "postings get written". It is that the books
cannot be made to disagree with themselves: every posting nets to zero, nothing
already written can be changed, revenue is recognised once per key whichever
path gets there first, and the answer does not depend on the order in which
racing writers land.

Phase 10: raw-SQL attacks arrive through a connection from outside the
application's pool. Where SQLite could only DETECT an unbalanced posting with
`verify()`, Postgres now REFUSES it at commit; `verify()` is exercised by first
dropping the commit-time triggers, the only way such a posting can now exist.
"""

from __future__ import annotations

import threading

import psycopg
import pytest

from zerotrust.ledger import (
    CLEARING,
    REVENUE,
    RESOLVED,
    SALE,
    SUSPENSE_CLEARING,
    SUSPENSE_REVENUE,
    SUSPENSE_REVERSED,
    Ledger,
    LedgerConflict,
    UnbalancedTransaction,
    UnknownAccount,
)

KEY = "agent_1:key-1"

#: The commit-time triggers that refuse unbalanced or incomplete postings.
BALANCE_TRIGGERS = (("ledger_entries_balanced", "ledger_entries"),
                    ("ledger_transactions_balanced", "ledger_transactions"))


@pytest.fixture
def ledger(db):
    return Ledger(db)


def raw(ledger):
    """An autocommit connection from outside the pool. The caller closes it."""
    return ledger.db.outside_connection()


def unguarded(ledger):
    """An outside connection with the commit-time balance check removed --
    what someone with DDL rights could do, and what `verify()` exists for."""
    conn = raw(ledger)
    for trigger, table in BALANCE_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    return conn


def header(conn, scope_key="forged", line_count=2):
    return conn.execute(
        "INSERT INTO ledger_transactions (kind, scope_key, attempt, line_count, "
        "posted_at) VALUES ('SALE', %s, 1, %s, 0) RETURNING txn_id",
        (scope_key, line_count)).fetchone()["txn_id"]


def line(conn, txn_id, account, amount):
    conn.execute("INSERT INTO ledger_entries (txn_id, account, amount_paise) "
                 "VALUES (%s, %s, %s)", (txn_id, account, amount))


def kinds(ledger):
    return [t["kind"] for t in reversed(ledger.recent(100))]


# -- 1. a posting must balance ---------------------------------------------

def test_a_balanced_posting_is_recorded(ledger):
    ledger.post(SALE, KEY, [(CLEARING, 15_000), (REVENUE, -15_000)])
    assert ledger.trial_balance()[CLEARING] == 15_000
    assert ledger.trial_balance()[REVENUE] == -15_000


def test_an_unbalanced_posting_is_refused_and_nothing_is_written(ledger):
    with pytest.raises(UnbalancedTransaction):
        ledger.post(SALE, KEY, [(CLEARING, 15_000), (REVENUE, -14_000)])
    assert ledger.verify().transactions == 0
    assert sum(ledger.trial_balance().values()) == 0


def test_a_single_line_posting_is_refused(ledger):
    with pytest.raises(UnbalancedTransaction):
        ledger.post(SALE, KEY, [(CLEARING, 0)])


def test_an_unknown_account_is_refused(ledger):
    with pytest.raises(UnknownAccount):
        ledger.post(SALE, KEY, [("assets:somewhere_else", 100), (REVENUE, -100)])


def test_a_non_integer_amount_is_refused(ledger):
    with pytest.raises(UnbalancedTransaction):
        ledger.post(SALE, KEY, [(CLEARING, 1.5), (REVENUE, -1.5)])


@pytest.mark.parametrize("account,amount", [
    (CLEARING, 1.5),            # would be silently rounded by a BIGINT column
    (CLEARING, 0),
    ("assets:nowhere", 100),
])
def test_the_database_itself_refuses_a_bad_line(ledger, account, amount):
    """The CHECKs hold for writers that never went through this class."""
    with raw(ledger) as conn:
        with pytest.raises(psycopg.IntegrityError):
            with conn.transaction():
                txn = header(conn)
                line(conn, txn, account, amount)
    assert ledger.verify().transactions == 0


def test_there_is_no_balance_column_to_drift(ledger):
    """Balances are SUM over history. A stored balance can disagree with the
    postings that produced it; a derived one cannot."""
    with raw(ledger) as conn:
        columns = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name IN ('ledger_transactions', 'ledger_entries')")}
    assert columns, "read no columns -- the check would pass vacuously"
    assert not any("balance" in c for c in columns)


# -- 2. nothing written can change -----------------------------------------

@pytest.mark.parametrize("sql", [
    "UPDATE ledger_entries SET amount_paise = 1",
    "DELETE FROM ledger_entries",
    "TRUNCATE ledger_entries",
    "UPDATE ledger_transactions SET memo = 'nothing to see here'",
    "DELETE FROM ledger_transactions",
    "TRUNCATE ledger_transactions CASCADE",
])
def test_updates_deletes_and_truncates_are_blocked_by_the_database(ledger, sql):
    ledger.record_sale(KEY, 15_000, attempt=1)
    with raw(ledger) as conn:
        with pytest.raises(psycopg.IntegrityError, match="append-only"):
            conn.execute(sql)
    assert ledger.verify().balanced
    assert ledger.trial_balance()[REVENUE] == -15_000


def test_a_line_cannot_be_appended_to_a_closed_transaction(ledger):
    """UPDATE/DELETE triggers alone would allow this: a one-sided INSERT into
    an old, balanced posting."""
    txn = ledger.record_sale(KEY, 15_000, attempt=1)
    with raw(ledger) as conn:
        with pytest.raises(psycopg.IntegrityError, match="closed"):
            line(conn, txn, CLEARING, 999)


def test_a_line_for_a_transaction_that_does_not_exist_is_refused(ledger):
    with raw(ledger) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            line(conn, 4242, CLEARING, 100)


# -- 3. an unbalanced posting is refused at COMMIT ---------------------------

def test_a_raw_unbalanced_posting_is_refused_at_commit(ledger):
    """Each line on its own is valid; together they net +100. Nothing in the
    statements is wrong, so only a check over the whole posting can see it."""
    ledger.record_sale(KEY, 15_000, attempt=1)
    with raw(ledger) as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="unbalanced"):
            with conn.transaction():
                txn = header(conn)
                line(conn, txn, CLEARING, 500)
                line(conn, txn, REVENUE, -400)
    report = ledger.verify()
    assert report.balanced and report.transactions == 1


def test_a_raw_posting_missing_a_line_is_refused_at_commit(ledger):
    with raw(ledger) as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="incomplete"):
            with conn.transaction():
                txn = header(conn)
                line(conn, txn, CLEARING, 500)
    assert ledger.verify().transactions == 0


def test_a_raw_header_with_no_lines_is_refused_at_commit(ledger):
    """A header alone fires no trigger on ledger_entries, which is why the
    check is attached to both tables."""
    with raw(ledger) as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="incomplete"):
            header(conn)  # autocommit: this statement is the whole transaction
    assert ledger.verify().transactions == 0


def test_the_balance_check_is_deferred_so_lines_can_arrive_one_at_a_time(ledger):
    """After the first line a posting is necessarily unbalanced. A check that
    ran per statement would make every posting impossible to write."""
    with raw(ledger) as conn:
        with conn.transaction():
            txn = header(conn, scope_key=KEY)
            line(conn, txn, CLEARING, 700)
            line(conn, txn, REVENUE, -700)
    assert ledger.verify().balanced


# -- 4. what the database no longer sees, verify() still catches --------------

def test_with_the_check_removed_an_unbalanced_posting_is_named_by_verify(ledger):
    ledger.record_sale(KEY, 15_000, attempt=1)
    with unguarded(ledger) as conn:
        with conn.transaction():
            forged = header(conn)
            line(conn, forged, CLEARING, 500)
            line(conn, forged, REVENUE, -400)

    report = ledger.verify()
    assert not report.balanced
    assert report.unbalanced == [(forged, 100)]
    assert report.summary.startswith("UNBALANCED")
    assert str(forged) in report.summary


def test_with_the_check_removed_a_missing_line_is_named_by_verify(ledger):
    with unguarded(ledger) as conn:
        with conn.transaction():
            half = header(conn, scope_key="half")
            line(conn, half, CLEARING, 500)

    report = ledger.verify()
    assert not report.balanced
    assert (half, 1, 2) in report.incomplete


def test_an_empty_ledger_does_not_claim_to_be_balanced(ledger):
    """Same lesson as the hash chain (JOURNAL.md Entry 21): nothing broken is
    not the same as something proven."""
    summary = ledger.verify().summary
    assert summary == "no postings yet"
    assert "balanced" not in summary


# -- 5. revenue is recognised once -----------------------------------------

def test_a_sale_books_clearing_against_revenue(ledger):
    ledger.record_sale(KEY, 15_000, attempt=1, agent_id="agent_1")
    tb = ledger.trial_balance()
    assert tb[CLEARING] == 15_000 and tb[REVENUE] == -15_000
    assert type(tb[REVENUE]) is int, "NUMERIC sums must come back as int paise"
    assert ledger.balance(REVENUE, agent_id="agent_1") == -15_000
    assert ledger.balance(REVENUE, agent_id="someone_else") == 0


def test_booking_the_same_sale_twice_books_it_once(ledger):
    first = ledger.record_sale(KEY, 15_000, attempt=1)
    second = ledger.record_sale(KEY, 15_000, attempt=2)
    assert first == second
    assert ledger.trial_balance()[REVENUE] == -15_000


def test_a_second_sale_for_a_different_amount_is_a_conflict(ledger):
    ledger.record_sale(KEY, 15_000, attempt=1)
    with pytest.raises(LedgerConflict):
        ledger.record_sale(KEY, 90_000, attempt=1)
    assert ledger.trial_balance()[REVENUE] == -15_000


def test_a_second_sale_is_refused_by_the_database_even_by_raw_sql(ledger):
    ledger.record_sale(KEY, 15_000, attempt=1)
    with raw(ledger) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                txn = conn.execute(
                    "INSERT INTO ledger_transactions (kind, scope_key, attempt, "
                    "recognizes_revenue, line_count, posted_at) "
                    "VALUES ('SALE', %s, 2, TRUE, 2, 0) RETURNING txn_id",
                    (KEY,)).fetchone()["txn_id"]
                line(conn, txn, CLEARING, 15_000)
                line(conn, txn, REVENUE, -15_000)


@pytest.mark.parametrize("run", range(5))
def test_concurrent_sales_for_one_key_recognise_revenue_once(ledger, run):
    threads_n = 16
    barrier = threading.Barrier(threads_n)
    ids, errors = [], []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            txn = ledger.record_sale(KEY, 15_000, attempt=1)
            with lock:
                ids.append(txn)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(set(ids)) == 1
    assert ledger.trial_balance()[REVENUE] == -15_000
    assert kinds(ledger).count(SALE) == 1


# -- 6. unknown outcomes: suspense, then reversal ---------------------------

def test_an_unknown_outcome_is_booked_to_suspense_not_revenue(ledger):
    ledger.record_unverified(KEY, 15_000, attempt=1)
    tb = ledger.trial_balance()
    assert tb[SUSPENSE_CLEARING] == 15_000 and tb[SUSPENSE_REVENUE] == -15_000
    assert tb[REVENUE] == 0
    assert ledger.exposure() == 15_000


def test_resolving_as_executed_reverses_suspense_and_books_the_sale(ledger):
    unverified = ledger.record_unverified(KEY, 15_000, attempt=1)
    ledger.record_sale(KEY, 15_000, attempt=1)

    tb = ledger.trial_balance()
    assert tb[SUSPENSE_CLEARING] == 0 and tb[SUSPENSE_REVENUE] == 0
    assert tb[REVENUE] == -15_000
    reversal = next(t for t in ledger.recent() if t["kind"] == SUSPENSE_REVERSED)
    assert reversal["reverses_txn_id"] == unverified


def test_resolving_as_not_executed_reverses_suspense_and_books_nothing(ledger):
    ledger.record_unverified(KEY, 15_000, attempt=1)
    ledger.record_not_executed(KEY, attempt=1)

    tb = ledger.trial_balance()
    assert all(v == 0 for v in tb.values())
    assert kinds(ledger) == ["UNVERIFIED", SUSPENSE_REVERSED, RESOLVED]


def test_resolving_twice_reverses_once(ledger):
    ledger.record_unverified(KEY, 15_000, attempt=1)
    first = ledger.record_not_executed(KEY, attempt=1)
    second = ledger.record_not_executed(KEY, attempt=1)
    assert first == second
    assert kinds(ledger).count(SUSPENSE_REVERSED) == 1


def test_a_posting_can_be_reversed_at_most_once_even_by_raw_sql(ledger):
    unverified = ledger.record_unverified(KEY, 15_000, attempt=1)
    ledger.record_not_executed(KEY, attempt=1)
    with raw(ledger) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO ledger_transactions (kind, scope_key, attempt, "
                "reverses_txn_id, line_count, posted_at) "
                "VALUES ('SUSPENSE_REVERSED', %s, 1, %s, 2, 0)", (KEY, unverified))


# -- 7. the answer does not depend on who lands first -----------------------

def test_a_resolution_that_lands_before_the_suspense_posting_wins(ledger):
    """The gateway froze the record, then a reconciler settled it as not
    executed before the gateway got to post suspense."""
    ledger.record_not_executed(KEY, attempt=1)
    assert ledger.record_unverified(KEY, 15_000, attempt=1) is None
    assert ledger.exposure() == 0
    assert ledger.verify().balanced


def test_a_sale_that_lands_before_the_suspense_posting_wins(ledger):
    ledger.record_sale(KEY, 15_000, attempt=1)
    assert ledger.record_unverified(KEY, 15_000, attempt=1) is None
    assert ledger.exposure() == 0
    assert ledger.trial_balance()[REVENUE] == -15_000


def test_a_later_attempt_can_go_to_suspense_after_an_earlier_one_settled(ledger):
    ledger.record_unverified(KEY, 15_000, attempt=1)
    ledger.record_not_executed(KEY, attempt=1)
    assert ledger.record_unverified(KEY, 15_000, attempt=2) is not None
    assert ledger.exposure() == 15_000


def test_a_sale_closes_open_suspense_from_every_attempt(ledger):
    ledger.record_unverified(KEY, 15_000, attempt=1)
    ledger.record_unverified(KEY, 15_000, attempt=2)
    ledger.record_sale(KEY, 15_000, attempt=2)
    assert ledger.exposure() == 0
    assert ledger.trial_balance()[REVENUE] == -15_000


@pytest.mark.parametrize("run", range(5))
def test_concurrent_mixed_postings_balance_on_every_read(ledger, run):
    """Writers race across keys and kinds while a reader re-adds the books.
    Every read must net to zero -- a posting is visible whole or not at all."""
    keys = [f"agent_1:k{i}" for i in range(40)]
    stop = threading.Event()
    reads, bad, errors = [0], [], []

    def reader():
        while not stop.is_set():
            total = sum(ledger.trial_balance().values())
            reads[0] += 1
            if total != 0:
                bad.append(total)

    def writer(key, i):
        try:
            if i % 3 == 0:
                ledger.record_unverified(key, 1_000 + i, attempt=1)
                ledger.record_sale(key, 1_000 + i, attempt=1)
            elif i % 3 == 1:
                ledger.record_unverified(key, 1_000 + i, attempt=1)
                ledger.record_not_executed(key, attempt=1)
            else:
                ledger.record_not_executed(key, attempt=1)
                ledger.record_unverified(key, 1_000 + i, attempt=1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    r = threading.Thread(target=reader)
    r.start()
    writers = [threading.Thread(target=writer, args=(k, i)) for i, k in enumerate(keys)]
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    stop.set()
    r.join()

    assert errors == []
    assert bad == []
    assert reads[0] > 0
    assert ledger.exposure() == 0
    expected = sum(1_000 + i for i in range(len(keys)) if i % 3 == 0)
    assert ledger.trial_balance()[REVENUE] == -expected
    assert ledger.verify().balanced


# -- 8. joining the caller's transaction ------------------------------------

def test_a_posting_inside_a_transaction_that_rolls_back_is_never_written(ledger):
    """The mechanism behind one-transaction purchases: the posting commits or
    fails with whatever else the caller wrote."""
    with pytest.raises(RuntimeError):
        with ledger.db.transaction() as conn:
            ledger.record_sale(KEY, 15_000, attempt=1, conn=conn)
            raise RuntimeError("the purchase record failed to save")
    assert ledger.verify().transactions == 0
    assert ledger.record_sale(KEY, 15_000, attempt=1) is not None


# -- 9. the books against the idempotency store -----------------------------

def test_discrepancies_name_every_kind_of_disagreement(ledger):
    ledger.record_sale("a:completed-with-sale", 100, attempt=1)
    ledger.record_sale("a:sale-but-failed", 100, attempt=1)
    ledger.record_unverified("a:pending-with-suspense", 100, attempt=1)
    ledger.record_unverified("a:suspense-but-completed", 100, attempt=1)
    records = [
        {"key": "a:completed-with-sale", "status": "COMPLETED"},
        {"key": "a:completed-no-sale", "status": "COMPLETED"},
        {"key": "a:sale-but-failed", "status": "FAILED"},
        {"key": "a:pending-with-suspense", "status": "PENDING_VERIFICATION"},
        {"key": "a:pending-no-suspense", "status": "PENDING_VERIFICATION"},
        {"key": "a:suspense-but-completed", "status": "COMPLETED"},
    ]
    found = {(d["key"], d["problem"]) for d in ledger.discrepancies(records)}
    assert found == {
        ("a:completed-no-sale", "missing_sale"),
        ("a:pending-no-suspense", "pending_without_suspense"),
        ("a:sale-but-failed", "sale_without_completion"),
        ("a:suspense-but-completed", "missing_sale"),
        ("a:suspense-but-completed", "open_suspense_not_pending"),
    }


def test_agreeing_books_report_no_discrepancies(ledger):
    ledger.record_sale("a:k1", 100, attempt=1)
    assert ledger.discrepancies([{"key": "a:k1", "status": "COMPLETED"}]) == []


# -- 10. history from before the books began --------------------------------

def test_purchases_from_before_the_books_are_counted_not_reported(db):
    """Found against the real demo database: 56 purchases predated the ledger,
    and reporting each as a missing sale sent the repair pass to query the
    live provider about all of them. There were no books to post them to."""
    ledger = Ledger(db, clock=lambda: 1_000.0)
    records = [
        {"key": "a:old", "status": "COMPLETED", "created_at": 999.0},
        {"key": "a:new", "status": "COMPLETED", "created_at": 1_000.0},
    ]
    assert [d["key"] for d in ledger.discrepancies(records)] == ["a:new"]
    assert ledger.predates(records) == 1


def test_reopening_the_books_never_moves_when_they_began(db):
    Ledger(db, clock=lambda: 1_000.0)
    assert Ledger(db, clock=lambda: 5_000.0).started_at == 1_000.0


@pytest.mark.parametrize("sql", [
    "UPDATE ledger_meta SET value = 0",
    "DELETE FROM ledger_meta",
    "TRUNCATE ledger_meta",
])
def test_when_the_books_began_cannot_be_rewritten(ledger, sql):
    with raw(ledger) as conn:
        with pytest.raises(psycopg.IntegrityError, match="write-once"):
            conn.execute(sql)
