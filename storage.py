from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage, StorageKey

from database import db_manager

logger = logging.getLogger(__name__)


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class SingleProcessEventIsolation(BaseEventIsolation):
    """Serialize updates for one FSM key within this bot process."""

    def __init__(self) -> None:
        self._locks: dict[StorageKey, _LockEntry] = {}

    @asynccontextmanager
    async def lock(self, key: StorageKey) -> AsyncIterator[None]:
        entry = self._locks.get(key)
        if entry is None:
            entry = _LockEntry(lock=asyncio.Lock())
            self._locks[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and not entry.lock.locked():
                self._locks.pop(key, None)

    async def close(self) -> None:
        self._locks.clear()


class SQLiteStorage(BaseStorage):
    """
    SQLite FSM Storage для Aiogram 3.13.x.

    Поддерживает:
        SQLiteStorage()
        SQLiteStorage("test.db")

    State и data обновляются независимо.
    Поддерживаются все поля StorageKey.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path) if path is not None else None

    @staticmethod
    def _key_values(key: StorageKey) -> tuple:
        return (
            int(key.bot_id),
            int(key.chat_id),
            int(key.user_id),
            int(key.thread_id if key.thread_id is not None else -1),
            str(key.business_connection_id or ""),
            str(key.destiny or "default"),
        )

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        if self.path is None or self.path == str(db_manager.path):
            async with db_manager.connect() as db:
                yield db
            return

        test_path = Path(self.path)
        test_path.parent.mkdir(parents=True, exist_ok=True)

        db = await aiosqlite.connect(
            self.path,
            timeout=10.0,
            check_same_thread=False,
        )

        db.row_factory = aiosqlite.Row
        db.isolation_level = None

        try:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 10000")
            yield db
        finally:
            await db.close()

    @staticmethod
    async def _ensure_table(db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS fsm_storage (
                bot_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL DEFAULT -1,
                business_connection_id TEXT NOT NULL DEFAULT '',
                destiny TEXT NOT NULL DEFAULT 'default',
                state TEXT,
                data TEXT NOT NULL DEFAULT '{}',

                PRIMARY KEY (
                    bot_id,
                    chat_id,
                    user_id,
                    thread_id,
                    business_connection_id,
                    destiny
                )
            )
            """
        )

    async def set_state(
        self,
        key: StorageKey,
        state: State | str | None = None,
    ) -> None:
        state_value = state.state if isinstance(state, State) else state

        if state_value is not None and not isinstance(state_value, str):
            raise TypeError("state должен быть State, str или None")

        values = self._key_values(key)

        async with self._connect() as db:
            await self._ensure_table(db)

            await db.execute(
                """
                INSERT INTO fsm_storage (
                    bot_id,
                    chat_id,
                    user_id,
                    thread_id,
                    business_connection_id,
                    destiny,
                    state
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT (
                    bot_id,
                    chat_id,
                    user_id,
                    thread_id,
                    business_connection_id,
                    destiny
                )
                DO UPDATE SET
                    state = excluded.state
                """,
                (*values, state_value),
            )

            await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        values = self._key_values(key)

        async with self._connect() as db:
            await self._ensure_table(db)

            async with db.execute(
                """
                SELECT state
                FROM fsm_storage
                WHERE bot_id = ?
                  AND chat_id = ?
                  AND user_id = ?
                  AND thread_id = ?
                  AND business_connection_id = ?
                  AND destiny = ?
                """,
                values,
            ) as cursor:
                row = await cursor.fetchone()

        return row["state"] if row else None

    async def set_data(
        self,
        key: StorageKey,
        data: dict[str, Any],
    ) -> None:
        if not isinstance(data, dict):
            raise TypeError("FSM data должно быть словарём")

        try:
            serialized = json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"FSM data нельзя сериализовать в JSON: {exc}") from exc

        values = self._key_values(key)

        async with self._connect() as db:
            await self._ensure_table(db)

            await db.execute(
                """
                INSERT INTO fsm_storage (
                    bot_id,
                    chat_id,
                    user_id,
                    thread_id,
                    business_connection_id,
                    destiny,
                    data
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT (
                    bot_id,
                    chat_id,
                    user_id,
                    thread_id,
                    business_connection_id,
                    destiny
                )
                DO UPDATE SET
                    data = excluded.data
                """,
                (*values, serialized),
            )

            await db.commit()

    async def get_data(
        self,
        key: StorageKey,
    ) -> dict[str, Any]:
        values = self._key_values(key)

        async with self._connect() as db:
            await self._ensure_table(db)

            async with db.execute(
                """
                SELECT data
                FROM fsm_storage
                WHERE bot_id = ?
                  AND chat_id = ?
                  AND user_id = ?
                  AND thread_id = ?
                  AND business_connection_id = ?
                  AND destiny = ?
                """,
                values,
            ) as cursor:
                row = await cursor.fetchone()

        if not row or not row["data"]:
            return {}

        try:
            result = json.loads(row["data"])

        except (json.JSONDecodeError, TypeError):
            logger.exception("Повреждены FSM-данные SQLite")
            return {}

        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        return None
