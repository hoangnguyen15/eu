"""Image validation: real MIME sniffing (magic bytes + decoder), size, pixel
limits, SHA-256 verification and thumbnail generation."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_PIL_TO_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ImageValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ImageInfo:
    mime_type: str
    width: int
    height: int
    file_size: int
    sha256: str


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_image(data: bytes, *, allowed_mime: list[str], max_bytes: int, max_pixels: int,
                   expected_sha256: str | None = None, declared_mime: str | None = None) -> ImageInfo:
    if not data:
        raise ImageValidationError("EMPTY_FILE", "Image is empty")
    if len(data) > max_bytes:
        raise ImageValidationError("FILE_TOO_LARGE", f"Image exceeds {max_bytes} bytes")
    digest = sha256_hex(data)
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise ImageValidationError("CHECKSUM_MISMATCH", "SHA-256 does not match uploaded content")
    magic = sniff_mime(data)
    if magic is None or magic not in allowed_mime:
        raise ImageValidationError("UNSUPPORTED_MEDIA_TYPE", f"Unsupported or unrecognised image type: {magic}")
    if declared_mime is not None and declared_mime != magic:
        raise ImageValidationError("MIME_MISMATCH", f"Declared {declared_mime} but content is {magic}")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            width, height = probe.size
            if width * height > max_pixels:
                raise ImageValidationError("TOO_MANY_PIXELS", "Image dimensions exceed limit")
            probe.verify()  # structural integrity check
        with Image.open(io.BytesIO(data)) as full:
            full.load()  # verify() does not decode pixel data; load() does
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ImageValidationError("CORRUPT_IMAGE", f"Image could not be decoded: {exc}") from exc
    if _PIL_TO_MIME.get(fmt or "") != magic:
        raise ImageValidationError("MIME_MISMATCH", "Decoder format does not match magic bytes")
    return ImageInfo(mime_type=magic, width=width, height=height, file_size=len(data), sha256=digest)


def make_thumbnail(data: bytes, size: int) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((size, size))
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=80, method=4)
        return out.getvalue()
