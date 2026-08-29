"""Phase 7 — reproducible fault injection.

RAZORPAY.md's completion test asks for a failure that is *triggerable*, not a
flaky accident. So the faults live behind an explicit switch: nothing fires
unless a test or demo arms it, and an armed fault fires deterministically.

The fault that matters is `CRASH_AFTER_PROVIDER_CALL`: the money action
succeeds at the provider, and the process dies before the local ledger records
it. That is the genuinely dangerous shape, because the two sides now disagree
and only the provider knows the truth.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum


class Fault(str, Enum):
    #: The provider call succeeds; the local write that records it does not.
    CRASH_AFTER_PROVIDER_CALL = "CRASH_AFTER_PROVIDER_CALL"
    #: The provider call times out -- the true outcome is UNKNOWN, not failed.
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    #: Reconciliation cannot reach the provider, so it must not guess.
    RECONCILE_LOOKUP_TIMEOUT = "RECONCILE_LOOKUP_TIMEOUT"


class InjectedCrash(RuntimeError):
    """A deliberately injected crash between the provider call and the ledger."""


@dataclass
class FaultInjector:
    """Arm a fault, run the scenario, observe the divergence."""

    armed: set = field(default_factory=set)
    fired: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def arm(self, fault: Fault) -> "FaultInjector":
        with self._lock:
            self.armed.add(fault)
        return self

    def disarm(self, fault: Fault) -> None:
        with self._lock:
            self.armed.discard(fault)

    def is_armed(self, fault: Fault) -> bool:
        with self._lock:
            return fault in self.armed

    def fire_once(self, fault: Fault) -> bool:
        """Consume an armed fault. Returns True if it fired.

        One-shot by design: the interesting scenarios are "it failed, then the
        retry succeeded", which is impossible if the fault fires forever.
        """
        with self._lock:
            if fault not in self.armed:
                return False
            self.armed.discard(fault)
            self.fired.append(fault)
            return True

    @property
    def fired_faults(self) -> list:
        with self._lock:
            return list(self.fired)
