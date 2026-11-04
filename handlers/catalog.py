import html
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS
from database import db_manager
from keyboards import catalog_kb, anketa_kb


logger = logging.getLogger(__name__)
router = Router(name="catalog")


# ============================================================
# CONFIGURATION
# ============================================================

PAGE_SIZE = 5
FAVS_PAGE_SIZE = 6

VALID_SORT_TYPES = frozenset({"default", "price_asc", "price_desc"})
VALID_FAVORITE_ACTIONS = frozenset({"add", "del"})


# ============================================================
# FSM ДЛЯ ПОИСКА
# ============================================================

class SearchCode(StatesGroup):
    waiting_code = State()


# ============================================================
# HELPERS
# ============================================================

def _safe_int(value: Any, default: int = 0, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if minimum is not None and result < minimum:
        return default
    return result


def _escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _money(value: Any) -> str:
    amount = _safe_int(value, default=0, minimum=0)
    return f"{amount:,}".replace(",", " ")


def _valid_sort(value: str) -> str:
    return value if value in VALID_SORT_TYPES else "default"


def _valid_outcall(value: Any) -> int:
    result = _safe_int(value, default=0)
    return result if result in (0, 1) else 0


def _parse_catalog_context(parts: list[str]) -> tuple[int, str, int, str]:
    page = _safe_int(parts[2] if len(parts) > 2 else 0, minimum=0)
    sort_type = _valid_sort(parts[3] if len(parts) > 3 else "default")
    outcall = _valid_outcall(parts[4] if len(parts) > 4 else 0)
    source = parts[5] if len(parts) > 5 else "catalog"
    if source not in ("catalog", "favs"):
        source = "catalog"
    return page, sort_type, outcall, source


async def _safe_answer(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        # Telegram может вернуть ошибку, если callback уже истёк или был отвечен.
        logger.debug("Не удалось ответить на callback %s: %s", callback.id, exc)
    except TelegramAPIError:
        logger.exception("Ошибка Telegram API при ответе на callback %s", callback.id)


async def _edit_or_send_text(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        error = str(exc).lower()
        if "message is not modified" in error:
            return
        logger.debug("Не удалось изменить текстовое сообщение: %s", exc)
        try:
            await message.answer(text=text, reply_markup=markup, parse_mode="HTML")
        except TelegramAPIError:
            logger.exception("Не удалось отправить новое текстовое сообщение")


async def _show_error(callback: CallbackQuery, text: str) -> None:
    if callback.message:
        await callback.message.answer(text)


def _prices_text(profile: Any) -> str:
    return (
        f"💰 <b>1 час:</b> {_money(profile['price_1h'])} ₽\n"
        f"💰 <b>3 часа:</b> {_money(profile['price_3h'])} ₽\n"
        f"🌙 <b>Ночь:</b> {_money(profile['price_night'])} ₽"
    )


def _build_anketa_caption(profile: Any) -> str:
    outcall_text = "✅ Есть выезд" if profile["outcall"] else "🏡 Только апартаменты"
    age = _safe_int(profile["age"], default=0, minimum=0)
    return (
        f"👤 <b>{_escape(profile['name'])}</b>, {age} лет\n"
        f"🆔 Код анкеты: <code>{_escape(profile['code'])}</code>\n"
        f"🏙 {_escape(profile['city'])}\n"
        f"🚗 {outcall_text}\n\n"
        f"{_escape(profile['bio'])}\n\n"
        f"{_prices_text(profile)}"
    )


async def _notify_admins_view(bot: Bot, user_id: int, username: str | None, profile: Any) -> None:
    """Отправляет уведомление админам о просмотре анкеты."""
    if user_id in ADMIN_IDS:
        return  # Админы не спамят сами себе

    user_link = f"@{_escape(username)}" if username else f"ID <code>{user_id}</code>"
    text = (
        f"👁 <b>Просмотр анкеты!</b>\n\n"
        f"👤 Клиент: {user_link}\n"
        f"🌸 Анкета: <b>[{profile['code']}] {_escape(profile['name'])}</b>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except TelegramAPIError:
            logger.exception("Telegram API не доставил уведомление админу %s", admin_id)
        except Exception:
            logger.exception("Неожиданная ошибка уведомления админу %s", admin_id)


# ============================================================
# FAVORITES KEYBOARD
# ============================================================

def _favs_kb(anketas: list, page: int, total_pages: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for anketa in anketas:
        builder.button(
            text=f"[{anketa['code']}] {str(anketa['name'])[:20]}, {anketa['age']} лет",
            callback_data=f"view:{anketa['id']}:{page}:default:0:favs",
        )
    nav = []
    if page > 0:
        nav.append(("◀️", f"favs:{page - 1}"))
    if page < total_pages - 1:
        nav.append(("▶️", f"favs:{page + 1}"))
    for text, callback_data in nav:
        builder.button(text=text, callback_data=callback_data)
    builder.button(text="🏠 Главное меню", callback_data="menu:main")
    layout = [1] * len(anketas)
    if nav:
        layout.append(len(nav))
    layout.append(1)
    builder.adjust(*layout)
    return builder.as_markup()


# ============================================================
# CATALOG QUERY
# ============================================================

async def _get_catalog(user_id: int, page: int, sort_type: str, outcall_only: int) -> tuple[list, str | None, int]:
    page = max(0, page)
    sort_columns = {
        "default": "id DESC",
        "price_asc": "price_1h ASC, id DESC",
        "price_desc": "price_1h DESC, id DESC",
    }
    order_by = sort_columns[_valid_sort(sort_type)]

    async with db_manager.connect() as db:
        async with db.execute("SELECT city FROM users WHERE id = ?", (user_id,)) as cursor:
            user_row = await cursor.fetchone()
        city = user_row["city"] if user_row else None

        conditions = []
        params = []
        if city:
            conditions.append("city = ?")
            params.append(city)
        if outcall_only:
            conditions.append("outcall = 1")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        async with db.execute(f"SELECT COUNT(*) FROM anketas {where}", tuple(params)) as cursor:
            row = await cursor.fetchone()
        total = int(row[0]) if row else 0

        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages - 1)
        offset = page * PAGE_SIZE

        async with db.execute(
            f"""
            SELECT id, code, name, age, city, price_1h, price_3h, price_night, outcall
            FROM anketas
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (PAGE_SIZE, offset),
        ) as cursor:
            anketas = await cursor.fetchall()

    return anketas, city, total


# ============================================================
# CATALOG
# ============================================================

@router.callback_query(F.data.startswith("catalog:"))
async def show_catalog(callback: CallbackQuery) -> None:
    await _safe_answer(callback)
    if not callback.message or not callback.data:
        return
    parts = callback.data.split(":")
    page = _safe_int(parts[1] if len(parts) > 1 else 0, minimum=0)
    sort_type = _valid_sort(parts[2] if len(parts) > 2 else "default")
    outcall_only = _valid_outcall(parts[3] if len(parts) > 3 else 0)
    user_id = callback.from_user.id

    try:
        anketas, city, total = await _get_catalog(user_id=user_id, page=page, sort_type=sort_type, outcall_only=outcall_only)
    except Exception:
        logger.exception("Ошибка загрузки каталога пользователя %s", user_id)
        await _show_error(callback, "❌ Не удалось загрузить каталог. Попробуйте позже.")
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    if not anketas:
        builder = InlineKeyboardBuilder()
        builder.button(text="🏠 Главное меню", callback_data="menu:main")
        city_text = _escape(city) if city else "доступных городах"
        text = f"💎 <b>Каталог</b>\n\nПока нет анкет в {city_text}."
        await _edit_or_send_text(callback.message, text, builder.as_markup())
        return

    title = (
        f"💎 <b>Каталог</b>\n"
        f"🏙 Город: {_escape(city or 'Все города')}\n"
        f"📄 Страница {page + 1} из {total_pages}\n"
        f"📊 Всего анкет: {total}"
    )
    await _edit_or_send_text(
        callback.message,
        title,
        catalog_kb(anketas=anketas, page=page, max_pages=total_pages, sort_type=sort_type, outcall_only=outcall_only),
    )


# ============================================================
# ANKETA CARD
# ============================================================

async def _render_anketa(
    callback: CallbackQuery,
    anketa_id: int,
    page: int,
    sort_type: str,
    outcall_only: int,
    source: str,
    bot: Bot,
    notify_admins: bool = True, # <--- ДОБАВЛЕНО
) -> None:
    user_id = callback.from_user.id
    try:
        async with db_manager.connect() as db:
            async with db.execute("SELECT * FROM anketas WHERE id = ?", (anketa_id,)) as cursor:
                profile = await cursor.fetchone()
            if not profile:
                await _show_error(callback, "❌ Анкета не найдена или была удалена.")
                return
            async with db.execute(
                "SELECT 1 FROM favorites WHERE user_id = ? AND anketa_id = ?",
                (user_id, anketa_id),
            ) as cursor:
                is_fav = await cursor.fetchone() is not None
    except Exception:
        logger.exception("Ошибка загрузки анкеты %s для %s", anketa_id, user_id)
        await _show_error(callback, "❌ Не удалось загрузить анкету.")
        return

    # Отправляем уведомление админам ТОЛЬКО если notify_admins=True
    if notify_admins:
        await _notify_admins_view(bot, user_id, callback.from_user.username, profile)

    caption = _build_anketa_caption(profile)
    markup = anketa_kb(
        anketa_id=anketa_id,
        page=page,
        sort_type=sort_type,
        outcall_only=outcall_only,
        is_fav=is_fav,
        source=source,
    )
    photo_id = profile["photo_id"]
    if not callback.message:
        return
    try:
        if photo_id:
            await callback.message.edit_media(
                media=InputMediaPhoto(media=photo_id, caption=caption, parse_mode="HTML"),
                reply_markup=markup,
            )
        else:
            await callback.message.edit_text(text=caption, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        error = str(exc).lower()
        if "message is not modified" in error:
            return
        logger.debug("Не удалось отредактировать карточку %s: %s", anketa_id, exc)
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        try:
            if photo_id:
                await callback.message.answer_photo(photo=photo_id, caption=caption, reply_markup=markup, parse_mode="HTML")
            else:
                await callback.message.answer(caption, reply_markup=markup, parse_mode="HTML")
        except TelegramAPIError:
            logger.exception("Telegram API не отправил карточку анкеты %s", anketa_id)
        except Exception:
            logger.exception("Неожиданная ошибка отправки карточки анкеты %s", anketa_id)


@router.callback_query(F.data.startswith("view:"))
async def view_anketa(callback: CallbackQuery, bot: Bot) -> None:
    await _safe_answer(callback)
    if not callback.data:
        return
    parts = callback.data.split(":")
    if len(parts) < 2:
        return
    anketa_id = _safe_int(parts[1], default=-1, minimum=1)
    if anketa_id < 1:
        return
    page, sort_type, outcall, source = _parse_catalog_context(parts)
    await _render_anketa(
        callback=callback,
        anketa_id=anketa_id,
        page=page,
        sort_type=sort_type,
        outcall_only=outcall,
        source=source,
        bot=bot,
        notify_admins=True, # Явно указываем
    )


# ============================================================
# ПОИСК ПО КОДУ
# ============================================================

@router.callback_query(F.data == "search_by_code")
async def search_by_code_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(callback)
    if not callback.message:
        return
    await state.set_state(SearchCode.waiting_code)
    await callback.message.answer(
        "⌨️ Введите 4-значный номер (код) анкеты.\n"
        "Например: <code>4829</code>",
        parse_mode="HTML",
    )


@router.message(SearchCode.waiting_code, F.text)
async def search_by_code_process(message: Message, state: FSMContext, bot: Bot) -> None:
    code = message.text.strip()

    # ИСПРАВЛЕНО: добавлена проверка isascii()
    if not code.isascii() or not code.isdigit() or len(code) != 4:
        await message.answer("❌ Код должен состоять ровно из 4 цифр. Попробуйте еще раз или нажмите /start.")
        return

    user_id = message.from_user.id
    username = message.from_user.username

    # ИСПРАВЛЕНО: обработка ошибок БД, state.clear() после успеха
    try:
        async with db_manager.connect() as db:
            async with db.execute("SELECT * FROM anketas WHERE code = ?", (code,)) as cur:
                profile = await cur.fetchone()

            if profile:
                async with db.execute(
                    "SELECT 1 FROM favorites WHERE user_id = ? AND anketa_id = ?",
                    (user_id, profile["id"]),
                ) as cur:
                    is_fav = await cur.fetchone() is not None
            else:
                is_fav = False
    except Exception:
        logger.exception("Ошибка БД при поиске по коду %s", code)
        await message.answer("❌ Поиск временно недоступен. Попробуйте позже.")
        return

    await state.clear()

    if not profile:
        await message.answer(f"❌ Анкета с кодом <b>{html.escape(code)}</b> не найдена.\nПроверьте номер и попробуйте снова.", parse_mode="HTML")
        return

    # Отправляем уведомление админам
    await _notify_admins_view(bot, user_id, username, profile)

    # Так как это не callback, а сообщение, отправляем фото/текст напрямую
    caption = _build_anketa_caption(profile)
    markup = anketa_kb(
        anketa_id=profile["id"],
        page=0,
        sort_type="default",
        outcall_only=0,
        is_fav=is_fav, # ИСПРАВЛЕНО: передаем реальный статус
        source="catalog",
    )

    photo_id = profile["photo_id"]
    try:
        if photo_id:
            await message.answer_photo(photo=photo_id, caption=caption, reply_markup=markup, parse_mode="HTML")
        else:
            await message.answer(caption, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        logger.exception("Не удалось отправить найденную анкету %s пользователю %s", profile["id"], user_id)
        await message.answer("❌ Не удалось показать анкету. Попробуйте позже.")


# ============================================================
# FAVORITES
# ============================================================

@router.callback_query(F.data.startswith("fav:"))
async def toggle_favorite(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.data:
        await _safe_answer(callback)
        return
    parts = callback.data.split(":")
    if len(parts) < 3:
        await _safe_answer(callback)
        return
    action = parts[1]
    if action not in VALID_FAVORITE_ACTIONS:
        await _safe_answer(callback, "❌ Некорректное действие.", show_alert=True)
        return
    anketa_id = _safe_int(parts[2], default=-1, minimum=1)
    if anketa_id < 1:
        await _safe_answer(callback)
        return
    page, sort_type, outcall, source = _parse_catalog_context(["view", anketa_id] + parts[3:])
    user_id = callback.from_user.id

    try:
        async with db_manager.transaction() as db:
            if action == "add":
                async with db.execute("SELECT 1 FROM anketas WHERE id = ?", (anketa_id,)) as cursor:
                    exists = await cursor.fetchone()
                if not exists:
                    await _safe_answer(callback, "Анкета больше не существует.", show_alert=True)
                    return
                await db.execute(
                    "INSERT OR IGNORE INTO favorites (user_id, anketa_id) VALUES (?, ?)",
                    (user_id, anketa_id),
                )
            else:
                await db.execute(
                    "DELETE FROM favorites WHERE user_id = ? AND anketa_id = ?",
                    (user_id, anketa_id),
                )
    except Exception:
        logger.exception("Ошибка изменения избранного: user=%s anketa=%s", user_id, anketa_id)
        await _safe_answer(callback, "❌ Не удалось изменить избранное.", show_alert=True)
        return

    await _safe_answer(callback, "❤️ Добавлено в избранное." if action == "add" else "💔 Удалено из избранного.")

    # ИСПРАВЛЕНО: передаем notify_admins=False
    await _render_anketa(
        callback=callback,
        anketa_id=anketa_id,
        page=page,
        sort_type=sort_type,
        outcall_only=outcall,
        source=source,
        bot=bot,
        notify_admins=False,
    )


@router.callback_query(F.data.startswith("favs:"))
async def show_favorites(callback: CallbackQuery) -> None:
    await _safe_answer(callback)
    if not callback.data or not callback.message:
        return
    parts = callback.data.split(":")
    page = _safe_int(parts[1] if len(parts) > 1 else 0, minimum=0)
    user_id = callback.from_user.id

    try:
        async with db_manager.connect() as db:
            async with db.execute(
                """
                SELECT a.id, a.code, a.name, a.age, a.city, a.price_1h, a.price_3h, a.price_night, a.outcall
                FROM anketas AS a
                INNER JOIN favorites AS f ON f.anketa_id = a.id
                WHERE f.user_id = ?
                ORDER BY f.rowid DESC
                """,
                (user_id,),
            ) as cursor:
                all_favs = await cursor.fetchall()
    except Exception:
        logger.exception("Ошибка загрузки избранного пользователя %s", user_id)
        await _show_error(callback, "❌ Не удалось загрузить избранное.")
        return

    total = len(all_favs)
    if not all_favs:
        builder = InlineKeyboardBuilder()
        builder.button(text="🏠 Главное меню", callback_data="menu:main")
        await _edit_or_send_text(callback.message, "❤️ <b>Избранное</b>\n\nВаш список избранного пока пуст.", builder.as_markup())
        return

    total_pages = max(1, (total + FAVS_PAGE_SIZE - 1) // FAVS_PAGE_SIZE)
    page = min(page, total_pages - 1)
    start = page * FAVS_PAGE_SIZE
    page_items = all_favs[start:start + FAVS_PAGE_SIZE]

    title = (
        f"❤️ <b>Ваше избранное</b>\n"
        f"📄 Страница {page + 1} из {total_pages}\n"
        f"📊 Всего анкет: {total}"
    )
    await _edit_or_send_text(callback.message, title, _favs_kb(page_items, page, total_pages))
