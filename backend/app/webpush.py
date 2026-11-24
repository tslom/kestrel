"""Web Push, sender side: RFC 8291 encryption and a VAPID header (RFC 8292).

For the phone door's notifications (app/phone.py). `send()` refuses any endpoint
that isn't a known push service (PUSH_HOSTS): endpoints come from paired devices.
Design: DESIGN.md → `app/webpush.py`.
"""
from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Allowed push services: Apple (Safari, all iOS browsers), Google, Mozilla.
PUSH_HOSTS = (".push.apple.com", "fcm.googleapis.com",
              "updates.push.services.mozilla.com")
RECORD_SIZE = 4096
TTL_SECONDS = 600          # an ask older than this isn't worth waking a phone for
SEND_SECONDS = 10


class PushError(RuntimeError):
    """The push didn't go. `gone` means the subscription is dead: drop it."""

    def __init__(self, message: str, gone: bool = False):
        super().__init__(message)
        self.gone = gone


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def new_keypair() -> tuple[str, str]:
    """A VAPID key pair as (private scalar, public point), both base64url."""
    key = ec.generate_private_key(ec.SECP256R1())
    scalar = key.private_numbers().private_value.to_bytes(32, "big")
    return b64u(scalar), b64u(_public_bytes(key.public_key()))


def _private(scalar_b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64u(scalar_b64), "big"), ec.SECP256R1())


def encrypt(plaintext: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """One aes128gcm record (RFC 8188) keyed per RFC 8291. `salt` and `sender`
    are random unless a test pins them to the RFC's example."""
    ua_public_raw = unb64u(p256dh)
    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_raw)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    as_public_raw = _public_bytes(sender.public_key())
    salt = salt or os.urandom(16)
    shared = sender.exchange(ec.ECDH(), ua_public)
    ikm = _hkdf(unb64u(auth), shared,
                b"WebPush: info\x00" + ua_public_raw + as_public_raw, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(plaintext) > RECORD_SIZE - 17 - 86:
        raise PushError("payload too long for one record")
    # 0x02 marks the last (only) record.
    body = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public_raw)]) + as_public_raw
    return header + body


def vapid_header(endpoint: str, private_b64: str, public_b64: str, subject: str,
                 *, now: float | None = None) -> str:
    """`Authorization` for one push: a 12-hour ES256 JWT for the endpoint's origin."""
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}",
              "exp": int(now if now is not None else time.time()) + 12 * 3600,
              "sub": subject}
    signing = (b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
               + "." + b64u(json.dumps(claims, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(_private(private_b64).sign(signing.encode(),
                                                            ec.ECDSA(hashes.SHA256())))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={signing}.{b64u(sig)}, k={public_b64}"


def endpoint_ok(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


def subscription_ok(sub: object) -> bool:
    """The shape PushSubscription.toJSON() gives, pointing at a real push service."""
    if not isinstance(sub, dict) or not isinstance(sub.get("keys"), dict):
        return False
    keys = sub["keys"]
    try:
        return (endpoint_ok(str(sub.get("endpoint") or ""))
                and len(unb64u(str(keys.get("p256dh") or ""))) == 65
                and len(unb64u(str(keys.get("auth") or ""))) == 16)
    except (ValueError, TypeError):
        return False


def send(sub: dict, payload: dict, *, private_b64: str, public_b64: str, subject: str) -> None:
    """Encrypt `payload` to one subscription and hand it to its push service."""
    if not subscription_ok(sub):
        raise PushError("not a push subscription this server will send to", gone=True)
    body = encrypt(json.dumps(payload, separators=(",", ":")).encode(),
                   sub["keys"]["p256dh"], sub["keys"]["auth"])
    headers = {"Authorization": vapid_header(sub["endpoint"], private_b64, public_b64, subject),
               "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "TTL": str(TTL_SECONDS), "Urgency": "high"}
    try:
        r = httpx.post(sub["endpoint"], content=body, headers=headers, timeout=SEND_SECONDS)
    except httpx.HTTPError as e:
        raise PushError(f"couldn't reach the push service: {e!r}") from e
    if r.status_code in (404, 410):
        raise PushError("the phone's subscription is gone", gone=True)
    if r.status_code >= 300:
        raise PushError(f"push service said {r.status_code}: {r.text[:200]}")
