"""Baseline schema (spec §4.1) + immutable credit ledger.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES_DROP_ORDER = [
    "audit_logs",
    "outbox_events",
    "job_attempts",
    "worker_nodes",
    "flow_accounts",
    "image_assets",
    "credit_transactions",
    "image_jobs",
    "wallets",
    "users",
]

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    """
    CREATE TABLE users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email VARCHAR(255) UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role VARCHAR(30) NOT NULL DEFAULT 'user',
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE wallets (
        user_id UUID PRIMARY KEY REFERENCES users(id),
        balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
        reserved BIGINT NOT NULL DEFAULT 0 CHECK (reserved >= 0 AND reserved <= balance),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE image_jobs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES users(id),
        provider VARCHAR(50) NOT NULL,
        prompt TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 4000),
        aspect_ratio VARCHAR(10) NOT NULL DEFAULT '1:1',
        number_of_images SMALLINT NOT NULL DEFAULT 1 CHECK (number_of_images BETWEEN 1 AND 4),
        status VARCHAR(30) NOT NULL DEFAULT 'queued',
        credit_cost BIGINT NOT NULL CHECK (credit_cost >= 0),
        idempotency_key VARCHAR(150) NOT NULL,
        lease_until TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        UNIQUE (user_id, idempotency_key)
    )""",
    """
    CREATE TABLE credit_transactions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES users(id),
        job_id UUID REFERENCES image_jobs(id),
        type VARCHAR(30) NOT NULL,
        amount BIGINT NOT NULL,
        idempotency_key VARCHAR(150) UNIQUE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE image_assets (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id UUID NOT NULL REFERENCES image_jobs(id),
        storage_key TEXT NOT NULL UNIQUE,
        mime_type VARCHAR(100) NOT NULL,
        width INT,
        height INT,
        size_bytes BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE flow_accounts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        label VARCHAR(100) NOT NULL,
        status VARCHAR(30) NOT NULL DEFAULT 'disabled',
        secret_ref TEXT,
        max_concurrency INT NOT NULL DEFAULT 1 CHECK (max_concurrency > 0),
        last_health_check_at TIMESTAMPTZ,
        last_error_code VARCHAR(100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE worker_nodes (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        hostname VARCHAR(255) NOT NULL,
        status VARCHAR(30) NOT NULL DEFAULT 'offline',
        capacity INT NOT NULL DEFAULT 1 CHECK (capacity > 0),
        last_heartbeat TIMESTAMPTZ
    )""",
    """
    CREATE TABLE job_attempts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id UUID NOT NULL REFERENCES image_jobs(id),
        worker_id UUID REFERENCES worker_nodes(id),
        status VARCHAR(30) NOT NULL,
        error_code VARCHAR(100),
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ
    )""",
    """
    CREATE TABLE outbox_events (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        event_type VARCHAR(100) NOT NULL,
        aggregate_id UUID NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        published_at TIMESTAMPTZ
    )""",
    """
    CREATE TABLE audit_logs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        actor_id UUID REFERENCES users(id),
        action VARCHAR(100) NOT NULL,
        resource_type VARCHAR(100),
        resource_id UUID,
        metadata JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX idx_jobs_status_created ON image_jobs(status, created_at)",
    "CREATE INDEX idx_jobs_user_created ON image_jobs(user_id, created_at DESC)",
    "CREATE INDEX idx_attempts_job ON job_attempts(job_id)",
    "CREATE INDEX idx_outbox_pending ON outbox_events(created_at) WHERE published_at IS NULL",
    # Additions beyond the spec baseline (hygiene):
    "CREATE INDEX idx_ledger_user_created ON credit_transactions(user_id, created_at DESC)",
    "CREATE INDEX idx_assets_job ON image_assets(job_id)",
    # Ledger is append-only (spec §0 rule 5: "ledger bất biến").
    """
    CREATE FUNCTION forbid_ledger_mutation() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'credit_transactions is append-only';
    END;
    $$ LANGUAGE plpgsql""",
    """
    CREATE TRIGGER credit_transactions_immutable
    BEFORE UPDATE OR DELETE ON credit_transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation()""",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS credit_transactions_immutable ON credit_transactions")
    op.execute("DROP FUNCTION IF EXISTS forbid_ledger_mutation()")
    for table in TABLES_DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table}")
