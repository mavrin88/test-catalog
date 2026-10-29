import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_secure: bool
    internal_token: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is empty")
    return value


def load_settings() -> Settings:
    endpoint = os.environ.get("S3_ENDPOINT", "minio:9000").strip()
    secure = endpoint.startswith("https://")
    endpoint = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    return Settings(
        database_url=_required("DATABASE_URL"),
        s3_endpoint=endpoint,
        s3_access_key=_required("S3_ACCESS_KEY"),
        s3_secret_key=_required("S3_SECRET_KEY"),
        s3_bucket=os.environ.get("S3_BUCKET", "products").strip() or "products",
        s3_secure=secure,
        internal_token=_required("INTERNAL_TOKEN"),
    )
