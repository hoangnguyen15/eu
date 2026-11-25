"""S3-compatible object storage (MinIO, Cloudflare R2, AWS S3).

Two clients are kept: one bound to the internal endpoint (server-side reads and
writes) and one bound to the public endpoint, used only to *sign* URLs that
workers and mobile clients will call directly. The bucket must stay private.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings


def _client(endpoint: str | None) -> BaseClient:
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id,
        aws_secret_access_key=s.s3_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": s.s3_addressing_style},
                      retries={"max_attempts": 3, "mode": "standard"}),
    )


@lru_cache
def internal_client() -> BaseClient:
    return _client(get_settings().s3_endpoint_url)


@lru_cache
def signing_client() -> BaseClient:
    s = get_settings()
    return _client(s.s3_public_endpoint_url or s.s3_endpoint_url)


class ObjectTooLarge(Exception):
    pass


class ObjectMissing(Exception):
    pass


def presign_put(key: str, content_type: str, ttl: int | None = None) -> str:
    s = get_settings()
    return signing_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": s.s3_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=ttl or s.upload_url_ttl_seconds,
    )


def presign_get(key: str, ttl: int | None = None, download_name: str | None = None) -> str:
    s = get_settings()
    params: dict[str, Any] = {"Bucket": s.s3_bucket, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    return signing_client().generate_presigned_url("get_object", Params=params,
                                                   ExpiresIn=ttl or s.signed_url_ttl_seconds)


def _get_bytes_sync(key: str, max_bytes: int) -> bytes:
    s = get_settings()
    try:
        head = internal_client().head_object(Bucket=s.s3_bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            raise ObjectMissing(key) from exc
        raise
    if int(head["ContentLength"]) > max_bytes:
        raise ObjectTooLarge(key)
    obj = internal_client().get_object(Bucket=s.s3_bucket, Key=key)
    data: bytes = obj["Body"].read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ObjectTooLarge(key)
    return data


async def get_bytes(key: str, max_bytes: int) -> bytes:
    return await asyncio.to_thread(_get_bytes_sync, key, max_bytes)


async def put_bytes(key: str, data: bytes, content_type: str) -> None:
    s = get_settings()
    await asyncio.to_thread(internal_client().put_object, Bucket=s.s3_bucket, Key=key, Body=data,
                            ContentType=content_type)


async def delete_object(key: str) -> None:
    s = get_settings()
    await asyncio.to_thread(internal_client().delete_object, Bucket=s.s3_bucket, Key=key)


def ensure_bucket_sync() -> None:
    """Create the bucket if missing (dev/test only) and block public access."""
    s = get_settings()
    client = internal_client()
    try:
        client.head_bucket(Bucket=s.s3_bucket)
    except ClientError:
        client.create_bucket(Bucket=s.s3_bucket)


async def ping() -> bool:
    s = get_settings()
    try:
        await asyncio.to_thread(internal_client().head_bucket, Bucket=s.s3_bucket)
        return True
    except Exception:
        return False
