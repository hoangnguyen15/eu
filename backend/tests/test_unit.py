"""Unit tests: no external services required."""
from __future__ import annotations

import asyncio
import hashlib
import io
import time
import uuid

import jwt
import pytest
from PIL import Image
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.providers.approved import ApprovedImageProvider
from app.providers.base import ProviderDisabledError, ProviderJobState, ProviderRequest
from app.providers.google_flow import GoogleFlowConnector
from app.providers.mock import MockImageProvider, render_png
from app.providers.registry import provider_states
from app.storage.images import ImageValidationError, make_thumbnail, sniff_mime, validate_image
from app.workers import protocol as p

ALLOWED = ["image/png", "image/jpeg", "image/webp"]


# ------------------------------------------------------------- security ----
def test_password_hash_is_argon2id_and_verifies() -> None:
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")
    assert not verify_password("not-a-hash", "x")


def test_access_token_roundtrip_and_tamper() -> None:
    uid = uuid.uuid4()
    tok, ttl = create_access_token(uid, "user")
    assert ttl == get_settings().access_token_ttl_seconds
    claims = decode_access_token(tok)
    assert claims["sub"] == str(uid) and claims["type"] == "access"
    with pytest.raises(UnauthorizedError):
        decode_access_token(tok[:-2] + ("AA" if not tok.endswith("AA") else "BB"))


def test_expired_and_wrong_type_tokens_rejected() -> None:
    s = get_settings()
    now = int(time.time())
    expired = jwt.encode({"sub": "x", "type": "access", "iss": s.jwt_issuer, "iat": now - 100, "exp": now - 10},
                         s.jwt_secret, algorithm="HS256")
    with pytest.raises(UnauthorizedError) as e:
        decode_access_token(expired)
    assert e.value.code == "TOKEN_EXPIRED"
    refresh_like = jwt.encode({"sub": "x", "type": "refresh", "iss": s.jwt_issuer, "iat": now, "exp": now + 60},
                              s.jwt_secret, algorithm="HS256")
    with pytest.raises(UnauthorizedError):
        decode_access_token(refresh_like)
    none_alg = jwt.encode({"sub": "x", "type": "access", "iss": s.jwt_issuer, "iat": now, "exp": now + 60},
                          key="", algorithm="none")
    with pytest.raises(UnauthorizedError):
        decode_access_token(none_alg)


def test_token_hash_is_keyed_and_stable() -> None:
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != hashlib.sha256(b"abc").hexdigest()


# --------------------------------------------------------------- images ----
def _img(fmt: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 20, 30)).save(out, format=fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_validate_supported_formats(fmt: str, mime: str) -> None:
    data = _img(fmt)
    info = validate_image(data, allowed_mime=ALLOWED, max_bytes=10_000_000, max_pixels=10_000_000,
                          expected_sha256=hashlib.sha256(data).hexdigest(), declared_mime=mime)
    assert (info.mime_type, info.width, info.height, info.file_size) == (mime, 64, 32, len(data))


def test_validate_rejects_checksum_mismatch() -> None:
    with pytest.raises(ImageValidationError) as e:
        validate_image(_img("PNG"), allowed_mime=ALLOWED, max_bytes=10**7, max_pixels=10**7, expected_sha256="0" * 64)
    assert e.value.code == "CHECKSUM_MISMATCH"


def test_validate_rejects_declared_mime_mismatch_and_fake_types() -> None:
    with pytest.raises(ImageValidationError) as e:
        validate_image(_img("PNG"), allowed_mime=ALLOWED, max_bytes=10**7, max_pixels=10**7,
                       declared_mime="image/jpeg")
    assert e.value.code == "MIME_MISMATCH"
    with pytest.raises(ImageValidationError) as e:
        validate_image(b"<html>not an image</html>", allowed_mime=ALLOWED, max_bytes=10**7, max_pixels=10**7)
    assert e.value.code == "UNSUPPORTED_MEDIA_TYPE"
    gif = io.BytesIO()
    Image.new("RGB", (4, 4)).save(gif, format="GIF")
    with pytest.raises(ImageValidationError):
        validate_image(gif.getvalue(), allowed_mime=ALLOWED, max_bytes=10**7, max_pixels=10**7)


def test_validate_rejects_truncated_and_oversized() -> None:
    data = _img("PNG")
    with pytest.raises(ImageValidationError) as e:
        validate_image(data[: len(data) // 2], allowed_mime=ALLOWED, max_bytes=10**7, max_pixels=10**7)
    assert e.value.code == "CORRUPT_IMAGE"
    with pytest.raises(ImageValidationError) as e:
        validate_image(data, allowed_mime=ALLOWED, max_bytes=10, max_pixels=10**7)
    assert e.value.code == "FILE_TOO_LARGE"
    with pytest.raises(ImageValidationError) as e:
        validate_image(data, allowed_mime=ALLOWED, max_bytes=10**7, max_pixels=100)
    assert e.value.code == "TOO_MANY_PIXELS"


def test_thumbnail_is_webp_and_bounded() -> None:
    thumb = make_thumbnail(render_png("x", "16:9", 0), 128)
    assert sniff_mime(thumb) == "image/webp"
    with Image.open(io.BytesIO(thumb)) as im:
        assert max(im.size) <= 128


# ------------------------------------------------------------ providers ----
async def test_mock_provider_generates_valid_pngs() -> None:
    prov = MockImageProvider(latency_seconds=0.01)
    job = await prov.generate(ProviderRequest(task_id="t", prompt="city", aspect_ratio="16:9", number_of_images=2))
    for _ in range(100):
        st = await prov.get_status(job)
        if st.state == ProviderJobState.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    res = await prov.collect_result(job)
    assert len(res.images) == 2
    info = validate_image(res.images[0].data, allowed_mime=ALLOWED, max_bytes=10**7, max_pixels=10**7)
    assert (info.width, info.height) == (768, 432)
    assert res.images[0].data != res.images[1].data


@pytest.mark.parametrize("token,code", [("[mock:auth]", "AUTH_REQUIRED"), ("[mock:limit]", "PROVIDER_LIMITED"),
                                        ("[mock:review]", "REVIEW_REQUIRED")])
async def test_mock_provider_failure_modes(token: str, code: str) -> None:
    from app.providers.base import ProviderError

    with pytest.raises(ProviderError) as e:
        await MockImageProvider().generate(ProviderRequest("t", f"x {token}", "1:1", 1))
    assert e.value.code == code


async def test_google_flow_connector_disabled_by_default() -> None:
    conn = GoogleFlowConnector()
    assert conn.enabled is False
    with pytest.raises(ProviderDisabledError):
        await conn.generate(ProviderRequest("t", "x", "1:1", 1))
    with pytest.raises(ProviderDisabledError):
        await conn.get_status("j")
    assert provider_states()["google_flow"] is False


async def test_approved_provider_requires_configuration() -> None:
    prov = ApprovedImageProvider(base_url="", api_key="")
    assert prov.enabled is False
    with pytest.raises(ProviderDisabledError):
        await prov.generate(ProviderRequest("t", "x", "1:1", 1))


# ------------------------------------------------------------- protocol ----
def test_protocol_validation() -> None:
    env = p.Envelope.model_validate({"type": "TASK_ACCEPT",
                                     "payload": {"event_id": "e1", "task_id": str(uuid.uuid4())}})
    assert env.type == p.MsgType.TASK_ACCEPT
    with pytest.raises(ValidationError):
        p.Envelope.model_validate({"type": "DO_EVIL", "payload": {}})
    with pytest.raises(ValidationError):
        p.UploadRequestPayload.model_validate({"event_id": "e", "task_id": str(uuid.uuid4()),
                                               "files": [{"index": 0, "mime_type": "image/png", "size": 1,
                                                          "sha256": "zz"}]})


def test_pki_signs_csr_with_workstation_cn(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from app.workers import pki

    pki.init_pki(str(tmp_path), ["localhost"], force=True)
    import os
    os.environ["PKI_CA_CERT_PATH"] = str(tmp_path / "ca.crt")
    os.environ["PKI_CA_KEY_PATH"] = str(tmp_path / "ca.key")
    get_settings.cache_clear()
    pki._load_ca.cache_clear()
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "admin")])).sign(key, hashes.SHA256()))
        wid = uuid.uuid4()
        cert = pki.sign_agent_csr(csr.public_bytes(serialization.Encoding.PEM).decode(), wid)
        assert pki.cert_workstation_id(cert) == wid  # CSR subject ignored
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert ExtendedKeyUsageOID.CLIENT_AUTH in eku
        from urllib.parse import quote
        assert pki.fingerprint(pki.parse_forwarded_cert(quote(pki.cert_pem(cert)))) == pki.fingerprint(cert)

        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        weak_csr = (x509.CertificateSigningRequestBuilder()
                    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "w")]))
                    .sign(weak, hashes.SHA256()))
        with pytest.raises(pki.PkiError):
            pki.sign_agent_csr(weak_csr.public_bytes(serialization.Encoding.PEM).decode(), wid)
        with pytest.raises(pki.PkiError):
            pki.sign_agent_csr("-----BEGIN CERTIFICATE REQUEST-----\ngarbage\n-----END CERTIFICATE REQUEST-----"
                               + "x" * 100, wid)
    finally:
        get_settings.cache_clear()
        pki._load_ca.cache_clear()
