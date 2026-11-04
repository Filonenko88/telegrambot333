from collections.abc import Mapping, Sequence
from typing import Any

from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


CALLBACK_DATA_MAX_BYTES = 64
EXTRAS_PAGE_SIZE = 6
SESSION_CALLBACK_LENGTH = 16
PRICE_SUFFIX = "₽"


# ============================================================
# VALIDATION HELPERS
# ============================================================

def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} должен быть положительным числом.")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} должен быть неотрицательным числом.")
    return value


def _safe_token(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} должен быть непустой строкой.")
    if ":" in value:
        raise ValueError(f"{name} не должен содержать ':'.")
    return value


def _safe_session(session: str) -> str:
    session = _safe_token(session, "session")
    if len(session) != SESSION_CALLBACK_LENGTH:
        raise ValueError(f"session должен быть длины {SESSION_CALLBACK_LENGTH}.")
    # ИСПРАВЛЕНО: добавлена проверка isascii()
    if not session.isascii() or not session.isalnum():
        raise ValueError("session должен содержать только ASCII-буквы и цифры.")
    return session


def _add_button(builder: InlineKeyboardBuilder, text: str, callback_data: str) -> None:
    if len(callback_data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
        raise ValueError(f"callback_data превышает 64 байта: {callback_data!r}")
    builder.button(text=str(text), callback_data=callback_data)


def _get_price(value: Any, name: str = "price") -> int:
    return _non_negative_int(value, name)


def _get_page(page: int, max_pages: int) -> tuple[int, int]:
    _non_negative_int(page, "page")
    _non_negative_int(max_pages, "max_pages")
    max_pages = max(1, max_pages)
    page = min(page, max_pages - 1)
    return page, max_pages


def _validate_outcall(value: int) -> int:
    # ИСПРАВЛЕНО: добавлена проверка на bool
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise ValueError("outcall_only должен быть целым числом 0 или 1.")
    return value


# ============================================================
# MAIN MENU / CITY
# ============================================================

def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "🔎 Каталог", "catalog:0:default:0")
    _add_button(builder, "🔍 Найти по номеру", "search_by_code")
    _add_button(builder, "👤 Мой профиль", "profile")
    _add_button(builder, "❤️ Избранное", "favs:0")
    _add_button(builder, "📋 Мои заявки", "my_reqs")
    _add_button(builder, "ℹ️ Информация", "menu:info")
    _add_button(builder, "💬 Поддержка", "support")

    if is_admin:
        _add_button(builder, "👑 Админ-панель", "admin:panel")
        builder.adjust(2, 2, 2, 1)
    else:
        builder.adjust(2, 2, 2)
    return builder.as_markup()


def city_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геопозицию", request_location=True)],
            [KeyboardButton(text="✏️ Ввести город вручную")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ============================================================
# CATALOG
# ============================================================

def catalog_kb(anketas: Sequence[Mapping[str, Any]], page: int, max_pages: int, sort_type: str = "default", outcall_only: int = 0) -> InlineKeyboardMarkup:
    page, max_pages = _get_page(page, max_pages)
    outcall_only = _validate_outcall(outcall_only)
    sort_type = _safe_token(sort_type, "sort_type")

    builder = InlineKeyboardBuilder()

    for anketa in anketas:
        anketa_id = _positive_int(anketa["id"], "anketa_id")
        name = str(anketa["name"] or "").strip()
        age = _positive_int(anketa["age"], "age")
        price = _get_price(anketa["price_1h"], "price_1h")

        try:
            code = anketa["code"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"У анкеты #{anketa_id} отсутствует код.") from exc
        if (
            not isinstance(code, str)
            or len(code) != 4
            or not code.isascii()
            or not code.isdigit()
        ):
            raise ValueError(f"У анкеты #{anketa_id} должен быть корректный четырёхзначный код.")

        if not name:
            raise ValueError("Имя анкеты не может быть пустым.")

        btn_text = f"[{code}] {name}, {age} лет — от {price}{PRICE_SUFFIX}"
        _add_button(builder, btn_text, f"view:{anketa_id}:{page}:{sort_type}:{outcall_only}")

    sort_text = (
        "💰 По цене ↓"
        if sort_type == "price_desc"
        else "💰 По цене ↑"
    )
    next_sort = "price_desc" if sort_type == "price_asc" else "price_asc"
    _add_button(builder, sort_text, f"catalog:{page}:{next_sort}:{outcall_only}")

    outcall_text = "🏡 Все" if outcall_only else "🚗 Только с выездом"
    next_outcall = 0 if outcall_only else 1
    _add_button(builder, outcall_text, f"catalog:{page}:{sort_type}:{next_outcall}")

    nav_count = 0
    if page > 0:
        _add_button(builder, "◀️", f"catalog:{page - 1}:{sort_type}:{outcall_only}")
        nav_count += 1
    if page < max_pages - 1:
        _add_button(builder, "▶️", f"catalog:{page + 1}:{sort_type}:{outcall_only}")
        nav_count += 1

    _add_button(builder, "🏠 Главное меню", "menu:main")

    layout = [1] * len(anketas) + [2]
    if nav_count:
        layout.append(nav_count)
    layout.append(1)
    builder.adjust(*layout)
    return builder.as_markup()


def anketa_kb(anketa_id: int, page: int, sort_type: str, outcall_only: int, is_fav: bool = False, source: str = "catalog") -> InlineKeyboardMarkup:
    anketa_id = _positive_int(anketa_id, "anketa_id")
    page = _non_negative_int(page, "page")
    sort_type = _safe_token(sort_type, "sort_type")
    outcall_only = _validate_outcall(outcall_only)
    source = _safe_token(source, "source")
    if source not in ("catalog", "favs"):
        source = "catalog"

    builder = InlineKeyboardBuilder()
    fav_text = "💔 Убрать из избранного" if is_fav else "❤️ В избранное"
    fav_action = "del" if is_fav else "add"
    _add_button(builder, fav_text, f"fav:{fav_action}:{anketa_id}:{page}:{sort_type}:{outcall_only}:{source}")
    _add_button(builder, "✅ Оформить заявку", f"req:start:{anketa_id}")

    if source == "favs":
        back_data = f"favs:{page}"
    else:
        back_data = f"catalog:{page}:{sort_type}:{outcall_only}"
    _add_button(builder, "⬅️ Назад", back_data)
    builder.adjust(1)
    return builder.as_markup()


# ============================================================
# REQUEST (DURATION / EXTRAS / PAYMENT)
# ============================================================

def req_duration_kb(anketa_id: int, session: str, price_1h: int, price_3h: int, price_night: int) -> InlineKeyboardMarkup:
    anketa_id = _positive_int(anketa_id, "anketa_id")
    session = _safe_session(session)
    price_1h = _get_price(price_1h, "price_1h")
    price_3h = _get_price(price_3h, "price_3h")
    price_night = _get_price(price_night, "price_night")

    builder = InlineKeyboardBuilder()
    _add_button(builder, f"⏱️ 1 час — {price_1h}{PRICE_SUFFIX}", f"req_dur:{anketa_id}:1h:{session}")
    _add_button(builder, f"⏱️ 3 часа — {price_3h}{PRICE_SUFFIX}", f"req_dur:{anketa_id}:3h:{session}")
    _add_button(builder, f"🌙 Ночь — {price_night}{PRICE_SUFFIX}", f"req_dur:{anketa_id}:night:{session}")
    _add_button(builder, "⬅️ Отмена", f"view:{anketa_id}:0:default:0")
    builder.adjust(1)
    return builder.as_markup()


def req_extras_kb(anketa_id: int, srv_key: str, session: str, services: Sequence[Mapping[str, Any]], selected_ids: Sequence[int], page: int, total_price: int) -> InlineKeyboardMarkup:
    anketa_id = _positive_int(anketa_id, "anketa_id")
    srv_key = _safe_token(srv_key, "srv_key")
    session = _safe_session(session)
    _non_negative_int(page, "page")
    total_price = _get_price(total_price, "total_price")
    if srv_key not in ("1h", "3h", "night"):
        raise ValueError("Некорректный тариф.")

    selected = {_positive_int(sid, "service_id") for sid in selected_ids}
    total_pages = max(1, (len(services) + EXTRAS_PAGE_SIZE - 1) // EXTRAS_PAGE_SIZE)
    page, total_pages = _get_page(page, total_pages)
    start = page * EXTRAS_PAGE_SIZE
    page_services = services[start:start + EXTRAS_PAGE_SIZE]

    builder = InlineKeyboardBuilder()
    for service in page_services:
        service_id = _positive_int(service["id"], "service_id")
        name = str(service["name"] or "").strip()
        price = _get_price(service["price"])
        if not name:
            raise ValueError("Название услуги не может быть пустым.")
        mark = "✅" if service_id in selected else "▫️"
        _add_button(builder, f"{mark} {name} — {price}{PRICE_SUFFIX}", f"req_extra:{anketa_id}:{srv_key}:{service_id}:{page}:{session}")

    nav_count = 0
    if page > 0:
        _add_button(builder, "◀️ Допы", f"req_extras_page:{anketa_id}:{srv_key}:{page - 1}:{session}")
        nav_count += 1
    if page < total_pages - 1:
        _add_button(builder, "Допы ▶️", f"req_extras_page:{anketa_id}:{srv_key}:{page + 1}:{session}")
        nav_count += 1

    if services:
        _add_button(builder, "🗑 Сбросить всё", f"req_extras_reset:{anketa_id}:{srv_key}:{session}")

    _add_button(builder, f"✅ Продолжить (Итого: {total_price}{PRICE_SUFFIX})", f"req_done:{anketa_id}:{srv_key}:{session}")
    _add_button(builder, "⬅️ Назад", f"req_back_dur:{anketa_id}:{session}")

    layout = [1] * len(page_services)
    if nav_count:
        layout.append(nav_count)
    if services:
        layout.append(1)
    layout.extend([1, 1])
    builder.adjust(*layout)
    return builder.as_markup()


def req_payment_kb(anketa_id: int, srv_key: str, session: str) -> InlineKeyboardMarkup:
    anketa_id = _positive_int(anketa_id, "anketa_id")
    srv_key = _safe_token(srv_key, "srv_key")
    session = _safe_session(session)
    if srv_key not in ("1h", "3h", "night"):
        raise ValueError("Некорректный тариф.")

    builder = InlineKeyboardBuilder()
    _add_button(builder, "🎟 Ввести промокод", f"req_promo:{anketa_id}:{srv_key}:{session}")

    _add_button(builder, "💵 Наличные", f"req_pay:{anketa_id}:{srv_key}:cash:{session}")
    _add_button(builder, "💳 Карта", f"req_pay:{anketa_id}:{srv_key}:card:{session}")
    _add_button(builder, "🪙 Криптовалюта", f"req_pay:{anketa_id}:{srv_key}:crypto:{session}")
    _add_button(builder, "⬅️ Назад", f"req_extras_page:{anketa_id}:{srv_key}:0:{session}")
    builder.adjust(1)
    return builder.as_markup()


# ============================================================
# PROFILE
# ============================================================

def profile_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "📍 Изменить город", "prof:city")
    _add_button(builder, "🏠 В меню", "menu:main")
    builder.adjust(1)
    return builder.as_markup()


# ============================================================
# ADMIN PANEL & SUBMENUS
# ============================================================

def admin_panel_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "📊 Статистика", "admin:stats")
    _add_button(builder, "➕ Добавить анкету", "admin:add_anketa")
    _add_button(builder, "📋 Все заявки", "admin:reqs:0")
    _add_button(builder, "🖼 Фото", "admin:photos")
    _add_button(builder, "📝 Тексты", "admin:texts")
    _add_button(builder, "🎟 Промокоды", "admin:promos")
    _add_button(builder, "📢 Рассылка", "admin:broadcast")
    _add_button(builder, "🚫 Бан / Разбан", "admin:ban_menu")
    _add_button(builder, "🧹 Очистить историю", "admin:clear_menu")
    _add_button(builder, "📥 Экспорт CSV", "admin:export")
    _add_button(builder, "🏠 В меню", "menu:main")
    builder.adjust(2, 2, 2, 2, 1, 1)
    return builder.as_markup()


def admin_promos_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "➕ Создать промокод", "admin:promo:add")
    _add_button(builder, "📋 Список промокодов", "admin:promo:list")
    _add_button(builder, "⬅️ Назад", "admin:panel")
    builder.adjust(1)
    return builder.as_markup()


def admin_texts_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "👋 Приветствие", "admin:text:welcome_text")
    _add_button(builder, "🏠 Меню", "admin:text:main_menu_text")
    _add_button(builder, "ℹ️ Информация", "admin:text:info_text")
    _add_button(builder, "🔗 Ссылка на правила", "admin:text:rules_url")
    _add_button(builder, "🔗 Ссылка на соглашение", "admin:text:terms_url")
    _add_button(builder, "💰 Дефолт: 1 час", "admin:text:default_price_1h")
    _add_button(builder, "💰 Дефолт: 3 часа", "admin:text:default_price_3h")
    _add_button(builder, "💰 Дефолт: ночь", "admin:text:default_price_night")
    _add_button(builder, "⬅️ Назад", "admin:panel")
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()


def admin_photos_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "👋 Приветственное", "admin:photo:welcome")
    _add_button(builder, "🏠 Фото меню", "admin:photo:menu")
    _add_button(builder, "🗑 Удалить приветствие", "admin:photo:del_welcome")
    _add_button(builder, "🗑 Удалить фото меню", "admin:photo:del_menu")
    _add_button(builder, "⬅️ Назад", "admin:panel")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def admin_broadcast_filter_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "🌍 Всем активным", "bcast:all")
    _add_button(builder, "📋 С заявками", "bcast:with_reqs")
    _add_button(builder, "👤 Без заявок", "bcast:no_reqs")
    _add_button(builder, "⬅️ Отмена", "admin:panel")
    builder.adjust(1)
    return builder.as_markup()


def admin_ban_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "🚫 Забанить", "admin:do_ban")
    _add_button(builder, "✅ Разбанить", "admin:do_unban")
    _add_button(builder, "⬅️ Назад", "admin:panel")
    builder.adjust(2, 1)
    return builder.as_markup()


def admin_clear_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add_button(builder, "🧹 Очистить", "admin:do_clear")
    _add_button(builder, "⬅️ Назад", "admin:panel")
    builder.adjust(1)
    return builder.as_markup()


def admin_services_kb(anketa_id: int, all_services: Sequence[Mapping[str, Any]], selected_services: Sequence[Mapping[str, Any]]) -> InlineKeyboardMarkup:
    anketa_id = _positive_int(anketa_id, "anketa_id")
    builder = InlineKeyboardBuilder()
    selected_ids = set()
    for service in selected_services:
        service_id = _positive_int(service["service_id"], "service_id")
        selected_ids.add(service_id)

    for service in all_services:
        service_id = _positive_int(service["id"], "service_id")
        name = str(service["name"] or "").strip()
        default_price = _get_price(service["default_price"], "default_price")
        if not name:
            raise ValueError("Название услуги не может быть пустым.")
        if service_id in selected_ids:
            text = f"✅ {name} ({default_price}{PRICE_SUFFIX})"
        else:
            text = f"▫️ {name} ({default_price}{PRICE_SUFFIX})"
        _add_button(builder, text, f"admin_srv_tgl:{anketa_id}:{service_id}")

    _add_button(builder, "✅ Сохранить и продолжить", f"admin_srv_done:{anketa_id}")
    builder.adjust(1)
    return builder.as_markup()
