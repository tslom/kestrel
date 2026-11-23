"""Web Push encryption and VAPID (app/webpush.py), held to RFC 8291's own
worked example and to a receiver written from the other side."""

import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import webpush
from app.webpush import b64u, unb64u

# RFC 8291, section 5 and appendix A.
PLAIN = b"When I grow up, I want to be a watermelon"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
MESSAGE = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmY"
           "WAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexS"
           "gSxsj_Qulcy4a-fN")


def _key(scalar: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64u(scalar), "big"), ec.SECP256R1())


def _receive(message: bytes, ua_private: str, auth: str) -> bytes:
    """What the phone's browser does with a record: the receiver's half of
    RFC 8291, derived from its own private key."""
    salt, rs, idlen = message[:16], int.from_bytes(message[16:20], "big"), message[20]
    as_public_raw = message[21:21 + idlen]
    body = message[21 + idlen:]
    assert rs == webpush.RECORD_SIZE
    ua = _key(ua_private)
    ua_public_raw = ua.public_key().public_bytes(
        webpush.serialization.Encoding.X962, webpush.serialization.PublicFormat.UncompressedPoint)
    shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), as_public_raw))
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    def hkdf(salt_, ikm, info, n):
        return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt_, info=info).derive(ikm)
    ikm = hkdf(unb64u(auth), shared, b"WebPush: info\x00" + ua_public_raw + as_public_raw, 32)
    cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    out = AESGCM(cek).decrypt(nonce, body, None)
    assert out.endswith(b"\x02")
    return out[:-1]


def test_encrypt_matches_the_rfc_example_byte_for_byte():
    got = webpush.encrypt(PLAIN, UA_PUBLIC, AUTH, salt=unb64u(SALT), sender=_key(AS_PRIVATE))
    assert b64u(got) == MESSAGE


def test_a_random_record_opens_with_the_receivers_key():
    ua = ec.generate_private_key(ec.SECP256R1())
    scalar = b64u(ua.private_numbers().private_value.to_bytes(32, "big"))
    public = b64u(ua.public_key().public_bytes(
        webpush.serialization.Encoding.X962, webpush.serialization.PublicFormat.UncompressedPoint))
    auth = b64u(b"0123456789abcdef")
    one = webpush.encrypt(b'{"title":"Kestrel"}', public, auth)
    two = webpush.encrypt(b'{"title":"Kestrel"}', public, auth)
    assert _receive(one, scalar, auth) == b'{"title":"Kestrel"}'
    assert one != two          # a fresh salt and sender key each time


def test_the_vapid_header_is_an_es256_jwt_for_the_endpoints_origin():
    private, public = webpush.new_keypair()
    header = webpush.vapid_header("https://web.push.apple.com/abc/def", private, public,
                                  "mailto:kestrel@example.com", now=1_000)
    token, key = header.removeprefix("vapid t=").split(", k=")
    assert key == public and len(unb64u(public)) == 65
    head, claims, sig = token.split(".")
    assert json.loads(unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(unb64u(claims)) == {"aud": "https://web.push.apple.com",
                                          "exp": 1_000 + 12 * 3600,
                                          "sub": "mailto:kestrel@example.com"}
    raw = unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(public)).verify(
        der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("endpoint, ok", [
    ("https://web.push.apple.com/QOa", True),
    ("https://fcm.googleapis.com/fcm/send/x", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
    ("http://web.push.apple.com/x", False),
    ("https://push.apple.com.evil.example/x", False),
    ("https://127.0.0.1:8788/api/console/notify", False),
    ("https://evilfcm.googleapis.com/x", False),
])
def test_a_push_goes_only_to_a_push_service(endpoint, ok, monkeypatch):
    assert webpush.endpoint_ok(endpoint) is ok
    sub = {"endpoint": endpoint, "keys": {"p256dh": UA_PUBLIC, "auth": AUTH}}
    assert webpush.subscription_ok(sub) is ok
    if not ok:
        monkeypatch.setattr(webpush.httpx, "post", lambda *a, **k: pytest.fail("it posted"))
        private, public = webpush.new_keypair()
        with pytest.raises(webpush.PushError):
            webpush.send(sub, {"title": "x"}, private_b64=private, public_b64=public, subject="s")


def test_send_posts_ciphertext_and_reads_a_dead_subscription(monkeypatch):
    private, public = webpush.new_keypair()
    sub = {"endpoint": "https://web.push.apple.com/QOa", "keys": {"p256dh": UA_PUBLIC, "auth": AUTH}}
    seen = {}

    class R:
        status_code, text = 201, ""

    def post(url, content, headers, timeout):
        seen.update(url=url, content=content, headers=headers)
        return R
    monkeypatch.setattr(webpush.httpx, "post", post)
    webpush.send(sub, {"title": "Kestrel", "body": "needs an answer"},
                 private_b64=private, public_b64=public, subject="mailto:k@example.com")
    assert seen["headers"]["Content-Encoding"] == "aes128gcm" and seen["headers"]["TTL"] == "600"
    assert b"needs an answer" not in seen["content"]
    assert json.loads(_receive(seen["content"], UA_PRIVATE, AUTH))["body"] == "needs an answer"
    R.status_code = 410
    with pytest.raises(webpush.PushError) as e:
        webpush.send(sub, {}, private_b64=private, public_b64=public, subject="s")
    assert e.value.gone
