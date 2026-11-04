import asyncio
import html
import logging
import traceback

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

from config import ADMIN_IDS, BOT_TOKEN, ensure_directories
from database import db_manager
from handlers import (
    admin,
    catalog,
    client,
    requests,
    support,
)
from middlewares import AntiFlood, BanMiddleware
from storage import SingleProcessEventIsolation, SQLiteStorage
from utils.logging_config import setup_logging

ensure_directories()
setup_logging()

logger = logging.getLogger(__name__)


def _redact_secret(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value


# ============================================================
# BOT / DISPATCHER
# ============================================================

bot = Bot(token=BOT_TOKEN)
event_isolation = SingleProcessEventIsolation()

dp = Dispatcher(
    storage=SQLiteStorage(),
    events_isolation=event_isolation,
)


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================


@dp.errors()
async def error_handler(event: ErrorEvent) -> bool:
    exc = event.exception

    error_details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error(
        "Необработанная ошибка Telegram-бота:\n%s",
        _redact_secret(error_details, BOT_TOKEN),
    )

    error_text = html.escape(_redact_secret(str(exc), BOT_TOKEN)[:2500])

    notification = "🚨 <b>Ошибка бота</b>\n\n" f"<code>{error_text}</code>"

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                notification,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Не удалось отправить уведомление админу %s",
                admin_id,
            )

    return True


# ============================================================
# MIDDLEWARE
# ============================================================


def setup_middlewares() -> None:
    dp.message.middleware(BanMiddleware())
    dp.callback_query.middleware(BanMiddleware())

    dp.message.middleware(AntiFlood(limit=0.7))
    dp.callback_query.middleware(AntiFlood(limit=0.3))


# ============================================================
# ROUTERS
# ============================================================


def setup_routers() -> None:
    dp.include_router(admin.router)
    dp.include_router(support.router)
    dp.include_router(requests.router)
    dp.include_router(catalog.router)
    dp.include_router(client.router)

    logger.info("Все роутеры успешно подключены.")


# ============================================================
# MAIN
# ============================================================


async def main() -> None:
    logger.info("Инициализация бота...")

    try:
        # Инициализация базы данных.
        await db_manager.init()
        logger.info("База данных инициализирована.")

        # Подключение middleware.
        setup_middlewares()

        # Подключение обработчиков.
        setup_routers()

        me = await bot.get_me()
        logger.info("Бот авторизован: @%s (ID: %s)", me.username, me.id)
        logger.info("Бот запущен и ожидает обновления.")

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    except asyncio.CancelledError:
        logger.info("Получена команда остановки.")
        raise

    except Exception:
        logger.exception("Критическая ошибка при работе бота.")
        raise

    finally:
        logger.info("Завершение работы бота.")
        try:
            await dp.storage.close()
        finally:
            try:
                await event_isolation.close()
            finally:
                await bot.session.close()
        logger.info("Хранилище FSM и сессия Telegram закрыты.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")
