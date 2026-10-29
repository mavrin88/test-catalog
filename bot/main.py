import asyncio
import logging
import os
from dataclasses import dataclass
from io import BytesIO

import asyncpg
import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

log = logging.getLogger(__name__)

# У Telegram один потребитель getUpdates на бота. Лок лежит в Postgres:
# вторая реплика не стартует polling, а забирает его, когда лидер умирает.
LOCK_KEY = 58203411
CATALOG_TEXT = "У меня есть товар"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    api_url: str
    internal_token: str
    webapp_url: str
    admin_ids: frozenset[int]


def load_settings() -> Settings:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is empty")
    webapp_url = os.environ.get("WEBAPP_URL", "").strip()
    if not webapp_url:
        raise RuntimeError("WEBAPP_URL is empty")
    admin_ids: set[int] = set()
    for part in os.environ.get("ADMIN_IDS", "").split(","):
        part = part.strip()
        if part:
            admin_ids.add(int(part))
    return Settings(
        bot_token=token,
        database_url=os.environ["DATABASE_URL"].strip(),
        api_url=os.environ.get("API_URL", "http://backend:8000").rstrip("/"),
        internal_token=os.environ["INTERNAL_TOKEN"].strip(),
        webapp_url=webapp_url,
        admin_ids=frozenset(admin_ids),
    )


def catalog_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Каталог", web_app=WebAppInfo(url=url))]
        ]
    )


class AdminBook:
    def __init__(self, dsn: str, fixed: frozenset[int]) -> None:
        self.dsn = dsn
        self.fixed = fixed
        self.ids = set(fixed)
        self.pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=2)
        await self.pool.execute(
            "CREATE TABLE IF NOT EXISTS bot_admins (user_id bigint PRIMARY KEY)"
        )
        if self.fixed:
            return
        rows = await self.pool.fetch("SELECT user_id FROM bot_admins")
        self.ids = {row["user_id"] for row in rows}

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def has(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.ids

    async def claim_first(self, user_id: int) -> bool:
        if self.fixed or user_id in self.ids or self.pool is None:
            return False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("LOCK TABLE bot_admins IN EXCLUSIVE MODE")
                taken = await conn.fetchval("SELECT user_id FROM bot_admins LIMIT 1")
                if taken is not None:
                    self.ids.add(taken)
                    return False
                await conn.execute(
                    "INSERT INTO bot_admins (user_id) VALUES ($1)",
                    user_id,
                )
        self.ids.add(user_id)
        log.info("admin is %s", user_id)
        return True


def setup_router(settings: Settings, api: httpx.AsyncClient, admins: AdminBook) -> Router:
    router = Router()
    keyboard = catalog_keyboard(settings.webapp_url)
    admin_commands = [
        BotCommand(command="start", description="Каталог"),
        BotCommand(command="create", description="Создать товар"),
    ]

    async def show_catalog(message: Message) -> None:
        await message.answer(CATALOG_TEXT, reply_markup=keyboard)

    async def grant_commands(bot: Bot, user_id: int) -> None:
        try:
            await bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=user_id),
            )
        except Exception:
            log.warning("admin commands were not set for %s", user_id)

    async def remember(message: Message, bot: Bot) -> None:
        user = message.from_user
        if user is None or message.chat.type != "private":
            return
        if await admins.claim_first(user.id):
            await grant_commands(bot, user.id)

    @router.message(CommandStart())
    async def start(message: Message, bot: Bot) -> None:
        await remember(message, bot)
        await show_catalog(message)

    @router.message(Command("create"))
    async def create_cmd(message: Message, bot: Bot) -> None:
        await remember(message, bot)
        user = message.from_user
        if user is None or not admins.has(user.id):
            await show_catalog(message)
            return
        await message.answer(
            "Создать товар: пришлите одним сообщением фотографию и название в подписи."
        )

    @router.message(F.photo)
    async def add_product(message: Message, bot: Bot) -> None:
        await remember(message, bot)
        user = message.from_user
        if user is None or not admins.has(user.id):
            await show_catalog(message)
            return
        name = " ".join((message.caption or "").split())
        if not name:
            await message.answer(
                "Нужны название и фото в одном сообщении. Подпись к фото — это название."
            )
            return
        buf = BytesIO()
        await bot.download(message.photo[-1], destination=buf)
        payload = buf.getvalue()
        if not payload:
            await message.answer("Не удалось прочитать фото.")
            return
        try:
            response = await api.post(
                "/api/products",
                data={"name": name},
                files={"file": ("photo.jpg", payload, "image/jpeg")},
                headers={"X-Internal-Token": settings.internal_token},
            )
        except httpx.HTTPError:
            log.exception("catalog api request failed")
            await message.answer("Сервис каталога сейчас недоступен. Попробуйте ещё раз.")
            return
        if response.status_code >= 400:
            log.warning("create product failed: %s %s", response.status_code, response.text[:300])
            await message.answer("Не получилось сохранить товар.")
            return
        await message.answer(f"Товар «{name}» добавлен.", reply_markup=keyboard)

    @router.message()
    async def other(message: Message, bot: Bot) -> None:
        await remember(message, bot)
        await show_catalog(message)

    return router


async def health_server() -> web.AppRunner:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8081).start()
    return runner


async def try_lock(dsn: str) -> asyncpg.Connection | None:
    try:
        conn = await asyncpg.connect(dsn, server_settings={"application_name": "catalog-bot"})
    except (OSError, asyncpg.PostgresError) as exc:
        log.warning("postgres for lock: %s", exc)
        return None
    locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY)
    if not locked:
        await conn.close()
        return None
    return conn


async def heartbeat(conn: asyncpg.Connection) -> None:
    while True:
        await asyncio.sleep(15)
        await conn.fetchval("SELECT 1")


async def configure_bot(bot: Bot, settings: Settings) -> None:
    webapp = WebAppInfo(url=settings.webapp_url)
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Каталог", web_app=webapp))
    except Exception:
        log.warning("menu button was not set; WEBAPP_URL must be a public https address")
    await bot.set_my_commands([BotCommand(command="start", description="Каталог")])
    admin_commands = [
        BotCommand(command="start", description="Каталог"),
        BotCommand(command="create", description="Создать товар"),
    ]
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            log.warning("admin commands were not set for %s", admin_id)


async def run_bot(settings: Settings) -> None:
    bot = Bot(settings.bot_token)
    api = httpx.AsyncClient(base_url=settings.api_url, timeout=30)
    admins = AdminBook(settings.database_url, settings.admin_ids)
    await admins.open()
    dp = Dispatcher()
    dp.include_router(setup_router(settings, api, admins))
    try:
        await configure_bot(bot, settings)
        await dp.start_polling(bot)
    finally:
        await admins.close()
        await api.aclose()
        await bot.session.close()


async def lead(settings: Settings, conn: asyncpg.Connection) -> None:
    polling = asyncio.create_task(run_bot(settings))
    beat = asyncio.create_task(heartbeat(conn))
    try:
        done, _pending = await asyncio.wait(
            {polling, beat},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.result()
    finally:
        polling.cancel()
        beat.cancel()
        await asyncio.gather(polling, beat, return_exceptions=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not os.environ.get("BOT_TOKEN", "").strip():
        log.error("BOT_TOKEN пустой. Впишите токен в .env и выполните: docker compose up -d --force-recreate bot")
        while True:
            await asyncio.sleep(3600)
    settings = load_settings()
    runner = await health_server()
    try:
        while True:
            conn = await try_lock(settings.database_url)
            if conn is None:
                log.info("polling is held by another instance")
                await asyncio.sleep(5)
                continue
            log.info("polling lock acquired")
            try:
                await lead(settings, conn)
            finally:
                await conn.close()
            await asyncio.sleep(2)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
