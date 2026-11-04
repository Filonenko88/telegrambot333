import asyncio
import csv
import html
import io
import logging
import re
import sqlite3
import unicodedata
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ALL_STATUSES, ADMIN_IDS
from database import db_manager
from keyboards import (
    admin_ban_kb,
    admin_broadcast_filter_kb,
    admin_clear_kb,
    admin_panel_kb,
    admin_photos_kb,
    admin_promos_kb,
    admin_services_kb,
    admin_texts_kb,
    main_menu_kb,
)
from middlewares import IsAdmin
from services import settings as bot_settings
from services.anketa import create_anketa


logger = logging.getLogger(__name__)
router = Router(name="admin")

STATUS_CODES = dict(zip(ALL_STATUSES, "wpidc", strict=True))
STATUS_BY_CODE = {code: status for status, code in STATUS_CODES.items()}
ADMIN_STATUS_TARGETS = ALL_STATUSES[1:]


def _csv_safe_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    first_content = next(
        (
            character
            for character in value
            if not character.isspace()
            and not unicodedata.category(character).startswith("C")
        ),
        "",
    )
    if first_content in ("=", "+", "-", "@"):
        return "'" + value
    return value


# ============================================================
# FSM
# ============================================================

class AddAnketa(StatesGroup):
    name = State()
    age = State()
    city = State()
    bio = State()
    price_1h = State()
    price_3h = State()
    price_night = State()
    photo = State()
    services = State()


class BanActions(StatesGroup):
    waiting_ban_id = State()
    waiting_unban_id = State()


class ClearActions(StatesGroup):
    waiting_id = State()


class BroadcastActions(StatesGroup):
    choose_target = State()
    waiting_text = State()


class ReplySupport(StatesGroup):
    waiting = State()


class SettingsActions(StatesGroup):
    waiting_welcome_photo = State()
    waiting_menu_photo = State()


class EditText(StatesGroup):
    waiting_key = State()
    waiting_text = State()


class PromoActions(StatesGroup):
    waiting_code = State()
    waiting_discount = State()


# ============================================================
# HELPERS
# ============================================================

async def _safe_answer(cb: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        await cb.answer(text=text, show_alert=alert)
    except TelegramBadRequest:
        pass


async def _safe_edit(message, text: str, markup=None) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await message.answer(text=text, reply_markup=markup, parse_mode="HTML")


# ============================================================
# ГЛАВНАЯ ПАНЕЛЬ
# ============================================================

@router.callback_query(F.data == "admin:panel", IsAdmin())
async def admin_panel(cb: CallbackQuery) -> None:
    await _safe_answer(cb)
    await _safe_edit(cb.message, "👑 <b>Админ-панель</b>", admin_panel_kb())


# ============================================================
# СТАТИСТИКА
# ============================================================

@router.callback_query(F.data == "admin:stats", IsAdmin())
async def admin_stats(cb: CallbackQuery) -> None:
    await _safe_answer(cb)
    async with db_manager.connect() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            users_total = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1") as cur:
            users_banned = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM requests") as cur:
            req_row = await cur.fetchone()
            reqs_total, reqs_sum = req_row[0], req_row[1]
        async with db.execute("SELECT COUNT(*) FROM requests WHERE status='Ожидает реквизиты'") as cur:
            reqs_pending = (await cur.fetchone())[0]

    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей: <b>{users_total}</b> (из них забанено: {users_banned})\n\n"
        f"📋 Заявок всего: <b>{reqs_total}</b>\n"
        f"💰 Общая сумма заявок: <b>{reqs_sum:,} ₽</b>\n"
        f"🕐 Ожидают реквизиты: <b>{reqs_pending}</b>"
    ).replace(",", " ")

    builder = InlineKeyboardBuilder().button(text="⬅️ Назад", callback_data="admin:panel")
    await _safe_edit(cb.message, text, builder.as_markup())


# ============================================================
# ЭКСПОРТ В CSV
# ============================================================

@router.callback_query(F.data == "admin:export", IsAdmin())
async def admin_export(cb: CallbackQuery) -> None:
    await _safe_answer(cb, "Генерирую файл...")

    async with db_manager.connect() as db:
        async with db.execute("SELECT id, username, first_name, city, registered_at FROM users") as cur:
            users = await cur.fetchall()

    if not users:
        await cb.message.answer("❌ Нет данных для экспорта.")
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ID", "Username", "First Name", "City", "Registered At"])
    for u in users:
        writer.writerow(
            [
                u["id"],
                *(
                    _csv_safe_text(u[column])
                    for column in ("username", "first_name", "city", "registered_at")
                ),
            ]
        )

    output.seek(0)
    file = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="users_export.csv")

    await cb.message.answer_document(file, caption="📥 <b>База пользователей</b>", parse_mode="HTML")


# ============================================================
# ТЕКСТЫ (CMS)
# ============================================================

@router.callback_query(F.data == "admin:texts", IsAdmin())
async def admin_texts(cb: CallbackQuery) -> None:
    await _safe_answer(cb)
    await _safe_edit(
        cb.message,
        "📝 <b>Управление текстами</b>\n\n"
        "Выберите, что хотите изменить. Для HTML используйте теги <b>, <i>, <a href='...'>.",
        admin_texts_kb(),
    )


@router.callback_query(F.data.startswith("admin:text:"), IsAdmin())
async def edit_text_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(cb)
    key = cb.data.split(":")[2]
    current = await bot_settings.get_text(key, "Пусто")

    await state.update_data(text_key=key)
    await state.set_state(EditText.waiting_text)

    await cb.message.answer(
        f"Текущее значение для <code>{key}</code>:\n\n"
        f"{html.escape(current)}\n\n"
        "Отправьте новый текст или /cancel для отмены:"
    )


@router.message(EditText.waiting_text, F.text, IsAdmin())
async def edit_text_save(msg: Message, state: FSMContext) -> None:
    if msg.text == "/cancel":
        await state.clear()
        await msg.answer("Отменено.", reply_markup=main_menu_kb(True))
        return

    key = (await state.get_data()).get("text_key")
    await bot_settings.set_setting(key, msg.text)
    await state.clear()
    await msg.answer(f"✅ Текст <code>{key}</code> обновлён!", reply_markup=main_menu_kb(True))


# ============================================================
# ФОТО
# ============================================================

# ============================================================
# ПРОМОКОДЫ
# ============================================================

async def _render_promos(message) -> None:
    async with db_manager.connect() as db:
        async with db.execute(
            "SELECT id, code, discount_percent, is_active FROM promocodes ORDER BY id DESC LIMIT 20"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        text = "🎟 <b>Промокоды</b>\n\nПромокоды ещё не созданы."
    else:
        lines = ["🎟 <b>Промокоды</b>"]
        for row in rows:
            status = "✅ активен" if row["is_active"] else "⛔ отключён"
            lines.append(
                f"• <code>{html.escape(str(row['code']))}</code> — {row['discount_percent']}% ({status})"
            )
        text = "\n".join(lines)

    await _safe_edit(message, text, admin_promos_kb())


@router.callback_query(F.data == "admin:promos", IsAdmin())
async def admin_promos(cb: CallbackQuery) -> None:
    await _safe_answer(cb)
    await _render_promos(cb.message)


@router.callback_query(F.data == "admin:promo:add", IsAdmin())
async def admin_promo_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(cb)
    await state.set_state(PromoActions.waiting_code)
    await cb.message.answer("🎟 Введите код промокода (латиница, цифры, дефис):")


@router.message(PromoActions.waiting_code, F.text, IsAdmin())
async def admin_promo_add_code(msg: Message, state: FSMContext) -> None:
    code = (msg.text or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{3,20}", code):
        await msg.answer("❌ Некорректный код. Используйте 3–20 символов: буквы, цифры или дефис.")
        return

    await state.update_data(promo_code=code)
    await state.set_state(PromoActions.waiting_discount)
    await msg.answer("💰 Введите процент скидки от 1 до 100:")


@router.message(PromoActions.waiting_discount, F.text, IsAdmin())
async def admin_promo_add_discount(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    try:
        discount = int(text)
    except ValueError:
        await msg.answer("❌ Укажите целое число от 1 до 100.")
        return

    if not 1 <= discount <= 100:
        await msg.answer("❌ Процент должен быть от 1 до 100.")
        return

    data = await state.get_data()
    code = data.get("promo_code")
    if not code:
        await state.clear()
        await msg.answer("❌ Сначала введите код промокода.")
        return

    async with db_manager.transaction() as db:
        await db.execute(
            "INSERT INTO promocodes (code, discount_percent, is_active) VALUES (?, ?, 1) "
            "ON CONFLICT(code) DO UPDATE SET discount_percent = excluded.discount_percent, is_active = 1",
            (code, discount),
        )

    await state.clear()
    await msg.answer(
        f"✅ Промокод <code>{html.escape(code)}</code> сохранён со скидкой {discount}%.",
        reply_markup=main_menu_kb(True),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:promo:list", IsAdmin())
async def admin_promo_list(cb: CallbackQuery) -> None:
    await _safe_answer(cb)
    await _render_promos(cb.message)


# ============================================================
# ФОТО
# ============================================================

@router.callback_query(F.data == "admin:photos", IsAdmin())
async def admin_photos(cb: CallbackQuery) -> None:
    await _safe_answer(cb)
    await _safe_edit(cb.message, "🖼 <b>Управление фото</b>", admin_photos_kb())


@router.callback_query(F.data.startswith("admin:photo:"), IsAdmin())
async def photo_action(cb: CallbackQuery, state: FSMContext) -> None:
    action = cb.data.split(":")[2]

    if action == "welcome":
        await state.set_state(SettingsActions.waiting_welcome_photo)
        photo = await bot_settings.get_photo("welcome_photo")
        if photo:
            await cb.message.answer_photo(photo, caption="Текущее приветствие. Отправьте новое фото:")
        else:
            await cb.message.answer("Сейчас фото нет. Отправьте новое фото:")

    elif action == "menu":
        await state.set_state(SettingsActions.waiting_menu_photo)
        photo = await bot_settings.get_photo("main_menu_photo")
        if photo:
            await cb.message.answer_photo(photo, caption="Текущее фото меню. Отправьте новое фото:")
        else:
            await cb.message.answer("Сейчас фото нет. Отправьте новое фото:")

    elif action == "del_welcome":
        await bot_settings.delete_setting("welcome_photo")
        await _safe_answer(cb, "🗑 Удалено", alert=True)
    elif action == "del_menu":
        await bot_settings.delete_setting("main_menu_photo")
        await _safe_answer(cb, "🗑 Удалено", alert=True)


@router.message(SettingsActions.waiting_welcome_photo, F.photo, IsAdmin())
async def save_welcome_photo(msg: Message, state: FSMContext) -> None:
    file_id = msg.photo[-1].file_id
    await bot_settings.set_setting("welcome_photo", file_id)
    await state.clear()
    await msg.answer("✅ Приветственная картинка обновлена!", reply_markup=main_menu_kb(True))


@router.message(SettingsActions.waiting_menu_photo, F.photo, IsAdmin())
async def save_menu_photo(msg: Message, state: FSMContext) -> None:
    file_id = msg.photo[-1].file_id
    await bot_settings.set_setting("main_menu_photo", file_id)
    await state.clear()
    await msg.answer("✅ Картинка меню обновлена!", reply_markup=main_menu_kb(True))


# ============================================================
# ЗАЯВКИ (С ИСПРАВЛЕННЫМ РЕНДЕРОМ)
# ============================================================

@router.callback_query(F.data.startswith("admin:reqs:"), IsAdmin())
async def admin_requests(cb: CallbackQuery) -> None:
    await _safe_answer(cb)
    page = int(cb.data.split(":")[2])
    PAGE_SIZE = 5

    async with db_manager.connect() as db:
        async with db.execute("SELECT COUNT(*) FROM requests") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        builder = InlineKeyboardBuilder().button(text="⬅️ Назад", callback_data="admin:panel")
        await _safe_edit(cb.message, "📋 Заявок нет.", builder.as_markup())
        return

    max_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    builder = InlineKeyboardBuilder()
    for r in rows:
        builder.button(
            text=f"№{r['unique_id']} | {r['amount']}₽ | {r['status']}",
            callback_data=f"admin:req:{r['id']}",
        )
    nav = []
    if page > 0:
        nav.append(("◀️", f"admin:reqs:{page - 1}"))
    if page < max_pages - 1:
        nav.append(("▶️", f"admin:reqs:{page + 1}"))
    for t, c in nav:
        builder.button(text=t, callback_data=c)
    builder.button(text="🏠 В меню", callback_data="menu:main")

    layout = [1] * len(rows)
    if nav:
        layout.append(len(nav))
    layout.append(1)
    builder.adjust(*layout)

    await _safe_edit(cb.message, f"📋 <b>Заявки</b> (стр. {page + 1}/{max_pages})", builder.as_markup())


async def _render_req_detail(message, req_id: int) -> bool:
    """Вспомогательная функция для отрисовки карточки заявки (DRY)."""
    async with db_manager.connect() as db:
        async with db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)) as cur:
            req = await cur.fetchone()
        async with db.execute(
            """
            SELECT s.name, rs.price FROM request_services rs
            JOIN services s ON s.id = rs.service_id
            WHERE rs.request_id = ?
            """,
            (req_id,),
        ) as cur:
            extras = await cur.fetchall()

    if not req:
        return False

    text = (
        f"📋 <b>Заявка №{html.escape(req['unique_id'])}</b>\n\n"
        f"👤 ID: <code>{req['user_id']}</code>\n"
        f"💳 Оплата: {html.escape(req['payment_method'])}\n"
        f"⏱ Услуга: {html.escape(req['service'])}\n"
    )
    if extras:
        text += "\n<b>Допы:</b>\n"
        for e in extras:
            text += f"➕ {html.escape(e['name'])} — {e['price']}₽\n"
    text += f"\n💰 <b>Сумма: {req['amount']}₽</b>\n📊 Статус: <b>{html.escape(req['status'])}</b>"

    builder = InlineKeyboardBuilder()
    current_status_code = STATUS_CODES.get(req["status"])
    if current_status_code is not None:
        for status in ADMIN_STATUS_TARGETS:
            builder.button(
                text=f"→ {status}",
                callback_data=(
                    f"admin:req_status:{req_id}:"
                    f"{current_status_code}:{STATUS_CODES[status]}"
                ),
            )
    builder.button(text="⬅️ Назад", callback_data="admin:reqs:0")
    builder.adjust(2, 2, 1)

    await _safe_edit(message, text, builder.as_markup())
    return True


@router.callback_query(F.data.startswith("admin:req:"), IsAdmin())
async def admin_request_detail(cb: CallbackQuery) -> None:
    # Защита от конфликта callback_data (чтобы не перехватывал reqs: и req_status:)
    if cb.data.startswith("admin:reqs:") or cb.data.startswith("admin:req_status:"):
        return

    await _safe_answer(cb)
    req_id = int(cb.data.split(":")[2])

    found = await _render_req_detail(cb.message, req_id)
    if not found:
        await _safe_answer(cb, "Заявка не найдена", alert=True)


@router.callback_query(F.data.startswith("admin:req_status:"), IsAdmin())
async def admin_req_status(cb: CallbackQuery, bot: Bot) -> None:
    parts = (cb.data or "").split(":")
    if (
        len(parts) != 5
        or parts[0:2] != ["admin", "req_status"]
        or not parts[2].isdigit()
        or parts[3] not in STATUS_BY_CODE
        or parts[4] not in STATUS_BY_CODE
    ):
        await _safe_answer(cb, "Кнопка устарела. Откройте заявку снова.", alert=True)
        return

    req_id = int(parts[2])
    expected_status = STATUS_BY_CODE[parts[3]]
    new_status = STATUS_BY_CODE[parts[4]]
    if new_status not in ADMIN_STATUS_TARGETS:
        await _safe_answer(cb, "Некорректный статус.", alert=True)
        return

    response_text = None
    response_alert = False
    updated = False
    async with db_manager.transaction() as db:
        async with db.execute(
            "SELECT user_id, unique_id, status FROM requests WHERE id = ?",
            (req_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            response_text = "Заявка не найдена."
            response_alert = True
        elif row["status"] != expected_status:
            response_text = "Статус уже изменился. Карточка обновлена."
            response_alert = True
        elif new_status == expected_status:
            response_text = "Этот статус уже установлен."
        else:
            async with db.execute(
                """
                UPDATE requests
                SET status = ?
                WHERE id = ? AND status = ?
                """,
                (new_status, req_id, expected_status),
            ) as cursor:
                updated = cursor.rowcount == 1

    if response_text is not None:
        await _safe_answer(cb, response_text, alert=response_alert)
        if response_text != "Заявка не найдена.":
            await _render_req_detail(cb.message, req_id)
        return

    if not updated:
        await _safe_answer(cb, "Статус уже изменился. Карточка обновлена.", alert=True)
        await _render_req_detail(cb.message, req_id)
        return

    await _safe_answer(cb, f"✅ Статус обновлен: {new_status}")

    # Уведомляем клиента
    if row:
        try:
            b = InlineKeyboardBuilder().button(text="🏠 В меню", callback_data="menu:main")
            await bot.send_message(
                row["user_id"],
                f"🔔 Статус вашей заявки <b>№{html.escape(row['unique_id'])}</b> обновлён: <b>{new_status}</b>",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
        except Exception:
            logger.warning("Не удалось уведомить клиента %s", row["user_id"])

    # Перерисовываем карточку заявки на экране админа напрямую
    await _render_req_detail(cb.message, req_id)


# ============================================================
# ДОБАВЛЕНИЕ АНКЕТЫ
# ============================================================

@router.callback_query(F.data == "admin:add_anketa", IsAdmin())
async def add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(cb)
    await state.set_state(AddAnketa.name)
    await cb.message.answer("Шаг 1/8: имя анкеты:")


@router.message(AddAnketa.name, F.text, IsAdmin())
async def add_name(msg: Message, state: FSMContext) -> None:
    await state.update_data(name=msg.text)
    await state.set_state(AddAnketa.age)
    await msg.answer("Шаг 2/8: возраст (18+):")


@router.message(AddAnketa.age, F.text, IsAdmin())
async def add_age(msg: Message, state: FSMContext) -> None:
    if not msg.text.isdigit() or int(msg.text) < 18:
        await msg.answer("Введите число ≥ 18:")
        return
    await state.update_data(age=int(msg.text))
    await state.set_state(AddAnketa.city)
    await msg.answer("Шаг 3/8: город:")


@router.message(AddAnketa.city, F.text, IsAdmin())
async def add_city(msg: Message, state: FSMContext) -> None:
    await state.update_data(city=msg.text)
    await state.set_state(AddAnketa.bio)
    await msg.answer("Шаг 4/8: описание:")


@router.message(AddAnketa.bio, F.text, IsAdmin())
async def add_bio(msg: Message, state: FSMContext) -> None:
    await state.update_data(bio=msg.text)

    default_p1 = await bot_settings.get_int("default_price_1h", 8000)
    await state.set_state(AddAnketa.price_1h)
    await msg.answer(f"Шаг 5/8: цена за 1 час (₽). По умолчанию: {default_p1}")


@router.message(AddAnketa.price_1h, F.text, IsAdmin())
async def add_price_1h(msg: Message, state: FSMContext) -> None:
    if not msg.text.isdigit():
        await msg.answer("Только число:")
        return
    await state.update_data(price_1h=int(msg.text))

    default_p3 = await bot_settings.get_int("default_price_3h", 17000)
    await state.set_state(AddAnketa.price_3h)
    await msg.answer(f"Шаг 6/8: цена за 3 часа (₽). По умолчанию: {default_p3}")


@router.message(AddAnketa.price_3h, F.text, IsAdmin())
async def add_price_3h(msg: Message, state: FSMContext) -> None:
    if not msg.text.isdigit():
        await msg.answer("Только число:")
        return
    await state.update_data(price_3h=int(msg.text))

    default_pn = await bot_settings.get_int("default_price_night", 50000)
    await state.set_state(AddAnketa.price_night)
    await msg.answer(f"Шаг 7/8: цена за ночь (₽). По умолчанию: {default_pn}")


@router.message(AddAnketa.price_night, F.text, IsAdmin())
async def add_price_night(msg: Message, state: FSMContext) -> None:
    if not msg.text.isdigit():
        await msg.answer("Только число:")
        return
    await state.update_data(price_night=int(msg.text))
    await state.set_state(AddAnketa.photo)
    await msg.answer("Шаг 8/8: отправьте фото анкеты:")


@router.message(AddAnketa.photo, F.photo, IsAdmin())
async def add_photo(msg: Message, state: FSMContext) -> None:
    d = await state.get_data()
    file_id = msg.photo[-1].file_id

    try:
        anketa_id = await create_anketa(
            name=d["name"],
            age=d["age"],
            city=d["city"],
            bio=d["bio"],
            price_1h=d["price_1h"],
            price_3h=d["price_3h"],
            price_night=d["price_night"],
            photo_id=file_id,
        )
    except (ValueError, sqlite3.Error):
        logger.exception("Не удалось создать анкету из административного сценария.")
        await msg.answer(
            "❌ Не удалось сохранить анкету. Проверьте данные и повторите попытку."
        )
        return

    await state.update_data(anketa_id=anketa_id, selected_services=[])
    await state.set_state(AddAnketa.services)
    await _render_admin_services(msg, state)


async def _render_admin_services(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    anketa_id = data["anketa_id"]
    selected = data.get("selected_services", [])

    async with db_manager.connect() as db:
        async with db.execute("SELECT * FROM services ORDER BY id") as cur:
            services = await cur.fetchall()

    selected_list = [{"service_id": sid} for sid in selected]
    markup = admin_services_kb(anketa_id, services, selected_list)

    sent = await msg.answer(
        f"✅ Анкета #{anketa_id} сохранена.\n\n"
        "Теперь выберите допы для этой анкеты (цена — стандартная из справочника):",
        reply_markup=markup,
    )
    await state.update_data(services_message_id=sent.message_id)


@router.callback_query(AddAnketa.services, F.data.startswith("admin_srv_tgl:"), IsAdmin())
async def toggle_service(cb: CallbackQuery, state: FSMContext) -> None:
    if not cb.data or cb.message is None:
        await _safe_answer(cb)
        return
    try:
        _, raw_anketa_id, raw_service_id = cb.data.split(":")
        anketa_id = int(raw_anketa_id)
        service_id = int(raw_service_id)
    except (ValueError, AttributeError):
        await _safe_answer(cb, "Некорректная кнопка.", alert=True)
        return

    data = await state.get_data()
    if (
        anketa_id != data.get("anketa_id")
        or data.get("services_message_id") != cb.message.message_id
        or await state.get_state() != AddAnketa.services.state
    ):
        await _safe_answer(
            cb, "Эта кнопка устарела. Текущая анкета не изменена.", alert=True
        )
        return

    async with db_manager.connect() as db:
        async with db.execute("SELECT 1 FROM anketas WHERE id = ?", (anketa_id,)) as cur:
            anketa_exists = await cur.fetchone() is not None
        async with db.execute("SELECT 1 FROM services WHERE id = ?", (service_id,)) as cur:
            service_exists = await cur.fetchone() is not None
    if not anketa_exists or not service_exists:
        await _safe_answer(cb, "Анкета или услуга больше не существует.", alert=True)
        return

    selected = list(data.get("selected_services", []))
    if any(not isinstance(item, int) or isinstance(item, bool) for item in selected):
        await _safe_answer(cb, "Список услуг повреждён; данные не изменены.", alert=True)
        return
    if service_id in selected:
        selected.remove(service_id)
    else:
        selected.append(service_id)
    await state.update_data(selected_services=selected)
    await _safe_answer(cb)

    async with db_manager.connect() as db:
        async with db.execute("SELECT * FROM services ORDER BY id") as cur:
            services = await cur.fetchall()

    selected_list = [{"service_id": sid} for sid in selected]
    markup = admin_services_kb(anketa_id, services, selected_list)
    try:
        await cb.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        pass


@router.callback_query(AddAnketa.services, F.data.startswith("admin_srv_done:"), IsAdmin())
async def save_services(cb: CallbackQuery, state: FSMContext) -> None:
    if not cb.data or cb.message is None:
        await _safe_answer(cb)
        return
    try:
        _, raw_anketa_id = cb.data.split(":")
        anketa_id = int(raw_anketa_id)
    except (ValueError, AttributeError):
        await _safe_answer(cb, "Некорректная кнопка.", alert=True)
        return

    data = await state.get_data()
    if (
        anketa_id != data.get("anketa_id")
        or data.get("services_message_id") != cb.message.message_id
        or await state.get_state() != AddAnketa.services.state
    ):
        await _safe_answer(cb, "Эта кнопка устарела. Ничего не сохранено.", alert=True)
        return

    selected = list(data.get("selected_services", []))
    if any(not isinstance(item, int) or isinstance(item, bool) for item in selected):
        await _safe_answer(cb, "Список услуг повреждён; ничего не сохранено.", alert=True)
        return

    async with db_manager.transaction() as db:
        async with db.execute("SELECT 1 FROM anketas WHERE id = ?", (anketa_id,)) as cur:
            if await cur.fetchone() is None:
                await _safe_answer(cb, "Анкета больше не существует.", alert=True)
                return
        if selected:
            placeholders = ", ".join("?" for _ in selected)
            async with db.execute(
                f"SELECT id FROM services WHERE id IN ({placeholders})",
                selected,
            ) as cur:
                available_services = {row["id"] for row in await cur.fetchall()}
            if available_services != set(selected):
                await _safe_answer(
                    cb, "Услуга больше не существует; ничего не сохранено.", alert=True
                )
                return
        for sid in selected:
            await db.execute(
                "INSERT OR IGNORE INTO anketa_services (anketa_id, service_id, custom_price) "
                "VALUES (?, ?, 0)",
                (anketa_id, sid),
            )

    await _safe_answer(cb)
    await state.clear()
    await cb.message.answer(
        f"✅ Анкета #{anketa_id} полностью готова! ({len(selected)} допов)",
        reply_markup=main_menu_kb(True),
    )


# ============================================================
# БАН
# ============================================================

@router.callback_query(F.data == "admin:ban_menu", IsAdmin())
async def ban_menu(cb: CallbackQuery) -> None:
    await _safe_answer(cb)
    await _safe_edit(cb.message, "🚫 Управление банами:", admin_ban_kb())


@router.callback_query(F.data == "admin:do_ban", IsAdmin())
async def ban_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(cb)
    await state.set_state(BanActions.waiting_ban_id)
    await cb.message.answer("Введите ID пользователя для бана:")


@router.message(BanActions.waiting_ban_id, F.text, IsAdmin())
async def ban_apply(msg: Message, state: FSMContext) -> None:
    if not msg.text.isdigit():
        await msg.answer("ID должен быть числом.")
        return
    uid = int(msg.text)
    async with db_manager.transaction() as db:
        cur = await db.execute("UPDATE users SET is_blocked = 1 WHERE id = ?", (uid,))
    await state.clear()
    if cur.rowcount:
        await msg.answer(f"✅ Забанен {uid}", reply_markup=main_menu_kb(True))
    else:
        await msg.answer(f"❌ ID {uid} не найден", reply_markup=main_menu_kb(True))


@router.callback_query(F.data == "admin:do_unban", IsAdmin())
async def unban_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(cb)
    await state.set_state(BanActions.waiting_unban_id)
    await cb.message.answer("Введите ID пользователя для разбана:")


@router.message(BanActions.waiting_unban_id, F.text, IsAdmin())
async def unban_apply(msg: Message, state: FSMContext) -> None:
    if not msg.text.isdigit():
        await msg.answer("ID должен быть числом.")
        return
    uid = int(msg.text)
    async with db_manager.transaction() as db:
        cur = await db.execute("UPDATE users SET is_blocked = 0 WHERE id = ?", (uid,))
    await state.clear()
    await msg.answer(
        f"✅ Разбанен {uid}" if cur.rowcount else f"❌ ID {uid} не найден",
        reply_markup=main_menu_kb(True),
    )


# ============================================================
# ОЧИСТКА
# ============================================================

@router.callback_query(F.data == "admin:clear_menu", IsAdmin())
async def clear_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(cb)
    await state.set_state(ClearActions.waiting_id)
    await cb.message.answer("🧹 Введите ID пользователя, чью историю (заявки + поддержка) очистить:")


@router.message(ClearActions.waiting_id, F.text, IsAdmin())
async def clear_apply(msg: Message, state: FSMContext) -> None:
    if not msg.text.isdigit():
        await msg.answer("ID должен быть числом.")
        return
    uid = int(msg.text)
    async with db_manager.transaction() as db:
        await db.execute("DELETE FROM requests WHERE user_id = ?", (uid,))
        await db.execute("DELETE FROM support_msgs WHERE user_id = ?", (uid,))
    await state.clear()
    await msg.answer(f"✅ История {uid} очищена.", reply_markup=main_menu_kb(True))


# ============================================================
# РАССЫЛКА
# ============================================================

@router.callback_query(F.data == "admin:broadcast", IsAdmin())
async def broadcast_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(cb)
    await state.set_state(BroadcastActions.choose_target)
    await _safe_edit(cb.message, "📢 Выберите аудиторию для рассылки:", admin_broadcast_filter_kb())


@router.callback_query(BroadcastActions.choose_target, F.data.startswith("bcast:"), IsAdmin())
async def broadcast_target(cb: CallbackQuery, state: FSMContext) -> None:
    parts = (cb.data or "").split(":")
    target = parts[1] if len(parts) == 2 else None
    allowed_targets = {"all", "with_reqs", "no_reqs"}
    if target not in allowed_targets:
        await state.clear()
        await _safe_answer(cb, "❌ Неизвестная аудитория. Рассылка отменена.", alert=True)
        logger.warning("Broadcast cancelled: invalid audience %r", target)
        return

    await state.update_data(target=target)
    await state.set_state(BroadcastActions.waiting_text)
    await _safe_edit(cb.message, "✍️ Введите текст для рассылки (HTML разрешен):")


@router.message(BroadcastActions.waiting_text, F.text, IsAdmin())
async def broadcast_send(msg: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    target = data.get("target")
    allowed_targets = {"all", "with_reqs", "no_reqs"}
    if target not in allowed_targets:
        await state.clear()
        await msg.answer("❌ Аудитория рассылки не определена. Операция отменена.", reply_markup=main_menu_kb(True))
        logger.warning("Broadcast cancelled at send stage: invalid audience %r", target)
        return

    # Telegram считает длину текста в UTF-16 code units; len(str) в Python
    # не совпадает с этим для emoji и некоторых символов. Проверяем обе формы.
    prefix = "📢 <b>Сообщение:</b>\n\n"
    plain_prefix = "📢 Сообщение:\n\n"
    body = msg.text or ""
    def telegram_length(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    if telegram_length(prefix + body) > 4096 or telegram_length(plain_prefix + body) > 4096:
        await msg.answer("❌ Сообщение превышает лимит Telegram в 4096 символов. Сократите текст и отправьте заново.")
        return

    # Сначала формируем аудиторию только из активных пользователей.
    if target == "all":
        query = "SELECT id FROM users WHERE is_blocked = 0"
    elif target == "with_reqs":
        query = "SELECT DISTINCT user_id AS id FROM requests WHERE user_id IN (SELECT id FROM users WHERE is_blocked = 0)"
    else:  # target == "no_reqs"
        query = "SELECT id FROM users WHERE is_blocked = 0 AND id NOT IN (SELECT DISTINCT user_id FROM requests)"

    await state.clear()
    async with db_manager.connect() as db:
        async with db.execute(query) as cur:
            users = await cur.fetchall()

    await msg.answer(f"⏳ Рассылка для {len(users)} пользователей...")
    ok = fail = 0
    blocked = 0
    for u in users:
        user_id = u["id"]
        try:
            await bot.send_message(user_id, prefix + body, parse_mode="HTML")
            ok += 1
        except TelegramForbiddenError:
            fail += 1
            blocked += 1
            logger.info("Broadcast recipient unavailable/blocked: user_id=%s", user_id)
        except TelegramBadRequest as exc:
            # Некорректная HTML-разметка: безопасный fallback — отправить как обычный текст.
            try:
                plain = plain_prefix + body
                # Не обрезаем текст молча: его длина уже проверена выше.
                await bot.send_message(user_id, plain, parse_mode=None)
                ok += 1
                logger.warning("Broadcast HTML rejected; plain-text fallback used for user_id=%s: %s", user_id, exc)
            except Exception:
                fail += 1
                logger.exception("Broadcast failed after HTML fallback: user_id=%s", user_id)
        except Exception:
            fail += 1
            logger.exception("Broadcast failed: user_id=%s", user_id)
        await asyncio.sleep(0.05)

    await msg.answer(
        f"✅ Рассылка завершена.\nПопыток: {len(users)}\nДоставлено Telegram: {ok}\nОшибок: {fail}\nНедоступных/заблокировавших бота: {blocked}",
        reply_markup=main_menu_kb(True),
    )


# ============================================================
# ПОДДЕРЖКА (ОТВЕТ)
# ============================================================

@router.callback_query(F.data.startswith("sup_reply:"), IsAdmin())
async def reply_support_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _safe_answer(cb)
    target_id = int(cb.data.split(":")[1])
    await state.update_data(target_id=target_id)
    await state.set_state(ReplySupport.waiting)
    await cb.message.answer(f"✍️ Введите ответ для пользователя {target_id}:")


@router.message(ReplySupport.waiting, F.text, IsAdmin())
async def reply_support_send(msg: Message, state: FSMContext, bot: Bot) -> None:
    tid = (await state.get_data()).get("target_id")
    await state.clear()
    if not tid:
        await msg.answer("❌ Получатель не найден.")
        return
    try:
        b = InlineKeyboardBuilder().button(text="🏠 В меню", callback_data="menu:main")
        await bot.send_message(
            tid,
            f"📩 <b>Ответ поддержки:</b>\n\n{html.escape(msg.text)}",
            reply_markup=b.as_markup(),
            parse_mode="HTML",
        )
        await msg.answer("✅ Доставлено.")
    except TelegramForbiddenError:
        logger.info("Support reply recipient unavailable: user_id=%s", tid)
        await msg.answer("❌ Не удалось доставить ответ: пользователь заблокировал бота или недоступен.")
    except Exception:
        logger.exception("Support reply failed: user_id=%s", tid)
        await msg.answer("❌ Не удалось доставить ответ из-за ошибки Telegram. Подробности записаны в журнал.")
