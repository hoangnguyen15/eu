"""Internal PKI for worker mTLS.

* `init_pki()` creates a CA and a TLS server certificate for the agent gateway
  (dev / first install; production should keep the CA key in a secret store).
* `sign_agent_csr()` issues a short-lived client certificate whose CN is the
  workstation id — the CSR's own subject is ignored.
* The gateway (nginx) verifies the chain; the backend additionally pins the
  certificate by SHA-256 fingerprint stored on the workstation row, so a
  disabled workstation is rejected even while its certificate is still valid.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.core.config import get_settings


class PkiError(Exception):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@lru_cache
def _load_ca() -> tuple[x509.Certificate, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey]:
    s = get_settings()
    cert = x509.load_pem_x509_certificate(Path(s.pki_ca_cert_path).read_bytes())
    key = serialization.load_pem_private_key(Path(s.pki_ca_key_path).read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise PkiError("Unsupported CA key type")
    return cert, key


def ca_cert_pem() -> str:
    return _load_ca()[0].public_bytes(serialization.Encoding.PEM).decode()


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def sign_agent_csr(csr_pem: str, workstation_id: uuid.UUID) -> x509.Certificate:
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except ValueError as exc:
        raise PkiError("Malformed CSR") from exc
    if not csr.is_signature_valid:
        raise PkiError("CSR signature invalid")
    pub = csr.public_key()
    if isinstance(pub, rsa.RSAPublicKey):
        if pub.key_size < 2048:
            raise PkiError("RSA key must be >= 2048 bits")
    elif isinstance(pub, ec.EllipticCurvePublicKey):
        if pub.curve.name not in ("secp256r1", "secp384r1"):
            raise PkiError("Unsupported EC curve")
    else:
        raise PkiError("Unsupported key type")

    ca_cert, ca_key = _load_ca()
    now = _now()
    ttl = get_settings().agent_cert_ttl_days
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, str(workstation_id)),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "flow-agent"),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=ttl))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, content_commitment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert


def parse_forwarded_cert(value: str) -> x509.Certificate:
    """nginx `$ssl_client_escaped_cert` is the URL-encoded PEM."""
    try:
        return x509.load_pem_x509_certificate(unquote(value).encode())
    except ValueError as exc:
        raise PkiError("Unparseable client certificate") from exc


def cert_workstation_id(cert: x509.Certificate) -> uuid.UUID:
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not attrs:
        raise PkiError("Certificate has no CN")
    try:
        return uuid.UUID(str(attrs[0].value))
    except ValueError as exc:
        raise PkiError("Certificate CN is not a workstation id") from exc


def cert_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


# ------------------------------------------------------------- bootstrap ----
def _write(path: Path, data: bytes, mode: int) -> None:
    path.write_bytes(data)
    os.chmod(path, mode)


def init_pki(out_dir: str, server_names: list[str], *, force: bool = False) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ca_crt, ca_key_path = out / "ca.crt", out / "ca.key"
    if ca_crt.exists() and not force:
        print(f"PKI already present in {out} (use --force to recreate)")
        return
    now = _now()
    ca_key = ec.generate_private_key(ec.SECP384R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Flow Platform Agent CA")])
    ca = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA384())
    )
    srv_key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = []
    for n in server_names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    srv = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_names[0])]))
        .issuer_name(ca_name).public_key(srv_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    nopass = serialization.NoEncryption()
    pkcs8 = serialization.PrivateFormat.PKCS8
    _write(ca_crt, ca.public_bytes(pem), 0o644)
    _write(ca_key_path, ca_key.private_bytes(pem, pkcs8, nopass), 0o640)
    _write(out / "server.crt", srv.public_bytes(pem) + ca.public_bytes(pem), 0o644)
    _write(out / "server.key", srv_key.private_bytes(pem, pkcs8, nopass), 0o640)
    print(f"PKI written to {out}: ca.crt, ca.key, server.crt, server.key (SAN: {', '.join(server_names)})")


def main() -> None:
    p = argparse.ArgumentParser(description="Initialise the worker mTLS PKI")
    p.add_argument("--out", default="/certs")
    p.add_argument("--server-name", action="append", dest="names", default=None)
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    init_pki(a.out, a.names or ["localhost", "agent-gateway", "127.0.0.1"], force=a.force)


if __name__ == "__main__":
    main()
