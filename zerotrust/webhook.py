"""Webhook receipt, with the signature verified before anything is believed.

Phase 7 learns that something diverged by POLLING: the sweep asks Razorpay, on
an interval, whether the ledger still matches. A webhook inverts that — the
provider says so the moment it happens — but it also opens the one door this
system otherwise does not have: an inbound HTTP endpoint that anybody on the
internet can call.

So the rule here is the same rule that governs the LLM, stated for a new actor:

    A WEBHOOK IS UNTRUSTED INPUT. IT MAY INFORM; IT MAY NEVER AUTHORISE.

A verified webhook does not mark anything paid, release a velocity slot, or
write a payment outcome. It does exactly one thing: it asks the reconciler to
go and check with the provider, which is the same code path the sweep uses and
which trusts only what the provider's API says when asked directly. That keeps
the guarantee intact even if the signing secret leaks — a forged webhook can at
most cause a reconciliation that finds nothing wrong.

Three details in `verify_signature` are load-bearing rather than incidental:

1. The signature covers the RAW REQUEST BODY. Parsing JSON and re-serialising
   it changes key order and whitespace, so a signature computed over the
   re-serialised form would fail on every genuine delivery and, worse, could be
   made to pass on a crafted one.
2. The comparison is `hmac.compare_digest`. A plain `==` returns early on the
   first differing byte, which leaks the correct prefix through timing and
   turns forgery into a few thousand requests.
3. An absent secret is a REFUSAL, never a bypass. A receiver with no secret
   configured rejects everything rather than accepting everything, because the
   failure mode of the alternative is silent and total.
"""

from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Callable, Optional

from zerotrust.audit import Actor, AuditLog, EventType

#: Razorpay sends the HMAC-SHA256 of the raw body, hex-encoded, in this header.
SIGNATURE_HEADER = "X-Razorpay-Signature"

#: Deliveries older than this are refused. Razorpay retries a failed delivery,
#: so the receiver must tolerate repeats -- but a body captured off the wire
#: and replayed weeks later should not still be accepted. The window bounds
#: how long a captured delivery stays useful to an attacker.
DEFAULT_MAX_AGE_SECONDS = 300.0


class Rejection(str, Enum):
    """Why a delivery was refused. Specific, never a generic 'invalid'."""

    NO_SECRET = "NO_SECRET"                  # receiver not configured
    MISSING_SIGNATURE = "MISSING_SIGNATURE"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    MALFORMED_BODY = "MALFORMED_BODY"
    STALE = "STALE"                          # outside the freshness window
    ALREADY_SEEN = "ALREADY_SEEN"            # a retry of a delivery handled before


class WebhookRejected(Exception):
    """A delivery that failed verification. Carries the specific reason."""

    def __init__(self, rejection: Rejection, reason: str) -> None:
        super().__init__(reason)
        self.rejection = rejection
        self.reason = reason


def compute_signature(body: bytes, secret: str) -> str:
    """The HMAC Razorpay would send for this exact body."""
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def verify_signature(body: bytes, signature: Optional[str], secret: Optional[str]) -> bool:
    """True only if `signature` is the HMAC of `body` under `secret`.

    Every failure path returns False rather than raising, so a caller cannot
    accidentally treat "no secret configured" as "verification skipped".
    """
    if not secret or not signature:
        return False
    return hmac.compare_digest(compute_signature(body, secret), signature)


@dataclass(frozen=True)
class WebhookResult:
    """What the receiver did. `reconcile_requested` is the only side effect."""

    accepted: bool
    event: Optional[str] = None
    rejection: Optional[Rejection] = None
    reason: Optional[str] = None
    reconcile_requested: bool = False
    #: The receipt the delivery pointed at, when it named one.
    receipt: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "event": self.event,
            "rejection": self.rejection.value if self.rejection else None,
            "reason": self.reason,
            "reconcile_requested": self.reconcile_requested,
            "receipt": self.receipt,
        }


class WebhookReceiver:
    """Verifies deliveries, records them, and asks for a reconciliation.

    `on_verified` is deliberately narrow: it receives a receipt string and is
    expected to trigger a check against the provider. It is not handed the
    webhook payload's amounts or statuses, because nothing in this system
    should be able to update the ledger from a number that arrived over HTTP.
    """

    def __init__(
        self,
        secret: Optional[str],
        audit: Optional[AuditLog] = None,
        on_verified: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.time,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.secret = secret or None
        self.audit = audit
        self.on_verified = on_verified
        self._clock = clock
        self.max_age_seconds = max_age_seconds
        #: Delivery ids already handled, so a retry does not re-trigger work.
        self._seen: set[str] = set()

    @property
    def is_configured(self) -> bool:
        return self.secret is not None

    def receive(self, body: bytes, signature: Optional[str]) -> WebhookResult:
        """Verify, log, and -- only if verified -- request a reconciliation."""
        if not self.is_configured:
            return self._reject(
                Rejection.NO_SECRET,
                "no webhook secret configured; deliveries are refused rather "
                "than trusted",
                body,
            )
        if not signature:
            return self._reject(
                Rejection.MISSING_SIGNATURE,
                f"no {SIGNATURE_HEADER} header on the request",
                body,
            )
        if not verify_signature(body, signature, self.secret):
            return self._reject(
                Rejection.BAD_SIGNATURE,
                "signature does not match the body under the configured secret",
                body,
            )

        # Verified from here on: the body provably came from whoever holds the
        # secret. That still does not make its CONTENTS authoritative.
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._reject(Rejection.MALFORMED_BODY,
                                f"verified body is not a JSON object: {exc}", body)

        event = payload.get("event")
        created_at = payload.get("created_at")
        if isinstance(created_at, (int, float)):
            age = self._clock() - float(created_at)
            if age > self.max_age_seconds:
                return self._reject(
                    Rejection.STALE,
                    f"delivery is {age:.0f}s old, older than the "
                    f"{self.max_age_seconds:.0f}s window",
                    body, event=event,
                )

        delivery_id = _delivery_id(payload, body)
        if delivery_id in self._seen:
            # Not an error: Razorpay retries. Recorded and ignored, so a retry
            # cannot multiply the work a single delivery causes.
            self._log(EventType.WEBHOOK_RECEIVED, Actor.PROVIDER, event=event,
                      reason="duplicate delivery ignored",
                      details={"delivery_id": delivery_id, "duplicate": True})
            return WebhookResult(accepted=True, event=event,
                                 rejection=Rejection.ALREADY_SEEN,
                                 reason="duplicate delivery ignored",
                                 receipt=_receipt_of(payload))
        self._seen.add(delivery_id)

        receipt = _receipt_of(payload)
        self._log(
            EventType.WEBHOOK_RECEIVED, Actor.PROVIDER, event=event,
            reason="signature verified",
            details={
                "delivery_id": delivery_id,
                "receipt": receipt,
                # Stated in the log itself, so a reader of the trail alone can
                # see that this entry did not move money.
                "effect": "reconciliation requested; no ledger write",
            },
        )

        requested = False
        if receipt and self.on_verified is not None:
            self.on_verified(receipt)
            requested = True

        return WebhookResult(accepted=True, event=event,
                             reconcile_requested=requested, receipt=receipt)

    # -- internals ---------------------------------------------------------

    def _reject(self, rejection: Rejection, reason: str, body: bytes,
                event: Optional[str] = None) -> WebhookResult:
        # The actor is UNVERIFIED, not PROVIDER: the whole point of a failed
        # signature is that we do NOT know Razorpay sent this. Recording it as
        # PROVIDER would put a claim in the audit log that the log's own
        # evidence contradicts.
        self._log(
            EventType.WEBHOOK_REJECTED, Actor.UNVERIFIED, event=event,
            rule=rejection.value, reason=reason,
            details={"body_bytes": len(body)},
        )
        return WebhookResult(accepted=False, event=event, rejection=rejection,
                             reason=reason)

    def _log(self, event_type: EventType, actor: Actor, *, event: Optional[str],
             reason: str, details: dict, rule: Optional[str] = None) -> None:
        if self.audit is None:
            return
        self.audit.record(
            event_type, actor, rule=rule, reason=reason,
            details={"event": event, **details},
        )


def _receipt_of(payload: dict) -> Optional[str]:
    """The receipt from a Razorpay webhook payload, if it carries one.

    Razorpay nests the entity under `payload.<type>.entity`. Only the receipt
    is taken: it is an identifier used to go and ASK the provider, not a fact
    to be believed. Amounts and statuses in the body are deliberately ignored.
    """
    entities = payload.get("payload")
    if not isinstance(entities, dict):
        return None
    for wrapper in entities.values():
        if isinstance(wrapper, dict):
            entity = wrapper.get("entity")
            if isinstance(entity, dict) and entity.get("receipt"):
                return str(entity["receipt"])
    return None


def _delivery_id(payload: dict, body: bytes) -> str:
    """A stable id for one delivery, for duplicate suppression.

    Razorpay does not guarantee an id field on every event, so this falls back
    to a hash of the body -- which is exactly right for the purpose: two
    byte-identical deliveries ARE the same delivery.
    """
    for key in ("id", "event_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "sha256:" + sha256(body).hexdigest()[:32]
