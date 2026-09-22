"""Completed-result storage adapters.

The local adapter is the default. S3-compatible storage is opt-in and requires
both boto3 and explicit bucket/endpoint configuration.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode


class StorageError(RuntimeError):
    """Raised when configured result storage cannot complete an operation."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int


class ObjectStorage:
    def __init__(
        self,
        backend: str = "local",
        root: Path | None = None,
        bucket: str = "clipper-results",
        endpoint_url: str | None = None,
        signed_url_seconds: int = 300,
        client=None,
    ):
        self.backend = backend.lower()
        self.root = root or Path(os.getenv("RESULT_STORAGE_ROOT", "storage"))
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.signed_url_seconds = signed_url_seconds
        self._client = client
        if self.signed_url_seconds <= 0:
            raise ValueError("STORAGE_SIGNED_URL_SECONDS must be positive")
        if self.backend == "local":
            self.root.mkdir(parents=True, exist_ok=True)
        elif self.backend in {"s3", "minio"}:
            if self._client is None:
                try:
                    import boto3
                except ImportError as exc:
                    raise StorageError(
                        "S3/MinIO storage requires boto3; install the `storage` extra."
                    ) from exc
                self._client = boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    region_name=os.getenv("AWS_REGION", "us-east-1"),
                )
            if not bucket:
                raise StorageError("STORAGE_BUCKET is required for object storage")
        else:
            raise ValueError("STORAGE_BACKEND must be local, s3, or minio")

    @classmethod
    def from_env(cls) -> ObjectStorage:
        backend = os.getenv("STORAGE_BACKEND", "local")
        endpoint = os.getenv("S3_ENDPOINT_URL") or os.getenv("MINIO_ENDPOINT")
        return cls(
            backend=backend,
            bucket=os.getenv("STORAGE_BUCKET", "clipper-results"),
            endpoint_url=endpoint,
            signed_url_seconds=int(os.getenv("STORAGE_SIGNED_URL_SECONDS", "300")),
        )

    def upload(self, source: Path, key: str) -> StoredObject:
        if not source.is_file():
            raise StorageError(f"Result file does not exist: {source}")
        try:
            if self.backend == "local":
                target = self.root / key
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            else:
                self._client.upload_file(str(source), self.bucket, key)
            return StoredObject(key, source.stat().st_size)
        except Exception as exc:
            raise StorageError(f"Could not upload result object {key}") from exc

    def delete(self, key: str) -> None:
        try:
            if self.backend == "local":
                target = self.root / key
                target.unlink(missing_ok=True)
            else:
                self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"Could not delete result object {key}") from exc

    def signed_url(self, key: str, expires: int | None = None) -> str:
        ttl = self.signed_url_seconds if expires is None else min(expires, self.signed_url_seconds)
        if ttl <= 0:
            raise ValueError("Signed URL expiry must be positive")
        try:
            if self.backend == "local":
                return f"/api/storage/{key}?{urlencode({'expires': ttl})}"
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=ttl,
            )
        except Exception as exc:
            raise StorageError(f"Could not create a signed URL for {key}") from exc

    def presigned_post(self, key: str, content_type: str, max_bytes: int) -> dict:
        if self.backend == "local":
            raise StorageError("Presigned POST requires object storage")
        try:
            return self._client.generate_presigned_post(
                self.bucket,
                key,
                Fields={"Content-Type": content_type},
                Conditions=[
                    {"Content-Type": content_type},
                    ["content-length-range", 1, max_bytes],
                ],
                ExpiresIn=self.signed_url_seconds,
            )
        except Exception as exc:
            raise StorageError(f"Could not create an upload form for {key}") from exc

    def head(self, key: str) -> dict:
        if self.backend == "local":
            path = self.root / key
            if not path.is_file():
                raise StorageError(f"Source object does not exist: {key}")
            return {"ContentLength": path.stat().st_size}
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"Could not inspect source object {key}") from exc

    def download(self, key: str, target: Path) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if self.backend == "local":
                shutil.copyfile(self.root / key, target)
            else:
                self._client.download_file(self.bucket, key, str(target))
        except Exception as exc:
            raise StorageError(f"Could not download source object {key}") from exc

    def signed_url_until(
        self,
        key: str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> str:
        """Sign only for the remaining job lifetime, capped at one hour."""
        current = now or datetime.now(UTC)
        remaining = (expires_at - current).total_seconds()
        if remaining < 1:
            raise StorageError("Result has expired and cannot be downloaded")
        return self.signed_url(key, min(int(remaining), 3600))
