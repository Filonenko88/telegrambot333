import sqlite3

import pytest

from database import CURRENT_SCHEMA_VERSION, Database
from services import requests as request_service


@pytest.mark.asyncio
async def test_database_initialization_is_repeatable(tmp_path):
    database = Database(
        tmp_path / "fresh.db",
        backup_dir=tmp_path / "backups",
    )

    await database.init()
    await database.init()

    async with database.connect() as db:
        async with db.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == CURRENT_SCHEMA_VERSION
        async with db.execute("PRAGMA table_info(promocodes)") as cursor:
            columns = {row["name"]: row for row in await cursor.fetchall()}
        assert str(columns["discount_percent"]["dflt_value"]) == "10"


@pytest.mark.asyncio
async def test_database_upgrades_v10_promocodes_without_losing_rows(tmp_path):
    database = Database(
        tmp_path / "legacy-v10.db",
        backup_dir=tmp_path / "backups",
    )
    await database.init()

    async with database.connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            CREATE TABLE promocodes_legacy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                discount_percent INTEGER NOT NULL
                    CHECK (discount_percent > 0 AND discount_percent <= 100),
                is_active INTEGER DEFAULT 1
            )
            """
        )
        await db.execute(
            """
            INSERT INTO promocodes_legacy (id, code, discount_percent, is_active)
            VALUES (1, 'KEEP10', 10, 1)
            """
        )
        await db.execute("DROP TABLE promocodes")
        await db.execute("ALTER TABLE promocodes_legacy RENAME TO promocodes")
        await db.execute("PRAGMA user_version = 10")
        await db.execute("COMMIT")

    await database.init()

    async with database.connect() as db:
        async with db.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == CURRENT_SCHEMA_VERSION
        async with db.execute(
            "SELECT id, code, discount_percent, is_active FROM promocodes WHERE id = 1"
        ) as cursor:
            assert tuple(await cursor.fetchone()) == (1, "KEEP10", 10, 1)


@pytest.mark.asyncio
async def test_in_memory_database_keeps_single_connection_state():
    database = Database(":memory:", backup_dir=None)
    await database.init()

    async with database.connect() as first:
        await first.execute("CREATE TABLE IF NOT EXISTS temp_memory (value TEXT)")
        await first.execute("INSERT INTO temp_memory (value) VALUES ('ok')")
        first_conn_id = id(first)

    async with database.connect() as second:
        async with second.execute("SELECT value FROM temp_memory") as cursor:
            row = await cursor.fetchone()
        assert row[0] == "ok"
        assert id(second) == first_conn_id


@pytest.mark.asyncio
async def test_request_creation_rolls_back_if_service_relation_fails(
    tmp_path,
    monkeypatch,
):
    database = Database(
        tmp_path / "request-rollback.db",
        backup_dir=tmp_path / "backups",
    )
    await database.init()
    monkeypatch.setattr(request_service, "db_manager", database)

    with pytest.raises(sqlite3.IntegrityError):
        await request_service.create_request_idempotent(
            user_id=123,
            anketa_id=1,
            service="1 час + услуга",
            amount=1500,
            payment_method="Карта",
            session_token="0123456789abcdef",
            selected_services=[{"id": 999999, "price": 500}],
        )

    async with database.connect() as db:
        async with db.execute("SELECT COUNT(*) FROM requests") as cursor:
            assert (await cursor.fetchone())[0] == 0
