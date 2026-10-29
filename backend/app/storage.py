import asyncio
import io
import logging

from minio import Minio
from minio.error import S3Error

log = logging.getLogger(__name__)


class Storage:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
    ) -> None:
        self.bucket = bucket
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

    def ensure_bucket(self) -> None:
        try:
            if self.client.bucket_exists(self.bucket):
                return
            self.client.make_bucket(self.bucket)
        except S3Error as exc:
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    def get(self, key: str):
        return self.client.get_object(self.bucket, key)

    def remove(self, key: str) -> None:
        self.client.remove_object(self.bucket, key)


async def connect_storage(storage: Storage) -> None:
    delay = 0.4
    last_error: Exception | None = None
    for _ in range(25):
        try:
            await asyncio.to_thread(storage.ensure_bucket)
            return
        except Exception as exc:
            last_error = exc
            log.warning("minio not ready: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 1.7, 4)
    raise RuntimeError("minio unavailable") from last_error
