"""Object storage with a local-filesystem fallback (ARCHITECTURE.md section 6).

Documents are never publicly readable. The bucket has no anonymous policy and
the local path sits outside anything the web server serves, so the only route to
a file is the authorised, audited download endpoint (SECURITY.md section 6).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.config import StorageBackend, settings
from app.core.errors import (
    DependencyUnavailableError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)
from app.core.logging import get_logger

logger = get_logger("storage")

#: Extension -> (content type, magic-byte signatures). The extension alone is
#: never trusted; a file must actually start with what it claims to be.
ALLOWED_UPLOADS: dict[str, tuple[str, tuple[bytes, ...]]] = {
    "pdf": ("application/pdf", (b"%PDF-",)),
    "doc": ("application/msword", (b"\xd0\xcf\x11\xe0",)),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        (b"PK\x03\x04",),
    ),
    "xls": ("application/vnd.ms-excel", (b"\xd0\xcf\x11\xe0",)),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        (b"PK\x03\x04",),
    ),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "txt": ("text/plain", ()),
}

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]")


@dataclass(frozen=True, slots=True)
class StoredFile:
    storage_key: str
    backend: str
    original_filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str


def sanitise_filename(filename: str) -> str:
    """Strip anything that could escape a directory or confuse a browser."""
    name = Path(filename.replace("\\", "/")).name
    name = _UNSAFE_FILENAME.sub("_", name).strip(". ")
    return name[:200] or "upload"


def validate_upload(filename: str, content: bytes) -> tuple[str, str]:
    """Return (extension, content_type), or raise a user-facing error."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if extension not in ALLOWED_UPLOADS:
        allowed = ", ".join(sorted(ALLOWED_UPLOADS))
        raise UnsupportedMediaTypeError(f"That file type is not supported. Allowed: {allowed}.")
    if not content:
        raise UnsupportedMediaTypeError("That file is empty.")
    if len(content) > settings.MAX_UPLOAD_BYTES:
        limit = settings.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise PayloadTooLargeError(f"That file is larger than the {limit} MB limit.")

    content_type, signatures = ALLOWED_UPLOADS[extension]
    if signatures and not any(content.startswith(signature) for signature in signatures):
        raise UnsupportedMediaTypeError(
            "That file does not match the type its name suggests. Re-save it and try again."
        )
    return extension, content_type


class ObjectStore(Protocol):
    backend: str

    def put(self, key: str, content: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def open(self, key: str) -> BinaryIO: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


class LocalObjectStore:
    """Filesystem storage for development and the no-infrastructure path."""

    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are generated, never user-supplied — but a traversal check costs
        # nothing and turns a future mistake into an error instead of a breach.
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root.resolve()):
            raise DependencyUnavailableError("storage", log_detail=f"path traversal on {key!r}")
        return candidate

    def put(self, key: str, content: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise DependencyUnavailableError("storage", log_detail=f"missing object {key!r}")
        return path.read_bytes()

    def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        if not path.exists():
            raise DependencyUnavailableError("storage", log_detail=f"missing object {key!r}")
        return path.open("rb")

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3ObjectStore:
    """MinIO or S3. The bucket is private; no anonymous policy is ever set."""

    backend = "s3"

    def __init__(self) -> None:
        import boto3

        self.bucket = settings.MINIO_BUCKET
        self._client = boto3.client(
            "s3",
            endpoint_url=(
                settings.MINIO_ENDPOINT
                if settings.STORAGE_BACKEND is StorageBackend.MINIO
                else None
            ),
            aws_access_key_id=settings.MINIO_ACCESS_KEY or None,
            aws_secret_access_key=settings.MINIO_SECRET_KEY or None,
            region_name=settings.MINIO_REGION,
        )

    def put(self, key: str, content: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type)

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise DependencyUnavailableError("storage", log_detail=str(exc)) from exc
        return response["Body"].read()

    def open(self, key: str) -> BinaryIO:
        import io

        return io.BytesIO(self.get(key))

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return False
        return True


_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is None:
        if settings.STORAGE_BACKEND is StorageBackend.LOCAL:
            _store = LocalObjectStore(settings.LOCAL_STORAGE_PATH)
        else:
            try:
                _store = S3ObjectStore()
            except Exception as exc:  # pragma: no cover - only on a bad config
                logger.error("storage_init_failed_using_local", error=str(exc))
                _store = LocalObjectStore(settings.LOCAL_STORAGE_PATH)
        logger.info("storage_backend_selected", backend=_store.backend)
    return _store


def reset_object_store() -> None:
    global _store
    _store = None


def store_upload(filename: str, content: bytes) -> StoredFile:
    """Validate and persist an upload under an opaque key.

    The key carries no part of the original filename: a passport named
    `ahmed-passport-2027.pdf` must not be guessable or informative from a URL.
    """
    safe_name = sanitise_filename(filename)
    extension, content_type = validate_upload(safe_name, content)

    key = f"{uuid.uuid4().hex[:2]}/{uuid.uuid4().hex}.{extension}"
    get_object_store().put(key, content, content_type)

    return StoredFile(
        storage_key=key,
        backend=get_object_store().backend,
        original_filename=safe_name,
        content_type=content_type,
        size_bytes=len(content),
        checksum_sha256=hashlib.sha256(content).hexdigest(),
    )


def read_object(key: str) -> bytes:
    return get_object_store().get(key)


def delete_object(key: str) -> None:
    get_object_store().delete(key)


def purge_local_storage() -> None:
    """Test helper: empty the local store between runs."""
    if settings.LOCAL_STORAGE_PATH.exists():
        shutil.rmtree(settings.LOCAL_STORAGE_PATH, ignore_errors=True)
    settings.LOCAL_STORAGE_PATH.mkdir(parents=True, exist_ok=True)
    reset_object_store()


__all__ = [
    "ALLOWED_UPLOADS",
    "LocalObjectStore",
    "ObjectStore",
    "S3ObjectStore",
    "StoredFile",
    "delete_object",
    "get_object_store",
    "purge_local_storage",
    "read_object",
    "reset_object_store",
    "sanitise_filename",
    "store_upload",
    "validate_upload",
]
