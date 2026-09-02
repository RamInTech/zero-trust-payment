"""Serve the reference client.

    uv run python scripts/run_ui.py              # real Razorpay if .env has keys
    uv run python scripts/run_ui.py --simulated  # force the offline provider

Then open http://127.0.0.1:8000

Both the payment provider and the intent parser are chosen by what is
configured, not by a flag, and the choice is PRINTED at startup. A demo that
quietly stopped making real calls -- or quietly stopped using the real model --
would still look completely fine on screen, which is exactly the class of
failure this project keeps writing journal entries about.

The UI is a demonstration client for the API it mounts at /api. It holds no
authorisation logic. DEMO ONLY: no authentication, and the audit view exposes
mandate internals.
"""

import os
import sys
import time

import uvicorn
from dotenv import load_dotenv

from zerotrust.audit import AuditLog
from zerotrust.catalog import demo_catalog
from zerotrust.e2e import ServerIdentity
from zerotrust.faults import Fault, FaultInjector
from zerotrust.checkout import CheckoutService
from zerotrust.demo import create_demo_app
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.intent import (
    FallbackIntentParser, GroqIntentParser, RuleBasedIntentParser,
)
from zerotrust.mandate import ANY_SKU, Mandate, MandateStore
from zerotrust.narrate import GroqNarrator, TemplateNarrator
from zerotrust.policy import PolicyEngine
from zerotrust.provider import ProviderTimeout, SimulatedProvider
from zerotrust.reconcile import ReconciliationScheduler, Reconciler

HOUR = 3600.0
AGENT = "agent_alpha"
SWEEP_INTERVAL_SECONDS = 20.0


def _receipt(request) -> str:
    """One receipt per idempotency key, so the sweep can find the order again."""
    return f"ui_{request.idempotency_key[:18]}"
DBS = {"audit": "ui_audit.db", "policy": "ui_policy.db", "idem": "ui_idem.db"}


def build(force_simulated: bool = False):
    # Explicitly, and before anything reads os.environ. This used to happen
    # only as a side effect of RazorpayConfig.from_env(), which meant
    # `--simulated` never loaded .env at all -- and so silently ran without
    # the LLM even when GROQ_API_KEY was sitting right there in the file.
    load_dotenv()

    catalog = demo_catalog()
    offline_provider = SimulatedProvider()
    audit = AuditLog(DBS["audit"])
    engine = PolicyEngine(MandateStore(DBS["policy"]))
    store = IdempotencyStore(DBS["idem"])

    # The item list is NOT the boundary here; the per-transaction cap is.
    #
    # ANY_SKU rather than a snapshot of today's catalog, so an item stocked
    # later is purchasable without anyone remembering to reissue the mandate.
    # A demo where most of the shop is unbuyable teaches the wrong lesson: it
    # suggests the system's answer to risk is a short hardcoded list, when the
    # interesting behaviour is a bounded agent facing a real catalog and being
    # refused on the merits.
    #
    # The allowlist rule is untouched and still enforced -- this is a choice
    # about how the DEMO mandate is configured. A merchant wanting a bounded
    # item list still writes one, and the adversarial suite still exercises
    # SKU_NOT_ALLOWED with its own narrower mandate.
    existing = engine.mandates.active_for_agent(AGENT)
    if existing is not None and ANY_SKU not in existing.allowed_skus:
        # ui_policy.db persists between runs, so a narrower mandate issued
        # before this change would otherwise survive and keep denying items
        # the operator now expects to work. Config in this file wins over
        # whatever is on disk.
        engine.mandates.revoke(existing.mandate_id)
        print("  mandate  : replaced a stale mandate that predated "
              "the open catalog", flush=True)
        existing = None
    if existing is None:
        engine.mandates.issue(Mandate(
            agent_id=AGENT,
            max_amount_paise=50_000,
            allowed_skus=frozenset({ANY_SKU}),
            expires_at=time.time() + 24 * HOUR,
            velocity_limit=3,
            velocity_window_secs=HOUR,
        ))

    faults = FaultInjector()

    provider = None
    if not force_simulated:
        from zerotrust.config import MissingCredentialsError, RazorpayConfig
        from zerotrust.provider import RazorpayTestModeProvider

        try:
            provider = RazorpayTestModeProvider(RazorpayConfig.from_env())
        except MissingCredentialsError as exc:
            # Not fatal, but never silent: the difference between a real order
            # and a simulated one is invisible in the UI, so it is said here.
            print(f"  payments : SIMULATED -- no Razorpay credentials ({exc})", flush=True)

    live = provider is not None
    if live:
        print("  payments : LIVE -- real Razorpay test-mode orders (no real money)",
              flush=True)

        def execute(request):
            if faults.fire_once(Fault.PROVIDER_TIMEOUT):
                raise ProviderTimeout(
                    "order creation timed out; the order may or may not exist")
            return provider.create_order(
                request.amount_paise, "INR", receipt=_receipt(request))
    else:
        if force_simulated:
            print("  payments : SIMULATED -- forced with --simulated", flush=True)
        counter = {"n": 0}

        def execute(request):
            if faults.fire_once(Fault.PROVIDER_TIMEOUT):
                raise ProviderTimeout(
                    "order creation timed out; the order may or may not exist")
            counter["n"] += 1
            return offline_provider.create_order(
                request.amount_paise, receipt=_receipt(request))

    gateway = PurchaseGateway(engine, store, execute, audit=audit)

    # The agent is a real LLM when one is configured. The fallback is not a
    # nicety: a rate limit or a dropped connection would otherwise take the
    # whole chat surface down. Which parser actually ran is stamped onto every
    # ParsedIntent, so a downgrade shows in the UI and in the audit log rather
    # than passing for the real thing.
    rule_based = RuleBasedIntentParser(catalog)
    if os.environ.get("GROQ_API_KEY"):
        parser = FallbackIntentParser(GroqIntentParser(catalog), rule_based)
        narrator = GroqNarrator()
        print(f"  agent    : Groq ({parser.primary.model}), "
              f"falling back to {rule_based.name}", flush=True)
    else:
        parser, narrator = rule_based, TemplateNarrator()
        print("  agent    : rule-based -- no GROQ_API_KEY, so no LLM is in use",
              flush=True)

    checkout = CheckoutService(catalog, gateway,
                               parser=parser,
                               audit=audit,
                               server_identity=ServerIdentity())

    # The periodic sweep, so a purchase whose outcome is unknown resolves
    # itself instead of waiting for somebody to notice.
    reconciler = Reconciler(
        provider if live else offline_provider, store, audit=audit,
        policy=engine)
    scheduler = ReconciliationScheduler(
        reconciler,
        receipt_for=lambda scoped: f"ui_{scoped.split(':', 1)[-1][:18]}",
        interval_seconds=SWEEP_INTERVAL_SECONDS,
    ).start()

    return create_demo_app(checkout, engine, audit, catalog, agent_id=AGENT,
                           faults=faults, scheduler=scheduler,
                           narrator=narrator,
                           payments_mode="razorpay-test" if live else "simulated")


def main() -> int:
    # --live is still accepted; it is now the default when credentials exist.
    app = build(force_simulated="--simulated" in sys.argv)
    from zerotrust.demo import FRONTEND_DIST

    print("\n  Reference client on http://127.0.0.1:8000")
    print("  The API it demonstrates is mounted at /api")
    print(f"  Reconciliation sweep running every {SWEEP_INTERVAL_SECONDS:.0f}s")
    if not (FRONTEND_DIST / "index.html").exists():
        print("\n  NOTE: the frontend is not built. Run:")
        print("        cd frontend && npm install && npm run build")
    print()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
