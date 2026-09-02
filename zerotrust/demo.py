"""A reference client for the API. Not a product, and not an authority.

This wraps the production app rather than extending it:

    demo = FastAPI()
    demo.mount("/api", create_app(checkout, narrator=narrator))   # unmodified

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

import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from zerotrust.api import create_app
from zerotrust.audit import AuditLog, EventType
from zerotrust.catalog import Catalog, ItemNotInCatalog
from zerotrust.checkout import CheckoutService
from zerotrust.faults import Fault, FaultInjector
from zerotrust.mandate import Mandate
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


def create_demo_app(
    checkout: CheckoutService,
    engine: PolicyEngine,
    audit: AuditLog,
    catalog: Catalog,
    agent_id: str = "agent_alpha",
    faults: Optional[FaultInjector] = None,
    scheduler=None,
    narrator=None,
) -> FastAPI:
    demo = FastAPI(
        title="Zero-Trust Payment Authorization — reference client",
        description=(
            "A demonstration client for the API mounted at /api. It holds no "
            "authorisation logic; every decision shown here was made by the "
            "policy engine behind that API."
        ),
    )
    demo.mount("/api", create_app(checkout, narrator=narrator))

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

    @demo.get("/demo/config")
    def config():
        """What the client needs to describe itself honestly.

        `parser` matters: the chat surface must not imply a conversational AI
        when the intent layer is a deterministic parser. It labels every reply
        with whatever this reports.
        """
        import os

        return {
            "agent_id": agent_id,
            "parser": getattr(checkout.parser, "name", "none"),
            "llm_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
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
    def set_price(sku: str, body: PriceChange):
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
                "sku": first.details.get("sku"),
                "amount_paise": first.details.get("amount_paise"),
                "started_at": first.occurred_at,
                "updated_at": entries[-1].occurred_at,
                "rule": verdict.rule if verdict else None,
                "reason": verdict.reason if verdict else None,
                "event_count": len(entries),
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
        parsed_fields = sorted(ParsedIntent.__dataclass_fields__)
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
                    "title": "Append-only audit log",
                    "mechanism": "BEFORE UPDATE / BEFORE DELETE triggers RAISE(ABORT)",
                    "evidence": {
                        "entries": len(audit.all()),
                        "triggers": triggers,
                        "guarantee_present": len(triggers) >= 2,
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
                    "evidence": {
                        "e2e_configured": checkout.server_identity is not None,
                        "sealed_messages_stored": sealed_messages,
                        "plaintext_messages_stored": plaintext_messages,
                        "server_public_key_prefix": (
                            checkout.server_identity.public_key_b64[:12] + "…"
                            if checkout.server_identity else None
                        ),
                    },
                },
            ],
            "not_implemented": [
                {"id": "fraud_detection", "title": "Fraud detection",
                 "note": "Not implemented. No scoring or anomaly detection."},
                {"id": "tokenization", "title": "Tokenization",
                 "note": "Not implemented. Card data never reaches this system."},
                {"id": "mfa", "title": "Multi-factor authentication",
                 "note": "Not implemented. No user system, no login, no sessions."},
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
