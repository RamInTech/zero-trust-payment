"""Razorpay credentials, loaded from the environment.

Test-mode only, deliberately. A live key in this project would let a bug move
real money, so `from_env()` refuses anything that isn't an `rzp_test_` key
rather than trusting the operator to have picked the right dashboard tab.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

TEST_KEY_PREFIX = "rzp_test_"
DEFAULT_BASE_URL = "https://api.razorpay.com"


class MissingCredentialsError(RuntimeError):
    """Raised when Razorpay credentials are absent or unusable."""


@dataclass(frozen=True)
class RazorpayConfig:
    key_id: str
    key_secret: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls, *, load_dotenv_file: bool = True) -> "RazorpayConfig":
        if load_dotenv_file:
            load_dotenv()

        key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()

        missing = [
            name
            for name, value in (
                ("RAZORPAY_KEY_ID", key_id),
                ("RAZORPAY_KEY_SECRET", key_secret),
            )
            if not value
        ]
        if missing:
            raise MissingCredentialsError(
                f"{' and '.join(missing)} not set. Copy .env.example to .env and "
                f"fill in your Razorpay TEST-MODE keys "
                f"(Dashboard -> Test Mode -> Settings -> API Keys)."
            )

        if not key_id.startswith(TEST_KEY_PREFIX):
            raise MissingCredentialsError(
                f"RAZORPAY_KEY_ID must be a test-mode key (starts with "
                f"'{TEST_KEY_PREFIX}'), got '{key_id[:12]}...'. This project is "
                f"test-mode only and will not run against live credentials."
            )

        return cls(
            key_id=key_id,
            key_secret=key_secret,
            base_url=os.environ.get("RAZORPAY_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.key_id and self.key_secret)
