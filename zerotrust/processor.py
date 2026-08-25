"""A deliberately naive payment processor used to test the idempotency wrapper.

This mock has *no* idempotency of its own: every call to `charge()` moves money.
That is the point. If a test shows exactly one charge for N retries, the
guarantee being measured belongs entirely to the wrapper around this class and
is not borrowed from the processor.
"""

from __future__ import annotations

import threading
import time


class MockPaymentProcessor:
    def __init__(self, latency_seconds: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._latency = latency_seconds
        self.charges: list[dict] = []

    def charge(self, order_id: str, amount_paise: int, currency: str = "INR") -> dict:
        if self._latency:
            # Widens the window in which a concurrent caller can race us.
            time.sleep(self._latency)
        with self._lock:
            self._counter += 1
            charge = {
                "charge_id": f"chg_{self._counter:06d}",
                "order_id": order_id,
                "amount_paise": amount_paise,
                "currency": currency,
            }
            self.charges.append(charge)
        return charge

    @property
    def charge_count(self) -> int:
        with self._lock:
            return len(self.charges)

    def charges_for(self, order_id: str) -> list[dict]:
        with self._lock:
            return [c for c in self.charges if c["order_id"] == order_id]
