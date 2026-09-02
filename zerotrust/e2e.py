"""End-to-end encryption for the customer's raw purchase request text.

The customer's typed words ("buy me 2 mugs") are the one piece of free-form,
potentially sensitive text this system handles. Before this module existed,
`propose_from_text` wrote that text straight into the audit log's
INTENT_PARSED event, in plain text -- readable by anyone who could read the
database, whether or not the server was even running.

This closes that specific gap: the browser encrypts the text to the server's
public key before the request leaves the client, using NaCl's `Box`
(X25519 key agreement + XSalsa20-Poly1305 authenticated encryption, via
PyNaCl/libsodium). Only ciphertext is ever written to disk.

WHAT THIS DOES NOT CLAIM. The process that parses intent still needs the
plaintext to do its job, so a compromise of the *live, running* server --
with its private key resident in memory -- can still read a message as it
arrives, and the demo's own Security Hub proof shows that decrypted read
happening. What this closes is the far more common exposure: a database
backup, a read replica, a leaked `.db` file, or a raw SQL query run by
someone with access to storage but not the server's key material. Stating
that boundary precisely, rather than implying "encrypted" means "safe from
everything," is the same discipline the rest of this project's security
claims follow -- see the four protections declared absent in `demo.py`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from nacl.exceptions import CryptoError
from nacl.public import Box, PrivateKey, PublicKey


class DecryptionFailed(Exception):
    """Ciphertext could not be opened: wrong key, tampered, or malformed."""


@dataclass(frozen=True)
class SealedText:
    """What gets stored and transmitted. Never the plaintext."""

    ciphertext_b64: str
    sender_public_key_b64: str

    def as_dict(self) -> dict:
        return {
            "ciphertext_b64": self.ciphertext_b64,
            "sender_public_key_b64": self.sender_public_key_b64,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SealedText":
        return cls(
            ciphertext_b64=data["ciphertext_b64"],
            sender_public_key_b64=data["sender_public_key_b64"],
        )


def generate_keypair() -> tuple[str, str]:
    """(private_key_b64, public_key_b64) -- for a demo/test client identity."""
    sk = PrivateKey.generate()
    return (
        base64.b64encode(bytes(sk)).decode(),
        base64.b64encode(bytes(sk.public_key)).decode(),
    )


def seal(plaintext: str, sender_private_b64: str, recipient_public_b64: str) -> SealedText:
    sender_sk = PrivateKey(base64.b64decode(sender_private_b64))
    recipient_pk = PublicKey(base64.b64decode(recipient_public_b64))
    ciphertext = Box(sender_sk, recipient_pk).encrypt(plaintext.encode("utf-8"))
    return SealedText(
        ciphertext_b64=base64.b64encode(ciphertext).decode(),
        sender_public_key_b64=base64.b64encode(bytes(sender_sk.public_key)).decode(),
    )


def open_sealed(sealed: SealedText, recipient_private_b64: str) -> str:
    recipient_sk = PrivateKey(base64.b64decode(recipient_private_b64))
    sender_pk = PublicKey(base64.b64decode(sealed.sender_public_key_b64))
    try:
        plaintext = Box(recipient_sk, sender_pk).decrypt(
            base64.b64decode(sealed.ciphertext_b64))
    except CryptoError as exc:
        raise DecryptionFailed(str(exc)) from exc
    except Exception as exc:  # malformed base64 / truncated ciphertext
        raise DecryptionFailed(str(exc)) from exc
    return plaintext.decode("utf-8")


class ServerIdentity:
    """The server's long-lived X25519 keypair.

    One per process. The public half is served at `GET /e2e/public-key` so a
    browser can encrypt to it before the customer's first message is sent.
    """

    def __init__(self, private_key_b64: str | None = None) -> None:
        self._sk = (PrivateKey(base64.b64decode(private_key_b64))
                    if private_key_b64 else PrivateKey.generate())

    @property
    def private_key_b64(self) -> str:
        return base64.b64encode(bytes(self._sk)).decode()

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(bytes(self._sk.public_key)).decode()

    def open(self, sealed: SealedText) -> str:
        return open_sealed(sealed, self.private_key_b64)
