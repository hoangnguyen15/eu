"""Worker <-> Central wire protocol (JSON over WSS).

Envelope: {"type": str, "id": str, "ts": ISO8601, "payload": {...}}
Agent events that change state carry `payload.event_id`; the server stores
them in `worker_events` (dedup) and always answers with EVENT_ACK:
  {"type": "EVENT_ACK", "payload": {"event_id", "status": "ok|rejected|error",
   "result": {...}, "duplicate": bool, "retryable": bool}}
"""
from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MsgType(str, enum.Enum):
    REGISTER = "REGISTER"
    HEARTBEAT = "HEARTBEAT"
    TASK_OFFER = "TASK_OFFER"
    TASK_ACCEPT = "TASK_ACCEPT"
    TASK_REJECT = "TASK_REJECT"
    TASK_STARTED = "TASK_STARTED"
    TASK_PROGRESS = "TASK_PROGRESS"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCEL = "TASK_CANCEL"
    UPLOAD_REQUEST = "UPLOAD_REQUEST"
    UPLOAD_COMPLETED = "UPLOAD_COMPLETED"
    SESSION_STATUS = "SESSION_STATUS"
    EVENT_ACK = "EVENT_ACK"
    ERROR = "ERROR"


# Events that are deduplicated + persisted in worker_events.
PERSISTED_EVENTS = {
    MsgType.TASK_ACCEPT, MsgType.TASK_REJECT, MsgType.TASK_STARTED, MsgType.TASK_PROGRESS,
    MsgType.TASK_COMPLETED, MsgType.TASK_FAILED, MsgType.UPLOAD_REQUEST, MsgType.UPLOAD_COMPLETED,
    MsgType.SESSION_STATUS,
}


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Envelope(_M):
    type: MsgType
    id: str | None = None
    ts: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RegisterPayload(_M):
    hostname: str | None = Field(default=None, max_length=255)
    agent_version: str | None = Field(default=None, max_length=64)
    capacity: int = Field(default=1, ge=0, le=64)
    providers: list[str] = Field(default_factory=list, max_length=8)
    active_tasks: list[str] = Field(default_factory=list)


class HeartbeatPayload(_M):
    active_tasks: list[str] = Field(default_factory=list)
    free_slots: int | None = None


class SessionReport(_M):
    google_email: str = Field(max_length=320)
    profile_ref: str = Field(max_length=255)
    provider: str = Field(max_length=32)
    status: str = Field(max_length=16)


class SessionStatusPayload(_M):
    event_id: str
    sessions: list[SessionReport] = Field(default_factory=list, max_length=64)


class TaskRef(_M):
    event_id: str = Field(min_length=1, max_length=64)
    task_id: uuid.UUID


class TaskRejectPayload(TaskRef):
    reason: str | None = Field(default=None, max_length=500)


class TaskProgressPayload(TaskRef):
    progress: int = Field(ge=0, le=100)


class TaskFailedPayload(TaskRef):
    error_code: str = Field(max_length=64)
    error_message: str | None = Field(default=None, max_length=2000)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86400)


class UploadFileSpec(_M):
    index: int = Field(ge=0, le=15)
    mime_type: str = Field(max_length=64)
    size: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class UploadRequestPayload(TaskRef):
    files: list[UploadFileSpec] = Field(min_length=1, max_length=16)


class UploadedFile(UploadFileSpec):
    storage_key: str = Field(max_length=512)
    width: int | None = None
    height: int | None = None


class UploadCompletedPayload(TaskRef):
    files: list[UploadedFile] = Field(min_length=1, max_length=16)


class TaskCompletedPayload(TaskRef):
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=128)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


def message(msg_type: MsgType | str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": msg_type.value if isinstance(msg_type, MsgType) else msg_type,
        "id": uuid.uuid4().hex,
        "ts": datetime.now(UTC).isoformat(),
        "payload": payload,
    }
