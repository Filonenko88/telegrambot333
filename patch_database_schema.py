import asyncio
import logging

from database import db_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    await db_manager.init()
    logger.info("Схема базы данных проверена и обновлена.")


if __name__ == "__main__":
    asyncio.run(main())
