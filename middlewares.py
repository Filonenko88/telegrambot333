import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.types import (
    CallbackQuery,
    TelegramObject,
    User,
)

from config import ADMIN_IDS
from database import db_manager


logger = logging.getLogger(__name__)

# Используем множество для быстрой проверки прав администратора.
ADMIN_ID_SET = frozenset(ADMIN_IDS)


@dataclass
class _ThrottleState:
    last_action: float
    lock: asyncio.Lock


# ============================================================
# FILTER: ADMINISTRATOR
# ============================================================

class IsAdmin(Filter):
    """
    Пропускает только пользователей из ADMIN_IDS.
    """

    async def __call__(
        self,
        event: TelegramObject,
        event_from_user: User | None = None,
    ) -> bool:

        if event_from_user is None:
            return False

        return event_from_user.id in ADMIN_ID_SET


# ============================================================
# MIDDLEWARE: ANTI-FLOOD
# ============================================================

class AntiFlood(BaseMiddleware):
    """
    Ограничивает частоту действий пользователя.
    """

    def __init__(
        self,
        limit: float = 0.7,
        cleanup_threshold: int = 500,
        stale_after: float = 60.0,
    ) -> None:

        super().__init__()

        if limit <= 0:
            raise ValueError("AntiFlood: limit должен быть больше 0.")

        if cleanup_threshold < 1:
            raise ValueError(
                "AntiFlood: cleanup_threshold должен быть >= 1."
            )

        if stale_after <= 0:
            raise ValueError(
                "AntiFlood: stale_after должен быть больше 0."
            )

        if stale_after < limit:
            raise ValueError(
                "AntiFlood: stale_after не может быть меньше limit."
            )

        self.limit = limit
        self.cleanup_threshold = cleanup_threshold
        self.stale_after = stale_after

        self._users: dict[int, _ThrottleState] = {}
        self._counter = 0

    def _cleanup(self) -> None:
        now = time.monotonic()
        stale_users = [
            user_id
            for user_id, state in self._users.items()
            if not state.lock.locked()
            and now - state.last_action > self.stale_after
        ]
        for user_id in stale_users:
            del self._users[user_id]

    async def __call__(
        self,
        handler: Callable[
            [TelegramObject, dict[str, Any]],
            Awaitable[Any],
        ],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:

        user = data.get("event_from_user")

        if user is None:
            return await handler(event, data)

        now = time.monotonic()

        self._counter += 1

        if self._counter >= self.cleanup_threshold:
            self._cleanup()
            self._counter = 0

        state = self._users.get(user.id)
        if state is not None and now - state.last_action < self.limit:

            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("⏳ Подождите немного...", show_alert=False)
                except TelegramBadRequest:
                    logger.debug(
                        "Callback пользователя %s уже нельзя подтвердить.",
                        user.id,
                        exc_info=True,
                    )

            return None

        if state is None:
            state = _ThrottleState(last_action=now, lock=asyncio.Lock())
            self._users[user.id] = state
        else:
            state.last_action = now

        async with state.lock:
            return await handler(event, data)


# ============================================================
# MIDDLEWARE: BAN PROTECTION
# ============================================================

class BanMiddleware(BaseMiddleware):
    """
    Проверяет статус блокировки пользователя.
    """

    async def __call__(
        self,
        handler: Callable[
            [TelegramObject, dict[str, Any]],
            Awaitable[Any],
        ],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:

        user: User | None = data.get("event_from_user")

        if user is None or user.id in ADMIN_ID_SET:
            return await handler(event, data)

        try:
            is_blocked = await self._read_ban_status(user.id)

            if is_blocked is None:
                await self._register_user(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                )

                is_blocked = await self._read_ban_status(user.id)

                if is_blocked is None:
                    raise RuntimeError(
                        f"Пользователь {user.id} не найден "
                        "после регистрации."
                    )

            if is_blocked:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer(
                            "🚫 Вы заблокированы.",
                            show_alert=True,
                        )
                    except Exception:
                        logger.debug(
                            "Не удалось показать уведомление о бане "
                            "пользователю %s.",
                            user.id,
                            exc_info=True,
                        )

                return None

        except Exception:
            logger.exception(
                "Ошибка проверки блокировки пользователя %s.",
                user.id,
            )
            raise

        return await handler(event, data)

    @staticmethod
    async def _read_ban_status(
        user_id: int,
    ) -> bool | None:
        async with db_manager.connect() as db:
            async with db.execute(
                """
                SELECT is_blocked
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if row is None:
            return None

        return bool(row["is_blocked"])

    @staticmethod
    async def _register_user(
        user_id: int,
        username: str | None,
        first_name: str | None,
    ) -> None:
        async with db_manager.connect() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO users (
                    id,
                    username,
                    first_name
                )
                VALUES (?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    first_name,
                ),
            )
