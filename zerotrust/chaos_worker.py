"""Phase 11 — one purchase, in a process that really dies.

Run as a subprocess by `zerotrust/chaos.py`. It makes a genuine purchase
through the real gateway and then SIGKILLs itself, either after the provider
charged or before it did.

Why a separate process at all: nothing inside the interpreter can simulate
this. An exception unwinds the stack, so `except` clauses still record the
failure and `finally` blocks still run -- which is why Phase 7's injected crash
leaves a tidy FAILED record. A thread cannot be killed. SIGKILL cannot be
caught, blocked or handled: no `except`, no `finally`, no flush, no connection
close. What it leaves behind is exactly what a crashed server leaves behind,
and that is the state the invariants have to survive.

The provider is `ChaosProvider`, whose orders are rows committed by their own
statement, so a charge made microseconds before death is still there to be
found afterwards. That is what makes "did the money move?" answerable at all
once the process that moved it is gone.
"""

from __future__ import annotations

import os
import signal
import sys

from zerotrust.audit import AuditLog
from zerotrust.chaos import ChaosProvider
from zerotrust.db import Database
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.ledger import Ledger
from zerotrust.mandate import MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest

KILL_AFTER_CHARGE = "PROCESS_KILL_AFTER_CHARGE"
KILL_BEFORE_CHARGE = "PROCESS_KILL_BEFORE_CHARGE"

#: Matches the harness: a killed claimant's record must still be PROCESSING
#: when the round's invariants are checked, not reclaimed in the meantime.
STALE_AFTER_SECONDS = float(os.environ.get("CHAOS_STALE_AFTER", "3600"))


def main(argv: list[str]) -> int:
    dsn, schema, agent, key, amount, receipt, mode = argv
    db = Database(dsn, schema, max_connections=4)
    store = IdempotencyStore(db, stale_after_seconds=STALE_AFTER_SECONDS)
    gateway = PurchaseGateway(
        PolicyEngine(MandateStore(db)), store,
        _executor(ChaosProvider(db), receipt, mode),
        audit=AuditLog(db), ledger=Ledger(db))
    gateway.submit(PurchaseRequest(agent, "SKU-CHAOS", int(amount), key))
    # Only reached if the kill did not happen, which the harness treats as a
    # process that was never actually killed rather than a purchase that ran.
    return 0


def _executor(provider: ChaosProvider, receipt: str, mode: str):
    def execute(request: PurchaseRequest) -> dict:
        if mode == KILL_BEFORE_CHARGE:
            _die()
        order = provider.create_order(request.amount_paise, receipt=receipt)
        if mode == KILL_AFTER_CHARGE:
            # The money has moved. Nothing local knows it yet, and this process
            # is about to stop existing before it can write that down.
            _die()
        return order
    return execute


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
