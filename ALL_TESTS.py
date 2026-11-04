import asyncio
import logging
import sqlite3
import tempfile
from pathlib import Path

from database import CURRENT_SCHEMA_VERSION, Database

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s - %(name)s - %(message)s",
)
logger = logging.getLogger("all_tests")

EXPECTED_TABLES = {
    "users",
    "anketas",
    "favorites",
    "requests",
    "support_msgs",
    "anketa_photos",
    "anketa_reviews",
    "services",
    "anketa_services",
    "request_services",
    "bot_settings",
    "promocodes",
}


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def read_schema_version(db: Database) -> int:
    async with db.connect() as conn:
        async with conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
    return int(row[0])


async def check_integrity(db: Database) -> None:
    async with db.connect() as conn:
        async with conn.execute("PRAGMA integrity_check") as cursor:
            rows = await cursor.fetchall()

        async with conn.execute("PRAGMA foreign_key_check") as cursor:
            foreign_key_errors = await cursor.fetchall()

    assert_true(
        rows and rows[0][0] == "ok",
        f"SQLite integrity_check вернул неожиданный результат: {rows}",
    )
    assert_true(
        not foreign_key_errors,
        f"Обнаружены нарушения внешних ключей: {foreign_key_errors}",
    )


async def test_fresh_initialization(db: Database) -> None:
    logger.info("ТЕСТ 1: создание новой БД и актуальная схема")

    await db.init()

    version = await read_schema_version(db)
    assert_true(
        version == CURRENT_SCHEMA_VERSION,
        f"Ожидалась версия схемы {CURRENT_SCHEMA_VERSION}, получена {version}",
    )

    async with db.connect() as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master " "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ) as cursor:
            actual_tables = {row[0] for row in await cursor.fetchall()}

    missing_tables = EXPECTED_TABLES - actual_tables
    assert_true(
        not missing_tables,
        f"Не созданы обязательные таблицы: {sorted(missing_tables)}",
    )

    await check_integrity(db)
    logger.info("ТЕСТ 1 ПРОЙДЕН")


async def test_default_data(db: Database) -> None:
    logger.info("ТЕСТ 2: начальные услуги и настройки")

    async with db.connect() as conn:
        async with conn.execute("SELECT COUNT(*) FROM services") as cursor:
            services_count = (await cursor.fetchone())[0]

        async with conn.execute("SELECT COUNT(*) FROM bot_settings") as cursor:
            settings_count = (await cursor.fetchone())[0]

    assert_true(services_count > 0, "Таблица services не заполнена.")
    assert_true(settings_count > 0, "Таблица bot_settings не заполнена.")

    logger.info(
        "ТЕСТ 2 ПРОЙДЕН: услуг=%s, настроек=%s",
        services_count,
        settings_count,
    )


async def test_transaction_rollback(db: Database) -> None:
    logger.info("ТЕСТ 3: откат транзакции после ошибки")

    test_user_id = -987654321

    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO users (id, username, first_name) " "VALUES (?, ?, ?)",
                (test_user_id, "rollback_test", "Rollback test"),
            )
            raise RuntimeError("Преднамеренная ошибка для проверки rollback")

    except RuntimeError as exc:
        if "Преднамеренная ошибка" not in str(exc):
            raise

    async with db.connect() as conn:
        async with conn.execute(
            "SELECT 1 FROM users WHERE id = ?",
            (test_user_id,),
        ) as cursor:
            row = await cursor.fetchone()

    assert_true(row is None, "Запись сохранилась после отката транзакции.")
    logger.info("ТЕСТ 3 ПРОЙДЕН")


async def test_constraints_and_relations(db: Database) -> None:
    logger.info("ТЕСТ 4: ограничения, избранное и каскадное удаление")

    test_anketa_id = -987650001
    test_user_id = -987650002

    async with db.transaction() as conn:
        await conn.execute(
            """
            INSERT INTO anketas (
                id, code, name, age, city, bio,
                price_1h, price_3h, price_night, outcall
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                test_anketa_id,
                "9876",
                "Тестовая анкета",
                25,
                "Тестовый город",
                "Тестовая запись, будет удалена автоматически",
                8000,
                17000,
                50000,
                1,
            ),
        )

        await conn.execute(
            "INSERT INTO favorites (user_id, anketa_id) VALUES (?, ?)",
            (test_user_id, test_anketa_id),
        )

    async with db.connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE anketa_id = ?",
            (test_anketa_id,),
        ) as cursor:
            favorites_before = (await cursor.fetchone())[0]

    assert_true(favorites_before == 1, "Не удалось создать тестовую связь favorites.")

    async with db.transaction() as conn:
        await conn.execute(
            "DELETE FROM anketas WHERE id = ?",
            (test_anketa_id,),
        )

    async with db.connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE anketa_id = ?",
            (test_anketa_id,),
        ) as cursor:
            favorites_after = (await cursor.fetchone())[0]

    assert_true(
        favorites_after == 0,
        "Каскадное удаление не удалило связанную запись favorites.",
    )

    # Проверяем, что ограничение возраста действительно действует.
    invalid_insert_rejected = False
    try:
        async with db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO anketas (
                    code, name, age, price_1h, price_3h, price_night
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("9875", "Недопустимая тестовая запись", 17, 8000, 17000, 50000),
            )
    except sqlite3.IntegrityError:
        invalid_insert_rejected = True

    assert_true(
        invalid_insert_rejected,
        "Ограничение CHECK(age >= 18) не сработало.",
    )

    logger.info("ТЕСТ 4 ПРОЙДЕН")


async def test_backup(db: Database, backup_dir: Path) -> None:
    logger.info("ТЕСТ 5: резервная копия тестовой БД")

    backup_path_raw = await db.backup()
    assert_true(backup_path_raw is not None, "Метод backup() не вернул путь.")

    backup_path = Path(backup_path_raw)
    assert_true(backup_path.is_file(), "Файл резервной копии не создан.")
    assert_true(
        backup_path.parent.resolve() == backup_dir.resolve(),
        "Резервная копия создана за пределами временной папки теста.",
    )

    backup_db = Database(
        path=str(backup_path),
        backup_dir=str(backup_dir / "nested_backups"),
    )

    await check_integrity(backup_db)

    backup_version = await read_schema_version(backup_db)
    assert_true(
        backup_version == CURRENT_SCHEMA_VERSION,
        f"В резервной копии версия {backup_version}, "
        f"ожидалась {CURRENT_SCHEMA_VERSION}.",
    )

    logger.info("ТЕСТ 5 ПРОЙДЕН")


async def main() -> int:
    logger.info("Запуск интеграционных тестов SQLite.")
    logger.info("Рабочая БД проекта не используется.")

    with tempfile.TemporaryDirectory(prefix="project_db_tests_") as temp:
        root = Path(temp)
        test_db_path = root / "isolated_test.db"
        backup_dir = root / "backups"

        test_db = Database(
            path=str(test_db_path),
            backup_dir=str(backup_dir),
        )

        tests = [
            ("Инициализация схемы", lambda: test_fresh_initialization(test_db)),
            ("Начальные данные", lambda: test_default_data(test_db)),
            ("Откат транзакции", lambda: test_transaction_rollback(test_db)),
            ("Ограничения и связи", lambda: test_constraints_and_relations(test_db)),
            ("Резервная копия", lambda: test_backup(test_db, backup_dir)),
        ]

        failed = []

        for name, test in tests:
            try:
                await test()
            except Exception as exc:
                failed.append((name, exc))
                logger.exception("ТЕСТ НЕ ПРОЙДЕН: %s", name)
                break

        if failed:
            logger.error("ПРОВЕРКА НЕ ПРОЙДЕНА.")
            for name, exc in failed:
                logger.error("%s: %s: %s", name, type(exc).__name__, exc)
            return 1

        logger.info("ВСЕ ТЕСТЫ УСПЕШНО ПРОЙДЕНЫ.")
        logger.info("Временная БД и резервная копия будут удалены автоматически.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
