import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from database import CURRENT_SCHEMA_VERSION, Database
from handlers import admin, catalog
from keyboards import catalog_kb
from services import anketa as anketa_service


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_catalog_keyboard_accepts_sqlite_row_and_dict():
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT 1 AS id, '0123' AS code, 'Анна' AS name, "
            "28 AS age, 5000 AS price_1h"
        ) as cursor:
            row = await cursor.fetchone()

    assert not hasattr(row, "get")
    row_markup = catalog_kb([row], 0, 1)
    dict_markup = catalog_kb(
        [{"id": 1, "code": "0123", "name": "Анна", "age": 28, "price_1h": 5000}],
        0,
        1,
    )
    assert "[0123] Анна" in _buttons(row_markup)[0].text
    assert _buttons(row_markup)[0].text == _buttons(dict_markup)[0].text


@pytest.mark.parametrize(
    "record",
    [
        {"id": 1, "code": "12", "name": "Анна", "age": 28, "price_1h": 1},
        {"id": 1, "code": None, "name": "Анна", "age": 28, "price_1h": 1},
        {"id": 0, "code": "1234", "name": "Анна", "age": 28, "price_1h": 1},
        {"id": 1, "code": "١٢٣٤", "name": "Анна", "age": 28, "price_1h": 1},
    ],
)
def test_catalog_keyboard_rejects_invalid_required_fields(record):
    with pytest.raises(ValueError):
        catalog_kb([record], 0, 1)


def test_catalog_keyboard_handles_empty_and_many_records_and_sort_toggle():
    assert _buttons(catalog_kb([], 0, 1))
    records = [
        {
            "id": index + 1,
            "code": f"{index:04d}",
            "name": f"Анкета {index}",
            "age": 20,
            "price_1h": 5000 + index,
        }
        for index in range(20)
    ]
    ascending = _buttons(catalog_kb(records[:5], 0, 4, "price_asc"))
    descending = _buttons(catalog_kb(records[:5], 0, 4, "price_desc"))
    assert any(button.text == "💰 По цене ↑" and "price_desc" in button.callback_data for button in ascending)
    assert any(button.text == "💰 По цене ↓" and "price_asc" in button.callback_data for button in descending)
    assert any(button.callback_data == "catalog:1:price_asc:0" for button in ascending)


@pytest.mark.asyncio
async def test_catalog_query_sort_and_pagination_use_requested_sql_order(tmp_path, monkeypatch):
    database = Database(tmp_path / "catalog.db", backup_dir=tmp_path / "backups")
    await database.init()
    monkeypatch.setattr(catalog, "db_manager", database)
    async with database.transaction() as db:
        await db.execute("INSERT INTO users (id, city) VALUES (10, 'Город')")
        await db.executemany(
            "INSERT INTO anketas (code, name, age, city, price_1h) "
            "VALUES (?, ?, 25, 'Город', ?)",
            [
                (f"{index:04d}", f"A{index}", 100 + (19 - index))
                for index in range(20)
            ],
        )

    first_page, _, total = await catalog._get_catalog(10, 0, "price_asc", 0)
    second_page, _, _ = await catalog._get_catalog(10, 1, "price_asc", 0)
    assert total == 20
    assert len(first_page) == catalog.PAGE_SIZE
    assert first_page[0]["price_1h"] < first_page[-1]["price_1h"]
    assert first_page[-1]["price_1h"] < second_page[0]["price_1h"]


@pytest.mark.asyncio
async def test_create_anketa_generates_unique_code_and_can_be_found(tmp_path, monkeypatch):
    database = Database(tmp_path / "anketas.db", backup_dir=tmp_path / "backups")
    await database.init()
    monkeypatch.setattr(anketa_service, "db_manager", database)
    monkeypatch.setattr(anketa_service.secrets, "randbelow", lambda _: 0)
    async with database.transaction() as db:
        await db.execute(
            "INSERT INTO anketas (code, name, age, city) VALUES ('0000', 'Existing', 22, 'City')"
        )

    anketa_id = await anketa_service.create_anketa("New", 23, "City")
    row = await anketa_service.get_anketa_by_id(anketa_id)
    assert row["code"] == "0001"
    assert (await anketa_service.get_anketa_by_code("0001"))["id"] == anketa_id
    assert "[0001] New" in _buttons(
        catalog_kb([dict(row)], 0, 1)
    )[0].text


@pytest.mark.asyncio
async def test_create_anketa_exhausts_codes_without_unbounded_retry(tmp_path, monkeypatch):
    database = Database(tmp_path / "full-codes.db", backup_dir=tmp_path / "backups")
    await database.init()
    monkeypatch.setattr(anketa_service, "db_manager", database)
    async with database.transaction() as db:
        await db.executemany(
            "INSERT INTO anketas (code, name, age, city) VALUES (?, ?, 20, 'City')",
            [(f"{number:04d}", f"Anketa {number}") for number in range(10_000)],
        )

    with pytest.raises(ValueError, match="Исчерпаны все доступные"):
        await anketa_service.create_anketa("Out", 24, "City")


@pytest.mark.asyncio
async def test_admin_anketa_creation_uses_service(monkeypatch):
    create = AsyncMock(return_value=42)
    render = AsyncMock()
    monkeypatch.setattr(admin, "create_anketa", create)
    monkeypatch.setattr(admin, "_render_admin_services", render)
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "name": "Анна",
                "age": 25,
                "city": "Город",
                "bio": "Описание",
                "price_1h": 1000,
                "price_3h": 2000,
                "price_night": 3000,
            }
        ),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
    )
    message = SimpleNamespace(photo=[SimpleNamespace(file_id="photo")])

    await admin.add_photo(message, state)

    create.assert_awaited_once()
    assert create.await_args.kwargs["photo_id"] == "photo"
    assert "code" not in create.await_args.kwargs
    render.assert_awaited_once_with(message, state)


@pytest.mark.asyncio
async def test_anketa_migration_fills_null_code_and_preserves_existing_code(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "legacy-anketas.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE anketas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                name TEXT,
                age INTEGER,
                city TEXT,
                bio TEXT,
                price_1h INTEGER,
                price_3h INTEGER,
                price_night INTEGER,
                outcall INTEGER,
                photo_id TEXT
            )
            """
        )
        db.executemany(
            "INSERT INTO anketas (id, code, name, age, city) VALUES (?, ?, ?, 20, 'City')",
            [(1, "0000", "Existing"), (2, None, "Missing")],
        )
        db.execute("PRAGMA user_version=11")

    database = Database(path, backup_dir=tmp_path / "backups")
    await database.init()

    async with database.connect() as db:
        async with db.execute("SELECT id, code, name FROM anketas ORDER BY id") as cur:
            rows = await cur.fetchall()
        async with db.execute("PRAGMA user_version") as cur:
            version = (await cur.fetchone())[0]
    assert rows[0]["code"] == "0000"
    assert len(rows[1]["code"]) == 4 and rows[1]["code"].isascii() and rows[1]["code"].isdigit()
    assert rows[1]["code"] != rows[0]["code"]
    assert [row["name"] for row in rows] == ["Existing", "Missing"]
    assert version == CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_failed_anketa_migration_rolls_back_without_changing_user_version(tmp_path):
    path = tmp_path / "exhausted-legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE anketas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                name TEXT,
                age INTEGER,
                city TEXT,
                bio TEXT,
                price_1h INTEGER,
                price_3h INTEGER,
                price_night INTEGER,
                outcall INTEGER,
                photo_id TEXT
            )
            """
        )
        db.executemany(
            "INSERT INTO anketas (code, name, age, city) VALUES (?, ?, 20, 'City')",
            [(f"{number:04d}", f"Anketa {number}") for number in range(10_000)],
        )
        db.execute(
            "INSERT INTO anketas (code, name, age, city) VALUES (NULL, 'Missing', 20, 'City')"
        )
        db.execute("PRAGMA user_version=11")

    database = Database(path, backup_dir=tmp_path / "backups")
    with pytest.raises(RuntimeError, match="исчерпан диапазон 0000–9999"):
        await database.init()

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 11
        assert db.execute(
            "SELECT COUNT(*) FROM anketas WHERE code IS NULL AND name='Missing'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_stale_service_button_cannot_change_other_anketa(tmp_path, monkeypatch):
    database = Database(tmp_path / "stale-service.db", backup_dir=tmp_path / "backups")
    await database.init()
    monkeypatch.setattr(admin, "db_manager", database)
    async with database.transaction() as db:
        await db.execute(
            "INSERT INTO anketas (code, name, age, city) VALUES ('1234', 'One', 20, 'City')"
        )
        await db.execute(
            "INSERT INTO anketas (code, name, age, city) VALUES ('1235', 'Two', 20, 'City')"
        )
        await db.execute(
            "INSERT INTO services (name, default_price) VALUES ('Service', 500)"
        )

    selected = [1]
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "anketa_id": 2,
                "selected_services": selected,
                "services_message_id": 202,
            }
        ),
        get_state=AsyncMock(return_value=admin.AddAnketa.services.state),
        update_data=AsyncMock(),
    )
    callback = SimpleNamespace(
        data="admin_srv_tgl:1:1",
        message=SimpleNamespace(message_id=101, edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )
    await admin.toggle_service(callback, state)

    state.update_data.assert_not_awaited()
    callback.message.edit_reply_markup.assert_not_awaited()
    assert selected == [1]
