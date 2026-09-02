"""Shared pytest setup.

Exists for one reason: the LLM-backed tests gate on `GROQ_API_KEY` /
`ANTHROPIC_API_KEY` read straight from `os.environ`, but nothing in that path
loads `.env`. Without this, those tests skipped **silently and permanently**
even with a key sitting in the file -- reporting "no key configured" while the
key was configured. The Razorpay live tests were unaffected only by accident,
because `RazorpayConfig.from_env()` happens to call `load_dotenv()` itself.

A skip that cannot be turned off is worse than a failure: the suite stays
green and the untested path looks tested.
"""

from dotenv import load_dotenv

load_dotenv()
