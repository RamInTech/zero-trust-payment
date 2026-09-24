"""Phase 11 — the chaos harness: the same guarantees, under real failure.

Phase 7 proved the system survives a fault fired at a chosen line. That is
necessary and not sufficient. A fault I chose lands where I expected it to,
which means it can only ever test the interleavings I already thought of. The
failures that matter in production land where nobody chose: between the
provider charging and the completion write, inside a transaction that is about
to commit, or in a process that stops existing mid-statement.

So this harness does not call the code at known-dangerous points. It runs many
ordinary purchases and breaks things *underneath* them, at random, seeded so any
failure replays exactly:

  LATENCY                     slow provider calls, widening every window
  TIMEOUT_BEFORE_CALL         the provider never heard the request
  TIMEOUT_AFTER_CALL          it charged, and said nothing back
  CRASH_AFTER_CALL            an exception between the charge and the record
  DB_BACKEND_KILL             pg_terminate_backend on a live connection, fired
                              immediately after a charge, so the completion
                              transaction is the likely victim
  PROCESS_KILL_AFTER_CHARGE   a real subprocess SIGKILLed after the money moved
  PROCESS_KILL_BEFORE_CHARGE  the same, before it moved

The last two are the reason this module exists. A thread cannot be killed, and
an exception is not death: `finally` blocks still run, `except` still records
the failure. A SIGKILLed subprocess leaves exactly what a crashed server
leaves -- a claimed key, possibly a charge, and no completion, with nothing
cleaned up. That is the one case Phase 10 could not test (JOURNAL.md Entry 34).

THE PROVIDER LIVES IN POSTGRES. A killed process takes an in-memory
`SimulatedProvider` with it, and then nobody can tell whether it charged --
which would make every invariant unfalsifiable. `ChaosProvider` writes orders
to a table with its own autocommit connection, so a charge survives the death
of whatever made it, exactly like a real provider's.

WHAT IS CHECKED, AFTER EVERY ROUND IS RECONCILED. Each is a failure, not a
warning, and the seed is printed so it can be replayed:

  1. no receipt is charged twice;
  2. revenue booked equals the money the provider actually took;
  3. nothing is left in suspense;
  4. the books and the purchase records do not disagree (`discrepancies()`);
  5. the ledger re-adds to zero, and every mid-run read did too;
  6. the audit chain verifies;
  7. a record is COMPLETED if and only if its receipt was charged exactly once;
  8. no key is left in flight once it has been retried.

RETRYING THE DEAD (Phase 12). A killed claimant leaves its key PROCESSING. Once
the staleness timeout passes, every such key is retried through the same
gateway -- and the gateway now asks the provider before re-running a stale key.
A claimant that charged before it died is RECOVERED from its order; one that
died before charging is RECLAIMED and charged once. Before Phase 12 this retry
charged the first kind a second time, which is why `verify_before_reclaim=False`
exists: with it, the same rounds fail invariant 1, and a test asserts they do.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import psycopg

from zerotrust.audit import AuditLog
from zerotrust.db import DEFAULT_DSN, Database
from zerotrust.faults import InjectedCrash
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import (
    COMPLETED,
    FAILED,
    PROCESSING,
    IdempotencyStore,
    Outcome,
)
from zerotrust.ledger import (
    REVENUE,
    SUSPENSE_CLEARING,
    SUSPENSE_REVENUE,
    Ledger,
)
from zerotrust.mandate import ANY_SKU, Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest
from zerotrust.provider import ProviderTimeout
from zerotrust.reconcile import Reconciler, _unscope

AGENT = "agent_chaos"
SKU = "SKU-CHAOS"
HOUR = 3600.0

OK = "OK"
LATENCY = "LATENCY"
TIMEOUT_BEFORE = "TIMEOUT_BEFORE_CALL"
TIMEOUT_AFTER = "TIMEOUT_AFTER_CALL"
CRASH_AFTER = "CRASH_AFTER_CALL"
BACKEND_KILL = "DB_BACKEND_KILL"
PROCESS_KILL_AFTER = "PROCESS_KILL_AFTER_CHARGE"
PROCESS_KILL_BEFORE = "PROCESS_KILL_BEFORE_CHARGE"

#: In-process faults and their weights. Process kills are scheduled separately,
#: because each one costs an interpreter start.
IN_PROCESS_FAULTS = (
    (OK, 30), (LATENCY, 14), (TIMEOUT_BEFORE, 12),
    (TIMEOUT_AFTER, 14), (CRASH_AFTER, 14), (BACKEND_KILL, 16),
)

#: Short, so a killed claimant's key goes stale within the round and its retry
#: exercises the verify-before-reclaim path for real. Nothing retries a key
#: while its purchase is still running, so this cannot reclaim a live claim.
ROUND_STALE_AFTER_SECONDS = 0.2

_PROVIDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS chaos_provider_orders (
    order_no   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    amount     BIGINT NOT NULL,
    currency   TEXT NOT NULL,
    receipt    TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chaos_orders_receipt
    ON chaos_provider_orders(receipt);
"""


def receipt_for_key(scoped_or_key: str) -> str:
    """One receipt per idempotency key, so reconciliation can find the order."""
    _agent, key = _unscope(scoped_or_key) if ":" in scoped_or_key else (None, scoped_or_key)
    return f"chaos_{key}"


class ChaosProvider:
    """A provider whose orders outlive the process that created them.

    Same interface as `SimulatedProvider`, but backed by a table. A charge is
    committed by its own statement, so killing the caller a microsecond later
    leaves the charge standing -- which is the entire point: the harness has to
    be able to ask "did the money actually move?" after the asker is dead.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        db.apply_schema(_PROVIDER_SCHEMA)

    @staticmethod
    def _order(row: dict) -> dict:
        return {
            "id": f"order_CHAOS{row['order_no']:012d}",
            "entity": "order",
            "amount": row["amount"],
            "amount_paid": 0,
            "amount_due": row["amount"],
            "currency": row["currency"],
            "receipt": row["receipt"],
            "status": row["status"],
            "created_at": int(row["created_at"]),
            "simulated": True,
        }

    def create_order(self, amount_paise: int, currency: str = "INR",
                     receipt: str = "") -> dict:
        if amount_paise <= 0:
            raise ValueError("amount_paise must be positive")
        with self.db.connection() as conn:
            row = conn.execute(
                "INSERT INTO chaos_provider_orders "
                "(amount, currency, receipt, status, created_at) "
                "VALUES (%s, %s, %s, 'created', %s) RETURNING *",
                (amount_paise, currency, receipt, time.time()),
            ).fetchone()
        return self._order(row)

    def orders_for_receipt(self, receipt: str) -> list[dict]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chaos_provider_orders WHERE receipt = %s "
                "ORDER BY order_no", (receipt,)).fetchall()
        return [self._order(r) for r in rows]

    def all_orders(self) -> list[dict]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chaos_provider_orders ORDER BY order_no").fetchall()
        return [self._order(r) for r in rows]

    def simulate_capture(self, order_id: str, amount_paise: int) -> dict:
        from zerotrust.provider import _simulated_capture
        return _simulated_capture(order_id, amount_paise)


@dataclass
class RoundResult:
    """One round: what was injected, what survived, and what broke."""

    seed: int
    purchases: int
    faults: dict = field(default_factory=dict)
    backend_kills: int = 0
    process_kills: int = 0
    charged_orders: int = 0
    charged_paise: int = 0
    revenue_paise: int = 0
    repairs: int = 0
    stalled_retried: int = 0
    recovered: int = 0
    reclaimed: int = 0
    in_flight_records: int = 0
    mid_run_reads: int = 0
    violations: list = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def survived(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict:
        return {**self.__dict__, "survived": self.survived}


@dataclass
class ChaosReport:
    rounds: list
    purchases_per_round: int

    @property
    def survived(self) -> int:
        return sum(1 for r in self.rounds if r.survived)

    @property
    def survived_all(self) -> bool:
        return all(r.survived for r in self.rounds)

    @property
    def survival_rate(self) -> float:
        return self.survived / len(self.rounds) if self.rounds else 0.0

    @property
    def totals(self) -> dict:
        faults: Counter = Counter()
        for r in self.rounds:
            faults.update(r.faults)
        return {
            "rounds": len(self.rounds),
            "survived": self.survived,
            "purchases": sum(r.purchases for r in self.rounds),
            "charged_orders": sum(r.charged_orders for r in self.rounds),
            "charged_paise": sum(r.charged_paise for r in self.rounds),
            "revenue_paise": sum(r.revenue_paise for r in self.rounds),
            "backend_kills": sum(r.backend_kills for r in self.rounds),
            "process_kills": sum(r.process_kills for r in self.rounds),
            "repairs": sum(r.repairs for r in self.rounds),
            "stalled_retried": sum(r.stalled_retried for r in self.rounds),
            "recovered": sum(r.recovered for r in self.rounds),
            "reclaimed": sum(r.reclaimed for r in self.rounds),
            "in_flight_records": sum(r.in_flight_records for r in self.rounds),
            "mid_run_reads": sum(r.mid_run_reads for r in self.rounds),
            "faults": dict(sorted(faults.items())),
        }

    @property
    def failures(self) -> list:
        return [r for r in self.rounds if not r.survived]

    @property
    def summary(self) -> str:
        if not self.rounds:
            return "no rounds run"
        return (f"{self.survived}/{len(self.rounds)} rounds stayed correct "
                f"under injected failure")

    def to_json(self) -> str:
        return json.dumps({
            "summary": self.summary,
            "survival_rate": self.survival_rate,
            "totals": self.totals,
            "rounds": [r.as_dict() for r in self.rounds],
        }, indent=2)

    def to_markdown(self) -> str:
        t = self.totals
        lines = [
            "<!-- GENERATED FILE — do not edit by hand.",
            "     Regenerate with: uv run python scripts/run_chaos_harness.py -->",
            "",
            "# Chaos harness results",
            "",
            f"**{self.summary}.** Every round runs real purchases through the real "
            "gateway while failures are injected underneath them: slow and timing-out "
            "provider calls, crashes between the charge and the record, Postgres "
            "backends terminated mid-transaction, and processes SIGKILLed after the "
            "money moved. Each round then reconciles and checks that the money still "
            "adds up.",
            "",
            "| Measure | Value |",
            "|---|---|",
            f"| Rounds | {t['rounds']} |",
            f"| Rounds that stayed correct | {t['survived']} |",
            f"| Purchases attempted | {t['purchases']} |",
            f"| Orders the provider actually charged | {t['charged_orders']} |",
            f"| Money charged | ₹{t['charged_paise'] / 100:,.2f} |",
            f"| Revenue booked after reconciliation | ₹{t['revenue_paise'] / 100:,.2f} |",
            f"| Database backends killed mid-flight | {t['backend_kills']} |",
            f"| Processes SIGKILLed | {t['process_kills']} |",
            f"| Divergences repaired by reconciliation | {t['repairs']} |",
            f"| Stalled keys retried after the staleness timeout | {t['stalled_retried']} |",
            f"| ...recovered from an order the dead claimant made | {t['recovered']} |",
            f"| ...reclaimed and charged once | {t['reclaimed']} |",
            f"| Keys still in flight at the end | {t['in_flight_records']} |",
            f"| Mid-run reads of the books, all netting zero | {t['mid_run_reads']} |",
            "",
            "## Faults injected",
            "",
            "| Fault | Times |",
            "|---|---|",
        ]
        for name, count in t["faults"].items():
            lines.append(f"| `{name}` | {count} |")
        lines += [
            "",
            "## What each round asserts",
            "",
            "1. No receipt is charged twice.",
            "2. Revenue booked equals what the provider actually charged.",
            "3. Nothing is left in suspense.",
            "4. The books and the purchase records do not disagree.",
            "5. The ledger re-adds to zero, and so did every mid-run read.",
            "6. The audit hash chain verifies.",
            "7. A record is COMPLETED if and only if its receipt was charged once.",
            "8. No key is left in flight once it has been retried.",
            "",
            "## Rounds",
            "",
            "| Seed | Purchases | Charged | Repairs | In flight | Result |",
            "|---|---|---|---|---|---|",
        ]
        for r in self.rounds:
            verdict = "SURVIVED" if r.survived else f"FAILED: {r.violations[0]}"
            lines.append(
                f"| `{r.seed}` | {r.purchases} | {r.charged_orders} | "
                f"{r.repairs} | {r.in_flight_records} | {verdict} |")
        if self.failures:
            lines += ["", "## Failures", ""]
            for r in self.failures:
                lines.append(f"**Seed `{r.seed}`** — replay with "
                             f"`CHAOS_SEED={r.seed} uv run python "
                             f"scripts/run_chaos_harness.py`")
                for v in r.violations:
                    lines.append(f"- {v}")
                lines.append("")
        return "\n".join(lines) + "\n"


class ChaosHarness:
    """Runs rounds of purchases with failures injected underneath them."""

    def __init__(
        self,
        dsn: str = DEFAULT_DSN,
        *,
        rounds: int = 5,
        purchases: int = 16,
        threads: int = 8,
        process_kills: int = 2,
        seed: Optional[int] = None,
        book_sales: bool = True,
        inject_double_charge: bool = False,
        verify_before_reclaim: bool = True,
    ) -> None:
        self.dsn = dsn
        self.rounds = rounds
        self.purchases = purchases
        self.threads = threads
        self.process_kills = process_kills
        self.seed = seed if seed is not None else random.SystemRandom().randrange(1 << 30)
        #: Off only in the test showing what reconciliation can repair: a
        #: gateway with no ledger charges money and books nothing.
        self.book_sales = book_sales
        #: On only in the test that proves this harness can actually fail.
        #: Charging one receipt twice is a money error nothing downstream can
        #: undo -- reconciliation refuses to choose between the two orders --
        #: so a harness that reported it as a survival would be worthless.
        self.inject_double_charge = inject_double_charge
        #: Off only in the test showing what Phase 12 prevents: retrying a
        #: killed claimant without asking the provider charges it again.
        self.verify_before_reclaim = verify_before_reclaim

    # -- running -----------------------------------------------------------

    def run(self) -> ChaosReport:
        rng = random.Random(self.seed)
        seeds = [rng.randrange(1 << 30) for _ in range(self.rounds)]
        return ChaosReport([self.run_round(s) for s in seeds],
                           purchases_per_round=self.purchases)

    def run_round(self, seed: int) -> RoundResult:
        started = time.time()
        rng = random.Random(seed)
        db = Database.create_temporary(self.dsn, "chaos")
        result = RoundResult(seed=seed, purchases=self.purchases)
        try:
            self._run_round_on(db, rng, result)
        finally:
            result.duration_seconds = round(time.time() - started, 2)
            db.drop()
        return result

    def _plan(self, rng: random.Random) -> dict:
        names = [n for n, _ in IN_PROCESS_FAULTS]
        weights = [w for _, w in IN_PROCESS_FAULTS]
        plan = {f"k-{i:04d}": rng.choices(names, weights)[0]
                for i in range(self.purchases)}
        # Process kills are assigned, not drawn: each costs an interpreter
        # start, so a round must not be able to draw twenty of them.
        killable = rng.sample(sorted(plan), min(self.process_kills, len(plan)))
        for i, key in enumerate(killable):
            plan[key] = PROCESS_KILL_AFTER if i % 2 == 0 else PROCESS_KILL_BEFORE
        return plan

    def _run_round_on(self, db: Database, rng: random.Random,
                      result: RoundResult) -> None:
        audit = AuditLog(db)
        engine = PolicyEngine(MandateStore(db))
        engine.mandates.issue(Mandate(
            agent_id=AGENT, max_amount_paise=10_000_000,
            allowed_skus=frozenset({ANY_SKU}), expires_at=time.time() + HOUR,
            velocity_limit=10_000, velocity_window_secs=HOUR,
            cooldown_denials=0))
        store = IdempotencyStore(db, stale_after_seconds=ROUND_STALE_AFTER_SECONDS)
        ledger = Ledger(db)
        provider = ChaosProvider(db)

        plan = self._plan(rng)
        amounts = {key: rng.randrange(100, 50_000) for key in plan}
        double_charged = next((k for k in sorted(plan) if plan[k] == OK),
                              sorted(plan)[0]) if self.inject_double_charge else None
        result.faults = dict(sorted(Counter(plan.values()).items()))
        kills = threading.Lock()

        calm = threading.Event()   # set once retries begin: no more faults

        def execute(request: PurchaseRequest) -> dict:
            if calm.is_set():
                return provider.create_order(
                    request.amount_paise,
                    receipt=receipt_for_key(request.idempotency_key))
            fault = plan[request.idempotency_key]
            if fault == LATENCY:
                time.sleep(rng.uniform(0.005, 0.03))
            if fault == TIMEOUT_BEFORE:
                raise ProviderTimeout("timed out before reaching the provider")
            order = provider.create_order(
                request.amount_paise, receipt=receipt_for_key(request.idempotency_key))
            if request.idempotency_key == double_charged:
                provider.create_order(
                    request.amount_paise,
                    receipt=receipt_for_key(request.idempotency_key))
            if fault == TIMEOUT_AFTER:
                raise ProviderTimeout("timed out after the provider charged")
            if fault == CRASH_AFTER:
                raise InjectedCrash("died after the provider charged")
            if fault == BACKEND_KILL:
                # Right after the charge, so the connection most likely to die
                # is the one about to write the completion and its sale.
                killed = self._kill_backends(db)
                with kills:
                    result.backend_kills += killed
            return order

        gateway = PurchaseGateway(
            engine, store, execute, audit=audit,
            ledger=ledger if self.book_sales else None,
            find_orders=(
                (lambda r: provider.orders_for_receipt(
                    receipt_for_key(r.idempotency_key)))
                if self.verify_before_reclaim else None),
            # The chaos provider lists an order the instant it exists, so no
            # lag window is needed: an empty answer really means "never charged".
            not_found_grace_seconds=0)

        stop = threading.Event()
        bad_reads: list = []
        reads = [0]

        def reader() -> None:
            while not stop.is_set():
                try:
                    total = sum(ledger.trial_balance().values())
                except psycopg.Error:
                    continue  # this reader's connection was killed; not a finding
                reads[0] += 1
                if total != 0:
                    bad_reads.append(total)

        def submit(key: str) -> None:
            fault = plan[key]
            if fault in (PROCESS_KILL_AFTER, PROCESS_KILL_BEFORE):
                if self._kill_a_process(db, key, amounts[key], fault):
                    with kills:
                        result.process_kills += 1
                return
            try:
                gateway.submit(PurchaseRequest(AGENT, SKU, amounts[key], key))
            except (ProviderTimeout, InjectedCrash, psycopg.Error, RuntimeError):
                # Every one of these is a fault we injected, or its consequence.
                # The claim under test is not that nothing raises -- it is that
                # the money still adds up afterwards.
                pass

        watcher = threading.Thread(target=reader, daemon=True)
        watcher.start()
        try:
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                list(pool.map(submit, sorted(plan)))
        finally:
            stop.set()
            watcher.join(timeout=10)

        result.mid_run_reads = reads[0]
        if bad_reads:
            result.violations.append(
                f"the books did not net to zero on {len(bad_reads)} mid-run "
                f"read(s): {bad_reads[:3]}")

        calm.set()
        self._retry_stalled(gateway, store, amounts, result)

        result.repairs = self._reconcile_everything(provider, store, audit,
                                                    engine, ledger, plan)
        self._check(db, provider, store, ledger, audit, plan, result)

    # -- the failures ------------------------------------------------------

    @staticmethod
    def _kill_backends(db: Database, limit: int = 1) -> int:
        """Terminate live backends of this schema's pool, from outside it."""
        try:
            with db.outside_connection() as conn:
                rows = conn.execute(
                    "SELECT pg_terminate_backend(pid) AS killed "
                    "FROM pg_stat_activity "
                    "WHERE application_name = %s AND pid <> pg_backend_pid() "
                    "ORDER BY random() LIMIT %s",
                    (f"zerotrust-{db.schema}", limit)).fetchall()
            return sum(1 for r in rows if r["killed"])
        except psycopg.Error:
            return 0

    def _kill_a_process(self, db: Database, key: str, amount: int,
                        mode: str) -> bool:
        """Run one purchase in a real process and SIGKILL it mid-flight.

        Returns True if the process really died by signal. A clean exit would
        mean the kill never happened, and counting it would overstate what was
        tested.
        """
        proc = subprocess.run(
            [sys.executable, "-m", "zerotrust.chaos_worker", db.dsn, db.schema,
             AGENT, key, str(amount), receipt_for_key(key), mode],
            capture_output=True, timeout=120,
        )
        return proc.returncode < 0

    # -- putting it back together -----------------------------------------

    def _retry_stalled(self, gateway, store, amounts: dict,
                       result: RoundResult) -> None:
        """Retry every key a dead claimant left behind, the way a client would.

        This is the step that used to double charge: the retry reclaims a
        stale key and runs the purchase again, whether or not the dead
        claimant had already charged. The gateway now asks the provider first.
        """
        stalled = [r["key"] for r in store.records() if r["status"] == PROCESSING]
        if not stalled:
            return
        time.sleep(store.stale_after_seconds + 0.05)
        for scoped in stalled:
            _agent, key = _unscope(scoped)
            result.stalled_retried += 1
            try:
                outcome = gateway.submit(PurchaseRequest(AGENT, SKU, amounts[key], key))
            except (ProviderTimeout, InjectedCrash, psycopg.Error, RuntimeError):
                continue
            if outcome.outcome is Outcome.RECOVERED:
                result.recovered += 1
            elif outcome.outcome is Outcome.RECLAIMED:
                result.reclaimed += 1

    def _reconcile_everything(self, provider, store, audit, engine, ledger,
                              plan: dict) -> int:
        """What an operator would do after the dust settles: ask the provider
        about every key, then repair whatever the books still disagree on."""
        reconciler = Reconciler(provider, store, audit=audit, policy=engine,
                                ledger=ledger, not_found_grace_seconds=0)
        repairs = 0
        for key in sorted(plan):
            for attempt in range(3):  # a killed connection is worth one retry
                try:
                    result = reconciler.reconcile(key, receipt_for_key(key),
                                                  agent_id=AGENT)
                    if result.finding.value in ("DIVERGED_REPAIRED",
                                                "CONFIRMED_NOT_EXECUTED"):
                        repairs += 1
                    break
                except psycopg.Error:
                    if attempt == 2:
                        raise
        reconciler.repair_ledger(receipt_for_key)
        return repairs

    def _check(self, db: Database, provider, store, ledger, audit,
               plan: dict, result: RoundResult) -> None:
        orders = provider.all_orders()
        charged_by_receipt: Counter = Counter(o["receipt"] for o in orders)
        result.charged_orders = len(orders)
        result.charged_paise = sum(o["amount"] for o in orders)

        balances = ledger.trial_balance()
        result.revenue_paise = -balances[REVENUE]

        records = {r["key"]: r["status"] for r in store.records()}
        result.in_flight_records = sum(
            1 for s in records.values() if s not in (COMPLETED, FAILED))

        if result.in_flight_records:
            result.violations.append(
                f"{result.in_flight_records} key(s) still in flight after "
                f"their retry")

        doubled = [r for r, n in charged_by_receipt.items() if n > 1]
        if doubled:
            result.violations.append(
                f"{len(doubled)} receipt(s) charged more than once: {doubled[:3]}")

        if result.revenue_paise != result.charged_paise:
            result.violations.append(
                f"revenue booked ({result.revenue_paise}) does not equal money "
                f"charged ({result.charged_paise})")

        if balances[SUSPENSE_CLEARING] or balances[SUSPENSE_REVENUE]:
            result.violations.append(
                f"money left in suspense after reconciliation: "
                f"{balances[SUSPENSE_CLEARING]} / {balances[SUSPENSE_REVENUE]}")

        gaps = ledger.discrepancies(store.records())
        if gaps:
            result.violations.append(
                f"{len(gaps)} discrepancy(ies) between the books and the "
                f"records: {[g['problem'] for g in gaps[:3]]}")

        books = ledger.verify()
        if not books.balanced:
            result.violations.append(f"the ledger does not balance: {books.summary}")

        chain = audit.verify()
        if not chain.intact:
            result.violations.append(f"the audit chain is broken: {chain.summary}")

        for key in sorted(plan):
            scoped = f"{AGENT}:{key}"
            status = records.get(scoped)
            charged = charged_by_receipt.get(receipt_for_key(key), 0)
            if status == COMPLETED and charged != 1:
                result.violations.append(
                    f"'{key}' is COMPLETED but was charged {charged} time(s)")
            if charged == 1 and status != COMPLETED:
                result.violations.append(
                    f"'{key}' was charged but its record is {status}")


def run_harness(dsn: str = DEFAULT_DSN, *, rounds: int = 5, purchases: int = 16,
                process_kills: int = 2, seed: Optional[int] = None) -> ChaosReport:
    return ChaosHarness(dsn, rounds=rounds, purchases=purchases,
                        process_kills=process_kills, seed=seed).run()
