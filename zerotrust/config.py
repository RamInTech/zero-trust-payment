"""Razorpay credentials, loaded from the environment.

Test-mode only, deliberately. A live key in this project would let a bug move
real money, so `from_env()` refuses anything that isn't an `rzp_test_` key
rather than trusting the operator to have picked the right dashboard tab.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Optional

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


def webhook_secret_from_env(*, load_dotenv_file: bool = True) -> Optional[str]:
    """The webhook signing secret, or None if it is not set.

    Separate from `RazorpayConfig` because it is a separate credential with a
    separate lifecycle: it is set in the Razorpay dashboard when a webhook is
    created, not issued with the API keys, and a deployment can legitimately
    have API keys and no webhook.

    Returning None rather than raising is deliberate — the receiver treats an
    absent secret as "refuse every delivery", so an unconfigured webhook is a
    closed door rather than a startup failure.
    """
    if load_dotenv_file:
        load_dotenv()
    return os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip() or None


@dataclass(frozen=True)
class AdminConfig:
    """Credentials for the one admin account that may edit a mandate.

    `password_hash` is a bcrypt hash, never the plaintext password -- nothing
    in this codebase ever holds the real password in memory longer than the
    one comparison `AdminAuth.login()` makes.
    """
    username: str
    password_hash: str
    session_secret: str


def admin_config_from_env(*, load_dotenv_file: bool = True) -> Optional[AdminConfig]:
    """The admin login, or None if it is not configured.

    Same shape as `webhook_secret_from_env()`, and for the same reason:
    returning None rather than raising lets `AdminAuth` treat "not
    configured" as "refuse every login" -- a closed door, not a bypass. There
    is deliberately no fallback to an unauthenticated admin here; a script
    that wants the demo usable without env setup (see `scripts/run_ui.py`)
    generates and prints a real, random password instead of skipping the
    check.
    """
    if load_dotenv_file:
        load_dotenv()
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
    if not username or not password_hash:
        return None
    # A session-signing secret left unset does not need to fail closed the
    # way the credential itself does: it only needs to be unpredictable and
    # stable for the life of this process, and a per-process random secret
    # gives both -- sessions just do not survive a restart, which is already
    # true of the in-memory store they sign.
    session_secret = (os.environ.get("ADMIN_SESSION_SECRET", "").strip()
                      or secrets.token_hex(32))
    return AdminConfig(username=username, password_hash=password_hash,
                       session_secret=session_secret)
