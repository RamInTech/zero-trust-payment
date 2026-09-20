"""Phase 9 — the double-entry ledger: where the money actually is.

Every phase before this one proved something about AUTHORIZATION -- whether a
purchase may happen, whether it happens once, whether the decision is
recorded. None of them said where the money went. The audit log describes a
capture ("PAYMENT_CAPTURED, 15000 paise"); it does not keep books.

THE INVARIANT. Every posting is a set of lines whose signed amounts sum to
zero (debit positive, credit negative). So the sum of every line ever written
is also zero, at every instant, checkable by anyone with one SELECT. There is
no balance column anywhere: a balance is `SUM(amount_paise)` over history, so
it cannot drift from the postings that produced it.

Phase 10 (Postgres) moves that invariant from "checked" to "enforced". A
deferred constraint trigger re-adds every posting at COMMIT and refuses the
whole transaction if any posting is unbalanced or missing a line. SQLite could
only check that inside this class and detect a forged posting afterwards with
`verify()`; Postgres refuses it before it exists, even when it is written with
raw SQL. `verify()` remains, for what the database cannot see -- someone who
turned the triggers off.

THREE WAYS REVENUE CAN BE RECOGNISED, AND IT MUST HAPPEN ONCE. A sale is booked
when the gateway sees a success, when reconciliation repairs a divergence (the
provider charged, our record said FAILED), or when an unknown outcome resolves
as executed. Those paths run in different threads and can race. A partial
unique index allows exactly one revenue-recognising posting per idempotency
key, so the database -- not the ordering of calls -- decides.

UNKNOWN OUTCOMES GO TO SUSPENSE. A timeout books the amount into suspense
accounts rather than revenue, so exposure to money that may have moved is a
live number instead of silence. Resolution never edits that posting: it posts
a REVERSAL that points at it (`reverses_txn_id`, unique, so each suspense
posting can be reversed at most once), plus the real sale if it executed.

ORDER-INDEPENDENT BY CONSTRUCTION. The gateway freezes a purchase as pending
and then posts suspense; a reconciler can resolve the key in between. Each
operation gives the same books whichever order the writes land in: a sale
closes every open suspense for its key, a "not executed" resolution always
leaves a marker, and a suspense posting that arrives after either is a no-op.

JOINING THE CALLER'S TRANSACTION. Every write takes an optional `conn`. Given
one, the posting happens inside the caller's transaction instead of its own,
so a purchase record and the posting that mirrors it can commit or fail as a
single unit. That only works when the ledger and the other store share one
`Database`, which is exactly what Phase 10 made possible.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Optional

import psycopg

from zerotrust.db import Database

CLEARING = "assets:razorpay_clearing"
REVENUE = "revenue:sales"
SUSPENSE_CLEARING = "suspense:unverified_clearing"
SUSPENSE_REVENUE = "suspense:unverified_revenue"

#: The whole chart of accounts. A posting naming anything else is refused, in
#: Python and again by a CHECK constraint, so a typo cannot open a new account.
CHART = (CLEARING, REVENUE, SUSPENSE_CLEARING, SUSPENSE_REVENUE)

SALE = "SALE"
UNVERIFIED = "UNVERIFIED"
SUSPENSE_REVERSED = "SUSPENSE_REVERSED"
#: A zero-line marker: this attempt's outcome is settled as NOT executed. It
#: exists so a suspense posting that arrives late can see it and stand down.
RESOLVED = "RESOLVED"
KINDS = (SALE, UNVERIFIED, SUSPENSE_REVERSED, RESOLVED)

_ACCOUNTS_SQL = ", ".join(f"'{a}'" for a in CHART)
_KINDS_SQL = ", ".join(f"'{k}'" for k in KINDS)

#: Every table that must never change after it is written.
IMMUTABLE_TABLES = ("ledger_transactions", "ledger_entries")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ledger_meta (
    name  TEXT PRIMARY KEY,
    value DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    txn_id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ({_KINDS_SQL})),
    scope_key          TEXT NOT NULL,
    agent_id           TEXT,
    attempt            INTEGER NOT NULL,
    request_id         TEXT,
    recognizes_revenue BOOLEAN NOT NULL DEFAULT FALSE,
    -- Unique: a posting can be reversed at most once. NULL for everything
    -- that is not a reversal, and NULLs never collide.
    reverses_txn_id    BIGINT UNIQUE REFERENCES ledger_transactions(txn_id),
    -- Declared up front so a closed posting cannot grow a line later, and so
    -- the commit-time check knows how many lines a posting must have.
    line_count         INTEGER NOT NULL,
    posted_at          DOUBLE PRECISION NOT NULL,
    memo               TEXT,
    CHECK (line_count >= 2 OR (kind = 'RESOLVED' AND line_count = 0))
);

-- THE exactly-once guarantee for revenue, across every path that books a sale.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_one_sale_per_key
    ON ledger_transactions(scope_key) WHERE recognizes_revenue;

-- One suspense posting and one resolution marker per attempt of a key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_marker_per_attempt
    ON ledger_transactions(scope_key, attempt, kind)
    WHERE kind IN ('UNVERIFIED', 'RESOLVED');

CREATE INDEX IF NOT EXISTS idx_ledger_txn_scope ON ledger_transactions(scope_key);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    txn_id       BIGINT NOT NULL REFERENCES ledger_transactions(txn_id),
    account      TEXT NOT NULL CHECK (account IN ({_ACCOUNTS_SQL})),
    -- NUMERIC with an integer CHECK, not BIGINT. Assigning 1.5 to a BIGINT
    -- column in Postgres silently rounds it to 2; NUMERIC keeps the 1.5 so the
    -- CHECK can refuse it. Money must never be rounded without anyone asking.
    amount_paise NUMERIC NOT NULL
                 CHECK (amount_paise = trunc(amount_paise) AND amount_paise <> 0)
);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_txn ON ledger_entries(txn_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_account ON ledger_entries(account);

-- Append-only, enforced by the database, same as the audit log -- including
-- TRUNCATE, which does not fire row triggers.
CREATE OR REPLACE FUNCTION ledger_refuse_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$;

CREATE OR REPLACE FUNCTION ledger_meta_refuse_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger_meta is write-once: % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$;

CREATE OR REPLACE TRIGGER ledger_transactions_no_change
BEFORE UPDATE OR DELETE ON ledger_transactions
FOR EACH ROW EXECUTE FUNCTION ledger_refuse_change();

CREATE OR REPLACE TRIGGER ledger_transactions_no_truncate
BEFORE TRUNCATE ON ledger_transactions
FOR EACH STATEMENT EXECUTE FUNCTION ledger_refuse_change();

CREATE OR REPLACE TRIGGER ledger_entries_no_change
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION ledger_refuse_change();

CREATE OR REPLACE TRIGGER ledger_entries_no_truncate
BEFORE TRUNCATE ON ledger_entries
FOR EACH STATEMENT EXECUTE FUNCTION ledger_refuse_change();

CREATE OR REPLACE TRIGGER ledger_meta_no_change
BEFORE UPDATE OR DELETE ON ledger_meta
FOR EACH ROW EXECUTE FUNCTION ledger_meta_refuse_change();

CREATE OR REPLACE TRIGGER ledger_meta_no_truncate
BEFORE TRUNCATE ON ledger_meta
FOR EACH STATEMENT EXECUTE FUNCTION ledger_meta_refuse_change();

-- A posting takes exactly the lines it declared; the one after that is
-- refused, so a one-sided line cannot be slipped into an old posting.
CREATE OR REPLACE FUNCTION ledger_refuse_extra_line() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    declared INTEGER;
BEGIN
    SELECT line_count INTO declared FROM ledger_transactions
        WHERE txn_id = NEW.txn_id;
    -- No such posting: let the foreign key refuse it with its own message.
    IF declared IS NOT NULL AND
       (SELECT COUNT(*) FROM ledger_entries WHERE txn_id = NEW.txn_id) >= declared THEN
        RAISE EXCEPTION 'ledger transaction % is closed: it already has every line it declared', NEW.txn_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE TRIGGER ledger_entries_closed_transaction
BEFORE INSERT ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION ledger_refuse_extra_line();

-- The commit-time check: every posting touched by the transaction must have
-- all its lines, and they must sum to zero, or the whole transaction fails.
CREATE OR REPLACE FUNCTION ledger_check_posting() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    declared INTEGER;
    found    INTEGER;
    net      NUMERIC;
BEGIN
    SELECT line_count INTO declared FROM ledger_transactions
        WHERE txn_id = NEW.txn_id;
    SELECT COUNT(*), COALESCE(SUM(amount_paise), 0) INTO found, net
        FROM ledger_entries WHERE txn_id = NEW.txn_id;
    IF found <> declared THEN
        RAISE EXCEPTION 'ledger transaction % is incomplete: % of % lines', NEW.txn_id, found, declared
            USING ERRCODE = 'check_violation';
    END IF;
    IF net <> 0 THEN
        RAISE EXCEPTION 'ledger transaction % is unbalanced: its lines net % paise', NEW.txn_id, net
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;

-- CREATE OR REPLACE is not available for constraint triggers, hence the guard.
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'ledger_entries_balanced'
                   AND tgrelid = 'ledger_entries'::regclass) THEN
        CREATE CONSTRAINT TRIGGER ledger_entries_balanced
            AFTER INSERT ON ledger_entries DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ledger_check_posting();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'ledger_transactions_balanced'
                   AND tgrelid = 'ledger_transactions'::regclass) THEN
        -- A header with no lines at all never fires the trigger above.
        CREATE CONSTRAINT TRIGGER ledger_transactions_balanced
            AFTER INSERT ON ledger_transactions DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ledger_check_posting();
    END IF;
END
$do$;
"""


class LedgerError(RuntimeError):
    """Base class for postings the ledger refuses."""


class UnbalancedTransaction(LedgerError):
    """The lines of a posting do not sum to zero."""


class UnknownAccount(LedgerError):
    """A line names an account that is not in the chart."""


class LedgerConflict(LedgerError):
    """A key already has a sale booked, for a DIFFERENT amount.

    Silently keeping the first would hide that two paths disagree about how
    much money moved -- exactly the disagreement books exist to surface.
    """


Line = tuple[str, int]


def require_shared_database(ledger: Optional["Ledger"], db: Database,
                            owner: str) -> None:
    """Refuse a ledger that cannot join `db`'s transactions.

    A purchase record and its posting commit together only when both live in
    one Database. Handing a connection from one schema to a ledger in another
    would not fail -- it would quietly write the posting into the wrong schema.
    """
    if ledger is not None and ledger.db is not db:
        raise ValueError(
            f"{owner}: the ledger and the idempotency store must share one "
            f"Database, so a purchase and its posting commit as one transaction")


def _validate(lines: list[Line], *, allow_empty: bool = False) -> None:
    if not lines:
        if allow_empty:
            return
        raise UnbalancedTransaction("a posting needs at least two lines")
    if len(lines) < 2:
        raise UnbalancedTransaction("a posting needs at least two lines")
    for account, amount in lines:
        if account not in CHART:
            raise UnknownAccount(f"'{account}' is not in the chart of accounts")
        if type(amount) is not int:  # noqa: E721 -- bool is an int subclass; refuse it
            raise UnbalancedTransaction(
                f"amounts are integer paise, got {amount!r} for {account}")
        if amount == 0:
            raise UnbalancedTransaction(f"a zero line on {account} records nothing")
    total = sum(amount for _, amount in lines)
    if total != 0:
        raise UnbalancedTransaction(
            f"lines sum to {total} paise, not zero: debits and credits must match")


def _created_at(record) -> Optional[float]:
    try:
        return record["created_at"]
    except (KeyError, IndexError):
        return None


@dataclass(frozen=True)
class LedgerReport:
    """The result of re-adding every posting. `balanced` is the headline."""

    balanced: bool
    transactions: int
    entries: int
    grand_total: int
    #: (txn_id, net) for every posting whose lines do not sum to zero.
    unbalanced: list = field(default_factory=list)
    #: (txn_id, lines_found, lines_declared) for postings missing lines.
    incomplete: list = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One honest line. An empty ledger is not "balanced" in any sense
        worth displaying -- it has proven nothing yet (JOURNAL.md Entry 21)."""
        if self.unbalanced:
            txn, net = self.unbalanced[0]
            return f"UNBALANCED — transaction {txn} nets {net:+d} paise"
        if self.incomplete:
            txn, found, declared = self.incomplete[0]
            return (f"INCOMPLETE — transaction {txn} has {found} of "
                    f"{declared} lines")
        if self.grand_total != 0:
            return f"UNBALANCED — the ledger nets {self.grand_total:+d} paise"
        if self.transactions == 0:
            return "no postings yet"
        postings = "posting" if self.transactions == 1 else "postings"
        lines = "line" if self.entries == 1 else "lines"
        return (f"balanced — {self.transactions} {postings}, "
                f"{self.entries} {lines}, net 0")

    def as_dict(self) -> dict:
        return {
            "balanced": self.balanced,
            "summary": self.summary,
            "transactions": self.transactions,
            "entries": self.entries,
            "grand_total": self.grand_total,
            "unbalanced": [list(u) for u in self.unbalanced],
            "incomplete": [list(i) for i in self.incomplete],
        }


class Ledger:
    """Append-only double-entry books in one Postgres schema."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self._clock = clock
        db.apply_schema(_SCHEMA)
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO ledger_meta (name, value) VALUES ('started_at', %s) "
                "ON CONFLICT (name) DO NOTHING", (self._clock(),))

    @property
    def started_at(self) -> float:
        """When these books began. Written once; see `predates()`."""
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT value FROM ledger_meta WHERE name = 'started_at'"
            ).fetchone()["value"]

    @contextmanager
    def _tx(self, conn: Optional[psycopg.Connection]) -> Iterator[psycopg.Connection]:
        """Use the caller's transaction if given one, else open our own."""
        if conn is not None:
            yield conn
        else:
            with self.db.transaction() as own:
                yield own

    # -- writing -----------------------------------------------------------

    def _insert(
        self, conn: psycopg.Connection, kind: str, scope_key: str,
        lines: list[Line], *, agent_id: Optional[str], attempt: int,
        request_id: Optional[str], recognizes_revenue: bool = False,
        reverses_txn_id: Optional[int] = None, memo: Optional[str] = None,
    ) -> int:
        """Write one posting inside an open transaction.

        The SUM re-check reads what was actually stored, so a bug between the
        validated list and the INSERTs fails here with a clear message, before
        the commit-time trigger would refuse it anyway.
        """
        _validate(lines, allow_empty=(kind == RESOLVED))
        txn_id = conn.execute(
            "INSERT INTO ledger_transactions (kind, scope_key, agent_id, attempt, "
            "request_id, recognizes_revenue, reverses_txn_id, line_count, "
            "posted_at, memo) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING txn_id",
            (kind, scope_key, agent_id, attempt, request_id, recognizes_revenue,
             reverses_txn_id, len(lines), self._clock(), memo),
        ).fetchone()["txn_id"]
        if lines:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO ledger_entries (txn_id, account, amount_paise) "
                    "VALUES (%s, %s, %s)",
                    [(txn_id, account, amount) for account, amount in lines],
                )
        net = conn.execute(
            "SELECT COALESCE(SUM(amount_paise), 0)::bigint AS net "
            "FROM ledger_entries WHERE txn_id = %s", (txn_id,)).fetchone()["net"]
        if net != 0:
            raise UnbalancedTransaction(
                f"transaction {txn_id} stored lines netting {net} paise")
        return txn_id

    def post(
        self, kind: str, scope_key: str, lines: Iterable[Line], *,
        agent_id: Optional[str] = None, attempt: int = 1,
        request_id: Optional[str] = None, memo: Optional[str] = None,
        conn: Optional[psycopg.Connection] = None,
    ) -> int:
        """Post arbitrary balanced lines. Refuses anything that is not."""
        lines = list(lines)
        if kind not in KINDS:
            raise LedgerError(f"unknown posting kind '{kind}'")
        _validate(lines, allow_empty=(kind == RESOLVED))
        with self._tx(conn) as c:
            return self._insert(c, kind, scope_key, lines, agent_id=agent_id,
                                attempt=attempt, request_id=request_id, memo=memo)

    @staticmethod
    def _open_suspense(conn: psycopg.Connection, scope_key: str,
                       attempt: Optional[int] = None) -> list[dict]:
        sql = ("SELECT t.txn_id, t.attempt FROM ledger_transactions t "
               "WHERE t.scope_key = %s AND t.kind = 'UNVERIFIED' "
               "AND NOT EXISTS (SELECT 1 FROM ledger_transactions r "
               "WHERE r.reverses_txn_id = t.txn_id)")
        params: tuple = (scope_key,)
        if attempt is not None:
            sql += " AND t.attempt = %s"
            params += (attempt,)
        return conn.execute(sql + " ORDER BY t.txn_id", params).fetchall()

    def _reverse(self, conn: psycopg.Connection, txn_id: int, scope_key: str,
                 *, agent_id: Optional[str], attempt: int,
                 request_id: Optional[str], memo: str) -> int:
        """Post the exact mirror of an earlier posting. The amounts are read
        back from that posting, never re-supplied, so a reversal cannot
        disagree with what it reverses."""
        lines = [(row["account"], -row["amount_paise"]) for row in conn.execute(
            "SELECT account, amount_paise::bigint AS amount_paise "
            "FROM ledger_entries WHERE txn_id = %s ORDER BY entry_id", (txn_id,))]
        return self._insert(conn, SUSPENSE_REVERSED, scope_key, lines,
                            agent_id=agent_id, attempt=attempt,
                            request_id=request_id, reverses_txn_id=txn_id,
                            memo=memo)

    @staticmethod
    def _sale(conn: psycopg.Connection, scope_key: str) -> Optional[dict]:
        return conn.execute(
            "SELECT t.txn_id, (SELECT SUM(e.amount_paise)::bigint "
            "FROM ledger_entries e WHERE e.txn_id = t.txn_id "
            "AND e.account = %s) AS amount "
            "FROM ledger_transactions t WHERE t.scope_key = %s "
            "AND t.recognizes_revenue", (CLEARING, scope_key)).fetchone()

    def record_sale(
        self, scope_key: str, amount_paise: int, *, attempt: int,
        agent_id: Optional[str] = None, request_id: Optional[str] = None,
        conn: Optional[psycopg.Connection] = None,
    ) -> int:
        """Recognise revenue for a key -- once, whichever path gets here first.

        Returns the sale's txn_id: the new one, or the existing one if this
        key was already booked for the same amount. Closes every open suspense
        posting for the key in the same transaction.
        """
        _validate([(CLEARING, amount_paise), (REVENUE, -amount_paise)])
        txn_id = None
        with self._tx(conn) as c:
            # One key's books are decided by one writer at a time -- across
            # threads and processes, since advisory locks are database-wide.
            self.db.lock(c, f"ledger:{scope_key}")
            existing = self._sale(c, scope_key)
            if existing is None:
                for row in self._open_suspense(c, scope_key):
                    self._reverse(c, row["txn_id"], scope_key,
                                  agent_id=agent_id, attempt=row["attempt"],
                                  request_id=request_id,
                                  memo="resolved as executed")
                txn_id = self._insert(
                    c, SALE, scope_key,
                    [(CLEARING, amount_paise), (REVENUE, -amount_paise)],
                    agent_id=agent_id, attempt=attempt,
                    request_id=request_id, recognizes_revenue=True)
        # Judged after the transaction is settled, so a conflict is reported as
        # a conflict and never as a failed rollback (JOURNAL.md Entry 29).
        if existing is not None:
            return self._same_amount(existing, scope_key, amount_paise)
        return txn_id

    @staticmethod
    def _same_amount(existing: dict, scope_key: str, amount_paise: int) -> int:
        if existing["amount"] != amount_paise:
            raise LedgerConflict(
                f"'{scope_key}' is already booked as a sale of "
                f"{existing['amount']} paise; refusing to book {amount_paise}")
        return existing["txn_id"]

    def record_unverified(
        self, scope_key: str, amount_paise: int, *, attempt: int,
        agent_id: Optional[str] = None, request_id: Optional[str] = None,
        conn: Optional[psycopg.Connection] = None,
    ) -> Optional[int]:
        """Book an attempt whose outcome is unknown into suspense.

        A no-op (returns None) if the outcome is already settled -- a sale
        exists for the key, or this attempt is marked not executed -- because
        a reconciler may resolve the key before this posting arrives.
        """
        _validate([(SUSPENSE_CLEARING, amount_paise),
                   (SUSPENSE_REVENUE, -amount_paise)])
        with self._tx(conn) as c:
            self.db.lock(c, f"ledger:{scope_key}")
            settled = c.execute(
                "SELECT 1 FROM ledger_transactions WHERE scope_key = %s "
                "AND (recognizes_revenue OR (kind = 'RESOLVED' "
                "AND attempt = %s))", (scope_key, attempt)).fetchone()
            if settled is not None:
                return None
            prior = c.execute(
                "SELECT txn_id FROM ledger_transactions WHERE scope_key = %s "
                "AND kind = 'UNVERIFIED' AND attempt = %s",
                (scope_key, attempt)).fetchone()
            if prior is not None:
                return prior["txn_id"]
            return self._insert(
                c, UNVERIFIED, scope_key,
                [(SUSPENSE_CLEARING, amount_paise),
                 (SUSPENSE_REVENUE, -amount_paise)],
                agent_id=agent_id, attempt=attempt, request_id=request_id,
                memo="outcome unknown")

    def record_not_executed(
        self, scope_key: str, *, attempt: int,
        agent_id: Optional[str] = None, request_id: Optional[str] = None,
        conn: Optional[psycopg.Connection] = None,
    ) -> int:
        """Settle an attempt as never executed: reverse its suspense, if any,
        and always leave a marker so a late suspense posting stands down."""
        with self._tx(conn) as c:
            self.db.lock(c, f"ledger:{scope_key}")
            marker = c.execute(
                "SELECT txn_id FROM ledger_transactions WHERE scope_key = %s "
                "AND kind = 'RESOLVED' AND attempt = %s",
                (scope_key, attempt)).fetchone()
            if marker is not None:
                return marker["txn_id"]
            for row in self._open_suspense(c, scope_key, attempt):
                self._reverse(c, row["txn_id"], scope_key,
                              agent_id=agent_id, attempt=attempt,
                              request_id=request_id,
                              memo="resolved as not executed")
            return self._insert(c, RESOLVED, scope_key, [],
                                agent_id=agent_id, attempt=attempt,
                                request_id=request_id,
                                memo="outcome settled: not executed")

    # -- reading -----------------------------------------------------------

    def trial_balance(self) -> dict[str, int]:
        """Every account's balance, from ONE statement.

        One SELECT is one snapshot. Summing accounts in separate queries could
        straddle a commit and report an imbalance that never existed.
        """
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT account, SUM(amount_paise)::bigint AS total "
                "FROM ledger_entries GROUP BY account").fetchall()
        balances = {account: 0 for account in CHART}
        balances.update({row["account"]: row["total"] for row in rows})
        return balances

    def balance(self, account: str, agent_id: Optional[str] = None) -> int:
        if account not in CHART:
            raise UnknownAccount(f"'{account}' is not in the chart of accounts")
        with self.db.connection() as conn:
            if agent_id is None:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount_paise), 0)::bigint AS total "
                    "FROM ledger_entries WHERE account = %s", (account,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(e.amount_paise), 0)::bigint AS total "
                    "FROM ledger_entries e JOIN ledger_transactions t "
                    "ON t.txn_id = e.txn_id WHERE e.account = %s AND t.agent_id = %s",
                    (account, agent_id)).fetchone()
        return row["total"]

    def exposure(self) -> int:
        """Paise booked as possibly-moved but not yet resolved."""
        return self.balance(SUSPENSE_CLEARING)

    def verify(self) -> LedgerReport:
        """Re-add every posting from what is stored.

        With the triggers in place Postgres refuses an unbalanced posting at
        commit, so this finds nothing. It exists for what the database cannot
        see: postings written while someone had the triggers turned off.
        """
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT t.txn_id, t.line_count, COUNT(e.entry_id) AS found, "
                "COALESCE(SUM(e.amount_paise), 0)::bigint AS net "
                "FROM ledger_transactions t LEFT JOIN ledger_entries e "
                "ON e.txn_id = t.txn_id GROUP BY t.txn_id ORDER BY t.txn_id"
            ).fetchall()
            totals = conn.execute(
                "SELECT COUNT(*) AS entries, "
                "COALESCE(SUM(amount_paise), 0)::bigint AS total "
                "FROM ledger_entries").fetchone()
        unbalanced = [(r["txn_id"], r["net"]) for r in rows if r["net"] != 0]
        incomplete = [(r["txn_id"], r["found"], r["line_count"])
                      for r in rows if r["found"] != r["line_count"]]
        return LedgerReport(
            balanced=not unbalanced and not incomplete and totals["total"] == 0,
            transactions=len(rows), entries=totals["entries"],
            grand_total=totals["total"], unbalanced=unbalanced,
            incomplete=incomplete)

    def recent(self, limit: int = 20) -> list[dict]:
        with self.db.connection() as conn:
            txns = conn.execute(
                "SELECT * FROM ledger_transactions ORDER BY txn_id DESC LIMIT %s",
                (limit,)).fetchall()
            out = []
            for t in txns:
                lines = conn.execute(
                    "SELECT account, amount_paise::bigint AS amount_paise "
                    "FROM ledger_entries WHERE txn_id = %s ORDER BY entry_id",
                    (t["txn_id"],)).fetchall()
                out.append({
                    "txn_id": t["txn_id"], "kind": t["kind"],
                    "scope_key": t["scope_key"], "agent_id": t["agent_id"],
                    "attempt": t["attempt"], "request_id": t["request_id"],
                    "reverses_txn_id": t["reverses_txn_id"],
                    "posted_at": t["posted_at"], "memo": t["memo"],
                    "lines": [{"account": ln["account"],
                               "amount_paise": ln["amount_paise"]} for ln in lines],
                })
        return out

    def discrepancies(self, records: Iterable) -> list[dict]:
        """Where the books and the idempotency store disagree.

        `records` are idempotency rows (`key`, `status`, optionally
        `created_at`). Reported, never repaired here: fixing a gap needs the
        provider's answer, which is the reconciler's job
        (`Reconciler.repair_ledger`).

        A purchase created before the books began is not a missing posting --
        there were no books to post to. It is counted by `predates()` instead.
        """
        records = list(records)
        started = self.started_at
        status = {r["key"]: r["status"] for r in records}
        in_scope = {r["key"]: r["status"] for r in records
                    if not self._predates(r, started)}
        with self.db.connection() as conn:
            sales = {r["scope_key"] for r in conn.execute(
                "SELECT scope_key FROM ledger_transactions WHERE recognizes_revenue")}
            open_suspense = {r["scope_key"] for r in conn.execute(
                "SELECT t.scope_key FROM ledger_transactions t "
                "WHERE t.kind = 'UNVERIFIED' AND NOT EXISTS (SELECT 1 FROM "
                "ledger_transactions r WHERE r.reverses_txn_id = t.txn_id)")}

        found = []
        for key, st in in_scope.items():
            if st == "COMPLETED" and key not in sales:
                found.append({"key": key, "problem": "missing_sale",
                              "detail": "completed purchase with no sale booked"})
            if st == "PENDING_VERIFICATION" and key not in open_suspense:
                found.append({"key": key, "problem": "pending_without_suspense",
                              "detail": "unknown outcome not booked to suspense"})
        for key in sales:
            if status.get(key) != "COMPLETED":
                found.append({"key": key, "problem": "sale_without_completion",
                              "detail": f"sale booked but record is {status.get(key)}"})
        for key in open_suspense:
            if status.get(key) != "PENDING_VERIFICATION":
                found.append({"key": key, "problem": "open_suspense_not_pending",
                              "detail": f"suspense still open but record is {status.get(key)}"})
        return found

    @staticmethod
    def _predates(record, started: float) -> bool:
        created = _created_at(record)
        return created is not None and created < started

    def predates(self, records: Iterable) -> int:
        """How many purchases were created before these books began."""
        started = self.started_at
        return sum(1 for r in records if self._predates(r, started))
