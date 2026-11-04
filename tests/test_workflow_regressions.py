import csv
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database import Database
from handlers import admin, requests as request_handlers
from keyboards import req_payment_kb
from services import requests as request_service


class MemoryState:
    def __init__(self, data, state=None):
        self.data = dict(data)
        self.current_state = state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **updates):
        self.data.update(updates)
        return dict(self.data)

    async def get_state(self):
        return self.current_state

    async def set_state(self, state):
        self.current_state = state.state if hasattr(state, "state") else state

    async def clear(self):
        self.data.clear()
        self.current_state = None


def _keyboards(markup):
    return [button for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_promo_checkout_keeps_context_and_creates_discounted_request(
    tmp_path,
    monkeypatch,
):
    database = Database(tmp_path / "promo-checkout.db", backup_dir=tmp_path / "backups")
    await database.init()
    monkeypatch.setattr(request_handlers, "db_manager", database)
    monkeypatch.setattr(request_service, "db_manager", database)
    async with database.transaction() as db:
        await db.execute(
            "INSERT INTO anketas (code, name, age, city, price_1h) "
            "VALUES ('2345', 'Анна', 25, 'Город', 1000)"
        )
        await db.execute(
            "INSERT INTO promocodes (code, discount_percent, is_active) "
            "VALUES ('SAVE20', 20, 1)"
        )

    session = "0123456789abcdef"
    state = MemoryState(
        {
            "anketa_id": 1,
            "srv_key": "1h",
            "session_token": session,
            "extras": [],
            "promo_context": {
                "anketa_id": 1,
                "srv_key": "1h",
                "session": session,
            },
        },
        request_handlers.PromoFlow.waiting_promo.state,
    )
    message = SimpleNamespace(
        text="SAVE20",
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )
    await request_handlers.req_promo_apply(message, state)

    payment_message = message.answer.await_args.kwargs
    keyboard = _keyboards(payment_message["reply_markup"])
    assert any(":card:" in button.callback_data for button in keyboard)
    assert "800" in message.answer.await_args.args[0]
    assert state.data["anketa_id"] == 1
    assert state.data["srv_key"] == "1h"
    assert state.data["session_token"] == session
    assert state.data["promo_code"] == "SAVE20"
    assert state.data["promo_percent"] == 20
    assert state.current_state is None

    callback = SimpleNamespace(
        data=f"req_pay:1:1h:card:{session}",
        from_user=SimpleNamespace(id=123, username="client", first_name="Client"),
        message=SimpleNamespace(
            edit_caption=AsyncMock(),
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    await request_handlers.req_pay(callback, state, bot)

    async with database.connect() as db:
        async with db.execute(
            "SELECT amount, payment_method FROM requests WHERE user_id = 123"
        ) as cursor:
            request = await cursor.fetchone()
    assert request["amount"] == 800
    assert request["payment_method"] == "Карта"
    admin_notification = bot.send_message.await_args.args[1]
    assert "SAVE20" in admin_notification
    assert "800" in admin_notification


@pytest.mark.asyncio
async def test_cash_requires_completed_previous_meeting(tmp_path, monkeypatch):
    database = Database(tmp_path / "cash-history.db", backup_dir=tmp_path / "backups")
    await database.init()
    monkeypatch.setattr(request_handlers, "db_manager", database)
    async with database.transaction() as db:
        await db.execute(
            "INSERT INTO requests (unique_id, user_id, status) "
            "VALUES ('pending1', 123, 'Ожидает реквизиты')"
        )
    assert not await request_handlers._has_completed_meeting(123)

    async with database.transaction() as db:
        await db.execute(
            "INSERT INTO requests (unique_id, user_id, status) "
            "VALUES ('completed', 123, 'Завершено')"
        )
        await db.execute(
            "INSERT INTO requests (unique_id, user_id, status) "
            "VALUES ('pending2', 456, 'Ожидает реквизиты')"
        )
    assert await request_handlers._has_completed_meeting(123)
    assert not await request_handlers._has_completed_meeting(456)


def test_payment_keyboard_only_exposes_known_payment_handlers():
    markup = req_payment_kb(1, "1h", "0123456789abcdef")
    callbacks = {button.callback_data for button in _keyboards(markup)}
    assert "req_pay:1:1h:cash:0123456789abcdef" in callbacks
    assert "req_pay:1:1h:card:0123456789abcdef" in callbacks
    assert "req_pay:1:1h:crypto:0123456789abcdef" in callbacks


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=1+1", "'=1+1"),
        (" +SUM(A1:A2)", "' +SUM(A1:A2)"),
        ("\t-1+1", "'\t-1+1"),
        ("\x00@SUM(A1)", "'\x00@SUM(A1)"),
        ("обычный текст", "обычный текст"),
        (123, 123),
    ],
)
def test_csv_export_sanitizes_formula_prefixes_only_for_text(value, expected):
    assert admin._csv_safe_text(value) == expected


@pytest.mark.asyncio
async def test_csv_export_sanitizes_user_text_without_changing_database(
    tmp_path,
    monkeypatch,
):
    database = Database(tmp_path / "csv-export.db", backup_dir=tmp_path / "backups")
    await database.init()
    monkeypatch.setattr(admin, "db_manager", database)
    async with database.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username, first_name, city) "
            "VALUES (9, ?, ?, ?)",
            (" =1+1", "\t@SUM(A1)", "Москва"),
        )

    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(answer_document=AsyncMock()),
    )
    await admin.admin_export(callback)
    uploaded_file = callback.message.answer_document.await_args.args[0]
    contents = uploaded_file.data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(contents), delimiter=";"))
    assert rows[1][1:4] == ["' =1+1", "'\t@SUM(A1)", "Москва"]

    async with database.connect() as db:
        async with db.execute(
            "SELECT username, first_name FROM users WHERE id = 9"
        ) as cursor:
            row = await cursor.fetchone()
    assert row["username"] == " =1+1"
    assert row["first_name"] == "\t@SUM(A1)"


def _database_files_hash(path):
    digest = hashlib.sha256()
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists():
            digest.update(candidate.name.encode())
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


@pytest.mark.asyncio
async def test_check_database_is_read_only_and_does_not_change_file(
    tmp_path,
    monkeypatch,
):
    import check_database

    database_path = tmp_path / "diagnostic.db"
    database = Database(database_path, backup_dir=tmp_path / "backups")
    await database.init()
    async with database.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username) VALUES (10, 'unchanged')"
        )
    monkeypatch.setattr(check_database, "DB_PATH", database_path)

    before = _database_files_hash(database_path)
    assert await check_database.main() == 0
    after = _database_files_hash(database_path)
    assert before == after

    async with database.connect() as db:
        async with db.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 12
        async with db.execute("SELECT username FROM users WHERE id = 10") as cursor:
            assert (await cursor.fetchone())["username"] == "unchanged"


@pytest.mark.asyncio
async def test_check_database_reports_old_version_without_migration(
    tmp_path,
    monkeypatch,
):
    import check_database

    database_path = tmp_path / "old-diagnostic.db"
    database = Database(database_path, backup_dir=tmp_path / "backups")
    await database.init()
    async with database.connect() as db:
        await db.execute("PRAGMA user_version=11")
    monkeypatch.setattr(check_database, "DB_PATH", database_path)

    assert await check_database.main() == 1
    async with database.connect() as db:
        async with db.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 11
