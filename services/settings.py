from __future__ import annotations

import logging
from typing import Any

from database import db_manager

logger = logging.getLogger(__name__)

# Кеширует только фактические значения из БД. None означает, что ключ отсутствует;
# default не кешируется, поскольку он может различаться между вызовами.
_CACHE: dict[str, str | None] = {}


def _validate_key(key: str) -> str:
    if not isinstance(key, str):
        raise TypeError("Ключ настройки должен быть строкой.")
    key = key.strip()
    if not key:
        raise ValueError("Ключ настройки не может быть пустым.")
    if len(key) > 255:
        raise ValueError("Ключ настройки слишком длинный.")
    return key


def _validate_value(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Значение настройки должно быть строкой.")
    return value


async def get_setting(key: str, default: str | None = None) -> str | None:
    """Возвращает значение настройки; default используется только при отсутствии ключа."""
    key = _validate_key(key)

    if key in _CACHE:
        cached = _CACHE[key]
        return default if cached is None else cached

    async with db_manager.connect() as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()

    # Пустая строка — допустимое сохранённое значение, не заменяем её default.
    value = row["value"] if row is not None else None
    if value is not None and not isinstance(value, str):
        value = str(value)

    _CACHE[key] = value
    return default if value is None else value


async def set_setting(key: str, value: str) -> None:
    """Атомарно сохраняет настройку и обновляет локальный кеш после commit."""
    key = _validate_key(key)
    value = _validate_value(value)

    async with db_manager.transaction() as db:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    _CACHE[key] = value


async def delete_setting(key: str) -> None:
    """Удаляет настройку и её запись из локального кеша."""
    key = _validate_key(key)
    async with db_manager.transaction() as db:
        await db.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
    _CACHE.pop(key, None)


def invalidate_cache() -> None:
    """Очищает кеш (например, при старте бота или внешнем изменении БД)."""
    _CACHE.clear()


async def get_text(key: str, default: str = "") -> str:
    """Возвращает текстовую настройку, сохраняя допустимые пустые значения как default."""
    raw = await get_setting(key, default)
    return default if raw is None else raw


async def get_photo(key: str) -> str | None:
    """Возвращает Telegram file_id фотографии или None."""
    return await get_setting(key)


async def get_int(key: str, default: int = 0) -> int:
    """Возвращает целочисленную настройку; некорректное значение заменяет default."""
    if isinstance(default, bool) or not isinstance(default, int):
        raise TypeError("Значение по умолчанию должно быть целым числом.")

    raw = await get_setting(key)
    if raw is None or raw == "":
        return default

    try:
        # int() допускает пробелы и знак; это нормальное поведение для настроек.
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Настройка %s содержит некорректное целое значение.", key)
        return default
