"""Phase 2 — the payment provider seam.

Phase 1's guarantee was proven against a mock. The claim this phase makes is
that the *same wrapper, unchanged*, holds around a real provider -- so the
guarantee belongs to `IdempotencyStore`, not to anything underneath it.

That claim is only checkable if the wrapper cannot tell which provider it is
holding. Hence the `PaymentProvider` protocol: both the real Razorpay client and
the offline simulator satisfy it, and `tests/test_provider.py` runs the identical
retry sequence through both and asserts the outcomes match.

CAPTURE IS SIMULATED, NOT LIVE. `POST /v1/payments/{id}/capture` needs a
`payment_id`, which standard Razorpay Checkout only mints via a browser-based
customer authorisation step; no documented headless test-mode path exists. Order
creation below IS a real server-to-server call. The capture method is named
`simulate_capture()` and stamps `"simulated": True` into its result so that no
caller, log line, or demo can mistake it for a live capture.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Optional, Protocol, runtime_checkable

import httpx

from zerotrust.config import RazorpayConfig

DEFAULT_TIMEOUT_SECONDS = 10.0
#: Reconciliation pages through recent orders rather than trusting a server-side
#: receipt filter. See RazorpayTestModeProvider.orders_for_receipt.
DEFAULT_LOOKUP_PAGE_SIZE = 100   # Razorpay's maximum
DEFAULT_MAX_LOOKUP_PAGES = 20    # 2,000 orders; beyond this we admit we cannot tell


class ProviderError(RuntimeError):
    """A payment provider call failed in a way we can describe."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderTimeout(ProviderError):
    """The call timed out -- the true outcome is UNKNOWN, not failed.

    Kept distinct from ProviderError on purpose. A connection refused means
    nothing happened; a timeout means the order may or may not exist at
    Razorpay. Phase 7's PENDING_VERIFICATION state is what resolves this
    honestly. See the Phase 2 learning log for why retrying a timeout is not
    yet safe.
    """


@runtime_checkable
class PaymentProvider(Protocol):
    def create_order(
        self, amount_paise: int, currency: str = "INR", receipt: str = ""
    ) -> dict: ...

    def simulate_capture(self, order_id: str, amount_paise: int) -> dict: ...

    def orders_for_receipt(self, receipt: str) -> list[dict]:
        """Every order the provider holds for this receipt.

        The provider side of reconciliation (Phase 7). Answers the only
        question that matters after an ambiguous failure: did the money
        actually move? Returning [] means "the provider has no such order",
        NOT "we could not find out" -- an unanswerable lookup raises
        ProviderTimeout instead, because the difference between those two is
        the whole point of PENDING_VERIFICATION.
        """
        ...


class RazorpayTestModeProvider:
    """Real, server-to-server calls against Razorpay's test-mode API."""

    def __init__(
        self,
        config: RazorpayConfig,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Optional[httpx.BaseTransport] = None,
        lookup_page_size: int = DEFAULT_LOOKUP_PAGE_SIZE,
        max_lookup_pages: int = DEFAULT_MAX_LOOKUP_PAGES,
    ) -> None:
        self.config = config
        self.lookup_page_size = lookup_page_size
        self.max_lookup_pages = max_lookup_pages
        # `transport` exists so tests can stub the network with
        # httpx.MockTransport without touching the request-building code.
        self._client = httpx.Client(
            base_url=config.base_url,
            auth=(config.key_id, config.key_secret),
            timeout=timeout_seconds,
            transport=transport,
        )
        self._lock = threading.Lock()
        self.call_count = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RazorpayTestModeProvider":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def create_order(
        self, amount_paise: int, currency: str = "INR", receipt: str = ""
    ) -> dict:
        """POST /v1/orders -- a real call. Every invocation creates an order."""
        if amount_paise <= 0:
            raise ValueError("amount_paise must be positive")

        with self._lock:
            self.call_count += 1

        body = {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt or f"rcpt_{uuid.uuid4().hex[:12]}",
        }

        try:
            response = self._client.post("/v1/orders", json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"Razorpay order creation timed out: {exc}. The order may or may "
                f"not have been created -- do not assume either."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Razorpay request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"Razorpay rejected the order ({response.status_code}): "
                f"{_describe_error(response)}",
                status_code=response.status_code,
            )

        return response.json()

    def orders_for_receipt(self, receipt: str) -> list[dict]:
        """Find every order carrying this receipt, by paging and matching locally.

        NOT `GET /v1/orders?receipt=...`. That filter is silently ignored by
        the test-mode API: it returns `count: 0` for a receipt that provably
        exists, with HTTP 200 and no error. For a reconciler that is the worst
        possible failure -- an empty result means "the provider never executed
        this", so a silently-ignored filter would conclude a real purchase
        never happened and clear the way for a second charge. Verified against
        a live test-mode account; see JOURNAL.md.

        So the search is done here instead: page through recent orders and
        match the receipt ourselves. If the search cannot be completed within
        `max_pages`, that raises rather than returning [] -- "we could not
        finish looking" must never be reported as "it is not there".
        """
        matches: list[dict] = []
        skip = 0

        for _ in range(self.max_lookup_pages):
            with self._lock:
                self.call_count += 1

            try:
                response = self._client.get(
                    "/v1/orders",
                    params={"count": self.lookup_page_size, "skip": skip},
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeout(
                    f"Razorpay order lookup for receipt '{receipt}' timed out: "
                    f"{exc}. The provider's true state is still unknown."
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(f"Razorpay lookup failed: {exc}") from exc

            if response.status_code >= 400:
                raise ProviderError(
                    f"Razorpay rejected the lookup ({response.status_code}): "
                    f"{_describe_error(response)}",
                    status_code=response.status_code,
                )

            items = list(response.json().get("items", []))
            matches.extend(o for o in items if o.get("receipt") == receipt)

            if len(items) < self.lookup_page_size:
                return matches  # the account is exhausted; [] is trustworthy
            skip += self.lookup_page_size

        raise ProviderError(
            f"could not search far enough back to rule out receipt "
            f"'{receipt}' (stopped after {self.max_lookup_pages} pages of "
            f"{self.lookup_page_size}); the outcome is unknown, not absent"
        )

    def fetch_order(self, order_id: str) -> Optional[dict]:
        """GET /v1/orders/{id} -- a direct, immediately-consistent lookup.

        Unlike the list endpoint (see `orders_for_receipt`), fetching a known
        id reflects a just-created order straight away. Only usable when the
        id survived, which is exactly what a crash-before-the-ledger-write
        destroys -- hence the receipt search as well.
        """
        with self._lock:
            self.call_count += 1
        try:
            response = self._client.get(f"/v1/orders/{order_id}")
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"Razorpay order fetch for '{order_id}' timed out: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Razorpay fetch failed: {exc}") from exc

        if response.status_code == 400 or response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ProviderError(
                f"Razorpay rejected the fetch ({response.status_code}): "
                f"{_describe_error(response)}",
                status_code=response.status_code,
            )
        return response.json()

    def simulate_capture(self, order_id: str, amount_paise: int) -> dict:
        """NOT a live capture -- see this module's docstring for why."""
        return _simulated_capture(order_id, amount_paise)


class SimulatedProvider:
    """Offline stand-in with the same interface. No network, no credentials.

    Used to demonstrate that the idempotency wrapper behaves identically whether
    the provider underneath is real or simulated -- Phase 2 completion bullet 3.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.call_count = 0
        self.orders: list[dict] = []

    def create_order(
        self, amount_paise: int, currency: str = "INR", receipt: str = ""
    ) -> dict:
        if amount_paise <= 0:
            raise ValueError("amount_paise must be positive")
        with self._lock:
            self.call_count += 1
            order = {
                "id": f"order_SIM{self.call_count:012d}",
                "entity": "order",
                "amount": amount_paise,
                "amount_paid": 0,
                "amount_due": amount_paise,
                "currency": currency,
                "receipt": receipt or f"rcpt_sim_{self.call_count}",
                "status": "created",
                "created_at": int(time.time()),
                "simulated": True,
            }
            self.orders.append(order)
        return order

    def orders_for_receipt(self, receipt: str) -> list[dict]:
        with self._lock:
            return [o for o in self.orders if o.get("receipt") == receipt]

    def simulate_capture(self, order_id: str, amount_paise: int) -> dict:
        return _simulated_capture(order_id, amount_paise)


def _simulated_capture(order_id: str, amount_paise: int) -> dict:
    return {
        "id": f"pay_SIM{uuid.uuid4().hex[:14]}",
        "entity": "payment",
        "order_id": order_id,
        "amount": amount_paise,
        "status": "captured",
        "captured_at": int(time.time()),
        # Load-bearing: nothing downstream may mistake this for a live capture.
        "simulated": True,
        "simulation_reason": (
            "capture requires a payment_id that standard Razorpay Checkout only "
            "produces via a browser-based customer authorisation step; no "
            "headless test-mode path exists"
        ),
    }


def _describe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    return error.get("description") or str(payload)[:200]
