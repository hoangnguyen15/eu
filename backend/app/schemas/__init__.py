"""Pydantic v2 request/response schemas for the public REST API."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    success: bool = True
    data: T


def ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ------------------------------------------------------------------ auth ----
class RegisterRequest(StrictModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class LoginRequest(StrictModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=20, max_length=256)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserProfile(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    created_at: datetime


class CreditBalance(BaseModel):
    balance: int
    reserved_balance: int
    available_balance: int


class CreditTransactionOut(BaseModel):
    id: uuid.UUID
    generation_id: uuid.UUID | None
    transaction_type: str
    amount: int
    status: str
    balance_after: int
    reserved_after: int
    note: str | None
    created_at: datetime


# ----------------------------------------------------------- generations ----
class GenerationCreate(StrictModel):
    prompt: str = Field(min_length=1, max_length=2000)
    aspect_ratio: str = "1:1"
    number_of_images: int = Field(default=1, ge=1, le=16)
    reference_images: list[uuid.UUID] = Field(default_factory=list, max_length=4,
                                              description="IDs of the caller's own assets")
    provider: str | None = Field(default=None, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _small_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        import json

        if len(json.dumps(v, default=str)) > 4096:
            raise ValueError("metadata too large (max 4KB)")
        return v


class GenerationCreated(BaseModel):
    generation_id: uuid.UUID
    status: str
    created_at: datetime


class AssetOut(BaseModel):
    id: uuid.UUID
    generation_id: uuid.UUID
    image_index: int
    mime_type: str
    width: int
    height: int
    file_size: int
    sha256: str
    created_at: datetime
    url: str | None = None
    thumbnail_url: str | None = None
    url_expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GenerationOut(BaseModel):
    id: uuid.UUID
    status: str
    prompt: str
    aspect_ratio: str
    number_of_images: int
    provider: str
    progress: int
    error_code: str | None
    error_message: str | None
    credit_cost: int
    credit_state: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    assets: list[AssetOut] = Field(default_factory=list)
    provider_metadata: dict[str, Any] | None = None


class GenerationPage(BaseModel):
    items: list[GenerationOut]
    next_cursor: str | None


# ----------------------------------------------------------------- admin ----
class EnrollmentTokenCreate(StrictModel):
    workstation_name: str = Field(min_length=1, max_length=128)


class EnrollmentTokenOut(BaseModel):
    id: uuid.UUID
    token: str
    workstation_name: str
    expires_at: datetime


class GoogleAccountCreate(StrictModel):
    google_email: EmailStr
    authorization_status: Literal["PENDING", "AUTHORIZED", "REVOKED"] = "PENDING"
    attestation: str = Field(min_length=10, max_length=500,
                             description="Why use of this account by automation is authorized")


class GoogleAccountUpdate(StrictModel):
    authorization_status: Literal["PENDING", "AUTHORIZED", "REVOKED"] | None = None
    status: Literal["ACTIVE", "LIMITED", "DISABLED"] | None = None


class GoogleAccountOut(BaseModel):
    id: uuid.UUID
    google_email: str
    owner_id: uuid.UUID
    authorization_status: str
    session_status: str
    status: str
    limited_until: datetime | None
    created_at: datetime


class WorkstationOut(BaseModel):
    id: uuid.UUID
    name: str
    hostname: str | None
    status: str
    agent_version: str | None
    capacity: int
    providers: list[str]
    last_heartbeat_at: datetime | None
    cert_expires_at: datetime | None
    active_leases: int = 0


class CreditGrant(StrictModel):
    user_id: uuid.UUID
    amount: int = Field(gt=0, le=1_000_000)
    note: str | None = Field(default=None, max_length=255)


class ResolveReview(StrictModel):
    action: Literal["refund", "charge"]
    note: str | None = Field(default=None, max_length=255)


class AgentEnrollRequest(StrictModel):
    token: str = Field(min_length=20, max_length=256)
    csr_pem: str = Field(min_length=100, max_length=16384)
    hostname: str | None = Field(default=None, max_length=255)


class AgentEnrollResponse(BaseModel):
    workstation_id: uuid.UUID
    certificate_pem: str
    ca_certificate_pem: str
    expires_at: datetime
