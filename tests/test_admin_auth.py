"""Admin authentication: a real login gating the mandate editor.

The property under test throughout: an admin session is a REAL credential
check, not a shared password -- a wrong username, a wrong password, a
tampered token, an expired token, and an unconfigured admin account are five
different failures, and every one of them must be refused, never bypassed.
"""

from __future__ import annotations

import time

import bcrypt
import pytest

from zerotrust.admin_auth import AdminAuth, AdminAuthError
from zerotrust.config import AdminConfig

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()


@pytest.fixture
def auth():
    config = AdminConfig(username=USERNAME, password_hash=PASSWORD_HASH,
                         session_secret="fixture-session-secret")
    return AdminAuth(config)


def test_the_right_username_and_password_logs_in(auth):
    token = auth.login(USERNAME, PASSWORD)
    assert isinstance(token, str) and "." in token


def test_a_valid_token_verifies_to_the_right_username(auth):
    token = auth.login(USERNAME, PASSWORD)
    session = auth.verify(token)
    assert session.username == USERNAME
    assert session.expires_at > session.issued_at


def test_the_wrong_password_is_refused(auth):
    with pytest.raises(AdminAuthError):
        auth.login(USERNAME, "not the password")


def test_the_wrong_username_is_refused(auth):
    with pytest.raises(AdminAuthError):
        auth.login("not-admin", PASSWORD)


def test_wrong_username_and_wrong_password_give_the_same_error_message(auth):
    """A distinct message for 'no such user' is an oracle that lets an
    attacker enumerate valid usernames one login attempt at a time."""
    try:
        auth.login("not-admin", PASSWORD)
        wrong_username_msg = None
    except AdminAuthError as exc:
        wrong_username_msg = exc.reason

    try:
        auth.login(USERNAME, "not the password")
        wrong_password_msg = None
    except AdminAuthError as exc:
        wrong_password_msg = exc.reason

    assert wrong_username_msg is not None and wrong_password_msg is not None
    assert wrong_username_msg == wrong_password_msg


def test_a_tampered_token_is_refused(auth):
    """The attack this exists to stop: take a real token and edit it."""
    token = auth.login(USERNAME, PASSWORD)
    payload_b64, _, signature = token.rpartition(".")
    forged = payload_b64 + "." + ("0" if signature[0] != "0" else "1") + signature[1:]
    with pytest.raises(AdminAuthError):
        auth.verify(forged)


def test_a_token_signed_with_a_different_secret_is_refused(auth):
    other = AdminAuth(AdminConfig(username=USERNAME, password_hash=PASSWORD_HASH,
                                  session_secret="a-different-secret"))
    token = other.login(USERNAME, PASSWORD)
    with pytest.raises(AdminAuthError):
        auth.verify(token)


def test_a_missing_token_is_refused(auth):
    with pytest.raises(AdminAuthError):
        auth.verify(None)
    with pytest.raises(AdminAuthError):
        auth.verify("")


def test_a_malformed_token_is_refused(auth):
    with pytest.raises(AdminAuthError):
        auth.verify("this-is-not-a-signed-token-at-all")


def test_an_expired_token_is_refused():
    config = AdminConfig(username=USERNAME, password_hash=PASSWORD_HASH,
                         session_secret="fixture-session-secret")
    long_ago = AdminAuth(config, clock=lambda: time.time() - 100_000,
                        session_ttl_seconds=1.0)
    stale_token = long_ago.login(USERNAME, PASSWORD)

    now = AdminAuth(config)  # real clock
    with pytest.raises(AdminAuthError):
        now.verify(stale_token)


def test_a_token_just_inside_its_ttl_still_verifies():
    """Not just 'expiry is enforced' -- the boundary itself must be right."""
    config = AdminConfig(username=USERNAME, password_hash=PASSWORD_HASH,
                         session_secret="fixture-session-secret")
    start = time.time()
    issuing = AdminAuth(config, clock=lambda: start, session_ttl_seconds=3600.0)
    token = issuing.login(USERNAME, PASSWORD)

    almost_expired = AdminAuth(config, clock=lambda: start + 3599.0)
    session = almost_expired.verify(token)  # must not raise
    assert session.username == USERNAME


def test_an_unconfigured_admin_refuses_every_login():
    """No credentials configured must mean 'refuse everything', not 'skip
    the check' -- the same fail-closed posture as an absent webhook secret."""
    unconfigured = AdminAuth(None)
    assert unconfigured.is_configured is False
    with pytest.raises(AdminAuthError):
        unconfigured.login(USERNAME, PASSWORD)


def test_an_unconfigured_admin_refuses_every_verification():
    unconfigured = AdminAuth(None)
    with pytest.raises(AdminAuthError):
        unconfigured.verify("anything-at-all")


def test_the_password_hash_is_never_the_plaintext_password(auth):
    """A sanity check on the fixture itself, not the code -- if this ever
    failed it would mean a test was comparing plaintext, defeating the point
    of storing a hash at all."""
    assert PASSWORD not in auth.config.password_hash
    assert auth.config.password_hash.startswith("$2b$")  # bcrypt's own prefix
