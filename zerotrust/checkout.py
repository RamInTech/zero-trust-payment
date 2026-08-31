"""Phase 5 — display, confirm, execute.

Three locked design decisions live in this file. Each one closes a specific
hole, and each is easy to get wrong in a way that looks fine.

1. THE IDEMPOTENCY KEY IS MINTED AT DISPLAY TIME, ONCE PER INTENT.
   It is pinned to the pending request and reused by every confirmation of that
   request. The naive alternative -- generating a key inside the confirm
   handler -- defeats idempotency entirely: two taps on one button become two
   unrelated first-time requests, each with its own key, each a real charge.
   Displaying a request happens once; clicking confirm can happen many times.
   So the key belongs to the display.

2. THE PRICE IS RE-VALIDATED AT CONFIRM TIME.
   The amount shown to the human is not trusted when the confirmation comes
   back. The true price is re-read from the catalog and compared. A mismatch is
   REJECTED, never silently reconciled -- otherwise the amount charged could
   differ from the amount a human actually approved, whether from an honest
   price change or a tampered client value.

3. THE MANDATE IS RE-CHECKED AT CONFIRM TIME, NOT ONLY AT DISPLAY TIME.
   A mandate that tightened or was revoked while the request sat pending
   governs the outcome. Confirmation is not authorisation; the policy engine
   runs on confirmed requests too, and can still deny them.

Declining is terminal: no policy check, no execution, no charge.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from zerotrust.audit import Actor, AuditLog, EventType
from zerotrust.catalog import Catalog, ItemNotInCatalog
from zerotrust.gateway import PurchaseGateway, PurchaseOutcome
from zerotrust.intent import IntentParser, ParsedIntent
from zerotrust.policy import Decision, PurchaseRequest, Rule

DEFAULT_PENDING_TTL_SECONDS = 900.0  # 15 minutes


class PendingStatus(str, Enum):
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    DECLINED = "DECLINED"


class CheckoutError(RuntimeError):
    """A request could not proceed. Carries a specific, structured reason."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass
class PendingPurchase:
    """A parsed request shown to a human, awaiting their answer."""

    request_id: str
    agent_id: str
    sku: str
    item_name: str
    displayed_amount_paise: int
    quantity: int
    # Minted HERE, at display time, and reused by every confirm of this request.
    idempotency_key: str
    created_at: float
    status: PendingStatus = PendingStatus.AWAITING_CONFIRMATION
    parser: str = "unknown"
    notes: dict = field(default_factory=dict)

    def prompt(self) -> str:
        """What the human is actually shown."""
        rupees = self.displayed_amount_paise / 100
        return f"Confirm: buy {self.item_name} ({self.sku}) for \u20b9{rupees:,.2f}?"

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "sku": self.sku,
            "item_name": self.item_name,
            "quantity": self.quantity,
            "displayed_amount_paise": self.displayed_amount_paise,
            "prompt": self.prompt(),
            "status": self.status.value,
            "parser": self.parser,
        }


class CheckoutService:
    """The surface an agent actually calls."""

    def __init__(
        self,
        catalog: Catalog,
        gateway: PurchaseGateway,
        parser: Optional[IntentParser] = None,
        audit: Optional[AuditLog] = None,
        clock: Callable[[], float] = time.time,
        pending_ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS,
    ) -> None:
        self.catalog = catalog
        self.gateway = gateway
        self.parser = parser
        self.audit = audit or gateway.audit
        self._clock = clock
        self.pending_ttl_seconds = pending_ttl_seconds
        self._pending: dict[str, PendingPurchase] = {}
        self._lock = threading.Lock()

    # -- step 1: propose ---------------------------------------------------

    def propose(
        self, agent_id: str, sku: str, quantity: int = 1,
        parsed: Optional[ParsedIntent] = None,
    ) -> PendingPurchase:
        """Build a request from a STRUCTURED ask and show it for confirmation.

        The catalog check happens here -- before the policy engine ever runs --
        so an item that doesn't exist is rejected without consuming a velocity
        slot or producing a policy decision about a fictional product.
        """
        request_id = AuditLog.new_request_id()
        try:
            item = self.catalog.get(sku)
        except ItemNotInCatalog as exc:
            self._log(EventType.PURCHASE_REQUESTED, Actor.AGENT,
                      request_id=request_id, agent_id=agent_id,
                      reason=str(exc), details={"sku": sku, "rejected": True})
            raise CheckoutError(str(exc), code="ITEM_NOT_IN_CATALOG") from None

        if not item.available:
            raise CheckoutError(
                f"'{item.name}' ({sku}) is out of stock",
                code="ITEM_UNAVAILABLE",
            )

        quantity = max(1, int(quantity))
        pending = PendingPurchase(
            request_id=request_id,
            agent_id=agent_id,
            sku=sku,
            item_name=item.name,
            displayed_amount_paise=item.price_paise * quantity,
            quantity=quantity,
            # ONE key per intent, minted once, right here.
            idempotency_key=f"intent_{uuid.uuid4().hex[:16]}",
            created_at=self._clock(),
            parser=parsed.parser if parsed else "structured",
        )
        with self._lock:
            self._pending[request_id] = pending

        self._log(
            EventType.PURCHASE_REQUESTED, Actor.AGENT,
            request_id=request_id, agent_id=agent_id,
            idempotency_key=pending.idempotency_key,
            details={"sku": sku, "quantity": quantity,
                     "displayed_amount_paise": pending.displayed_amount_paise},
        )
        self._log(
            EventType.PRICE_VALIDATED, Actor.SYSTEM,
            request_id=request_id, agent_id=agent_id,
            reason="price read from catalog at display time",
            details={"amount_paise": pending.displayed_amount_paise},
        )
        return pending

    def propose_from_text(self, agent_id: str, text: str) -> PendingPurchase:
        """Natural language -> structured draft -> shown for confirmation.

        No policy check happens here. The LLM's output is a proposal; it goes
        to a human before it goes anywhere near authorisation.
        """
        if self.parser is None:
            raise CheckoutError("no intent parser configured",
                                code="NO_PARSER")

        intent = self.parser.parse(text)
        request_id_for_log = AuditLog.new_request_id()
        self._log(
            EventType.INTENT_PARSED, Actor.AGENT,
            request_id=request_id_for_log, agent_id=agent_id,
            reason=intent.clarification,
            details={"raw_text": text, "sku": intent.sku,
                     "understood": intent.understood, "parser": intent.parser},
        )

        if intent.needs_clarification:
            # Ambiguity is asked about, never guessed into a purchase.
            raise CheckoutError(
                intent.clarification or "could not understand the request",
                code="NEEDS_CLARIFICATION",
            )

        return self.propose(agent_id, intent.sku, intent.quantity, parsed=intent)

    # -- step 2: the human answers ----------------------------------------

    def decline(self, request_id: str) -> PendingPurchase:
        """Terminal. No policy check, no execution, no charge."""
        pending = self._get(request_id)
        pending.status = PendingStatus.DECLINED
        self._log(
            EventType.USER_DECLINED, Actor.HUMAN,
            request_id=request_id, agent_id=pending.agent_id,
            idempotency_key=pending.idempotency_key,
            reason="human declined the purchase",
            details={"sku": pending.sku},
        )
        return pending

    def confirm(
        self, request_id: str, confirmed_amount_paise: Optional[int] = None
    ) -> PurchaseOutcome:
        """Human confirmation -> re-validate -> policy -> idempotent execution.

        `confirmed_amount_paise` is what the client claims was on screen. It is
        checked, not trusted -- passing a tampered value is rejected.
        """
        pending = self._get(request_id)

        if pending.status is PendingStatus.DECLINED:
            raise CheckoutError(
                "this request was declined and cannot be confirmed",
                code="ALREADY_DECLINED",
            )

        age = self._clock() - pending.created_at
        if age > self.pending_ttl_seconds:
            raise CheckoutError(
                f"this request expired {age - self.pending_ttl_seconds:.0f}s ago; "
                f"start a new one",
                code="REQUEST_EXPIRED",
            )

        # (a) the client's claimed amount must match what we displayed
        if (
            confirmed_amount_paise is not None
            and confirmed_amount_paise != pending.displayed_amount_paise
        ):
            self._reject_price(pending, confirmed_amount_paise,
                               pending.displayed_amount_paise,
                               "the confirmed amount does not match the "
                               "amount that was displayed")

        # (b) the displayed amount must still match the catalog's truth
        try:
            unit_price = self.catalog.current_price_paise(pending.sku)
        except ItemNotInCatalog as exc:
            raise CheckoutError(str(exc), code="ITEM_NOT_IN_CATALOG") from None

        true_amount = unit_price * pending.quantity
        if true_amount != pending.displayed_amount_paise:
            self._reject_price(pending, pending.displayed_amount_paise,
                               true_amount,
                               "the catalog price changed after this request "
                               "was displayed")

        self._log(
            EventType.PRICE_VALIDATED, Actor.SYSTEM,
            request_id=request_id, agent_id=pending.agent_id,
            idempotency_key=pending.idempotency_key,
            reason="re-validated against the catalog at confirm time",
            details={"amount_paise": true_amount},
        )
        self._log(
            EventType.USER_CONFIRMED, Actor.HUMAN,
            request_id=request_id, agent_id=pending.agent_id,
            idempotency_key=pending.idempotency_key,
            reason="human confirmed -- this is NOT an authorisation; the "
                   "policy engine still decides",
            details={"sku": pending.sku, "amount_paise": true_amount},
        )
        pending.status = PendingStatus.CONFIRMED

        # The policy engine runs on confirmed requests too, against the mandate
        # in force NOW -- not the one that was in force at display time.
        request = PurchaseRequest(
            agent_id=pending.agent_id,
            sku=pending.sku,
            amount_paise=true_amount,
            idempotency_key=pending.idempotency_key,  # the SAME key, every time
            currency="INR",
        )
        return self.gateway.submit(request, request_id=request_id)

    # -- helpers -----------------------------------------------------------

    def get_pending(self, request_id: str) -> PendingPurchase:
        return self._get(request_id)

    def _get(self, request_id: str) -> PendingPurchase:
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            raise CheckoutError(f"unknown request '{request_id}'",
                                code="UNKNOWN_REQUEST")
        return pending

    def _reject_price(self, pending: PendingPurchase, shown: int,
                      actual: int, why: str) -> None:
        reason = (
            f"{why}: displayed \u20b9{shown / 100:,.2f}, "
            f"actual \u20b9{actual / 100:,.2f}"
        )
        self._log(
            EventType.POLICY_DENIED, Actor.SYSTEM,
            request_id=pending.request_id, agent_id=pending.agent_id,
            idempotency_key=pending.idempotency_key,
            rule=Rule.MALFORMED_REQUEST.value,
            reason=reason,
            details={"displayed_paise": shown, "actual_paise": actual,
                     "check": "price_revalidation"},
        )
        raise CheckoutError(reason, code="PRICE_MISMATCH")

    def _log(self, event_type: EventType, actor: Actor, **kwargs) -> None:
        if self.audit is None:
            return
        self.audit.record(event_type, actor, **kwargs)
