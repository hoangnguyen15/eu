"""SQLAlchemy ORM models. Every table uses UUID primary keys; enums are stored as
VARCHAR guarded by CHECK constraints (simpler to evolve than PG ENUM types)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _check_in(column: str, values: type[enum.Enum]) -> str:
    joined = ", ".join(f"'{v.value}'" for v in values)
    return f"{column} IN ({joined})"


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
                         server_default=text("gen_random_uuid()"))


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------- enums ----
class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class AuthorizationStatus(str, enum.Enum):
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    REVOKED = "REVOKED"


class SessionStatus(str, enum.Enum):
    UNKNOWN = "UNKNOWN"
    ACTIVE = "ACTIVE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    EXPIRED = "EXPIRED"
    DISABLED = "DISABLED"


class AccountStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    LIMITED = "LIMITED"
    DISABLED = "DISABLED"


class WorkstationStatus(str, enum.Enum):
    PENDING = "PENDING"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"


class GenerationStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PROVIDER_LIMITED = "PROVIDER_LIMITED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


TERMINAL_GENERATION_STATUSES = frozenset({
    GenerationStatus.COMPLETED, GenerationStatus.FAILED, GenerationStatus.CANCELLED,
    GenerationStatus.AUTH_REQUIRED, GenerationStatus.PROVIDER_LIMITED, GenerationStatus.REVIEW_REQUIRED,
})


class CreditState(str, enum.Enum):
    NONE = "NONE"
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class LeaseStatus(str, enum.Enum):
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


ACTIVE_LEASE_STATUSES = (LeaseStatus.OFFERED, LeaseStatus.ACCEPTED, LeaseStatus.RUNNING, LeaseStatus.UPLOADING)
_ACTIVE_LEASE_SQL = "status IN ('OFFERED', 'ACCEPTED', 'RUNNING', 'UPLOADING')"


class CreditTxType(str, enum.Enum):
    GRANT = "GRANT"
    RESERVE = "RESERVE"
    SETTLE = "SETTLE"
    RELEASE = "RELEASE"
    ADJUST = "ADJUST"


class CreditTxStatus(str, enum.Enum):
    COMPLETED = "COMPLETED"


# --------------------------------------------------------------- tables ----
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_check_in("status", UserStatus), name="status"),
        CheckConstraint(_check_in("role", UserRole), name="role"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=UserRole.USER.value,
                                      server_default=UserRole.USER.value)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=UserStatus.ACTIVE.value,
                                        server_default=UserStatus.ACTIVE.value)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (Index(None, "user_id"), Index(None, "family_id"))
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class GoogleAccount(Base):
    """A provider account whose authorized use an admin has attested to.
    No password, cookie or session token is ever stored here."""
    __tablename__ = "google_accounts"
    __table_args__ = (
        CheckConstraint(_check_in("authorization_status", AuthorizationStatus), name="authorization_status"),
        CheckConstraint(_check_in("session_status", SessionStatus), name="session_status"),
        CheckConstraint(_check_in("status", AccountStatus), name="status"),
        Index(None, "owner_id"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    google_email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    authorization_status: Mapped[str] = mapped_column(String(16), nullable=False,
                                                      server_default=AuthorizationStatus.PENDING.value)
    session_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=SessionStatus.UNKNOWN.value)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=AccountStatus.ACTIVE.value)
    limited_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Workstation(Base):
    __tablename__ = "workstations"
    __table_args__ = (
        CheckConstraint(_check_in("status", WorkstationStatus), name="status"),
        CheckConstraint("capacity >= 0 AND capacity <= 64", name="capacity"),
        Index(None, "status", "last_heartbeat_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=WorkstationStatus.PENDING.value)
    agent_version: Mapped[str | None] = mapped_column(String(64))
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    providers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    cert_fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True)
    cert_serial: Mapped[str | None] = mapped_column(String(64))
    cert_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"
    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    workstation_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workstation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workstations.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()


class AccountSession(Base):
    """A browser/provider profile that lives on exactly one workstation.
    `profile_ref` is an opaque local identifier; session material never leaves the machine."""
    __tablename__ = "account_sessions"
    __table_args__ = (
        CheckConstraint(_check_in("status", SessionStatus), name="status"),
        UniqueConstraint("account_id", "workstation_id"),
        Index(None, "workstation_id", "status"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("google_accounts.id", ondelete="CASCADE"), nullable=False)
    workstation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workstations.id", ondelete="CASCADE"), nullable=False)
    profile_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=SessionStatus.UNKNOWN.value)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Generation(Base):
    __tablename__ = "generations"
    __table_args__ = (
        CheckConstraint(_check_in("status", GenerationStatus), name="status"),
        CheckConstraint(_check_in("credit_state", CreditState), name="credit_state"),
        CheckConstraint("number_of_images BETWEEN 1 AND 16", name="number_of_images"),
        UniqueConstraint("user_id", "idempotency_key"),
        Index(None, "status", "created_at"),
        Index(None, "user_id", "created_at"),
        Index(None, "worker_id"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("google_accounts.id", ondelete="SET NULL"))
    worker_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workstations.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(8), nullable=False)
    number_of_images: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_asset_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    client_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=GenerationStatus.QUEUED.value)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    credit_cost: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    credit_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=CreditState.NONE.value)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskLease(Base):
    """A time-bounded claim of one worker slot for one generation. Partial unique
    indexes guarantee one active lease per slot and per generation."""
    __tablename__ = "task_leases"
    __table_args__ = (
        CheckConstraint(_check_in("status", LeaseStatus), name="status"),
        Index("uq_task_leases_active_slot", "worker_id", "slot_index", unique=True,
              postgresql_where=text(_ACTIVE_LEASE_SQL)),
        Index("uq_task_leases_active_generation", "generation_id", unique=True,
              postgresql_where=text(_ACTIVE_LEASE_SQL)),
        Index(None, "status", "expires_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    generation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generations.id", ondelete="CASCADE"), nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workstations.id", ondelete="CASCADE"), nullable=False)
    account_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account_sessions.id", ondelete="SET NULL"))
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    upload_keys: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False,
                                                             server_default=text("'[]'::jsonb"))
    offered_at: Mapped[datetime] = created_at_col()
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("generation_id", "image_index"),
        CheckConstraint("file_size > 0", name="file_size"),
        Index(None, "user_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    generation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generations.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    image_index: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    thumbnail_key: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()


class GenerationMetadata(Base):
    __tablename__ = "generation_metadata"
    id: Mapped[uuid.UUID] = uuid_pk()
    generation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generations.id", ondelete="CASCADE"),
                                                     nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                              server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()


class CreditWallet(Base):
    __tablename__ = "credit_wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="balance_non_negative"),
        CheckConstraint("reserved_balance >= 0", name="reserved_non_negative"),
        CheckConstraint("reserved_balance <= balance", name="reserved_le_balance"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reserved_balance: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = updated_at_col()


class CreditTransaction(Base):
    """Append-only ledger. Partial unique indexes make reserve/settle/release
    idempotent per generation and forbid settling *and* releasing the same one."""
    __tablename__ = "credit_transactions"
    __table_args__ = (
        CheckConstraint(_check_in("transaction_type", CreditTxType), name="transaction_type"),
        CheckConstraint(_check_in("status", CreditTxStatus), name="status"),
        Index("uq_credit_tx_generation_type", "generation_id", "transaction_type", unique=True,
              postgresql_where=text("generation_id IS NOT NULL")),
        Index("uq_credit_tx_generation_final", "generation_id", unique=True,
              postgresql_where=text("transaction_type IN ('SETTLE', 'RELEASE')")),
        Index(None, "user_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("generations.id", ondelete="SET NULL"))
    transaction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=CreditTxStatus.COMPLETED.value)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = created_at_col()


class WorkerEvent(Base):
    """Inbound worker events, deduplicated by (worker_id, event_id). The stored
    `result` is replayed in EVENT_ACK when an agent re-sends an outbox event."""
    __tablename__ = "worker_events"
    __table_args__ = (UniqueConstraint("worker_id", "event_id"), Index(None, "created_at"))
    id: Mapped[uuid.UUID] = uuid_pk()
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workstations.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index(None, "resource_type", "resource_id"), Index(None, "actor_id", "created_at"))
    id: Mapped[uuid.UUID] = uuid_pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()
