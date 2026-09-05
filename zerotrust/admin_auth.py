"""Admin authentication, for the merchant-only mandate controls.

The mandate's four editable fields -- cap, allowlist, expiry, velocity -- and
its revoke route are merchant actions in the same sense `zerotrust/demo.py`
already argues for the cap alone: the agent has no route to any of them, and
nothing about them lets an agent widen its own authority. What was missing was
a boundary between "reachable by an agent" and "reachable by anyone with the
page open" -- the routes existed, but any browser tab could call them. This
closes that second gap with a real login, not a shared password: a bcrypt-
hashed credential, a short-lived signed session, and the same fail-closed
posture as the webhook receiver in `zerotrust/webhook.py` -- an unconfigured
admin account refuses every login rather than leaving the door open.

Two things this deliberately is NOT, stated rather than left implied:

1. NOT a multi-user system. One admin account, matching the one-merchant
   shape of everything else in this reference client. A real deployment
   would need per-user accounts and per-user audit attribution; this does
   not pretend to be that.
2. NOT durable session storage. A session is a signed, self-contained token
   -- verified by recomputing its HMAC, not by looking anything up -- so it
   is stateless by construction and does not survive changing the signing
   secret. A process restart without a fixed `ADMIN_SESSION_SECRET` logs
   everyone out, which is an accepted trade for not needing a session store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Callable, Optional

import bcrypt

from zerotrust.config import AdminConfig

#: A session lasts long enough for one working sitting at the mandate editor
#: without asking the merchant to log in again mid-edit, short enough that a
#: leaked token is not a standing credential.
DEFAULT_SESSION_TTL_SECONDS = 3600.0  # 1 hour


class AdminAuthError(Exception):
    """A login or a session check failed. Carries the specific reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AdminSession:
    username: str
    issued_at: float
    expires_at: float


class AdminAuth:
    def __init__(
        self,
        config: Optional[AdminConfig],
        clock: Callable[[], float] = time.time,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        self.config = config
        self._clock = clock
        self.session_ttl_seconds = session_ttl_seconds

    @property
    def is_configured(self) -> bool:
        return self.config is not None

    def login(self, username: str, password: str) -> str:
        """Verify a username and password, and issue a session token.

        Raises `AdminAuthError` on any failure -- unconfigured, wrong
        username, or wrong password all raise the SAME generic message
        ("invalid username or password"), on purpose: a distinct message for
        "no such username" is exactly the oracle that lets an attacker
        enumerate valid usernames one login attempt at a time. There being
        only one valid username in this design makes that a smaller risk
        than in a multi-user system, but the discipline costs nothing to
        keep and the caller here is bcrypt.checkpw() either way, so the two
        paths cost the same regardless.
        """
        if self.config is None:
            raise AdminAuthError("admin login is not configured")

        # bcrypt.checkpw() always does the hash-and-compare work, so a
        # wrong-username early-return would be the one branch that DOESN'T --
        # exactly the timing difference that reveals whether the username was
        # right before the password was even checked. Checking the password
        # unconditionally against the real hash removes that difference.
        password_ok = bcrypt.checkpw(password.encode("utf-8"),
                                     self.config.password_hash.encode("utf-8"))
        username_ok = hmac.compare_digest(username, self.config.username)
        if not (username_ok and password_ok):
            raise AdminAuthError("invalid username or password")

        return self._issue_token(username)

    def verify(self, token: Optional[str]) -> AdminSession:
        """Verify a session token. Raises `AdminAuthError` if it does not hold."""
        if self.config is None:
            raise AdminAuthError("admin login is not configured")
        if not token or "." not in token:
            raise AdminAuthError("missing or malformed session token")

        payload_b64, _, signature = token.rpartition(".")
        expected = hmac.new(self.config.session_secret.encode("utf-8"),
                            payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AdminAuthError("session token failed verification")

        try:
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except (ValueError, UnicodeDecodeError) as exc:
            # Unreachable if the signature above ever matched a payload this
            # code itself issued -- reachable only if the secret was reused
            # to sign something else, which is exactly the case worth failing
            # loudly on rather than crashing with a raw decode error.
            raise AdminAuthError(f"malformed session payload: {exc}") from exc

        if self._clock() > payload.get("exp", 0):
            raise AdminAuthError("session has expired; please log in again")

        return AdminSession(username=payload["sub"], issued_at=payload["iat"],
                           expires_at=payload["exp"])

    def _issue_token(self, username: str) -> str:
        now = self._clock()
        payload = {"sub": username, "iat": now, "exp": now + self.session_ttl_seconds}
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True).encode("utf-8")).rstrip(b"=")
        signature = hmac.new(self.config.session_secret.encode("utf-8"),
                             payload_b64, hashlib.sha256).hexdigest()
        return payload_b64.decode("ascii") + "." + signature
