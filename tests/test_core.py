import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey

from handlers import client
from middlewares import IsAdmin
from storage import SingleProcessEventIsolation, SQLiteStorage


@pytest.mark.asyncio
async def test_is_admin_filter():
    # IsAdmin использует ADMIN_ID_SET, поэтому подменяем именно его.
    with patch("middlewares.ADMIN_ID_SET", frozenset({12345})):
        admin_filter = IsAdmin()

        mock_user = MagicMock()
        mock_user.id = 12345
        assert await admin_filter(None, mock_user) is True

        mock_user.id = 99999
        assert await admin_filter(None, mock_user) is False


@pytest.mark.asyncio
async def test_sqlite_storage_restart(tmp_path):
    test_db = str(tmp_path / "test_fsm.db")
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)

    storage1 = SQLiteStorage(test_db)
    await storage1.set_state(key, State("TestState"))
    await storage1.set_data(key, {"foo": "bar"})

    # Создаём новый экземпляр: данные должны пережить перезапуск хранилища.
    storage2 = SQLiteStorage(test_db)

    assert await storage2.get_state(key) == State("TestState").state
    assert await storage2.get_data(key) == {"foo": "bar"}


@pytest.mark.asyncio
async def test_state_and_data_updates_do_not_overwrite_each_other(tmp_path):
    test_db = str(tmp_path / "test_fsm_independent.db")
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    storage = SQLiteStorage(test_db)

    await storage.set_state(key, State("FirstState"))
    await storage.set_data(key, {"step": 1})

    # Обновление данных не должно сбрасывать состояние.
    await storage.set_data(key, {"step": 2})
    assert await storage.get_state(key) == State("FirstState").state

    # Обновление состояния не должно сбрасывать данные.
    await storage.set_state(key, State("SecondState"))
    assert await storage.get_data(key) == {"step": 2}
    assert await storage.get_state(key) == State("SecondState").state


@pytest.mark.asyncio
async def test_concurrent_fsm_update_data_does_not_lose_fields(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "concurrent_fsm.db"))
    isolation = SingleProcessEventIsolation()
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    original_get_data = storage.get_data
    original_set_data = storage.set_data

    async def update_data(key, **updates):
        async with isolation.lock(key):
            data = await original_get_data(key)
            await asyncio.sleep(0)
            data.update(updates)
            await original_set_data(key, data)

    await asyncio.gather(
        update_data(key, first="value"),
        update_data(key, second="value"),
    )

    assert await storage.get_data(key) == {
        "first": "value",
        "second": "value",
    }
    assert isolation._locks == {}


@pytest.mark.asyncio
async def test_cancelled_fsm_handler_releases_key_lock():
    isolation = SingleProcessEventIsolation()
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    acquired = asyncio.Event()
    never = asyncio.Event()

    async def hold_lock():
        async with isolation.lock(key):
            acquired.set()
            await never.wait()

    task = asyncio.create_task(hold_lock())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def reacquire():
        async with isolation.lock(key):
            return

    await asyncio.wait_for(reacquire(), timeout=1)
    assert isolation._locks == {}


@pytest.mark.asyncio
async def test_main_menu_info_uses_configured_text(monkeypatch):
    async def fake_get_text(key, default=""):
        assert key == "info_text"
        return "<b>Гарантии</b> и правила безопасности"

    monkeypatch.setattr(client.bot_settings, "get_text", fake_get_text)

    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=123),
        message=message,
    )

    await client.show_info(callback)

    message.edit_text.assert_awaited_once()
    args, kwargs = message.edit_text.call_args
    assert "Гарантии" in args[0]
    assert kwargs["parse_mode"] == "HTML"
