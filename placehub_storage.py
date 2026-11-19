"""
PLACEHUB file storage adapter.

Local mode:
    Uses Flask's existing static/uploads directory.

Cloud mode:
    Uses a private Backblaze B2 bucket through its S3-compatible API.
    Cloud mode is enabled only when PLACEHUB_STORAGE=b2.

Database paths remain unchanged, for example:
    uploads/resumes/<filename>.pdf
    uploads/profile_pictures/<filename>.jpg
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Optional


class StorageConfigurationError(RuntimeError):
    """Raised when cloud storage is selected but not configured correctly."""


class StorageError(RuntimeError):
    """Raised when a storage operation fails."""


def _storage_mode() -> str:
    return os.getenv("PLACEHUB_STORAGE", "local").strip().lower()


def is_cloud_storage_enabled() -> bool:
    return _storage_mode() == "b2"


def _local_upload_root() -> Path:
    # Keep the existing Flask static/uploads location as the local default.
    from flask import current_app

    return Path(current_app.static_folder).resolve() / "uploads"


def _safe_key(key: str) -> str:
    normalized = str(key or "").replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]

    if not parts or any(part == ".." for part in parts):
        raise ValueError("Invalid upload key")

    # Keep the existing database convention: paths start with uploads/.
    if parts[0] != "uploads":
        parts.insert(0, "uploads")

    return "/".join(parts)


def _b2_client():
    if _storage_mode() != "b2":
        raise StorageConfigurationError("Backblaze B2 storage is not enabled.")

    required = {
        "B2_ENDPOINT_URL": os.getenv("B2_ENDPOINT_URL"),
        "B2_REGION": os.getenv("B2_REGION"),
        "B2_BUCKET_NAME": os.getenv("B2_BUCKET_NAME"),
        "B2_APPLICATION_KEY_ID": os.getenv("B2_APPLICATION_KEY_ID"),
        "B2_APPLICATION_KEY": os.getenv("B2_APPLICATION_KEY"),
    }
    missing = [name for name, value in required.items() if not value]

    if missing:
        raise StorageConfigurationError(
            "Backblaze B2 configuration is incomplete. Missing: "
            + ", ".join(missing)
        )

    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise StorageConfigurationError(
            "boto3 is required when PLACEHUB_STORAGE=b2."
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=required["B2_ENDPOINT_URL"],
        region_name=required["B2_REGION"],
        aws_access_key_id=required["B2_APPLICATION_KEY_ID"],
        aws_secret_access_key=required["B2_APPLICATION_KEY"],
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def save_upload(file_obj: BinaryIO, key: str, content_type: Optional[str] = None) -> str:
    """
    Save an uploaded file and return its stable database path.

    In local mode this writes to static/uploads.
    In B2 mode it writes to the configured private bucket.
    """
    safe_key = _safe_key(key)

    if is_cloud_storage_enabled():
        try:
            client = _b2_client()
            extra_args = {}
            if content_type:
                extra_args["ContentType"] = content_type

            file_obj.seek(0)
            client.upload_fileobj(
                file_obj,
                os.environ["B2_BUCKET_NAME"],
                safe_key,
                ExtraArgs=extra_args,
            )
        except Exception as exc:
            raise StorageError(f"Could not upload {safe_key} to Backblaze B2.") from exc
        return safe_key

    target = (_local_upload_root() / Path(*safe_key.split("/")[1:])).resolve()
    root = _local_upload_root()

    if root != target and root not in target.parents:
        raise ValueError("Upload path escapes the local upload directory")

    target.parent.mkdir(parents=True, exist_ok=True)
    file_obj.seek(0)
    try:
        with target.open("wb") as destination:
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
    except Exception as exc:
        raise StorageError(f"Could not save local upload {safe_key}.") from exc

    return safe_key


def download_to_path(key: str, destination: str | os.PathLike) -> Path:
    """Download an object to a caller-owned temporary or working path."""
    safe_key = _safe_key(key)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    if is_cloud_storage_enabled():
        try:
            _b2_client().download_file(
                os.environ["B2_BUCKET_NAME"],
                safe_key,
                str(destination_path),
            )
        except Exception as exc:
            raise StorageError(f"Could not download {safe_key} from Backblaze B2.") from exc
        return destination_path

    source = (_local_upload_root() / Path(*safe_key.split("/")[1:])).resolve()
    root = _local_upload_root()

    if root != source and root not in source.parents:
        raise ValueError("Upload path escapes the local upload directory")
    if not source.is_file():
        raise FileNotFoundError(safe_key)

    destination_path.write_bytes(source.read_bytes())
    return destination_path


def create_temporary_copy(key: str, suffix: str = "") -> str:
    """Create a temporary local copy for existing PDF parsing/scoring code."""
    handle = tempfile.NamedTemporaryFile(prefix="placehub_", suffix=suffix, delete=False)
    path = handle.name
    handle.close()

    try:
        download_to_path(key, path)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

    return path


def delete_upload(key: Optional[str]) -> None:
    """Delete an upload. Missing objects are treated as already deleted."""
    if not key:
        return

    safe_key = _safe_key(key)

    if is_cloud_storage_enabled():
        try:
            _b2_client().delete_object(
                Bucket=os.environ["B2_BUCKET_NAME"],
                Key=safe_key,
            )
        except Exception as exc:
            raise StorageError(f"Could not delete {safe_key} from Backblaze B2.") from exc
        return

    target = (_local_upload_root() / Path(*safe_key.split("/")[1:])).resolve()
    root = _local_upload_root()

    if root != target and root not in target.parents:
        raise ValueError("Upload path escapes the local upload directory")

    try:
        target.unlink(missing_ok=True)
    except Exception as exc:
        raise StorageError(f"Could not delete local upload {safe_key}.") from exc
