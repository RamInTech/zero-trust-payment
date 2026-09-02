/**
 * Client-side half of end-to-end encrypted chat (see zerotrust/e2e.py).
 *
 * Uses tweetnacl's box (X25519 + XSalsa20-Poly1305) -- the same primitive
 * PyNaCl's `Box` wraps on the server. The wire format is nonce (24 bytes)
 * prepended to ciphertext, base64-encoded: PyNaCl's `Box.decrypt()` expects
 * exactly that when no nonce is passed separately.
 *
 * The keypair here is generated fresh per browser session and never leaves
 * it -- the server only ever receives the public half.
 */
import nacl from "tweetnacl"
import { decodeBase64, decodeUTF8, encodeBase64, encodeUTF8 } from "tweetnacl-util"

export interface Sealed {
  ciphertext_b64: string
  sender_public_key_b64: string
}

let keypair: nacl.BoxKeyPair | null = null

export function clientKeypair(): nacl.BoxKeyPair {
  if (!keypair) keypair = nacl.box.keyPair()
  return keypair
}

export function seal(plaintext: string, serverPublicKeyB64: string): Sealed {
  const kp = clientKeypair()
  const serverPublicKey = decodeBase64(serverPublicKeyB64)
  const nonce = nacl.randomBytes(nacl.box.nonceLength)
  const message = decodeUTF8(plaintext)
  const box = nacl.box(message, nonce, serverPublicKey, kp.secretKey)

  const combined = new Uint8Array(nonce.length + box.length)
  combined.set(nonce)
  combined.set(box, nonce.length)

  return {
    ciphertext_b64: encodeBase64(combined),
    sender_public_key_b64: encodeBase64(kp.publicKey),
  }
}

/** For the Security Hub proof: shows the ciphertext is real, not a stand-in. */
export function inspectCiphertext(ciphertext_b64: string): { bytes: number; preview: string } {
  const raw = decodeBase64(ciphertext_b64)
  return { bytes: raw.length, preview: ciphertext_b64.slice(0, 32) + "…" }
}

export { encodeUTF8 }
