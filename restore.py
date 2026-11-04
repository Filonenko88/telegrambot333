import asyncio
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path

import aiosqlite

from config import BACKUPS_DIR, DB_PATH
from database import CURRENT_SCHEMA_VERSION, Database

logger = logging.getLogger(__name__)


async def _copy_database(source_path: Path, destination_path: Path) -> None:
    async with aiosqlite.connect(str(source_path)) as source:
        async with aiosqlite.connect(str(destination_path)) as destination:
            await source.backup(destination)


async def _validate_database(path: Path) -> None:
    database = Database(path, backup_dir=path.parent / "backups")
    async with database.connect() as db:
        async with db.execute("PRAGMA integrity_check") as cursor:
            integrity = await cursor.fetchall()
        if not integrity or any(row[0] != "ok" for row in integrity):
            raise RuntimeError(f"Проверка integrity_check не пройдена: {integrity!r}")

        async with db.execute("PRAGMA foreign_key_check") as cursor:
            foreign_key_errors = await cursor.fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"В резервной копии найдены нарушения внешних ключей: " f"{foreign_key_errors!r}"
            )

        async with db.execute("PRAGMA user_version") as cursor:
            version_row = await cursor.fetchone()
        version = int(version_row[0]) if version_row else 0
        if version != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"Несовместимая версия схемы {version}; " f"ожидалась {CURRENT_SCHEMA_VERSION}."
            )

        await database._validate_schema(db)


async def _checkpoint_and_close(path: Path) -> None:
    if not path.exists():
        return

    async with aiosqlite.connect(str(path), timeout=1.0) as db:
        async with db.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
            checkpoint = await cursor.fetchone()
        if checkpoint and checkpoint[0] != 0:
            raise RuntimeError(
                "Не удалось безопасно завершить WAL checkpoint; "
                "убедитесь, что бот и другие процессы закрыли базу."
            )

        async with db.execute("PRAGMA journal_mode=DELETE") as cursor:
            journal_mode = await cursor.fetchone()
        if not journal_mode or str(journal_mode[0]).lower() != "delete":
            raise RuntimeError(
                "Не удалось переключить базу в режим DELETE; "
                "убедитесь, что бот и другие процессы закрыли базу."
            )

    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            raise RuntimeError(
                f"После checkpoint остался SQLite sidecar {sidecar}; "
                "замена базы небезопасна."
            )


def _remove_temporary_database(path: Path) -> None:
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.exception("Не удалось удалить временный файл %s", candidate)


async def safe_restore(
    backup_file: str | Path,
    destination: str | Path = DB_PATH,
    backup_dir: str | Path = BACKUPS_DIR,
) -> Path:
    """Restore a validated backup. Stop the bot before replacing its database."""
    source_path = Path(backup_file).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Резервная копия не найдена: {source_path}")
    if source_path == destination_path:
        raise ValueError("Исходный файл резервной копии совпадает с рабочей базой.")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    restore_path = destination_path.with_name(
        f"{destination_path.name}.restore-{uuid.uuid4().hex}.tmp"
    )
    rollback_backup: str | None = None

    try:
        await _copy_database(source_path, restore_path)
        with tempfile.TemporaryDirectory(
            prefix=".restore-migration-backups-",
            dir=destination_path.parent,
        ) as migration_backups:
            restored_database = Database(
                restore_path,
                backup_dir=migration_backups,
            )
            await restored_database.init()
        await _validate_database(restore_path)

        if destination_path.exists():
            current_database = Database(
                destination_path,
                backup_dir=backup_dir,
            )
            rollback_backup = await current_database.backup()
            await _checkpoint_and_close(destination_path)

        os.replace(restore_path, destination_path)
        try:
            await _validate_database(destination_path)
        except Exception as restore_error:
            if rollback_backup is not None:
                rollback_path = destination_path.with_name(
                    f"{destination_path.name}.rollback-{uuid.uuid4().hex}.tmp"
                )
                try:
                    try:
                        await _checkpoint_and_close(destination_path)
                    except Exception as checkpoint_error:
                        logger.exception(
                            "Не удалось безопасно завершить восстановленную БД. "
                            "Автоматический откат остановлен; сохранена предыдущая "
                            "копия %s.",
                            rollback_backup,
                        )
                        restore_error.add_note(
                            "Автоматический откат остановлен из-за ошибки checkpoint "
                            f"({checkpoint_error}); предыдущая база сохранена в "
                            f"{rollback_backup}. Требуется ручное вмешательство."
                        )
                    else:
                        try:
                            await _copy_database(Path(rollback_backup), rollback_path)
                            os.replace(rollback_path, destination_path)
                        except Exception as rollback_error:
                            logger.exception(
                                "Не удалось автоматически восстановить предыдущую БД "
                                "из %s.",
                                rollback_backup,
                            )
                            restore_error.add_note(
                                "Автоматический откат также завершился ошибкой "
                                f"({rollback_error}); предыдущая база сохранена в "
                                f"{rollback_backup}. Требуется ручное вмешательство."
                            )
                finally:
                    _remove_temporary_database(rollback_path)
            raise restore_error

        logger.info("База данных восстановлена из %s", source_path)
        return destination_path
    finally:
        _remove_temporary_database(restore_path)


async def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python restore.py <путь_к_резервной_копии.db>")
        return 2

    try:
        await safe_restore(sys.argv[1])
    except Exception:
        logger.exception(
            "Восстановление не выполнено. Рабочая база не должна использоваться "
            "параллельно с restore.py."
        )
        return 1

    print("Восстановление успешно завершено.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(main()))
