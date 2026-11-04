import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import aiosqlite

from database import Database


logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s - %(name)s - %(message)s",
)
logger = logging.getLogger("safe_database_tests")


async def checkpoint_database(db_path: Path) -> None:
    """Сбрасывает WAL перед файловыми операциями."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


async def check_integrity(db_path: Path) -> None:
    """Проверяет целостность SQLite-файла."""
    async with aiosqlite.connect(str(db_path)) as db:
        async with db.execute("PRAGMA integrity_check") as cursor:
            rows = await cursor.fetchall()

    if not rows or rows[0][0] != "ok":
        raise RuntimeError(f"Проверка целостности не пройдена: {rows}")


async def restore_into_test_database(
    backup_path: Path,
    destination_path: Path,
) -> None:
    """
    Восстанавливает резервную копию только в переданный тестовый путь.
    Не использует config.DB_PATH и не вызывает рабочий restore.py.
    """
    if not backup_path.is_file():
        raise FileNotFoundError(f"Резервная копия не найдена: {backup_path}")

    temporary_restore = destination_path.with_name(
        destination_path.name + ".restore_tmp"
    )

    try:
        async with aiosqlite.connect(str(backup_path)) as source:
            async with aiosqlite.connect(str(temporary_restore)) as target:
                await source.backup(target)

        await check_integrity(temporary_restore)

        # Удаляем только sidecar-файлы тестовой БД.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(destination_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        os.replace(temporary_restore, destination_path)

    finally:
        if temporary_restore.exists():
            temporary_restore.unlink()


async def test_backup_and_restore(test_db: Database, db_path: Path) -> None:
    logger.info("ТЕСТ 1: резервное копирование и восстановление")

    marker_id = -910001

    async with test_db.transaction() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO users (id, username, first_name)
            VALUES (?, ?, ?)
            """,
            (marker_id, "safe_restore_test", "Тест восстановления"),
        )

    await checkpoint_database(db_path)

    backup_path_raw = await test_db.backup()
    if not backup_path_raw:
        raise RuntimeError("Метод backup() не вернул путь к резервной копии.")

    backup_path = Path(backup_path_raw)
    if not backup_path.is_file():
        raise RuntimeError("Файл резервной копии не был создан.")

    # Закрываем все соединения до имитации повреждения файла.
    await checkpoint_database(db_path)

    # Удаляем только WAL/SHM временной БД, не рабочей базы проекта.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    # Имитируем повреждение исключительно временной тестовой БД.
    db_path.write_bytes(b"CORRUPTED TEST DATABASE")

    await restore_into_test_database(backup_path, db_path)
    await check_integrity(db_path)

    async with aiosqlite.connect(str(db_path)) as db:
        async with db.execute(
            "SELECT username FROM users WHERE id = ?",
            (marker_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None or row[0] != "safe_restore_test":
        raise AssertionError("После восстановления тестовая запись не найдена.")

    logger.info("ТЕСТ 1 ПРОЙДЕН: тестовая БД восстановлена из копии.")


async def test_cancelled_transaction(test_db: Database) -> None:
    logger.info("ТЕСТ 2: отмена транзакции")

    marker_id = -910002

    async def transaction_that_will_be_cancelled() -> None:
        async with test_db.transaction() as db:
            await db.execute(
                "INSERT INTO users (id, username) VALUES (?, ?)",
                (marker_id, "cancelled_transaction_test"),
            )
            await asyncio.sleep(30)

    task = asyncio.create_task(transaction_that_will_be_cancelled())

    try:
        await asyncio.sleep(0.3)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Задача неожиданно не была отменена.")

    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async with test_db.connect() as db:
        async with db.execute(
            "SELECT 1 FROM users WHERE id = ?",
            (marker_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if row is not None:
        raise AssertionError(
            "Тестовая запись сохранилась: rollback после отмены не сработал."
        )

    logger.info("ТЕСТ 2 ПРОЙДЕН: отменённая транзакция откатилась.")


async def main() -> int:
    logger.info("Запуск безопасных тестов на отдельной временной БД.")

    with tempfile.TemporaryDirectory(prefix="bot_db_tests_") as temp_dir:
        root = Path(temp_dir)
        test_db_path = root / "isolated_test.db"
        test_backup_dir = root / "backups"

        # Создаём отдельный экземпляр Database с временным путём.
        test_db = Database(
            path=str(test_db_path),
            backup_dir=str(test_backup_dir),
        )

        try:
            await test_db.init()
            await test_backup_and_restore(test_db, test_db_path)
            await test_cancelled_transaction(test_db)

            logger.info("ВСЕ БЕЗОПАСНЫЕ ТЕСТЫ ПРОЙДЕНЫ.")
            return 0

        except Exception:
            logger.exception("ТЕСТИРОВАНИЕ ЗАВЕРШИЛОСЬ ОШИБКОЙ.")
            return 1

        # TemporaryDirectory автоматически удалит тестовую БД и её копии.


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
