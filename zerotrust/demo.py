"""A reference client for the API. Not a product, and not an authority.

This wraps the production app rather than extending it:

    demo = FastAPI()
    demo.mount("/api", create_app(checkout, narrator=narrator,
                                  recommender=recommender,
                                  webhooks=webhooks))   # unmodified

`zerotrust/api.py` gains nothing from this module -- not one route. The reason
is the same one that kept FastAPI thin in the first place: a rule that lives in
the UI is bypassable by not using the UI, so the UI holds no rules. The page
renders state and posts to endpoints that already existed.

The routes added here are demo-only and deliberately outside `/api`:
changing a catalog price mid-flight, and attempting to rewrite the audit log.
They exist so a viewer can make the system refuse things in front of them --
which is worth more than any reassuring text the page could display.

DEMO ONLY: there is no authentication. The audit view exposes mandate internals
and idempotency keys. That is right for a demonstration and wrong for anything
else.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from zerotrust.admin_auth import AdminAuth, AdminAuthError, AdminSession
from zerotrust.api import create_app
from zerotrust.audit import AuditLog, EventType
from zerotrust.catalog import Catalog, CatalogItem, ItemNotInCatalog
from zerotrust.checkout import CheckoutService
from zerotrust.explain import first_detail, provider_order_id
from zerotrust.faults import Fault, FaultInjector
from zerotrust.mandate import ANY_SKU, Mandate
from zerotrust.intent import ParsedIntent
from zerotrust.policy import PolicyEngine

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

_NOT_BUILT = """<!doctype html><html><head><meta charset="utf-8">
<title>Frontend not built</title>
<style>body{background:#0b0d12;color:#e6e9ef;font:15px/1.7 system-ui;
padding:56px;max-width:640px;margin:auto}
code{background:#171a21;padding:2px 7px;border-radius:5px;
font-family:ui-monospace,Menlo,monospace}</style></head><body>
<h2>The frontend has not been built yet</h2>
<p>The API is running and fully usable &mdash; it is mounted at
<code>/api</code>, and the demo endpoints are under <code>/demo</code>.
Only the React interface is missing.</p>
<p>Build it:</p>
<p><code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>Then reload this page. The build output is not committed to the repository,
so this step is required once after cloning.</p>
</body></html>"""
HOUR = 3600.0

#: The statements the tamper demo attempts against the real audit database.
TAMPER_STATEMENTS = [
    "UPDATE audit_log SET reason = 'nothing to see here'",
    "UPDATE audit_log SET rule = NULL WHERE rule IS NOT NULL",
    "DELETE FROM audit_log WHERE event_type = 'POLICY_DENIED'",
    "DELETE FROM audit_log",
]


class PriceChange(BaseModel):
    price_paise: int


class CapChange(BaseModel):
    max_amount_paise: int


class AllowlistChange(BaseModel):
    #: Explicit SKUs, or omit and set allow_any=True for the wildcard.
    #: Deliberately not both at once -- "everything, plus these three" is not
    #: a real allowlist, it is the wildcard wearing a disguise.
    skus: list[str] = []
    allow_any: bool = False


class ExpiryChange(BaseModel):
    #: A duration from now, not an absolute timestamp. The mandate is always
    #: issued as "now plus a window" (see run_ui.py), and asking a merchant to
    #: type a Unix epoch invites exactly the kind of off-by-one-timezone
    #: mistake this project spends its whole audit-log story arguing against.
    extends_seconds: float


class VelocityChange(BaseModel):
    velocity_limit: int
    velocity_window_secs: float


class AdminLogin(BaseModel):
    username: str
    password: str


class NewItem(BaseModel):
    sku: str
    name: str
    price_paise: int


class ItemUpdate(BaseModel):
    #: Both optional -- send whichever changed. Omitting both is refused
    #: rather than treated as a no-op update, since a request naming no change
    #: at all is almost certainly a client mistake worth surfacing.
    name: Optional[str] = None
    price_paise: Optional[int] = None


def _audit_triggers(db_path: str) -> list[str]:
    """The append-only triggers, read straight from the schema.

    Returned to the page verbatim so a viewer can read the mechanism rather
    than trust an error message about it.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'audit_log' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


#: The lifecycle a purchase walks, in order, each stage named by the audit
#: event that marks it reached. The UI renders this as a progress track, so it
#: is defined here from real event types rather than invented on the client --
#: a tracker showing stages the log cannot substantiate would be decoration.
LIFECYCLE = [
    ("proposed", "Proposed", EventType.PURCHASE_REQUESTED),
    ("confirmed", "Confirmed", EventType.USER_CONFIRMED),
    ("authorised", "Authorised", EventType.POLICY_APPROVED),
    ("executing", "Executing", EventType.PAYMENT_ATTEMPTED),
    ("settled", "Settled", EventType.PAYMENT_CAPTURED),
]

#: Stages that END a purchase early. Reaching one means the track stops there
#: rather than continuing -- a denied request is finished, not stalled.
TERMINAL_STAGES = {
    EventType.POLICY_DENIED: ("denied", "Denied"),
    EventType.USER_DECLINED: ("declined", "Declined"),
    EventType.PAYMENT_FAILED: ("failed", "Failed"),
    EventType.PAYMENT_PENDING_VERIFICATION: ("unknown", "Outcome unknown"),
}


#: Terminal outcomes for a transaction, most specific first. A request's status
#: is the first of these that appears in its audit trail.
_STATUS_ORDER = [
    (EventType.PAYMENT_PENDING_VERIFICATION, "PENDING_VERIFICATION"),
    (EventType.PAYMENT_FAILED, "FAILED"),
    (EventType.POLICY_DENIED, "DENIED"),
    (EventType.USER_DECLINED, "DECLINED"),
    (EventType.IDEMPOTENCY_REPLAYED, "REPLAYED"),
    (EventType.PAYMENT_CAPTURED, "COMPLETED"),
    (EventType.POLICY_APPROVED, "APPROVED"),
]


def _reissue(engine: PolicyEngine, current: Mandate, **overrides) -> Mandate:
    """Revoke the active mandate and issue its replacement.

    Every merchant-facing edit route below is this one operation with a
    different field overridden -- mandates are immutable by design (Phase 3),
    so "editing" one always means this: the old one is kept, revoked, so the
    history of what was permitted when is never lost, and a fresh one takes
    its place with everything unchanged except what the merchant actually
    asked to change.
    """
    engine.mandates.revoke(current.mandate_id)
    fields = dict(
        agent_id=current.agent_id,
        max_amount_paise=current.max_amount_paise,
        allowed_skus=current.allowed_skus,
        currency=current.currency,
        expires_at=current.expires_at,
        velocity_limit=current.velocity_limit,
        velocity_window_secs=current.velocity_window_secs,
        cooldown_denials=current.cooldown_denials,
        cooldown_window_secs=current.cooldown_window_secs,
        created_at=engine._clock(),
    )
    fields.update(overrides)
    replacement = Mandate(**fields)
    engine.mandates.issue(replacement)
    return replacement


def _lifecycle_of(types: set) -> dict:
    """Where this request has got to, and how it ended if it did.

    `reached` lists the stages the audit log can actually evidence. `halted_at`
    is set when the request ended before the end of the track, so the UI can
    stop the progress bar rather than implying a settlement that never came.
    """
    reached = [key for key, _label, event in LIFECYCLE if event in types]
    halted = next(
        ((k, label) for event, (k, label) in TERMINAL_STAGES.items()
         if event in types), None)
    return {
        "reached": reached,
        "stage": (halted[0] if halted
                  else (reached[-1] if reached else "proposed")),
        "stage_label": (halted[1] if halted
                        else next((label for key, label, _e in LIFECYCLE
                                   if key == (reached[-1] if reached else "")),
                                  "Proposed")),
        "halted_at": halted[0] if halted else None,
    }


def create_demo_app(
    checkout: CheckoutService,
    engine: PolicyEngine,
    audit: AuditLog,
    catalog: Catalog,
    agent_id: str = "agent_alpha",
    faults: Optional[FaultInjector] = None,
    scheduler=None,
    narrator=None,
    payments_mode: str = "unknown",
    recommender=None,
    webhooks=None,
    admin_auth: Optional[AdminAuth] = None,
) -> FastAPI:
    demo = FastAPI(
        title="Zero-Trust Payment Authorization — reference client",
        description=(
            "A demonstration client for the API mounted at /api. It holds no "
            "authorisation logic; every decision shown here was made by the "
            "policy engine behind that API."
        ),
    )
    demo.mount("/api", create_app(checkout, narrator=narrator,
                                  recommender=recommender,
                                  webhooks=webhooks))

    index_html = FRONTEND_DIST / "index.html"
    if index_html.exists():
        demo.mount("/assets",
                   StaticFiles(directory=FRONTEND_DIST / "assets"),
                   name="assets")

    @demo.get("/")
    def index():
        if index_html.exists():
            return FileResponse(index_html)
        return HTMLResponse(_NOT_BUILT, status_code=200)

    def require_admin(authorization: str = Header(default="")) -> AdminSession:
        """A dependency guarding every mandate-edit, revoke, and catalog-write
        route.

        Not applied to the rest of `/demo` -- the Security Hub's other proof
        buttons (tamper-audit, fault injection, parser compromise) are
        deliberately public demonstrations of what the SYSTEM refuses; this
        guards the routes that are a genuine merchant decision -- editing the
        boundary an agent operates inside, or what the shop stocks and at what
        price -- which is a different kind of action from watching the system
        defend itself. The Security Hub's own `PriceSwap` demonstration runs
        through this same gate now, as an authenticated admin action, rather
        than a route anyone could hit.
        """
        if admin_auth is None or not admin_auth.is_configured:
            raise HTTPException(
                status_code=501,
                detail={"reason": "admin login is not configured on this server"})
        token = authorization[7:] if authorization.startswith("Bearer ") else authorization
        try:
            return admin_auth.verify(token)
        except AdminAuthError as exc:
            raise HTTPException(status_code=401, detail={"reason": exc.reason})

    @demo.post("/demo/admin/login")
    def admin_login(body: AdminLogin):
        if admin_auth is None:
            raise HTTPException(
                status_code=501,
                detail={"reason": "admin login is not configured on this server"})
        try:
            token = admin_auth.login(body.username, body.password)
        except AdminAuthError as exc:
            raise HTTPException(status_code=401, detail={"reason": exc.reason})
        return {"session_token": token,
                "expires_in_seconds": admin_auth.session_ttl_seconds}

    @demo.get("/demo/config")
    def config():
        """What the client needs to describe itself honestly.

        `parser` matters: the chat surface must not imply a conversational AI
        when the intent layer is a deterministic parser. It labels every reply
        with whatever this reports.
        """
        import os

        # Whichever provider is actually configured -- reporting only the one
        # this build no longer uses would be a quietly wrong green light.
        providers = [name for name, var in (("groq", "GROQ_API_KEY"),
                                            ("anthropic", "ANTHROPIC_API_KEY"))
                     if os.environ.get(var)]
        return {
            "agent_id": agent_id,
            "parser": getattr(checkout.parser, "name", "none"),
            "llm_configured": bool(providers),
            "llm_providers": providers,
            # Named separately from `parser` so the UI can say "backed up by"
            # rather than implying a fallback already happened.
            "parser_fallback": getattr(checkout.parser, "fallback_name", None),
            # Whether orders actually reach Razorpay. The UI used to state
            # "Test mode" statically, which stayed reassuringly true-looking
            # even when nothing was leaving the process at all -- the two
            # cases are indistinguishable on screen, so the badge has to be
            # told which one it is rather than assuming.
            "payments_mode": payments_mode,
            # Still false with an LLM in place: the intent layer performs
            # structured extraction, not conversation. The model does not
            # write the replies the user sees.
            "conversational": False,
        }

    @demo.get("/demo/mandate/{agent}")
    def mandate(agent: str):
        """The boundary, visible before anything is spent."""
        active = engine.mandates.active_for_agent(agent)
        if active is None:
            raise HTTPException(status_code=404,
                                detail={"reason": f"no mandate for '{agent}'"})
        now = engine._clock()
        used = engine.slots_used(agent, active.velocity_window_secs)
        return {
            "mandate_id": active.mandate_id,
            "agent_id": active.agent_id,
            "max_amount_paise": active.max_amount_paise,
            "allowed_skus": sorted(active.allowed_skus),
            # So the UI can say "any catalog item" instead of rendering a
            # literal "*", which looks like a bug rather than a policy.
            "allows_any_sku": ANY_SKU in active.allowed_skus,
            "currency": active.currency,
            "expires_at": active.expires_at,
            "seconds_until_expiry": max(0.0, active.expires_at - now),
            "expired": active.is_expired(now),
            "velocity_limit": active.velocity_limit,
            "velocity_window_secs": active.velocity_window_secs,
            "velocity_used": used,
            "velocity_remaining": max(0, active.velocity_limit - used),
        }

    @demo.get("/demo/pending/{request_id}")
    def pending(request_id: str):
        """The pending record, including its idempotency key.

        The key is deliberately NOT in the production API's response: this is a
        transparency view, and adding a field to `create_app()` for the sake of
        the UI is the same mistake as adding a route to it. The page reads it
        from here instead.
        """
        from zerotrust.checkout import CheckoutError

        try:
            record = checkout.get_pending(request_id)
        except CheckoutError as exc:
            raise HTTPException(status_code=404, detail={"reason": exc.reason})
        return {
            **record.as_dict(),
            "idempotency_key": record.idempotency_key,
            "created_at": record.created_at,
        }

    @demo.get("/demo/audit/recent")
    def recent_audit(limit: int = 40):
        entries = audit.all()[-limit:]
        return {
            "count": len(entries),
            "events": [
                {
                    "event_id": e.event_id,
                    "event_type": e.event_type.value,
                    "actor": e.actor.value,
                    "occurred_at": e.occurred_at,
                    "request_id": e.request_id,
                    "rule": e.rule,
                    "reason": e.reason,
                    "details": e.details,
                }
                for e in entries
            ],
        }

    @demo.post("/demo/catalog/{sku}/price")
    def set_price(sku: str, body: PriceChange,
                  admin: AdminSession = Depends(require_admin)):
        """Change a price mid-flight, to drive the confirm-time rejection.

        A legitimate merchant action, not a backdoor: the purchase that gets
        rejected afterwards is rejected by the same re-validation that runs on
        every confirmation.
        """
        try:
            before = catalog.current_price_paise(sku)
        except ItemNotInCatalog as exc:
            raise HTTPException(status_code=404, detail={"reason": str(exc)})
        catalog.set_price(sku, body.price_paise)
        return {"sku": sku, "was_paise": before, "now_paise": body.price_paise}

    @demo.post("/demo/mandate/{agent}/cap")
    def set_cap(agent: str, body: CapChange, admin: AdminSession = Depends(require_admin)):
        """Let the merchant move the per-transaction limit.

        Issuing a mandate is a MERCHANT action, which is why this is allowed at
        all: the mandate is the merchant's statement of how much they are
        willing to let an agent spend. The agent cannot reach this route, and
        nothing about it lets an agent raise its own ceiling -- that would
        invert the entire authorisation model.

        Mandates are immutable, so this revokes the old one and issues a
        replacement rather than editing in place. The old mandate stays in the
        store, revoked, so the history of what was permitted when survives.
        """
        if body.max_amount_paise <= 0:
            raise HTTPException(
                status_code=400,
                detail={"reason": "the cap must be a positive number of paise"})

        current = engine.mandates.active_for_agent(agent)
        if current is None:
            raise HTTPException(status_code=404,
                                detail={"reason": f"no mandate for '{agent}'"})

        _reissue(engine, current, max_amount_paise=body.max_amount_paise)
        return {"agent_id": agent,
                "was_paise": current.max_amount_paise,
                "now_paise": body.max_amount_paise}

    @demo.post("/demo/mandate/{agent}/allowlist")
    def set_allowlist(agent: str, body: AllowlistChange, admin: AdminSession = Depends(require_admin)):
        """Let the merchant change which items the agent may buy at all.

        Same merchant-only reasoning as `set_cap`: the agent cannot reach this
        route, and nothing here lets an agent add itself to its own allowlist.
        `allow_any` is the wildcard used elsewhere in this codebase (ANY_SKU) --
        the cap remains the constraint that actually bounds spend when it's
        set, so widening the allowlist is not the same as removing all limits.
        """
        current = engine.mandates.active_for_agent(agent)
        if current is None:
            raise HTTPException(status_code=404,
                                detail={"reason": f"no mandate for '{agent}'"})

        if body.allow_any:
            skus = frozenset({ANY_SKU})
        else:
            unknown = []
            for s in body.skus:
                try:
                    catalog.get(s)
                except ItemNotInCatalog:
                    unknown.append(s)
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail={"reason": f"not in the catalog: {', '.join(unknown)}"})
            if not body.skus:
                raise HTTPException(
                    status_code=400,
                    detail={"reason": "an allowlist needs at least one SKU, "
                                      "or allow_any=true for the wildcard"})
            skus = frozenset(body.skus)

        _reissue(engine, current, allowed_skus=skus)
        return {"agent_id": agent,
                "was_skus": sorted(current.allowed_skus),
                "now_skus": sorted(skus)}

    @demo.post("/demo/mandate/{agent}/expiry")
    def set_expiry(agent: str, body: ExpiryChange, admin: AdminSession = Depends(require_admin)):
        """Let the merchant push out (or pull in) when the mandate lapses."""
        if body.extends_seconds <= 0:
            raise HTTPException(
                status_code=400,
                detail={"reason": "extends_seconds must be positive -- to "
                                  "shorten a mandate to expire sooner, use a "
                                  "smaller positive value from now, not a "
                                  "negative one; to end it immediately, revoke it"})

        current = engine.mandates.active_for_agent(agent)
        if current is None:
            raise HTTPException(status_code=404,
                                detail={"reason": f"no mandate for '{agent}'"})

        new_expiry = engine._clock() + body.extends_seconds
        _reissue(engine, current, expires_at=new_expiry)
        return {"agent_id": agent,
                "was_expires_at": current.expires_at,
                "now_expires_at": new_expiry}

    @demo.post("/demo/mandate/{agent}/velocity")
    def set_velocity(agent: str, body: VelocityChange, admin: AdminSession = Depends(require_admin)):
        """Let the merchant change the spend-frequency limit and its window."""
        if body.velocity_limit <= 0:
            raise HTTPException(
                status_code=400,
                detail={"reason": "velocity_limit must be a positive integer"})
        if body.velocity_window_secs <= 0:
            raise HTTPException(
                status_code=400,
                detail={"reason": "velocity_window_secs must be positive"})

        current = engine.mandates.active_for_agent(agent)
        if current is None:
            raise HTTPException(status_code=404,
                                detail={"reason": f"no mandate for '{agent}'"})

        _reissue(engine, current, velocity_limit=body.velocity_limit,
                velocity_window_secs=body.velocity_window_secs)
        return {"agent_id": agent,
                "was": {"velocity_limit": current.velocity_limit,
                        "velocity_window_secs": current.velocity_window_secs},
                "now": {"velocity_limit": body.velocity_limit,
                        "velocity_window_secs": body.velocity_window_secs}}

    @demo.post("/demo/mandate/{agent}/revoke")
    def revoke_mandate(agent: str, admin: AdminSession = Depends(require_admin)):
        """Withdraw an agent's authority immediately -- the kill switch.

        A MERCHANT action, like setting the cap: the mandate is the merchant's
        statement of what an agent may spend, so withdrawing it is theirs to
        do. The agent has no route to this and cannot reinstate itself.

        Revocation is not deletion. The row stays with `revoked_at` set, so the
        record of what was permitted, and when it stopped being permitted,
        survives -- the same reason the audit log is append-only. What changes
        is that `active_for_agent()` stops returning it, so the very next
        request is denied with NO_ACTIVE_MANDATE rather than being evaluated
        against a mandate nobody stands behind any more.
        """
        current = engine.mandates.active_for_agent(agent)
        if current is None:
            raise HTTPException(
                status_code=404,
                detail={"reason": f"no active mandate for '{agent}' to revoke"})

        engine.mandates.revoke(current.mandate_id)
        return {
            "agent_id": agent,
            "revoked_mandate_id": current.mandate_id,
            "was_max_amount_paise": current.max_amount_paise,
            "note": "the next request from this agent is denied before its "
                    "mandate rules are evaluated",
        }

    @demo.post("/demo/catalog")
    def add_item(body: NewItem, admin: AdminSession = Depends(require_admin)):
        """Stock a new product, so an agent can be asked for anything.

        A merchant action, and the reason it is safe: the PRICE is set here,
        by the merchant, and stored in the catalog. Neither the customer
        nor the model supplies it. That keeps the invariant that makes
        confirm-time re-validation meaningful -- prices come from the catalog,
        never from the request (see `_SYSTEM_PROMPT` in zerotrust/intent.py).

        A mandate carrying ANY_SKU covers new items automatically; a mandate
        with an explicit list does not, which is the correct behaviour -- an
        item nobody authorised should not become spendable by appearing.
        """
        sku = body.sku.strip().upper()
        if not sku or not body.name.strip():
            raise HTTPException(status_code=400,
                                detail={"reason": "sku and name are required"})
        if body.price_paise <= 0:
            raise HTTPException(status_code=400,
                                detail={"reason": "price must be positive"})
        if catalog.has(sku):
            raise HTTPException(status_code=409,
                                detail={"reason": f"{sku} is already stocked"})

        catalog.add(CatalogItem(sku=sku, name=body.name.strip(),
                                price_paise=body.price_paise))
        mandate = engine.mandates.active_for_agent(agent_id)
        return {
            "sku": sku,
            "name": body.name.strip(),
            "price_paise": body.price_paise,
            # Said plainly, because "I stocked it but the agent still cannot
            # buy it" is otherwise a confusing five minutes.
            "purchasable_by_agent": bool(mandate and mandate.allows_sku(sku)),
        }

    @demo.post("/demo/catalog/{sku}")
    def update_item(sku: str, body: ItemUpdate,
                     admin: AdminSession = Depends(require_admin)):
        """Rename an item and/or reprice it in one call.

        The SKU itself is never editable -- it is what every reference to
        this item (an allowlist, a pending purchase, the audit log) is keyed
        on, so changing it would silently orphan all of them. Renaming or
        repricing leaves it alone; only `name` and/or `price_paise` move.
        """
        if body.name is None and body.price_paise is None:
            raise HTTPException(
                status_code=400,
                detail={"reason": "name and/or price_paise must be given"})
        if body.price_paise is not None and body.price_paise <= 0:
            raise HTTPException(status_code=400,
                                detail={"reason": "price must be positive"})
        if body.name is not None and not body.name.strip():
            raise HTTPException(status_code=400,
                                detail={"reason": "name cannot be blank"})
        try:
            before = catalog.get(sku)
        except ItemNotInCatalog as exc:
            raise HTTPException(status_code=404, detail={"reason": str(exc)})

        if body.name is not None:
            catalog.rename(sku, body.name.strip())
        if body.price_paise is not None:
            catalog.set_price(sku, body.price_paise)

        after = catalog.get(sku)
        return {
            "sku": sku,
            "was_name": before.name, "now_name": after.name,
            "was_paise": before.price_paise, "now_paise": after.price_paise,
        }

    @demo.delete("/demo/catalog/{sku}")
    def delete_item(sku: str, admin: AdminSession = Depends(require_admin)):
        """Unstock an item entirely.

        A mandate's allowlist may still name this SKU afterwards -- left as
        is, not cleaned up, because reaching into every agent's mandate from
        here would make this route responsible for a decision it has no
        business making. The effect is still correct: a purchase attempt
        against a removed SKU fails at the catalog lookup with
        `ITEM_NOT_IN_CATALOG`, before the policy engine ever runs, same as any
        SKU that never existed.
        """
        try:
            item = catalog.get(sku)
        except ItemNotInCatalog as exc:
            raise HTTPException(status_code=404, detail={"reason": str(exc)})
        catalog.remove(sku)
        return {"sku": sku, "was_name": item.name, "was_paise": item.price_paise}

    @demo.post("/demo/tamper-audit")
    def tamper_audit():
        """Try to rewrite history, and show exactly why it fails.

        Runs against the LIVE audit database with a raw sqlite3 connection,
        bypassing AuditLog entirely -- that is the point. The guarantee is a
        property of the store, not of the code path used to reach it.
        """
        triggers = _audit_triggers(audit.db_path)

        # The one real risk in this endpoint is that its safety depends on the
        # very thing it demonstrates. So verify the guarantee exists before
        # relying on it, and refuse rather than run unprotected DELETEs.
        if len(triggers) < 2:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GUARANTEE_MISSING",
                    "reason": (
                        "the append-only triggers are not present on this "
                        "database, so this demonstration will not run a DELETE "
                        "against it"
                    ),
                    "triggers_found": triggers,
                },
            )

        before = len(audit.all())
        attempts = []
        conn = sqlite3.connect(audit.db_path)
        try:
            for sql in TAMPER_STATEMENTS:
                try:
                    cursor = conn.execute(sql)
                    # No error AND no rows touched is not a defence -- the
                    # statement simply matched nothing, so the trigger never
                    # had to fire. Reporting that as "not blocked" would show
                    # a false breach on screen; reporting it as "blocked"
                    # would claim a defence that was never tested. It is its
                    # own outcome.
                    if cursor.rowcount in (0, -1):
                        attempts.append({
                            "sql": sql,
                            "outcome": "NO_ROWS_MATCHED",
                            "error": None,
                            "rows": 0,
                        })
                    else:
                        attempts.append({
                            "sql": sql,
                            "outcome": "SUCCEEDED",
                            "error": None,
                            "rows": cursor.rowcount,
                        })
                except sqlite3.Error as exc:
                    attempts.append({
                        "sql": sql,
                        "outcome": "BLOCKED",
                        "error": str(exc),
                        "rows": 0,
                    })
            conn.rollback()
        finally:
            conn.close()
        after = len(audit.all())

        tested = [a for a in attempts if a["outcome"] != "NO_ROWS_MATCHED"]
        breached = [a for a in attempts if a["outcome"] == "SUCCEEDED"]

        return {
            "attempts": attempts,
            "tested": len(tested),
            "blocked": len([a for a in tested if a["outcome"] == "BLOCKED"]),
            "breached": len(breached),
            # True only if every statement that actually matched rows was
            # refused. A run where nothing matched proves nothing and says so.
            "all_blocked": bool(tested) and not breached,
            "entries_before": before,
            "entries_after": after,
            "unchanged": before == after,
            "triggers": triggers,
        }

    @demo.post("/demo/agent")
    def fresh_agent():
        """Issue a throwaway agent with its own mandate.

        The Security Hub's demonstrations each take one of these. Sharing a
        single agent made them share a velocity budget, so a proof run later in
        the sequence measured a budget the earlier ones had already spent --
        and reported the wrong thing while looking fine. That is the same
        failure recorded in JOURNAL.md Entry 7, and the same fix: each
        demonstration stands alone.
        """
        template = engine.mandates.active_for_agent(agent_id)
        if template is None:
            raise HTTPException(
                status_code=409,
                detail={"reason": f"no mandate for '{agent_id}' to copy"})
        now = engine._clock()
        new_id = f"{agent_id}_demo_{uuid.uuid4().hex[:8]}"
        engine.mandates.issue(Mandate(
            agent_id=new_id,
            max_amount_paise=template.max_amount_paise,
            allowed_skus=template.allowed_skus,
            currency=template.currency,
            expires_at=now + template.velocity_window_secs * 24,
            velocity_limit=template.velocity_limit,
            velocity_window_secs=template.velocity_window_secs,
            created_at=now,
        ))
        return {"agent_id": new_id, "velocity_limit": template.velocity_limit}

    @demo.get("/demo/sweep")
    def sweep_status():
        """What the periodic reconciliation sweep has been doing."""
        if scheduler is None:
            return {"running": False, "cycles": 0, "last_cycle": None,
                    "records_resolved": {}, "errors": 0,
                    "note": "no scheduler in this stack"}
        return scheduler.status()

    @demo.post("/demo/sweep/run")
    def sweep_now():
        """Run one sweep immediately, rather than waiting for the interval."""
        if scheduler is None:
            raise HTTPException(status_code=501,
                                detail={"reason": "no scheduler in this stack"})
        return scheduler.run_once().as_dict()

    @demo.post("/demo/webhook/simulate")
    def simulate_webhook(tamper: bool = False):
        """Send this server a webhook it signs itself. DEMO ONLY.

        No real Razorpay delivery arrives in this project: capture is
        simulated, and `order.paid` only fires after a genuine browser
        checkout. Rather than leave the receiver unexercised, this builds a
        Razorpay-shaped payload, signs it with the configured secret, and posts
        it through the same verification path an external delivery would take.

        It is labelled synthetic in its own payload and in the response, so
        nothing here can be mistaken for evidence that live delivery works.
        With `tamper=true` the body is edited AFTER signing -- the signature
        then covers different bytes, and the receiver refuses it.
        """
        if webhooks is None or not webhooks.is_configured:
            raise HTTPException(
                status_code=501,
                detail={
                    "reason": "no RAZORPAY_WEBHOOK_SECRET configured, so the "
                              "receiver refuses every delivery rather than "
                              "trusting it",
                })

        from zerotrust.webhook import compute_signature

        body = json.dumps({
            "entity": "event",
            "event": "order.paid",
            "created_at": int(time.time()),
            "synthetic": True,
            "payload": {"order": {"entity": {
                "id": "order_SYNTHETIC",
                "receipt": "ui_synthetic_demo",
                "amount": 15000,
                "status": "paid",
            }}},
        }).encode("utf-8")
        signature = compute_signature(body, webhooks.secret)

        if tamper:
            # Signed, then altered. This is exactly the attack the HMAC exists
            # to stop, performed on the real receiver rather than described.
            body = body.replace(b'"amount": 15000', b'"amount": 100000000')

        result = webhooks.receive(body, signature)
        return {
            "synthetic": True,
            "note": ("Generated and signed by this server. Razorpay sent "
                     "nothing; test mode produces no real delivery."),
            "tampered_after_signing": tamper,
            "result": result.as_dict(),
        }

    @demo.get("/demo/stats")
    def stats():
        """Dashboard totals, derived from the audit log rather than counted
        separately -- so the numbers cannot disagree with the record."""
        entries = audit.all()
        denials = {}
        spend = 0
        for e in entries:
            if e.event_type is EventType.POLICY_DENIED and e.rule:
                denials[e.rule] = denials.get(e.rule, 0) + 1
            if e.event_type is EventType.PAYMENT_CAPTURED:
                spend += int(e.details.get("amount_paise") or 0)
        return {
            "audit_entries": len(entries),
            "purchases": audit.count_of(EventType.PAYMENT_CAPTURED),
            "spend_paise": spend,
            "replays": audit.count_of(EventType.IDEMPOTENCY_REPLAYED),
            "conflicts": audit.count_of(EventType.IDEMPOTENCY_CONFLICT),
            "pending_verification": audit.count_of(
                EventType.PAYMENT_PENDING_VERIFICATION),
            "denials": denials,
            "denials_total": sum(denials.values()),
        }

    @demo.get("/demo/transactions")
    def transactions(limit: int = 25):
        """One row per request, assembled by grouping the audit log.

        Deliberately built from the log rather than by reading the idempotency
        store: the store is Phase 1's core and this view has no business
        reaching into it.
        """
        grouped: dict[str, list] = {}
        for entry in audit.all():
            if entry.request_id:
                grouped.setdefault(entry.request_id, []).append(entry)

        rows = []
        for request_id, entries in grouped.items():
            types = {e.event_type for e in entries}
            status = "IN_PROGRESS"
            for event_type, label in _STATUS_ORDER:
                if event_type in types:
                    status = label
                    break
            first = entries[0]
            verdict = next(
                (e for e in entries
                 if e.event_type in (EventType.POLICY_DENIED,
                                     EventType.POLICY_APPROVED)), None)
            rows.append({
                "request_id": request_id,
                "status": status,
                "agent_id": first.agent_id,
                "idempotency_key": next(
                    (e.idempotency_key for e in entries if e.idempotency_key),
                    None),
                "sku": first_detail(entries, "sku"),
                # PURCHASE_REQUESTED records this as `displayed_amount_paise`;
                # reading only `amount_paise` off the opening event meant every
                # row reported no amount at all.
                "amount_paise": first_detail(entries, "amount_paise",
                                             "displayed_amount_paise"),
                "quantity": first_detail(entries, "quantity"),
                # Read back from the logged provider response so a receipt
                # opened after a reload can still show the order.
                "order_id": provider_order_id(entries),
                "started_at": first.occurred_at,
                "updated_at": entries[-1].occurred_at,
                "rule": verdict.rule if verdict else None,
                "reason": verdict.reason if verdict else None,
                "event_count": len(entries),
                **_lifecycle_of(types),
            })
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return {"count": len(rows), "transactions": rows[:limit]}

    @demo.get("/demo/security/layers")
    def security_layers():
        """Live evidence for each protection that actually exists.

        Nothing here is aspirational. Three layers a reader might expect --
        fraud detection, tokenization, and multi-factor authentication -- are
        NOT implemented in this system, and are reported as such rather than
        being rendered as though they were.
        """
        triggers = _audit_triggers(audit.db_path)
        chain = audit.verify()
        parsed_fields = sorted(ParsedIntent.__dataclass_fields__)
        # Named from the parser actually in use, not from an env var: an
        # unused key must not make the page claim data goes somewhere it does
        # not, and a configured parser must not be able to hide that it does.
        parser_name = getattr(checkout.parser, "name", "") or ""
        llm_provider = next(
            (p for p in ("groq", "claude") if p in parser_name.lower()), None)
        sealed_messages = sum(
            1 for e in audit.all()
            if e.event_type == EventType.INTENT_PARSED
            and "raw_text_sealed" in e.details
        )
        plaintext_messages = sum(
            1 for e in audit.all()
            if e.event_type == EventType.INTENT_PARSED
            and "raw_text" in e.details
        )
        return {
            "implemented": [
                {
                    "id": "exactly_once",
                    "title": "Exactly-once execution",
                    "mechanism": "UNIQUE-constraint INSERT on the idempotency key",
                    "evidence": {
                        "replays_served": audit.count_of(
                            EventType.IDEMPOTENCY_REPLAYED),
                        "conflicts_rejected": audit.count_of(
                            EventType.IDEMPOTENCY_CONFLICT),
                        "executions": audit.count_of(
                            EventType.IDEMPOTENCY_EXECUTED),
                    },
                },
                {
                    "id": "mandate",
                    "title": "Bounded mandate",
                    "mechanism": "Cap, allowlist, expiry and a sliding velocity window",
                    "evidence": {
                        "rules_enforced": ["AMOUNT_EXCEEDS_CAP", "SKU_NOT_ALLOWED",
                                           "MANDATE_EXPIRED", "VELOCITY_EXCEEDED",
                                           "CURRENCY_MISMATCH", "MALFORMED_REQUEST",
                                           "NO_ACTIVE_MANDATE"],
                        "requests_denied": stats()["denials_total"],
                        "denials_by_rule": stats()["denials"],
                    },
                },
                {
                    "id": "confirmation",
                    "title": "Confirmation is not authorisation",
                    "mechanism": "Policy runs on confirmed requests; only POLICY_ENGINE authorises",
                    "evidence": {
                        "human_confirmations": audit.count_of(
                            EventType.USER_CONFIRMED),
                        "declined": audit.count_of(EventType.USER_DECLINED),
                    },
                },
                {
                    "id": "append_only_audit",
                    "title": "Append-only, hash-chained audit log",
                    "mechanism": (
                        "BEFORE UPDATE / BEFORE DELETE triggers RAISE(ABORT); "
                        "each entry carries the SHA-256 of its contents linked "
                        "to its predecessor"
                    ),
                    # The chain detects what the triggers cannot see: a change
                    # made around the database rather than through it. Say
                    # plainly where that stops, because a self-consistent chain
                    # is not the same as an unaltered one.
                    "boundary": (
                        "The triggers refuse edits through the database; the "
                        "chain makes an edit made around it — a swapped file, "
                        "a restored backup, a row rewritten after dropping the "
                        "triggers — detectable. It does not detect a complete "
                        "rewrite: anyone able to drop the triggers can also "
                        "recompute every later hash. Catching that needs the "
                        "head hash below compared against a copy held "
                        "elsewhere, which this demo does not do."
                    ),
                    "evidence": {
                        "entries": len(audit.all()),
                        # The string, not the bare boolean: `intact` is True on
                        # a log with nothing chained, and "chain intact" beside
                        # zero protected entries is a claim this page must not
                        # make. See ChainReport.summary.
                        "hash_chain": chain.summary,
                        "head_hash": (chain.head or "—")[:16],
                        "triggers": triggers,
                        "guarantee_present": len(triggers) >= 2,
                    },
                },
                {
                    "id": "webhook_verification",
                    "title": "Webhooks are verified, and cannot authorise",
                    "mechanism": (
                        "HMAC-SHA256 over the raw request body, compared with "
                        "hmac.compare_digest; a verified delivery triggers "
                        "reconciliation and writes nothing"
                    ),
                    "boundary": (
                        "No real Razorpay delivery reaches this project: "
                        "capture is simulated and order.paid needs a genuine "
                        "browser checkout, so the demonstration signs its own "
                        "delivery and says so. The signature proves who sent a "
                        "message, never that its contents are true — which is "
                        "why the payload's amounts and statuses are discarded "
                        "and only the receipt is used, to go and ask the "
                        "provider directly."
                    ),
                    "evidence": {
                        "receiver_configured": bool(
                            webhooks is not None and webhooks.is_configured),
                        "verified_deliveries": audit.count_of(
                            EventType.WEBHOOK_RECEIVED),
                        "refused_deliveries": audit.count_of(
                            EventType.WEBHOOK_REJECTED),
                        "can_write_the_ledger": False,
                    },
                },
                {
                    "id": "admin_auth",
                    "title": "Editing the mandate requires a real login",
                    "mechanism": (
                        "bcrypt-hashed password, a short-lived HMAC-signed "
                        "session; an unconfigured admin refuses every login "
                        "rather than leaving the mandate editor open"
                    ),
                    "boundary": (
                        "One admin account, not a multi-user system — this "
                        "matches the one-merchant shape of the rest of the "
                        "reference client, not a claim of role-based access "
                        "control. The session is stateless (verified by "
                        "recomputing its signature, never looked up), so it "
                        "does not survive changing the signing secret, and "
                        "'multi-factor' genuinely is not implemented: a "
                        "password is the only factor."
                    ),
                    "evidence": {
                        "admin_login_configured": bool(
                            admin_auth is not None and admin_auth.is_configured),
                        "session_ttl_seconds": (
                            admin_auth.session_ttl_seconds
                            if admin_auth is not None else 0),
                        "mandate_edit_routes_gated": 5,
                    },
                },
                {
                    "id": "price_revalidation",
                    "title": "Confirm-time price re-validation",
                    "mechanism": "The amount is re-read from the catalog at confirm time",
                    "evidence": {
                        "validations": audit.count_of(EventType.PRICE_VALIDATED),
                    },
                },
                {
                    "id": "unknown_outcomes",
                    "title": "Unknown outcomes stay unknown",
                    "mechanism": "A timeout freezes the record as PENDING_VERIFICATION",
                    "evidence": {
                        "pending": audit.count_of(
                            EventType.PAYMENT_PENDING_VERIFICATION),
                        "divergences_detected": audit.count_of(
                            EventType.DIVERGENCE_DETECTED),
                        "divergences_resolved": audit.count_of(
                            EventType.DIVERGENCE_RESOLVED),
                    },
                },
                {
                    "id": "llm_no_authority",
                    "title": "The LLM holds no authority",
                    "mechanism": "ParsedIntent has no field for an amount or an approval",
                    "evidence": {
                        "parsed_intent_fields": parsed_fields,
                        "can_state_a_price": "amount_paise" in parsed_fields,
                        "can_approve": "approved" in parsed_fields,
                        "intents_parsed": audit.count_of(EventType.INTENT_PARSED),
                    },
                },
                {
                    "id": "e2e_chat_encryption",
                    "title": "End-to-end encrypted chat",
                    "mechanism": "NaCl Box (X25519 + XSalsa20-Poly1305): browser encrypts to the server's public key; only ciphertext is ever stored",
                    # The boundary, stated rather than implied. Parsing needs
                    # the plaintext, so with an LLM parser configured the
                    # decrypted text is also sent to that provider. Storage is
                    # what this protects; it is not protection from everyone.
                    "boundary": (
                        "Protects data at rest: a database backup, replica or "
                        "leaked file contains no readable message. It does not "
                        "hide the message from the running server, which must "
                        "decrypt it to parse it"
                        + (f", nor from the {llm_provider} API it is sent to for "
                           f"parsing" if llm_provider else "")
                        + "."
                    ),
                    "evidence": {
                        "e2e_configured": checkout.server_identity is not None,
                        "sealed_messages_stored": sealed_messages,
                        "plaintext_messages_stored": plaintext_messages,
                        "decrypted_text_sent_to": llm_provider or "nothing external",
                        "server_public_key_prefix": (
                            checkout.server_identity.public_key_b64[:12] + "…"
                            if checkout.server_identity else None
                        ),
                    },
                },
                {
                    "id": "instant_revocation",
                    "title": "Authority can be withdrawn instantly",
                    "mechanism": "Revoking a mandate takes effect on the next request, including one already awaiting confirmation",
                    # Worth stating because "we can revoke" is a weaker claim
                    # than it sounds: what matters is WHEN it takes effect. A
                    # revocation that only applied to future drafts would leave
                    # every pending request still spendable.
                    "boundary": (
                        "Stops future authorisation. It cannot claw back a "
                        "payment that has already executed -- an order already "
                        "placed with Razorpay stays placed, and reversing it is "
                        "a refund, which this system does not implement"
                    ),
                    "evidence": {
                        "revocable_now": engine.mandates.active_for_agent(
                            agent_id) is not None,
                        "mandates_revoked": engine.mandates.revoked_count(),
                        "denial_rule": "NO_ACTIVE_MANDATE",
                        "revoked_records_deleted": False,
                    },
                },
            ],
            "not_implemented": [
                {"id": "fraud_detection", "title": "Fraud detection",
                 "note": "Not implemented. No scoring or anomaly detection."},
                {"id": "tokenization", "title": "Tokenization",
                 "note": "Not implemented. Card data never reaches this system."},
                {"id": "mfa", "title": "Multi-factor authentication",
                 "note": ("Not implemented for the one admin login this system "
                         "has (see 'Editing the mandate requires a real login' "
                         "above) — a password is the only factor, and there is "
                         "no per-customer account system at all.")},
            ],
        }

    @demo.get("/demo/adversarial")
    def adversarial():
        """The generated results file, not a retyped copy of it."""
        path = Path(__file__).parent.parent / "docs" / "adversarial-results.json"
        if not path.exists():
            raise HTTPException(
                status_code=404,
                detail={"reason": "no generated results; run "
                                  "scripts/run_adversarial_suite.py"})
        import json
        return json.loads(path.read_text())

    @demo.post("/demo/fault/timeout")
    def arm_timeout():
        """Arm a one-shot provider timeout, to demonstrate PENDING_VERIFICATION."""
        if faults is None:
            raise HTTPException(
                status_code=501,
                detail={"reason": "this demo stack has no fault injector"})
        faults.arm(Fault.PROVIDER_TIMEOUT)
        return {"armed": Fault.PROVIDER_TIMEOUT.value, "one_shot": True}

    @demo.post("/demo/parser/compromise")
    def compromise_parser(enabled: bool = True):
        """Replace the intent parser with one that fully serves an attacker.

        The point is that it changes nothing about what can be authorised.
        """
        from zerotrust.adversary import CompromisedParser

        if enabled:
            checkout.parser = CompromisedParser()
        else:
            from zerotrust.intent import RuleBasedIntentParser

            checkout.parser = RuleBasedIntentParser(catalog)
        return {"parser": checkout.parser.name,
                "note": "a compromised parser can propose; it cannot approve"}

    return demo
