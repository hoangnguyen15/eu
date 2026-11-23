"""Run against a real PostgreSQL: TEST_DATABASE_URL=postgresql+asyncpg://user:pw@host/db

The target database's `public` schema is DROPPED and recreated - use a throwaway database.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.db

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)

BACKEND_DIR = Path(__file__).resolve().parent.parent
PG_DSN = TEST_DB_URL.replace("+asyncpg", "")

EXPECTED_TABLES = {
    "users", "wallets", "image_jobs", "credit_transactions", "image_assets",
    "flow_accounts", "worker_nodes", "job_attempts", "outbox_events", "audit_logs",
    "alembic_version",
}  # fmt: skip


def alembic(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": TEST_DB_URL},
        check=True,
        capture_output=True,
        text=True,
    )


def run(coro_fn):
    async def inner():
        conn = await asyncpg.connect(PG_DSN)
        try:
            return await coro_fn(conn)
        finally:
            await conn.close()

    return asyncio.run(inner())


@pytest.fixture
def migrated():
    async def reset(conn):
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")

    run(reset)
    alembic("upgrade", "head")
    yield
    run(reset)


async def _tables(conn) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    return {r["tablename"] for r in rows}


async def _make_user(conn) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO users(email, password_hash) VALUES($1, 'x') RETURNING id",
        f"{uuid.uuid4()}@example.com",
    )


def test_upgrade_creates_all_tables(migrated) -> None:
    assert run(_tables) == EXPECTED_TABLES


def test_downgrade_removes_everything(migrated) -> None:
    alembic("downgrade", "base")
    assert run(_tables) == {"alembic_version"}
    alembic("upgrade", "head")  # re-appliable
    assert run(_tables) == EXPECTED_TABLES


def test_wallet_constraints(migrated) -> None:
    async def go(conn):
        uid = await _make_user(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO wallets(user_id, balance) VALUES($1, -1)", uid)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO wallets(user_id, balance, reserved) VALUES($1, 5, 6)", uid
            )
        await conn.execute("INSERT INTO wallets(user_id, balance, reserved) VALUES($1, 5, 5)", uid)

    run(go)


def test_job_idempotency_key_unique_per_user(migrated) -> None:
    async def go(conn):
        u1, u2 = await _make_user(conn), await _make_user(conn)
        ins = (
            "INSERT INTO image_jobs(user_id, provider, prompt, credit_cost, idempotency_key)"
            " VALUES($1, 'mock', 'p', 5, $2)"
        )
        await conn.execute(ins, u1, "k1")
        await conn.execute(ins, u2, "k1")  # other user may reuse the key
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(ins, u1, "k1")

    run(go)


def test_job_validation_checks(migrated) -> None:
    async def go(conn):
        uid = await _make_user(conn)
        base = (
            "INSERT INTO image_jobs(user_id, provider, prompt, number_of_images, credit_cost,"
            " idempotency_key) VALUES($1, 'mock', $2, $3, $4, $5)"
        )
        for args in [("", 1, 5), ("x" * 4001, 1, 5), ("ok", 0, 5), ("ok", 5, 5), ("ok", 1, -1)]:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(base, uid, args[0], args[1], args[2], str(uuid.uuid4()))

    run(go)


def test_ledger_is_append_only_and_idempotent(migrated) -> None:
    async def go(conn):
        uid = await _make_user(conn)
        ins = (
            "INSERT INTO credit_transactions(user_id, type, amount, idempotency_key)"
            " VALUES($1, 'topup', 10, $2)"
        )
        await conn.execute(ins, uid, "tx-1")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(ins, uid, "tx-1")
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("UPDATE credit_transactions SET amount = 999")
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("DELETE FROM credit_transactions")
        assert await conn.fetchval("SELECT amount FROM credit_transactions") == 10

    run(go)


def test_outbox_pending_partial_index_exists(migrated) -> None:
    async def go(conn):
        defn = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE indexname='idx_outbox_pending'"
        )
        assert "published_at IS NULL" in defn

    run(go)


def test_ready_endpoint_against_real_database(migrated) -> None:
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL not set")
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    with TestClient(
        create_app(Settings(app_env="test", database_url=TEST_DB_URL, redis_url=redis_url))
    ) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"] == {"database": "ok", "redis": "ok"}
