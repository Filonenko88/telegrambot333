import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database import Database
from handlers import admin


@pytest.mark.asyncio
async def test_admin_safe_edit_ignores_missing_message():
    await admin._safe_edit(None, "Тест")


async def _database_with_request(path, backup_dir):
    database = Database(path, backup_dir=backup_dir)
    await database.init()
    async with database.transaction() as db:
        await db.execute(
            """
            INSERT INTO requests (unique_id, user_id, service, amount)
            VALUES ('12345678', 123, '1 час', 1000)
            """
        )
    return database


@pytest.mark.asyncio
async def test_stale_admin_status_callback_cannot_overwrite_newer_status(
    tmp_path,
    monkeypatch,
):
    database = await _database_with_request(
        tmp_path / "admin-status.db",
        tmp_path / "backups",
    )
    monkeypatch.setattr(admin, "db_manager", database)
    monkeypatch.setattr(admin, "_safe_answer", AsyncMock())
    monkeypatch.setattr(admin, "_render_req_detail", AsyncMock(return_value=True))
    bot = SimpleNamespace(send_message=AsyncMock())
    message = object()

    await admin.admin_req_status(
        SimpleNamespace(data="admin:req_status:1:w:p", message=message),
        bot,
    )
    await admin.admin_req_status(
        SimpleNamespace(data="admin:req_status:1:w:c", message=message),
        bot,
    )

    async with database.connect() as db:
        async with db.execute("SELECT status FROM requests WHERE id = 1") as cursor:
            assert (await cursor.fetchone())["status"] == "Оплачено"

    assert bot.send_message.await_count == 1
    assert admin._safe_answer.await_args.args[1] == ("Статус уже изменился. Карточка обновлена.")


@pytest.mark.asyncio
async def test_concurrent_admin_status_callbacks_only_apply_one_transition(
    tmp_path,
    monkeypatch,
):
    database = await _database_with_request(
        tmp_path / "concurrent-admin-status.db",
        tmp_path / "backups",
    )
    monkeypatch.setattr(admin, "db_manager", database)
    answer = AsyncMock()
    monkeypatch.setattr(admin, "_safe_answer", answer)
    monkeypatch.setattr(admin, "_render_req_detail", AsyncMock(return_value=True))
    bot = SimpleNamespace(send_message=AsyncMock())
    message = object()

    await asyncio.gather(
        admin.admin_req_status(
            SimpleNamespace(data="admin:req_status:1:w:p", message=message),
            bot,
        ),
        admin.admin_req_status(
            SimpleNamespace(data="admin:req_status:1:w:c", message=message),
            bot,
        ),
    )

    async with database.connect() as db:
        async with db.execute("SELECT status FROM requests WHERE id = 1") as cursor:
            final_status = (await cursor.fetchone())["status"]

    assert final_status in {"Оплачено", "Отменено"}
    assert bot.send_message.await_count == 1
    assert any(
        call.args[1] == "Статус уже изменился. Карточка обновлена."
        for call in answer.await_args_list
    )
