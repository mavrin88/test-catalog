import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

TABLE = """
CREATE TABLE IF NOT EXISTS products (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    image_key text NOT NULL,
    content_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""

INDEX = """
CREATE INDEX IF NOT EXISTS products_created_at_idx ON products (created_at DESC)
"""


async def connect(dsn: str) -> asyncpg.Pool:
    delay = 0.4
    last_error: Exception | None = None
    for _ in range(25):
        try:
            pool = await asyncpg.create_pool(
                dsn,
                min_size=1,
                max_size=4,
                server_settings={"application_name": "catalog-api"},
            )
        except (OSError, asyncpg.PostgresError) as exc:
            last_error = exc
            log.warning("postgres not ready: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 1.7, 4)
            continue
        async with pool.acquire() as conn:
            await conn.execute(TABLE)
            await conn.execute(INDEX)
        return pool
    raise RuntimeError("postgres unavailable") from last_error
