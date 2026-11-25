"""Test harness.

Unit tests need nothing. Integration tests (marked `integration`) need real
PostgreSQL, Redis and S3-compatible storage — the docker-compose services or
the ones started by scripts/test-services.sh. Connection settings:

  TEST_PG_DSN     postgresql://flow:devpass@localhost:55432/flow   (admin DSN)
  TEST_REDIS_URL  redis://localhost:56379/1
  TEST_S3_URL     http://localhost:59000 (+ TEST_S3_KEY / TEST_S3_SECRET)

A fresh database `flow_test` is created and migrated with Alembic, then the API
(uvicorn) and the scheduler are launched as real subprocesses.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
PG_DSN = os.environ.get("TEST_PG_DSN", "postgresql://flow:devpass@localhost:55432/flow")
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/1")
S3_URL = os.environ.get("TEST_S3_URL", "http://localhost:59000")
S3_KEY = os.environ.get("TEST_S3_KEY", "devminio")
S3_SECRET = os.environ.get("TEST_S3_SECRET", "devminio-secret-123")
TEST_DB = "flow_test"
GATEWAY_SECRET = "test-gateway-secret-0123456789abcdef"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-password-123456"

_PKI_DIR = tempfile.mkdtemp(prefix="flow-test-pki-")


def _test_db_url(driver: str = "postgresql+asyncpg") -> str:
    base = PG_DSN.rsplit("/", 1)[0].replace("postgresql://", f"{driver}://")
    return f"{base}/{TEST_DB}"


BASE_ENV: dict[str, str] = {
    "ENVIRONMENT": "test",
    "LOG_LEVEL": "WARNING",
    "DATABASE_URL": _test_db_url(),
    "REDIS_URL": REDIS_URL,
    "JWT_SECRET": "test-jwt-secret-0123456789abcdef0123456789",
    "GATEWAY_SHARED_SECRET": GATEWAY_SECRET,
    "S3_ENDPOINT_URL": S3_URL,
    "S3_ACCESS_KEY_ID": S3_KEY,
    "S3_SECRET_ACCESS_KEY": S3_SECRET,
    "S3_BUCKET": "flow-test",
    "S3_CREATE_BUCKET": "true",
    "PKI_CA_CERT_PATH": f"{_PKI_DIR}/ca.crt",
    "PKI_CA_KEY_PATH": f"{_PKI_DIR}/ca.key",
    "OFFER_TTL_SECONDS": "3",
    "JOB_TIMEOUT_SECONDS": "6",
    "WORKER_OFFLINE_AFTER_SECONDS": "4",
    "HEARTBEAT_INTERVAL_SECONDS": "2",
    "SCHEDULER_POLL_INTERVAL_SECONDS": "0.3",
    "MAX_DISPATCH_ATTEMPTS": "3",
    "MAX_CONCURRENT_JOBS_PER_ACCOUNT": "8",
    "RATE_LIMIT_AUTH_PER_MINUTE": "1000",
    "RATE_LIMIT_GENERATIONS_PER_MINUTE": "1000",
    "SIGNUP_BONUS_CREDITS": "10",
}

# Unit tests import app modules; give them a complete, harmless config.
for _k, _v in BASE_ENV.items():
    os.environ.setdefault(_k, _v)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs Postgres/Redis/S3 and spawns API + scheduler")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _services_available() -> str | None:
    import asyncpg  # noqa: F401
    import redis

    try:
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2).ping()
    except Exception as exc:
        return f"redis unavailable: {exc}"
    try:
        httpx.get(S3_URL + "/minio/health/live", timeout=2)
    except Exception as exc:
        return f"s3 unavailable: {exc}"
    return None


async def _recreate_db() -> None:
    import asyncpg

    conn = await asyncpg.connect(PG_DSN)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def stack() -> Iterator[dict[str, Any]]:
    reason = _services_available()
    if reason:
        pytest.skip(reason)
    import redis

    redis.Redis.from_url(REDIS_URL).flushdb()
    asyncio.run(_recreate_db())
    env = {**os.environ, **BASE_ENV, "PYTHONPATH": str(BACKEND)}
    subprocess.run([sys.executable, "-m", "app.cli", "init-pki", "--out", _PKI_DIR, "--force"], cwd=BACKEND,
                   env=env, check=True, capture_output=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, check=True,
                   capture_output=True)
    subprocess.run([sys.executable, "-m", "app.cli", "create-admin", "--email", ADMIN_EMAIL], cwd=BACKEND,
                   env={**env, "ADMIN_PASSWORD": ADMIN_PASSWORD}, check=True, capture_output=True)

    port = _free_port()
    logdir = Path(tempfile.mkdtemp(prefix="flow-test-logs-"))
    api_log = open(logdir / "api.log", "w")  # noqa: SIM115
    sched_log = open(logdir / "scheduler.log", "w")  # noqa: SIM115
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port",
                            str(port)], cwd=BACKEND, env=env, stdout=api_log, stderr=subprocess.STDOUT)
    sched = subprocess.Popen([sys.executable, "-m", "app.scheduler"], cwd=BACKEND, env=env, stdout=sched_log,
                             stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(base + "/api/v1/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    else:
        api.terminate()
        sched.terminate()
        raise RuntimeError(f"API did not start; see {logdir}/api.log:\n" + (logdir / "api.log").read_text())
    try:
        yield {"base": base, "ws": f"ws://127.0.0.1:{port}", "env": env, "logdir": logdir}
    finally:
        for proc in (api, sched):
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        api_log.close()
        sched_log.close()
        print(f"\n[stack logs in {logdir}]")


@pytest.fixture
async def client(stack: dict[str, Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=stack["base"], timeout=30) as c:
        yield c


async def register_and_login(client: httpx.AsyncClient, email: str | None = None,
                             password: str = "correct-horse-battery") -> dict[str, Any]:
    email = email or f"user-{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    return {"email": email, "password": password, "id": None, **data,
            "headers": {"Authorization": f"Bearer {data['access_token']}"}}


@pytest.fixture
async def user(client: httpx.AsyncClient) -> dict[str, Any]:
    u = await register_and_login(client)
    prof = await client.get("/api/v1/user/profile", headers=u["headers"])
    u["id"] = prof.json()["data"]["id"]
    return u


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    tok = r.json()["data"]["access_token"]
    return {"headers": {"Authorization": f"Bearer {tok}"}}


def make_csr() -> tuple[str, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "attacker-chosen-name")]))
           .sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode(), key


async def enroll(client: httpx.AsyncClient, admin: dict[str, Any], name: str = "ws") -> dict[str, Any]:
    r = await client.post("/api/v1/admin/enrollment-tokens", json={"workstation_name": name},
                          headers=admin["headers"])
    assert r.status_code == 201, r.text
    token = r.json()["data"]["token"]
    csr, _ = make_csr()
    r = await client.post("/api/v1/agent/enroll", json={"token": token, "csr_pem": csr, "hostname": name})
    assert r.status_code == 200, r.text
    return {**r.json()["data"], "token": token}


async def ensure_account(client: httpx.AsyncClient, admin: dict[str, Any], email: str) -> None:
    r = await client.post("/api/v1/admin/google-accounts",
                          json={"google_email": email, "authorization_status": "AUTHORIZED",
                                "attestation": "test fixture: mock provider account"},
                          headers=admin["headers"])
    assert r.status_code in (201, 409), r.text


async def wait_for(predicate: Any, timeout: float = 20, interval: float = 0.2) -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


async def wait_status(client: httpx.AsyncClient, headers: dict[str, str], gen_id: str, statuses: set[str],
                      timeout: float = 25) -> dict[str, Any]:
    async def check() -> dict[str, Any] | None:
        r = await client.get(f"/api/v1/generations/{gen_id}", headers=headers)
        d = r.json()["data"]
        return d if d["status"] in statuses else None
    return await wait_for(check, timeout)
