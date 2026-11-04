import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from config import DB_PATH
from database import (
    CURRENT_SCHEMA_VERSION,
    EXPECTED_SCHEMA,
    REQUIRED_FOREIGN_KEYS,
    REQUIRED_INDEXES,
    REQUIRED_UNIQUE_CONSTRAINTS,
    TABLE_DDL,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s - %(name)s - %(message)s",
)
logger = logging.getLogger("database_check")


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def main() -> int:
    db_path = Path(DB_PATH).expanduser().resolve()
    logger.info("Путь к БД: %s", db_path)
    if not db_path.is_file():
        print(f"ОШИБКА: файл базы данных не существует: {db_path}")
        return 1

    wal_path = Path(f"{db_path}-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        print(
            "ОШИБКА: WAL содержит данные; read-only диагностика остановлена, "
            "чтобы не игнорировать незаписанные в основной файл транзакции."
        )
        return 1

    uri = f"{db_path.as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check")]
        foreign_key_errors = list(db.execute("PRAGMA foreign_key_check"))
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

        schema_errors = []
        missing_tables = set(EXPECTED_SCHEMA) - tables
        if missing_tables:
            schema_errors.append(f"Отсутствуют таблицы: {sorted(missing_tables)}")

        for table_name, expected_columns in EXPECTED_SCHEMA.items():
            if table_name not in tables:
                continue
            quoted_table = _quote_identifier(table_name)
            actual_columns = {
                row["name"]: row
                for row in db.execute(f"PRAGMA table_info({quoted_table})")
            }
            missing_columns = set(expected_columns) - set(actual_columns)
            if missing_columns:
                schema_errors.append(
                    f"{table_name}: отсутствуют колонки {sorted(missing_columns)}"
                )
                continue
            for column_name, (expected_type, expected_notnull, _, expected_pk) in (
                expected_columns.items()
            ):
                actual = actual_columns[column_name]
                if (
                    actual["type"].upper() != expected_type.upper()
                    or actual["notnull"] != expected_notnull
                    or actual["pk"] != expected_pk
                ):
                    schema_errors.append(
                        f"{table_name}.{column_name}: структура колонки не соответствует схеме"
                    )

        for index_name, specification in REQUIRED_INDEXES.items():
            row = db.execute(
                "SELECT tbl_name FROM sqlite_master "
                "WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            if row is None or row["tbl_name"] != specification["table"]:
                schema_errors.append(f"Отсутствует индекс {index_name}")
                continue
            index_info = list(
                db.execute(f"PRAGMA index_info({_quote_identifier(index_name)})")
            )
            actual_columns = [item["name"] for item in index_info]
            if actual_columns != specification["cols"]:
                schema_errors.append(f"Неверные колонки индекса {index_name}")

        for table_name, unique_columns in REQUIRED_UNIQUE_CONSTRAINTS.items():
            if table_name not in tables:
                continue
            indexes = db.execute(
                f"PRAGMA index_list({_quote_identifier(table_name)})"
            ).fetchall()
            for expected_column in unique_columns:
                found = False
                for index in indexes:
                    if not index["unique"] or index["partial"]:
                        continue
                    columns = [
                        item["name"]
                        for item in db.execute(
                            f"PRAGMA index_info({_quote_identifier(index['name'])})"
                        )
                    ]
                    if columns == [expected_column]:
                        found = True
                        break
                if not found:
                    schema_errors.append(
                        f"{table_name}.{expected_column}: отсутствует UNIQUE-ограничение"
                    )

        for table_name, expected_keys in REQUIRED_FOREIGN_KEYS.items():
            if table_name not in tables:
                continue
            actual_keys = sorted(
                (
                    row["from"],
                    row["table"],
                    row["to"],
                    row["on_update"],
                    row["on_delete"],
                    row["match"],
                )
                for row in db.execute(
                    f"PRAGMA foreign_key_list({_quote_identifier(table_name)})"
                )
            )
            if actual_keys != sorted(expected_keys):
                schema_errors.append(f"{table_name}: внешние ключи не соответствуют схеме")

        for table_name, ddl in TABLE_DDL.items():
            if table_name not in tables:
                continue
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()["sql"] or ""
            if table_sql.upper().count("CHECK") < ddl.upper().count("CHECK"):
                schema_errors.append(f"{table_name}: отсутствует обязательное CHECK-ограничение")

    print("\n========== РЕЗУЛЬТАТ ПРОВЕРКИ БД ==========")
    print(f"Файл БД: {db_path}")
    print(f"Режим открытия: read-only")
    print(f"Версия схемы: {version} (ожидается {CURRENT_SCHEMA_VERSION})")
    print(f"PRAGMA integrity_check: {integrity}")
    print(f"Ошибки внешних ключей: {len(foreign_key_errors)}")
    print(f"Обязательные таблицы: {len(set(EXPECTED_SCHEMA) & tables)}/{len(EXPECTED_SCHEMA)}")
    for error in schema_errors:
        print(f"ОШИБКА СХЕМЫ: {error}")

    if version != CURRENT_SCHEMA_VERSION:
        print("ОШИБКА: версия схемы не соответствует ожидаемой; миграция не запускалась.")
        return 1
    if integrity != ["ok"] or foreign_key_errors or schema_errors:
        print("ОШИБКА: обнаружены проблемы целостности или схемы.")
        return 1

    print("\nПРОВЕРКА ПРОЙДЕНА: SQLite, схема и ограничения в порядке.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        logger.exception("Проверка БД завершилась ошибкой: %s", exc)
        raise SystemExit(2) from exc
