import asyncio
import logging
import secrets
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.db import connect
from app.settings import Settings, load_settings
from app.storage import Storage, connect_storage

log = logging.getLogger(__name__)

MAX_IMAGE = 8 * 1024 * 1024
ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class ProductOut(BaseModel):
    id: UUID
    name: str
    image_url: str
    created_at: datetime


def iter_object(response, size: int = 64 * 1024) -> Iterator[bytes]:
    try:
        for chunk in response.stream(size):
            yield chunk
    finally:
        response.close()
        response.release_conn()


def clean_name(raw: str) -> str:
    return " ".join(raw.split())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.db = await connect(settings.database_url)
    storage = Storage(
        settings.s3_endpoint,
        settings.s3_access_key,
        settings.s3_secret_key,
        settings.s3_bucket,
        settings.s3_secure,
    )
    await connect_storage(storage)
    app.state.storage = storage
    try:
        yield
    finally:
        await app.state.db.close()


app = FastAPI(title="catalog", lifespan=lifespan)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def to_out(row) -> ProductOut:
    return ProductOut(
        id=row["id"],
        name=row["name"],
        image_url=f"/api/products/{row['id']}/image",
        created_at=row["created_at"],
    )


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/products", response_model=list[ProductOut])
async def list_products(request: Request) -> list[ProductOut]:
    rows = await request.app.state.db.fetch(
        """
        SELECT id, name, created_at
        FROM products
        ORDER BY created_at DESC
        """
    )
    return [to_out(row) for row in rows]


@app.get("/api/products/{product_id}/image")
async def product_image(product_id: UUID, request: Request):
    row = await request.app.state.db.fetchrow(
        "SELECT image_key, content_type FROM products WHERE id = $1",
        product_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        obj = await asyncio.to_thread(request.app.state.storage.get, row["image_key"])
    except Exception as exc:
        log.warning("image %s is missing in storage: %s", product_id, exc)
        raise HTTPException(status_code=404, detail="not found") from exc
    return StreamingResponse(
        iter_object(obj),
        media_type=row["content_type"],
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/products", response_model=ProductOut, status_code=201)
async def create_product(
    request: Request,
    name: str = Form(),
    file: UploadFile = File(),
    x_internal_token: str = Header(),
) -> ProductOut:
    settings: Settings = request.app.state.settings
    if not secrets.compare_digest(x_internal_token, settings.internal_token):
        raise HTTPException(status_code=401, detail="unauthorized")

    title = clean_name(name)
    if not title or len(title) > 200:
        raise HTTPException(status_code=400, detail="name is required")

    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    ext = ALLOWED_TYPES.get(content_type)
    if ext is None:
        raise HTTPException(status_code=400, detail="unsupported image type")

    data = await file.read(MAX_IMAGE + 1)
    if not data or len(data) > MAX_IMAGE:
        raise HTTPException(status_code=400, detail="bad image size")

    product_id = uuid.uuid4()
    key = f"{product_id}.{ext}"
    storage: Storage = request.app.state.storage
    await asyncio.to_thread(storage.put, key, data, content_type)
    try:
        row = await request.app.state.db.fetchrow(
            """
            INSERT INTO products (id, name, image_key, content_type)
            VALUES ($1, $2, $3, $4)
            RETURNING id, name, created_at
            """,
            product_id,
            title,
            key,
            content_type,
        )
    except Exception:
        log.exception("product insert failed, removing %s", key)
        await asyncio.to_thread(storage.remove, key)
        raise
    return to_out(row)
