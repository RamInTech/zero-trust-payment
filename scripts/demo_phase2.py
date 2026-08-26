"""Phase 2 manual demo — the Phase 1 wrapper around REAL Razorpay calls.

    uv run python scripts/demo_phase2.py

Creates real test-mode orders. Every ">>> API CALL" line below is an actual
HTTPS request to Razorpay; count them against the number of submissions.
Requires .env with rzp_test_ credentials.
"""

import os

from zerotrust.config import MissingCredentialsError, RazorpayConfig
from zerotrust.idempotency import IdempotencyStore
from zerotrust.provider import RazorpayTestModeProvider, SimulatedProvider

DB = "demo_phase2.db"


class LoudRazorpay(RazorpayTestModeProvider):
    """Announces every real API call so you can count them yourself."""

    def create_order(self, amount_paise, currency="INR", receipt=""):
        print(f"        >>> API CALL to Razorpay: POST /v1/orders "
              f"(Rs.{amount_paise / 100:,.2f})")
        return super().create_order(amount_paise, currency, receipt)


def banner(n, title):
    print(f"\n{'=' * 72}\n  {n}. {title}\n{'=' * 72}")


def main():
    try:
        config = RazorpayConfig.from_env()
    except MissingCredentialsError as exc:
        print(f"Cannot run: {exc}")
        return

    if os.path.exists(DB):
        os.remove(DB)

    print(f"Using test-mode key {config.key_id[:14]}... against {config.base_url}")
    store = IdempotencyStore(DB)

    with LoudRazorpay(config) as rzp:
        # ------------------------------------------------------------------
        banner(1, "One real order — the baseline")
        order = rzp.create_order(50_000, receipt="demo_baseline")
        print(f"    Razorpay returned: {order['id']}  "
              f"status={order['status']}  amount={order['amount']}")
        print(f"    API calls so far: {rzp.call_count}")

        # ------------------------------------------------------------------
        banner(2, "Same intent submitted 5x through the wrapper")
        print("    An agent retrying a request it never got an answer to.\n")
        payload = {"amount_paise": 75_000, "currency": "INR",
                   "receipt": "demo_retries"}
        before = rzp.call_count

        for i in range(5):
            r = store.execute(
                "demo-key-1", payload,
                lambda: rzp.create_order(75_000, receipt="demo_retries"))
            print(f"    submission {i + 1}  -> {r.outcome.value:<9} "
                  f"order={r.response['id']}")

        print(f"\n    API calls made by those 5 submissions: "
              f"{rzp.call_count - before}   <- must be 1")
        print("    Every submission returned the SAME order id.")

        # ------------------------------------------------------------------
        banner(3, "Tampered retry — same key, different amount")
        print("    Rejected before any API call is made.\n")
        before = rzp.call_count
        tampered = {**payload, "amount_paise": 5_000_000}
        r = store.execute(
            "demo-key-1", tampered,
            lambda: rzp.create_order(5_000_000, receipt="demo_retries"))
        print(f"    -> {r.outcome.value}")
        print(f"       {r.reason}")
        print(f"\n    API calls made: {rzp.call_count - before}   <- must be 0")

        # ------------------------------------------------------------------
        banner(4, "Capture — SIMULATED, and it says so")
        cap = rzp.simulate_capture(order["id"], 50_000)
        print(f"    payment id : {cap['id']}")
        print(f"    status     : {cap['status']}")
        print(f"    simulated  : {cap['simulated']}   <- NOT a live capture")
        print(f"    why        : {cap['simulation_reason']}")

        total_real_calls = rzp.call_count

    # ------------------------------------------------------------------
    banner(5, "The wrapper doesn't know which provider it holds")
    print("    Identical retry sequence against the offline simulator.\n")
    sim = SimulatedProvider()
    sim_store = IdempotencyStore(DB)
    sim_payload = {"amount_paise": 75_000, "currency": "INR", "receipt": "sim"}
    for i in range(5):
        r = sim_store.execute(
            "demo-key-sim", sim_payload,
            lambda: sim.create_order(75_000, receipt="sim"))
        print(f"    submission {i + 1}  -> {r.outcome.value:<9} "
              f"order={r.response['id']}")
    print(f"\n    Simulator calls: {sim.call_count}   <- also 1")
    print("    Same outcome sequence, same guarantee, different provider.")

    banner("", "TALLY")
    print(f"    Real Razorpay API calls this run: {total_real_calls}")
    print("      1 baseline submission   -> 1 call")
    print("      5 retries of one intent -> 1 call")
    print("      1 tampered retry        -> 0 calls")
    print(f"      7 submissions total     -> {total_real_calls} real orders")
    print("\n    Check Test Mode -> Orders in the Razorpay dashboard: you should")
    print(f"    see {total_real_calls} orders from this run, not 7.")


if __name__ == "__main__":
    main()
