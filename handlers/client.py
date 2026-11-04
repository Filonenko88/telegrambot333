import html
import logging
import math
import re
from datetime import datetime

import httpx
import asyncio

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardRemove,
)

from config import ADMIN_IDS, NOMINATIM_CONTACT
from database import db_manager
from keyboards import (
    main_menu_kb,
    city_kb,
    profile_kb,
)
from services import settings as bot_settings


logger = logging.getLogger(__name__)

router = Router(name="client")

ADMIN_ID_SET = frozenset(ADMIN_IDS)

MAX_CITY_LENGTH = 100
MIN_CITY_LENGTH = 2

# ============================================================
# GEOCODER (Nominatim / OpenStreetMap)
# ============================================================

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

NOMINATIM_HEADERS = {
    "User-Agent": (
        f"EscortBot/1.0 ({NOMINATIM_CONTACT})"
        if NOMINATIM_CONTACT
        else "EscortBot/1.0"
    ),
    "Accept-Language": "ru",
}

GEOCODER_TIMEOUT = 10.0

# Простой кеш: (округлённые координаты) -> (город, timestamp)
_geocode_cache: dict[tuple[float, float], tuple[str | None, float]] = {}
_GEOCODE_CACHE_TTL = 3600.0  # 1 час
_GEOCODE_CACHE_PRECISION = 2  # ~1 км
_GEOCODE_CACHE_MAX_SIZE = 1000
_NOMINATIM_LOCK = asyncio.Lock()
_LAST_NOMINATIM_REQUEST = 0.0
_NOMINATIM_MIN_INTERVAL = 1.1  # публичный сервис: не более ~1 запроса/секунду


async def reverse_geocode(lat: float, lon: float) -> str | None:
    """Определяет город по координатам через Nominatim с кешированием и rate limit."""
    import time

    if not math.isfinite(lat) or not math.isfinite(lon):
        logger.warning("Геокодер: получены нечисловые координаты")
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        logger.warning("Геокодер: координаты вне допустимого диапазона")
        return None

    cache_key = (round(lat, _GEOCODE_CACHE_PRECISION), round(lon, _GEOCODE_CACHE_PRECISION))
    now = time.monotonic()
    cached = _geocode_cache.get(cache_key)
    if cached and now - cached[1] < _GEOCODE_CACHE_TTL:
        return cached[0]

    global _LAST_NOMINATIM_REQUEST
    async with _NOMINATIM_LOCK:
        # Повторно проверяем кеш: другой coroutine мог уже получить ответ.
        now = time.monotonic()
        cached = _geocode_cache.get(cache_key)
        if cached and now - cached[1] < _GEOCODE_CACHE_TTL:
            return cached[0]

        wait_for = _NOMINATIM_MIN_INTERVAL - (now - _LAST_NOMINATIM_REQUEST)
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        params = {
            "format": "json",
            "lat": lat,
            "lon": lon,
            "zoom": 10,
            "addressdetails": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=GEOCODER_TIMEOUT) as client:
                resp = await client.get(
                    NOMINATIM_URL,
                    params=params,
                    headers=NOMINATIM_HEADERS,
                )
                _LAST_NOMINATIM_REQUEST = time.monotonic()
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("Геокодер: HTTP %s", exc.response.status_code)
            _cache_geocode_result(cache_key, None, time.monotonic())
            return None
        except httpx.HTTPError as exc:
            logger.warning("Геокодер: сетевая ошибка: %s", exc)
            _cache_geocode_result(cache_key, None, time.monotonic())
            return None
        except (ValueError, TypeError) as exc:
            logger.warning("Геокодер: некорректный JSON-ответ: %s", exc)
            _cache_geocode_result(cache_key, None, time.monotonic())
            return None
        except Exception:
            logger.exception("Геокодер: неожиданная ошибка")
            _cache_geocode_result(cache_key, None, time.monotonic())
            return None

    address = data.get("address") or {}
    for key in ("city", "town", "village", "municipality", "county", "state"):
        value = address.get(key)
        if value:
            city = str(value).strip()
            _cache_geocode_result(cache_key, city, time.monotonic())
            return city

    _cache_geocode_result(cache_key, None, time.monotonic())
    return None


def _cache_geocode_result(
    key: tuple[float, float],
    city: str | None,
    timestamp: float,
) -> None:
    """Сохраняет результат геокодирования, ограничивая размер кеша."""
    _geocode_cache[key] = (city, timestamp)
    if len(_geocode_cache) > _GEOCODE_CACHE_MAX_SIZE:
        oldest_key = min(_geocode_cache, key=lambda item: _geocode_cache[item][1])
        _geocode_cache.pop(oldest_key, None)


# ============================================================
# STATES
# ============================================================

class ClientStates(StatesGroup):
    choosing_city = State()
    entering_city = State()


# ============================================================
# HELPERS
# ============================================================

def normalize_city(city: str) -> str:
    """Нормализует название города."""
    return re.sub(r"\s+", " ", city).strip()


def validate_city(city: str) -> str | None:
    """Проверяет название города."""
    city = normalize_city(city)

    if not MIN_CITY_LENGTH <= len(city) <= MAX_CITY_LENGTH:
        return None

    if not any(char.isalpha() for char in city):
        return None

    if any(ord(char) < 32 for char in city):
        return None

    return city


def format_date(raw: str | None) -> str:
    """
    Форматирует дату регистрации для отображения.

    Поддерживает:
        - 'YYYY-MM-DD HH:MM:SS'
        - 'YYYY-MM-DD'
        - ISO datetime строки
    """
    if not raw:
        return "Неизвестно"

    raw = str(raw).strip()

    # Пробуем несколько форматов
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%d.%m.%Y в %H:%M") if ":" in raw else dt.strftime("%d.%m.%Y")
        except ValueError:
            continue

    # Если это ISO (например '2024-01-15T10:30:00')
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y в %H:%M")
    except (ValueError, AttributeError):
        pass

    # Если ничего не подошло — отдаём первые 10 символов
    return raw[:10] if len(raw) >= 10 else raw


async def save_user_city(
    user_id: int,
    username: str | None,
    first_name: str | None,
    city: str,
) -> None:
    """Сохраняет город пользователя."""
    async with db_manager.transaction() as db:
        await db.execute(
            """
            INSERT INTO users (id, username, first_name, city)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                city = excluded.city,
                username = COALESCE(excluded.username, users.username),
                first_name = COALESCE(excluded.first_name, users.first_name)
            """,
            (user_id, username, first_name, city),
        )


async def show_main_menu(
    message: Message,
    user_id: int,
    city: str | None = None,
) -> None:
    """Показывает главное меню пользователя."""
    is_admin = user_id in ADMIN_ID_SET

    text = "🏠 <b>Главное меню</b>"
    if city:
        text += f"\n\nВаш город: <b>{html.escape(city)}</b>"

    await message.answer(
        text,
        reply_markup=main_menu_kb(is_admin=is_admin),
        parse_mode="HTML",
    )


# ============================================================
# /START
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Точка входа."""
    await state.clear()

    user = message.from_user
    if user is None:
        return

    try:
        async with db_manager.transaction() as db:
            await db.execute(
                """
                INSERT INTO users (id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name)
                """,
                (user.id, user.username, user.first_name),
            )

            async with db.execute(
                "SELECT city FROM users WHERE id = ?",
                (user.id,),
            ) as cursor:
                row = await cursor.fetchone()

    except Exception:
        logger.exception("Ошибка регистрации пользователя %s.", user.id)
        await message.answer(
            "❌ Не удалось загрузить ваш профиль. Попробуйте позже."
        )
        return

    city = row["city"] if row else None

    if not city:
        await message.answer(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Для начала выберите ваш город, "
            "чтобы мы могли показать актуальные анкеты:",
            reply_markup=city_kb(),
            parse_mode="HTML",
        )
        await state.set_state(ClientStates.choosing_city)
        return

    await show_main_menu(message=message, user_id=user.id, city=city)


# ============================================================
# CITY SELECTION
# ============================================================

@router.message(
    ClientStates.choosing_city,
    F.text == "✏️ Ввести город вручную",
)
async def process_city_manual_btn(
    message: Message,
    state: FSMContext,
) -> None:
    """Переход к ручному вводу города."""
    await message.answer(
        "✍️ Напишите название вашего города:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(ClientStates.entering_city)


@router.message(ClientStates.choosing_city, F.location)
async def process_location(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Обработка геолокации с реальным определением города.
    """
    user = message.from_user
    if user is None or message.location is None:
        return

    status_msg = await message.answer(
        "🔍 Определяю ваш город по координатам...",
        reply_markup=ReplyKeyboardRemove(),
    )

    lat = message.location.latitude
    lon = message.location.longitude

    city = await reverse_geocode(lat, lon)

    if not city:
        try:
            await status_msg.edit_text(
                "❌ Не удалось определить город по координатам.\n\n"
                "Пожалуйста, напишите название города вручную:"
            )
        except TelegramBadRequest:
            pass

        await state.set_state(ClientStates.entering_city)
        return

    city = validate_city(city) or city

    try:
        await save_user_city(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            city=city,
        )
    except Exception:
        logger.exception("Не удалось сохранить город пользователя %s.", user.id)
        await message.answer("❌ Не удалось сохранить город. Попробуйте позже.")
        await state.clear()
        return

    await state.clear()

    try:
        await status_msg.edit_text(
            f"📍 Ваш город определён: <b>{html.escape(city)}</b>\n\n"
            f"Координаты: <code>{lat:.4f}, {lon:.4f}</code>",
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass

    await show_main_menu(message=message, user_id=user.id, city=city)


@router.message(ClientStates.entering_city, F.text)
async def process_city_text(
    message: Message,
    state: FSMContext,
) -> None:
    """Обработка введённого пользователем города."""
    user = message.from_user
    if user is None:
        return

    raw_city = message.text or ""
    city_name = validate_city(raw_city)

    if city_name is None:
        await message.answer(
            "❌ Некорректное название города.\n\n"
            f"Введите название длиной от {MIN_CITY_LENGTH} "
            f"до {MAX_CITY_LENGTH} символов."
        )
        return

    try:
        await save_user_city(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            city=city_name,
        )
    except Exception:
        logger.exception("Не удалось сохранить город пользователя %s.", user.id)
        await message.answer("❌ Не удалось сохранить город. Попробуйте ещё раз.")
        return

    await state.clear()

    await message.answer(
        f"🏙 Отлично! Ваш город: <b>{html.escape(city_name)}</b>",
        reply_markup=main_menu_kb(is_admin=user.id in ADMIN_ID_SET),
        parse_mode="HTML",
    )


# ============================================================
# MAIN MENU
# ============================================================

@router.callback_query(F.data == "menu:main")
async def back_to_main(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Возврат в главное меню."""
    await callback.answer()
    await state.clear()

    user = callback.from_user
    message = callback.message
    if not isinstance(message, Message):
        return

    try:
        async with db_manager.connect() as db:
            async with db.execute(
                "SELECT city FROM users WHERE id = ?",
                (user.id,),
            ) as cursor:
                row = await cursor.fetchone()
        city = row["city"] if row else None
    except Exception:
        logger.exception("Ошибка получения города пользователя %s.", user.id)
        city = None

    is_admin = user.id in ADMIN_ID_SET
    text = "🏠 <b>Главное меню</b>"
    if city:
        text += f"\n\nВаш город: <b>{html.escape(city)}</b>"

    try:
        await message.edit_text(
            text,
            reply_markup=main_menu_kb(is_admin=is_admin),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning(
            "Не удалось обновить главное меню пользователя %s: %s",
            user.id, exc,
        )
        await message.answer(
            text,
            reply_markup=main_menu_kb(is_admin=is_admin),
            parse_mode="HTML",
        )


# ============================================================
# INFO
# ============================================================

@router.callback_query(F.data == "menu:info")
async def show_info(callback: CallbackQuery) -> None:
    """Показывает текст информации из настроек бота."""
    await callback.answer()

    user = callback.from_user
    text = await bot_settings.get_text("info_text", "ℹ️ <b>Информация</b>\n\nТекст отсутствует.")
    message = callback.message
    if message is None:
        return

    try:
        await message.edit_text(
            f"ℹ️ <b>Информация</b>\n\n{text}",
            reply_markup=main_menu_kb(is_admin=user.id in ADMIN_ID_SET),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Не удалось показать информацию пользователю %s: %s", user.id, exc)
            await message.answer(
                f"ℹ️ <b>Информация</b>\n\n{text}",
                reply_markup=main_menu_kb(is_admin=user.id in ADMIN_ID_SET),
                parse_mode="HTML",
            )


# ============================================================
# PROFILE (с полными данными)
# ============================================================

@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery) -> None:
    """Показывает расширенный профиль пользователя."""
    await callback.answer()

    user_id = callback.from_user.id

    try:
        async with db_manager.connect() as db:
            async with db.execute(
                """
                SELECT id, username, first_name, city, registered_at,
                       notif_on, is_blocked
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ) as cursor:
                user_row = await cursor.fetchone()

            async with db.execute(
                "SELECT COUNT(*) AS cnt FROM requests WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                req_row = await cursor.fetchone()

            async with db.execute(
                "SELECT COUNT(*) AS cnt FROM favorites WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                fav_row = await cursor.fetchone()

            async with db.execute(
                "SELECT COUNT(*) AS cnt FROM requests WHERE user_id = ? AND status = ?",
                (user_id, "Завершено"),
            ) as cursor:
                done_row = await cursor.fetchone()

    except Exception:
        logger.exception("Ошибка загрузки профиля пользователя %s.", user_id)
        await callback.message.answer(
            "❌ Не удалось загрузить профиль. Попробуйте позже."
        )
        return

    if not user_row:
        await callback.message.answer(
            "❌ Профиль не найден.\nПожалуйста, отправьте /start."
        )
        return

    requests_count = int(req_row["cnt"]) if req_row else 0
    favorites_count = int(fav_row["cnt"]) if fav_row else 0
    completed_count = int(done_row["cnt"]) if done_row else 0

    username = user_row["username"]
    username_str = f"@{html.escape(username)}" if username else "не указан"

    first_name = user_row["first_name"] or "Не указано"
    city = user_row["city"] or "Не указан"
    reg_date = format_date(user_row["registered_at"])
    notif_on = "включены ✅" if user_row["notif_on"] else "выключены ❌"

    is_admin = user_id in ADMIN_ID_SET
    is_blocked = bool(user_row["is_blocked"])

    lines = [
        "👤 <b>Мой профиль</b>",
        "",
        f"🆔 ID: <code>{user_row['id']}</code>",
        f"📱 Username: {username_str}",
        f"📛 Имя: {html.escape(str(first_name))}",
        f"🏙 Город: <b>{html.escape(str(city))}</b>",
        f"📅 Дата регистрации: {html.escape(reg_date)}",
        "",
        f"📋 Всего заявок: <b>{requests_count}</b>",
        f"✅ Завершено: <b>{completed_count}</b>",
        f"❤️ В избранном: <b>{favorites_count}</b>",
        f"🔔 Уведомления: {notif_on}",
    ]

    # Блок «только для админа»
    if is_admin:
        lines.append("")
        lines.append("👑 <b>Админ-статус</b>")
        lines.append(f"🚫 Забанен: {'ДА' if is_blocked else 'нет'}")

    text = "\n".join(lines)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=profile_kb(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning(
            "Не удалось показать профиль пользователя %s: %s",
            user_id, exc,
        )
        await callback.message.answer(
            text,
            reply_markup=profile_kb(),
            parse_mode="HTML",
        )


# ============================================================
# CHANGE CITY
# ============================================================

@router.callback_query(F.data == "prof:city")
async def change_city_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Запуск смены города из профиля."""
    await callback.answer()
    await state.clear()

    message = callback.message
    if not isinstance(message, Message):
        return

    try:
        await message.edit_text(
            "🏙 <b>Изменение города</b>\n\n"
            "Выберите способ указания нового города:",
            reply_markup=None,
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning(
                "Не удалось обновить сообщение смены города: %s", exc,
            )

    await message.answer(
        "Выберите новый город:",
        reply_markup=city_kb(),
    )
    await state.set_state(ClientStates.choosing_city)


# ============================================================
# END OF CLIENT HANDLERS
# ============================================================
