"""Phase 5 — the HTTP surface an agent calls.

A thin adapter. Every rule lives in `CheckoutService`, `PolicyEngine`,
`IdempotencyStore` and `AuditLog`; this module translates HTTP to those and
back. Deliberately so -- a security property that only holds when reached over
HTTP is a property that can be bypassed by not using HTTP.

The flow an agent follows:

    GET  /catalog                        what may I buy?
    POST /intents                        natural language -> a draft, shown to a human
    POST /purchase-intents               a structured ask -> the same draft
    POST /intents/{id}/confirm           the human says yes  -> policy -> execution
    POST /intents/{id}/decline           the human says no   -> terminal
    GET  /audit/{request_id}             what happened, and why
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from zerotrust.checkout import CheckoutError, CheckoutService
from zerotrust.e2e import SealedText
from zerotrust.explain import UnknownRequest, explain
from zerotrust.narrate import ExplanationWriter
from zerotrust.provider import ProviderTimeout

_CODE_STATUS = {
    "ITEM_NOT_IN_CATALOG": 404,
    "UNKNOWN_REQUEST": 404,
    "ITEM_UNAVAILABLE": 409,
    "ALREADY_DECLINED": 409,
    "REQUEST_EXPIRED": 410,
    "PRICE_MISMATCH": 409,
    "NEEDS_CLARIFICATION": 422,
    "NO_PARSER": 501,
    "NO_E2E": 501,
    "DECRYPTION_FAILED": 400,
}


class SealedTextRequest(BaseModel):
    """A customer's message, encrypted client-side to the server's public
    key (`GET /e2e/public-key`) before it ever left the browser."""

    ciphertext_b64: str
    sender_public_key_b64: str


class IntentRequest(BaseModel):
    agent_id: str
    text: Optional[str] = Field(
        None, description="natural language purchase request, in plain text")
    sealed: Optional[SealedTextRequest] = Field(
        None, description="the same request, end-to-end encrypted instead")


class StructuredRequest(BaseModel):
    agent_id: str
    sku: str
    quantity: int = 1


class ConfirmRequest(BaseModel):
    """`amount_paise` is what the client claims was on screen.

    It is checked against what we displayed AND against the catalog's current
    price. It is never used as the amount to charge.
    """

    amount_paise: Optional[int] = None


def create_app(
    checkout: CheckoutService,
    narrator: Optional[ExplanationWriter] = None,
) -> FastAPI:
    app = FastAPI(
        title="Zero-Trust Payment Authorization for AI Agents",
        description=(
            "An agent may propose a purchase. It cannot approve one. Every "
            "request passes human confirmation and an independent policy check."
        ),
        version="0.1.0",
    )

    def _fail(exc: CheckoutError):
        raise HTTPException(
            status_code=_CODE_STATUS.get(exc.code, 400),
            detail={"code": exc.code, "reason": exc.reason},
        )

    @app.get("/catalog")
    def get_catalog():
        return {"items": [item.as_dict() for item in checkout.catalog.all()]}

    @app.post("/intents", status_code=201)
    def create_intent(body: IntentRequest):
        """Natural language in, plain or end-to-end encrypted. Returns a
        DRAFT for a human to confirm."""
        sealed = None
        if body.sealed is not None:
            sealed = SealedText(body.sealed.ciphertext_b64,
                                body.sealed.sender_public_key_b64)
        try:
            basket = checkout.propose_basket_from_text(
                body.agent_id, body.text, sealed=sealed)
        except CheckoutError as exc:
            _fail(exc)

        return {
            # The first line, kept under the original key so every existing
            # client and test keeps working. A single-item request is just a
            # basket of one.
            "awaiting_confirmation": basket[0].as_dict(),
            "basket": [p.as_dict() for p in basket],
            # A DISPLAY figure. It is not an amount anyone is authorised to
            # spend: each line is confirmed and policy-checked on its own, and
            # the per-transaction cap applies to each line, not to this sum.
            "basket_total_paise": sum(p.displayed_amount_paise for p in basket),
            "note": "no policy check has run yet; confirmation is required first",
        }

    @app.get("/e2e/public-key")
    def e2e_public_key():
        """The server's X25519 public key, so a browser can encrypt a
        purchase request to it before sending. See `zerotrust/e2e.py`."""
        if checkout.server_identity is None:
            raise HTTPException(
                status_code=501,
                detail={"reason": "end-to-end encryption not configured on this server"})
        return {
            "public_key_b64": checkout.server_identity.public_key_b64,
            "algorithm": "x25519-xsalsa20-poly1305",
        }

    @app.post("/purchase-intents", status_code=201)
    def create_structured_intent(body: StructuredRequest):
        """A structured ask, bypassing the LLM entirely.

        The structured surface exists regardless of the intent layer: the LLM
        is untrusted and must not be the only way in.
        """
        try:
            pending = checkout.propose(body.agent_id, body.sku, body.quantity)
        except CheckoutError as exc:
            _fail(exc)
        return {"awaiting_confirmation": pending.as_dict()}

    @app.get("/intents/{request_id}")
    def read_intent(request_id: str):
        try:
            return checkout.get_pending(request_id).as_dict()
        except CheckoutError as exc:
            _fail(exc)

    @app.post("/intents/{request_id}/confirm")
    def confirm_intent(request_id: str, body: ConfirmRequest = ConfirmRequest()):
        try:
            outcome = checkout.confirm(request_id, body.amount_paise)
        except ProviderTimeout as exc:
            # The outcome is UNKNOWN, not failed. 503 rather than 500: the
            # request is not erroneous, it is unresolved. The body says so
            # explicitly, because a client that reads this as a failure and
            # retries with a fresh intent is how a timeout becomes a double
            # charge. The record is frozen until reconciliation resolves it.
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PENDING_VERIFICATION",
                    "reason": str(exc),
                    "guidance": (
                        "the outcome of this purchase is unknown and is "
                        "awaiting reconciliation; do NOT resubmit it as a new "
                        "purchase"
                    ),
                },
            )
        except CheckoutError as exc:
            _fail(exc)

        return {
            "request_id": outcome.request_id,
            "approved": outcome.approved,
            "rule": outcome.rule.value if outcome.rule else None,
            "reason": outcome.reason,
            "idempotency_outcome": outcome.outcome.value if outcome.outcome else None,
            "executed": outcome.executed,
            "response": outcome.response,
        }

    @app.post("/intents/{request_id}/decline")
    def decline_intent(request_id: str):
        try:
            pending = checkout.decline(request_id)
        except CheckoutError as exc:
            _fail(exc)
        return {
            "request_id": pending.request_id,
            "status": pending.status.value,
            "note": "declined -- no policy check, no execution, no charge",
        }

    @app.get("/explain/{request_id}")
    def explain_decision(request_id: str):
        """Why this request was approved or denied, as WHY / WHAT / EVIDENCE.

        Reconstructed from the audit log; it decides nothing and writes
        nothing. Structured rather than prose so the answer can be asserted on
        and rendered, not merely read.
        """
        if checkout.audit is None:
            raise HTTPException(status_code=501,
                                detail={"reason": "no audit log configured"})
        try:
            return explain(checkout.audit, request_id, narrator).as_dict()
        except UnknownRequest as exc:
            raise HTTPException(status_code=404,
                                detail={"code": "UNKNOWN_REQUEST",
                                        "reason": str(exc)}) from None

    @app.get("/audit/{request_id}")
    def read_audit(request_id: str):
        if checkout.audit is None:
            raise HTTPException(status_code=501, detail="no audit log configured")
        entries = checkout.audit.for_request(request_id)
        return {
            "request_id": request_id,
            "events": [
                {
                    "event_id": e.event_id,
                    "event_type": e.event_type.value,
                    "actor": e.actor.value,
                    "rule": e.rule,
                    "reason": e.reason,
                    "details": e.details,
                }
                for e in entries
            ],
        }

    return app
