import asyncio

import pytest

from database import Database
from handlers.requests import _safe_edit, _truncate_request_lines
from services import requests as request_service


@pytest.fixture
async def request_database(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "requests.db"))
    async with database.connect() as db:
        await db.execute(
            """
            CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_id TEXT NOT NULL UNIQUE,
                session_token TEXT UNIQUE,
                user_id INTEGER NOT NULL,
                anketas_id INTEGER,
                service TEXT,
                amount INTEGER,
                payment_method TEXT DEFAULT 'Не выбран',
                status TEXT DEFAULT 'Ожидает реквизиты',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE request_services (
                request_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                price INTEGER NOT NULL,
                PRIMARY KEY (request_id, service_id)
            )
            """
        )
    monkeypatch.setattr(request_service, "db_manager", database)
    return database


@pytest.mark.asyncio
async def test_create_request_is_idempotent(request_database):
    request_data = {
        "user_id": 123,
        "anketa_id": 456,
        "service": "1 час + услуга",
        "amount": 1500,
        "payment_method": "Карта",
        "session_token": "0123456789abcdef",
        "selected_services": [{"id": 7, "price": 500}],
    }

    created = await request_service.create_request_idempotent(**request_data)
    repeated = await request_service.create_request_idempotent(**request_data)

    assert created["created"] is True
    assert repeated["created"] is False
    assert repeated["id"] == created["id"]
    assert repeated["unique_id"] == created["unique_id"]

    async with request_database.connect() as db:
        async with db.execute("SELECT COUNT(*) FROM requests") as cursor:
            assert (await cursor.fetchone())[0] == 1
        async with db.execute(
            "SELECT service_id, price FROM request_services WHERE request_id = ?",
            (created["id"],),
        ) as cursor:
            assert tuple(await cursor.fetchone()) == (7, 500)


@pytest.mark.asyncio
async def test_reusing_request_session_with_different_data_fails(request_database):
    request_data = {
        "user_id": 123,
        "anketa_id": 456,
        "service": "1 час",
        "amount": 1000,
        "payment_method": "Карта",
        "session_token": "0123456789abcdef",
    }
    await request_service.create_request_idempotent(**request_data)

    with pytest.raises(request_service.RequestConflictError):
        await request_service.create_request_idempotent(
            **{**request_data, "amount": 1200}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", True),
        ("anketa_id", True),
        ("amount", True),
        ("selected_services", [{"id": True, "price": 1}]),
        ("selected_services", [{"id": 1, "price": True}]),
    ],
)
async def test_request_validation_rejects_booleans_as_integers(
    request_database,
    field,
    value,
):
    request_data = {
        "user_id": 123,
        "anketa_id": 456,
        "service": "1 час",
        "amount": 1000,
        "payment_method": "Карта",
        "session_token": "0123456789abcdef",
    }

    with pytest.raises(ValueError):
        await request_service.create_request_idempotent(
            **{**request_data, field: value}
        )


@pytest.mark.asyncio
async def test_concurrent_request_creation_is_idempotent(request_database):
    request_data = {
        "user_id": 123,
        "anketa_id": 456,
        "service": "1 час",
        "amount": 1000,
        "payment_method": "Карта",
        "session_token": "0123456789abcdef",
    }

    first, second = await asyncio.gather(
        request_service.create_request_idempotent(**request_data),
        request_service.create_request_idempotent(**request_data),
    )

    assert sorted((first["created"], second["created"])) == [False, True]
    assert first["id"] == second["id"]

    async with request_database.connect() as db:
        async with db.execute("SELECT COUNT(*) FROM requests") as cursor:
            assert (await cursor.fetchone())[0] == 1


def test_request_list_truncates_only_between_complete_html_records():
    lines = ["📋 <b>Ваши заявки:</b>", ""]
    for request_id in range(20):
        lines.extend(
            [
                f"🕐 <b>№{request_id}</b> — 100 ₽",
                f"   Услуга &amp; {request_id}",
                "   Статус: <i>Ожидает реквизиты</i>",
                "",
            ]
        )

    text = _truncate_request_lines(lines, limit=180)

    assert len(text.encode("utf-16-le")) // 2 <= 180
    assert text.endswith("<i>Показана часть списка.</i>")
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<i>") == text.count("</i>")
    assert "Услуга &amp;" in text
    assert "Услуга &amp; 19" not in text


@pytest.mark.asyncio
async def test_safe_edit_ignores_missing_message():
    await _safe_edit(None, "Тест")
