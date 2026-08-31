"""Probe: can Razorpay Checkout be driven headlessly to yield a payment_id?

    uv run python scripts/probe_browser_capture.py [headed]

This exists because RAZORPAY.md's third open item asked whether capture could
be made real via browser automation instead of simulated. It was attempted,
and it did not work. This script is the reproducible evidence for that answer,
kept in the repo so the conclusion can be re-checked rather than taken on
trust.

What it does: creates a real test-mode order, serves a page that opens Razorpay
Checkout against it, and drives the contact and card steps with Playwright,
watching for the `handler` callback that would carry `razorpay_payment_id` --
the id `POST /v1/payments/{id}/capture` requires.

WHAT WAS OBSERVED (see JOURNAL.md Entry 10 for the full account):
  - early runs: the checkout rendered headlessly, and the contact and card
    steps were both reachable and fillable
  - submitting the card produced no result, no error and no navigation
    within 60s -- the flow simply stopped
  - the page embeds invisible hCaptcha and "human security" frames
  - headed mode never loaded the checkout iframe at all in this environment
  - later runs, including this script as it stands, stopped reaching the form
    at all after roughly a dozen attempts against the same key

That last point matters for how much weight to put on any of this. The
behaviour was not stable across runs, so the failure cannot be cleanly
attributed: silent bot detection, rate limiting after repeated attempts, and
plain fragility in this automation are all consistent with what was seen. What
can be said honestly is narrow and sufficient: NO WORKING HEADLESS CAPTURE PATH
WAS FOUND. Capture therefore stays simulated and is labelled as such
everywhere in the codebase.

If this script ever does obtain a payment_id it says so loudly and exits 0 --
at which point RAZORPAY.md Section 4 needs updating, not this docstring.
"""

import http.server
import socketserver
import sys
import threading
import time

from zerotrust.config import MissingCredentialsError, RazorpayConfig
from zerotrust.provider import RazorpayTestModeProvider

PORT = 8791
TEST_CARD = "4111111111111111"


def build_page(key_id: str, order_id: str, amount: int) -> str:
    return """<!doctype html><html><body>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
window.__result = null; window.__err = null;
var rzp = new Razorpay({
  key: "%s", order_id: "%s", amount: %d, currency: "INR",
  name: "Zero-Trust capture probe",
  handler: function (r) { window.__result = r; },
  modal: { escape: false, ondismiss: function () { window.__result = {dismissed: true}; } }
});
rzp.on('payment.failed', function (r) { window.__err = r.error; });
// Opened immediately rather than on 'load': the load event does not reliably
// fire once Checkout's own polling starts, which made the probe flaky.
rzp.open();
</script></body></html>""" % (key_id, order_id, amount)


def serve(page: str):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page.encode())

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def checkout_frame(page):
    for frame in page.frames:
        if "api.razorpay.com/v1/checkout/public" in frame.url:
            return frame
    return None


def main() -> int:
    headed = len(sys.argv) > 1 and sys.argv[1] == "headed"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed: uv add --dev playwright "
              "&& uv run playwright install chromium")
        return 2

    try:
        config = RazorpayConfig.from_env()
    except MissingCredentialsError as exc:
        print(f"cannot run: {exc}")
        return 2

    provider = RazorpayTestModeProvider(config)
    order = provider.create_order(50_000, "INR",
                                  receipt=f"probe_{int(time.time())}")
    print(f"created real order {order['id']} (headless={not headed})")

    server = serve(build_page(config.key_id, order["id"], order["amount"]))
    payment_calls = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        context.on("request", lambda r: payment_calls.append((r.method, r.url[:110]))
                   if "/v1/payments" in r.url else None)
        page = context.new_page()

        try:
            page.goto(f"http://127.0.0.1:{PORT}/",
                      wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"could not load the page: {str(exc)[:120]}")
            browser.close(); server.shutdown(); return 1

        # The checkout iframe appears asynchronously; poll for the form.
        frame = None
        deadline = time.time() + 60
        while time.time() < deadline and frame is None:
            candidate = checkout_frame(page)
            if candidate:
                try:
                    if candidate.locator("input[name='card.number']").count() or \
                       candidate.locator("input[name='contact']").count():
                        frame = candidate
                except Exception:
                    pass
            time.sleep(1)

        if frame is None:
            print("FINDING: the checkout form never became available.")
            print("frames:", [f.url[:70] for f in page.frames])
            browser.close(); server.shutdown(); return 1
        print("checkout form is available")

        if frame.locator("input[name='contact']").count():
            frame.fill("input[name='contact']", "9999999999")
            frame.fill("input[name='email']", "test@example.com")
            _click_first(frame, ("Continue",))
            print("contact step submitted")
            time.sleep(6)
            frame = checkout_frame(page) or frame

        frame.fill("input[name='card.number']", TEST_CARD)
        frame.fill("input[name='card.expiry']", "12/30")
        frame.fill("input[name='card.cvv']", "123")
        print("card details filled")
        time.sleep(1)
        _click_first(frame, ("Continue", "Pay"))

        for i in range(15):
            time.sleep(4)
            result = page.evaluate("window.__result")
            error = page.evaluate("window.__err")
            if result or error:
                print(f"\nRESULT after {(i + 1) * 4}s: {result}")
                print(f"ERROR:  {error}")
                if result and result.get("razorpay_payment_id"):
                    print("\n*** A payment_id was obtained. Live capture IS "
                          "reachable; update RAZORPAY.md Section 4. ***")
                    browser.close(); server.shutdown(); return 0
                break
        else:
            print("\nFINDING: no result, no error, no navigation after 60s.")

        print(f"payment-related network calls observed: {len(payment_calls)}")
        final = provider.fetch_order(order["id"])
        print(f"order status at Razorpay: {final.get('status')} "
              f"(attempts: {final.get('attempts')})")
        browser.close()

    server.shutdown()
    print("\nNo payment_id obtained. Capture remains simulated -- see the "
          "module docstring and JOURNAL.md Entry 10.")
    return 1


def _click_first(frame, labels) -> bool:
    buttons = frame.locator("button")
    for i in range(buttons.count()):
        element = buttons.nth(i)
        try:
            text = (element.inner_text() or "").strip()
            if element.is_visible() and text.startswith(tuple(labels)):
                element.click(timeout=6_000, force=True)
                return True
        except Exception:
            continue
    return False


if __name__ == "__main__":
    sys.exit(main())
