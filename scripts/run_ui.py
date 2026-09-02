"""Serve the reference client.

    uv run python scripts/run_ui.py          # stubbed executor, no credentials
    uv run python scripts/run_ui.py --live   # real Razorpay test-mode orders

Then open http://127.0.0.1:8000

The UI is a demonstration client for the API it mounts at /api. It holds no
authorisation logic. DEMO ONLY: no authentication, and the audit view exposes
mandate internals.
"""

import sys
import time

import uvicorn

from zerotrust.audit import AuditLog
from zerotrust.catalog import demo_catalog
from zerotrust.e2e import ServerIdentity
from zerotrust.faults import Fault, FaultInjector
from zerotrust.checkout import CheckoutService
from zerotrust.demo import create_demo_app
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.intent import RuleBasedIntentParser
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.narrate import TemplateNarrator
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


def build(live: bool):
    catalog = demo_catalog()
    offline_provider = SimulatedProvider()
    audit = AuditLog(DBS["audit"])
    engine = PolicyEngine(MandateStore(DBS["policy"]))
    store = IdempotencyStore(DBS["idem"])

    if engine.mandates.active_for_agent(AGENT) is None:
        engine.mandates.issue(Mandate(
            agent_id=AGENT,
            max_amount_paise=50_000,
            allowed_skus=frozenset({"SKU-COFFEE", "SKU-CAKE", "SKU-TEA"}),
            expires_at=time.time() + 24 * HOUR,
            velocity_limit=3,
            velocity_window_secs=HOUR,
        ))

    faults = FaultInjector()

    if live:
        from zerotrust.config import RazorpayConfig
        from zerotrust.provider import RazorpayTestModeProvider

        provider = RazorpayTestModeProvider(RazorpayConfig.from_env())
        print("LIVE: order creation will hit the real Razorpay test-mode API")

        def execute(request):
            if faults.fire_once(Fault.PROVIDER_TIMEOUT):
                raise ProviderTimeout(
                    "order creation timed out; the order may or may not exist")
            return provider.create_order(
                request.amount_paise, "INR", receipt=_receipt(request))
    else:
        counter = {"n": 0}

        def execute(request):
            if faults.fire_once(Fault.PROVIDER_TIMEOUT):
                raise ProviderTimeout(
                    "order creation timed out; the order may or may not exist")
            counter["n"] += 1
            return offline_provider.create_order(
                request.amount_paise, receipt=_receipt(request))

    gateway = PurchaseGateway(engine, store, execute, audit=audit)
    checkout = CheckoutService(catalog, gateway,
                               parser=RuleBasedIntentParser(catalog),
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
                           narrator=TemplateNarrator())


def main() -> int:
    live = "--live" in sys.argv
    app = build(live)
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
