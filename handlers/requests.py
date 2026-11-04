import html
import logging
import re
import uuid
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS, STATUS_DONE
from database import db_manager
from keyboards import (
    req_duration_kb,
    req_extras_kb,
    req_payment_kb,
)
from services.requests import (
    RequestConflictError,
    create_request_idempotent,
)

logger = logging.getLogger(__name__)
router = Router(name="requests")


# Должно совпадать с SESSION_CALLBACK_LENGTH в keyboards.py
SESSION_LENGTH = 16

TARIFFS = {
    "1h": ("price_1h", "1 час"),
    "3h": ("price_3h", "3 часа"),
    "night": ("price_night", "Ночь"),
}

PAYMENT_METHODS = {
    "card": "Карта",
    "crypto": "Криптовалюта",
    "cash": "Наличные",
}

STATUS_EMOJI = {
    "Ожидает реквизиты": "🕐",
    "Оплачено": "✅",
    "В работе": "🚗",
    "Завершено": "🎉",
    "Отменено": "❌",
}

VALID_TARIFFS = frozenset(TARIFFS)
VALID_PAYMENTS = frozenset(PAYMENT_METHODS)


class PromoFlow(StatesGroup):
    waiting_promo = State()


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
    amount = _safe_int(value, minimum=0)
    return f"{amount:,}".replace(",", " ")


def _truncate_request_lines(lines: list[str], limit: int = 3900) -> str:
    text = "\n".join(lines)
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text

    header = "\n".join(lines[:2])
    groups = [
        "\n".join(lines[index:index + 4])
        for index in range(2, len(lines), 4)
    ]
    kept = []
    suffix = "\n\n<i>Показана часть списка.</i>"
    for group in groups:
        candidate = header + "\n".join(kept + [group]) + suffix
        if len(candidate.encode("utf-16-le")) // 2 > limit:
            break
        kept.append(group)

    return header + "\n".join(kept) + suffix


async def _safe_edit(message, text: str, markup=None) -> None:
    """Edit text/caption. Fallback: send new. Never answers callback."""
    if message is None:
        return
    try:
        await message.edit_caption(caption=text, reply_markup=markup, parse_mode="HTML")
        return
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
    try:
        await message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
        return
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
    try:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось обновить сообщение")


def _valid_session(data: dict, session: str) -> bool:
    return bool(session and data.get("session_token") == session)


async def _resolve_promo(code: str, user_id: int | None = None) -> dict[str, Any] | None:
    token = (code or "").strip().upper()
    if not token or not re.fullmatch(r"[A-Z0-9-]{3,20}", token):
        return None

    async with db_manager.connect() as db:
        async with db.execute(
            "SELECT id, code, discount_percent, is_active FROM promocodes WHERE code = ?",
            (token,),
        ) as cur:
            row = await cur.fetchone()

    if row is None or not row["is_active"]:
        return None

    discount = int(row["discount_percent"])
    if not 1 <= discount <= 100:
        return None

    return {"id": int(row["id"]), "code": str(row["code"]), "discount_percent": discount}


# ============================================================
# DB
# ============================================================

async def _get_anketa(anketa_id: int):
    async with db_manager.connect() as db:
        async with db.execute(
            "SELECT id, name, price_1h, price_3h, price_night "
            "FROM anketas WHERE id = ?",
            (anketa_id,),
        ) as cur:
            return await cur.fetchone()


async def _get_services(anketa_id: int) -> list:
    async with db_manager.connect() as db:
        async with db.execute(
            """
            SELECT s.id, s.name, s.default_price AS price
            FROM services s
            INNER JOIN anketa_services a ON a.service_id = s.id
            WHERE a.anketa_id = ?
            ORDER BY s.id
            """,
            (anketa_id,),
        ) as cur:
            return list(await cur.fetchall())


def _base_price(anketa, srv_key: str) -> int:
    field = TARIFFS[srv_key][0]
    price = _safe_int(anketa[field], default=-1, minimum=0)
    if price < 0:
        raise ValueError("Некорректная стоимость тарифа")
    return price


async def _calculate_cart(anketa_id: int, srv_key: str, extras_ids: list):
    """Пересчитывает корзину по актуальным ценам, сохраняя порядок выбора."""
    if srv_key not in VALID_TARIFFS:
        return None
    anketa = await _get_anketa(anketa_id)
    if not anketa:
        return None
    services = await _get_services(anketa_id)
    services_by_id = {s["id"]: s for s in services}

    ordered_ids = list(dict.fromkeys(
        sid for sid in extras_ids
        if isinstance(sid, int) and sid in services_by_id
    ))
    selected = [services_by_id[sid] for sid in ordered_ids]

    base = _base_price(anketa, srv_key)
    extras_sum = sum(_safe_int(s["price"], 0, 0) for s in selected)
    return {
        "anketa": anketa,
        "services": services,
        "selected": selected,
        "selected_ids": ordered_ids,
        "base": base,
        "extras_sum": extras_sum,
        "total": base + extras_sum,
    }


def _payment_screen(
    result: dict[str, Any],
    anketa_id: int,
    srv_key: str,
    session: str,
    promo_code: str | None = None,
    promo_percent: int = 0,
) -> tuple[str, Any, int]:
    total = result["total"]
    if promo_code and 0 < promo_percent <= 100:
        total -= total * promo_percent // 100

    text = f"💳 <b>К оплате</b>\n\n⏱ {TARIFFS[srv_key][1]} — {_money(result['base'])} ₽\n"
    if result["selected"]:
        text += "\n<b>Дополнительные услуги:</b>\n"
        for service in result["selected"]:
            text += f"➕ {_escape(service['name'])} — {_money(service['price'])} ₽\n"
    if promo_code and 0 < promo_percent <= 100:
        discount = result["total"] - total
        text += (
            f"\n🎟 Промокод <code>{_escape(promo_code)}</code> "
            f"({promo_percent}%) — скидка {_money(discount)} ₽\n"
        )
    text += f"\n✅ <b>Итого: {_money(total)} ₽</b>\n\nВыберите способ оплаты:"
    return text, req_payment_kb(anketa_id, srv_key, session), total


async def _has_completed_meeting(user_id: int) -> bool:
    async with db_manager.connect() as db:
        async with db.execute(
            "SELECT 1 FROM requests WHERE user_id = ? AND status = ? LIMIT 1",
            (user_id, STATUS_DONE),
        ) as cursor:
            return await cursor.fetchone() is not None


# ============================================================
# PURE RENDERER (не отвечает на callback)
# ============================================================

async def _render_extras(callback: CallbackQuery, state: FSMContext, page: int = 0) -> None:
    if not callback.message:
        return
    data = await state.get_data()
    anketa_id = data.get("anketa_id")
    srv_key = data.get("srv_key")
    session = data.get("session_token")

    if not anketa_id or srv_key not in VALID_TARIFFS or not session:
        logger.error("_render_extras: invalid state")
        return

    result = await _calculate_cart(anketa_id, srv_key, data.get("extras", []))
    if result is None:
        await _safe_edit(callback.message, "❌ Анкета не найдена.", None)
        return

    await state.update_data(extras=result["selected_ids"], extras_page=page)

    text = (
        "✨ <b>Дополнительные услуги</b>\n\n"
        f"⏱ Тариф: <b>{TARIFFS[srv_key][1]}</b>\n"
        f"💰 База: {_money(result['base'])} ₽\n"
        f"➕ Допы: {_money(result['extras_sum'])} ₽\n"
        f"✅ <b>Итого: {_money(result['total'])} ₽</b>"
    )
    if not result["services"]:
        text += "\n\n<i>Для этой анкеты пока нет дополнительных услуг.</i>"

    markup = req_extras_kb(
        anketa_id=anketa_id,
        srv_key=srv_key,
        session=session,
        services=result["services"],
        selected_ids=result["selected_ids"],
        page=page,
        total_price=result["total"],
    )
    await _safe_edit(callback.message, text, markup)


# ============================================================
# HANDLERS
# ============================================================

@router.callback_query(F.data.startswith("req:start:"))
async def req_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[2], -1, 1)
    if anketa_id < 1:
        await callback.answer()
        return

    anketa = await _get_anketa(anketa_id)
    if not anketa:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return

    session = uuid.uuid4().hex[:SESSION_LENGTH]
    await state.update_data(
        session_token=session,
        anketa_id=anketa_id,
        srv_key=None,
        extras=[],
        extras_page=0,
    )

    text = f"👤 <b>{_escape(anketa['name'])}</b>\n\n⏱ <b>Выберите длительность:</b>"
    markup = req_duration_kb(
        anketa_id=anketa_id,
        session=session,
        price_1h=anketa["price_1h"],
        price_3h=anketa["price_3h"],
        price_night=anketa["price_night"],
    )
    await callback.answer()
    await _safe_edit(callback.message, text, markup)


@router.callback_query(F.data.startswith("req_back_dur:"))
async def req_back_dur(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору длительности без пересоздания session_token."""
    if not callback.data or not callback.message:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[1], -1, 1)
    session = parts[2]
    if anketa_id < 1:
        await callback.answer()
        return

    data = await state.get_data()
    if data.get("anketa_id") != anketa_id or not _valid_session(data, session):
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        return

    anketa = await _get_anketa(anketa_id)
    if not anketa:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return

    text = f"👤 <b>{_escape(anketa['name'])}</b>\n\n⏱ <b>Выберите длительность:</b>"
    markup = req_duration_kb(
        anketa_id=anketa_id,
        session=session,
        price_1h=anketa["price_1h"],
        price_3h=anketa["price_3h"],
        price_night=anketa["price_night"],
    )
    await callback.answer()
    await _safe_edit(callback.message, text, markup)


@router.callback_query(F.data.startswith("req_dur:"))
async def req_duration(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[1], -1, 1)
    srv_key = parts[2]
    session = parts[3]

    if anketa_id < 1 or srv_key not in VALID_TARIFFS:
        await callback.answer()
        return

    data = await state.get_data()
    if data.get("anketa_id") != anketa_id or not _valid_session(data, session):
        await callback.answer("Сессия истекла. Начните заново.", show_alert=True)
        return

    await state.update_data(srv_key=srv_key, extras=[], extras_page=0)
    await state.update_data(promo_code=None, promo_percent=0, promo_context=None)
    await callback.answer()
    await _render_extras(callback, state, 0)


@router.callback_query(F.data.startswith("req_extra:"))
async def req_extra_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 6:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[1], -1, 1)
    srv_key = parts[2]
    service_id = _safe_int(parts[3], -1, 1)
    page = _safe_int(parts[4], 0, 0)
    session = parts[5]

    if anketa_id < 1 or service_id < 1 or srv_key not in VALID_TARIFFS:
        await callback.answer()
        return

    data = await state.get_data()
    if (data.get("anketa_id") != anketa_id
            or data.get("srv_key") != srv_key
            or not _valid_session(data, session)):
        await callback.answer("Сессия истекла.", show_alert=True)
        return

    services = await _get_services(anketa_id)
    if service_id not in {s["id"] for s in services}:
        await callback.answer("Услуга больше недоступна.", show_alert=True)
        return

    extras = list(data.get("extras", []))
    if service_id in extras:
        extras.remove(service_id)
    else:
        extras.append(service_id)
    await state.update_data(extras=extras)

    await callback.answer()
    await _render_extras(callback, state, page)


@router.callback_query(F.data.startswith("req_extras_page:"))
async def req_extras_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[1], -1, 1)
    srv_key = parts[2]
    page = _safe_int(parts[3], 0, 0)
    session = parts[4]

    data = await state.get_data()
    if (anketa_id < 1
            or data.get("anketa_id") != anketa_id
            or data.get("srv_key") != srv_key
            or not _valid_session(data, session)):
        await callback.answer("Сессия истекла.", show_alert=True)
        return

    await callback.answer()
    await _render_extras(callback, state, page)


@router.callback_query(F.data.startswith("req_extras_reset:"))
async def req_extras_reset(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[1], -1, 1)
    srv_key = parts[2]
    session = parts[3]

    data = await state.get_data()
    if (anketa_id < 1
            or data.get("anketa_id") != anketa_id
            or data.get("srv_key") != srv_key
            or not _valid_session(data, session)):
        await callback.answer("Сессия истекла.", show_alert=True)
        return

    await state.update_data(extras=[], extras_page=0)
    await callback.answer("Допы сброшены.")
    await _render_extras(callback, state, 0)


@router.callback_query(F.data.startswith("req_done:"))
async def req_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[1], -1, 1)
    srv_key = parts[2]
    session = parts[3]

    data = await state.get_data()
    if (anketa_id < 1
            or srv_key not in VALID_TARIFFS
            or data.get("anketa_id") != anketa_id
            or data.get("srv_key") != srv_key
            or not _valid_session(data, session)):
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    result = await _calculate_cart(anketa_id, srv_key, data.get("extras", []))
    if result is None:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return

    data = await state.get_data()
    text, markup, _ = _payment_screen(
        result,
        anketa_id,
        srv_key,
        session,
        data.get("promo_code"),
        _safe_int(data.get("promo_percent"), 0, 0),
    )
    await callback.answer()
    await _safe_edit(callback.message, text, markup)


@router.callback_query(F.data.startswith("req_promo:"))
async def req_promo(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[1], -1, 1)
    srv_key = parts[2]
    session = parts[3]

    data = await state.get_data()
    if (anketa_id < 1
            or srv_key not in VALID_TARIFFS
            or data.get("anketa_id") != anketa_id
            or data.get("srv_key") != srv_key
            or not _valid_session(data, session)):
        await callback.answer("Сессия истекла. Начните заново.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(promo_context={"anketa_id": anketa_id, "srv_key": srv_key, "session": session})
    await state.set_state(PromoFlow.waiting_promo)
    await _safe_edit(
        callback.message,
        "🎟 <b>Введите промокод</b>\n\nКод можно ввести без пробелов, например: <code>WELCOME</code>",
        None,
    )


@router.message(PromoFlow.waiting_promo, F.text)
async def req_promo_apply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    context = data.get("promo_context")
    if (
        await state.get_state() != PromoFlow.waiting_promo.state
        or not isinstance(context, dict)
        or context.get("anketa_id") != data.get("anketa_id")
        or context.get("srv_key") != data.get("srv_key")
        or not _valid_session(data, str(context.get("session", "")))
    ):
        await message.answer("❌ Сессия оформления устарела. Начните заново.")
        return

    code = (message.text or "").strip()
    promo = await _resolve_promo(code, message.from_user.id if message.from_user else None)
    if promo is None:
        await message.answer("❌ Промокод не найден, неактивен или недоступен.")
        return

    result = await _calculate_cart(
        context["anketa_id"],
        context["srv_key"],
        data.get("extras", []),
    )
    if result is None:
        await message.answer("❌ Анкета больше недоступна. Начните оформление заново.")
        await state.clear()
        return

    await state.update_data(
        promo_code=promo["code"],
        promo_percent=promo["discount_percent"],
        promo_context=None,
    )
    await state.set_state(None)
    text, markup, _ = _payment_screen(
        result,
        context["anketa_id"],
        context["srv_key"],
        context["session"],
        promo["code"],
        promo["discount_percent"],
    )
    await message.answer(
        f"✅ Промокод <code>{html.escape(promo['code'])}</code> применён.\n\n{text}",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("req_pay:"))
async def req_pay(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer()
        return

    anketa_id = _safe_int(parts[1], -1, 1)
    srv_key = parts[2]
    method = parts[3]
    session = parts[4]

    if (
        anketa_id < 1
        or srv_key not in VALID_TARIFFS
        or method not in VALID_PAYMENTS
    ):
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    data = await state.get_data()
    if (
        data.get("anketa_id") != anketa_id
        or data.get("srv_key") != srv_key
        or not _valid_session(data, session)
    ):
        await callback.answer("Сессия истекла. Начните заново.", show_alert=True)
        return

    result = await _calculate_cart(anketa_id, srv_key, data.get("extras", []))
    if result is None:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return

    promo_code = data.get("promo_code")
    promo_percent = 0
    if promo_code:
        promo = await _resolve_promo(str(promo_code), callback.from_user.id)
        if promo is None:
            await state.update_data(promo_code=None, promo_percent=0)
            text, markup, _ = _payment_screen(result, anketa_id, srv_key, session)
            await callback.answer(
                "Промокод больше не активен. Сумма обновлена без скидки.",
                show_alert=True,
            )
            await _safe_edit(callback.message, text, markup)
            return
        promo_code = promo["code"]
        promo_percent = promo["discount_percent"]
        await state.update_data(promo_code=promo_code, promo_percent=promo_percent)

    if method == "cash" and not await _has_completed_meeting(callback.from_user.id):
        await callback.answer(
            "Наличные доступны только после завершённой предыдущей встречи. "
            "Выберите карту или криптовалюту.",
            show_alert=True,
        )
        return

    # Сразу закрываем Telegram callback; запись в БД выполняется отдельно.
    await callback.answer("⏳ Создаю заявку...")

    anketa = result["anketa"]
    selected = result["selected"]
    total = result["total"]
    if promo_code and 0 < promo_percent <= 100:
        total = max(0, total - (total * promo_percent // 100))
    method_name = PAYMENT_METHODS[method]
    tariff_name = TARIFFS[srv_key][1]

    service_title = tariff_name
    if selected:
        service_title += " + " + ", ".join(str(s["name"]) for s in selected)

    user = callback.from_user
    user_id = user.id

    try:
        request_result = await create_request_idempotent(
            user_id=user_id,
            anketa_id=anketa_id,
            service=service_title,
            amount=total,
            payment_method=method_name,
            session_token=session,
            selected_services=[
                {"id": int(item["id"]), "price": int(item["price"])}
                for item in selected
            ],
        )
    except RequestConflictError:
        logger.warning(
            "Повторное использование session_token с другими параметрами "
            "(user_id=%s, anketa_id=%s)",
            user_id,
            anketa_id,
        )
        await state.clear()
        await _safe_edit(
            callback.message,
            "⚠️ Эта сессия уже использована для другой заявки. "
            "Вернитесь в меню и начните оформление заново.",
            None,
        )
        return
    except Exception:
        logger.exception(
            "Не удалось создать заявку (user_id=%s, anketa_id=%s)",
            user_id,
            anketa_id,
        )
        await _safe_edit(
            callback.message,
            "❌ Не удалось создать заявку. Попробуйте начать оформление заново.",
            None,
        )
        return

    unique_id = request_result["unique_id"]
    created_new = request_result["created"]

    # Сбрасываем FSM только после успешного создания или подтверждения
    # уже существующей идентичной заявки.
    await state.clear()

    if created_new:
        extras_text = ""
        if selected:
            extras_text = "\n<b>Допы:</b>\n"
            for item in selected:
                extras_text += (
                    f"➕ {_escape(item['name'])} — "
                    f"{_money(item['price'])} ₽\n"
                )

        promo_text = ""
        if promo_code and promo_percent:
            promo_text = f"🎟 Промокод: <code>{_escape(promo_code)}</code> ({promo_percent}%)\n"

        success_text = (
            f"📋 <b>Заявка №{_escape(unique_id)}</b>\n\n"
            f"👤 {_escape(anketa['name'])}\n"
            f"⏱️ {tariff_name}\n{extras_text}\n"
            f"{promo_text}"
            f"💰 <b>Сумма: {_money(total)} ₽</b>\n"
            f"💳 Оплата: {_escape(method_name)}\n"
            "📊 Статус: Ожидает реквизиты\n\n"
            "⏳ Ожидайте реквизиты от администратора."
        )
    else:
        success_text = (
            f"ℹ️ <b>Заявка №{_escape(unique_id)} уже создана ранее.</b>\n\n"
            f"📋 Услуга: {_escape(request_result['service'])}\n"
            f"💰 Сумма: <b>{_money(request_result['amount'])} ₽</b>\n"
            f"💳 Оплата: {_escape(request_result['payment_method'])}\n"
            f"📊 Статус: {_escape(request_result['status'])}\n\n"
            "Ожидайте реквизиты от администратора."
        )

    builder = InlineKeyboardBuilder()
    builder.button(text="🏠 В главное меню", callback_data="menu:main")
    await _safe_edit(callback.message, success_text, builder.as_markup())

    # Повторный callback не дублирует уведомления администраторам.
    if not created_new:
        return

    if user.username:
        user_link = f"@{_escape(user.username)}"
    else:
        user_link = (
            f'<a href="tg://user?id={user.id}">'
            f'{_escape(user.first_name or "Пользователь")}</a>'
        )

    admin_text = (
        f"🔔 <b>Новая заявка №{_escape(unique_id)}</b>\n\n"
        f"👤 Клиент: {user_link}\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📋 Анкета: {_escape(anketa['name'])}\n"
        f"⏱️ Услуга: {_escape(tariff_name)}\n"
    )

    if selected:
        admin_text += "\n<b>Допы:</b>\n"
        for item in selected:
            admin_text += (
                f"➕ {_escape(item['name'])} — "
                f"{_money(item['price'])} ₽\n"
            )

    admin_text += (
        (
            f"\n🎟 Промокод: <code>{_escape(promo_code)}</code> "
            f"({promo_percent}%)"
            if promo_code and promo_percent
            else ""
        )
        +
        f"\n💰 <b>Сумма: {_money(total)} ₽</b>\n"
        f"💳 Оплата: {_escape(method_name)}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Не удалось уведомить админа %s", admin_id)


@router.callback_query(F.data == "my_reqs")
async def my_reqs(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    user_id = callback.from_user.id

    try:
        async with db_manager.connect() as db:
            async with db.execute(
                """
                SELECT unique_id, service, amount, payment_method, status, created_at
                FROM requests WHERE user_id = ?
                ORDER BY id DESC LIMIT 15
                """,
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
    except Exception:
        logger.exception("Ошибка загрузки заявок %s", user_id)
        await callback.message.answer("❌ Не удалось загрузить заявки.")
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="🏠 В главное меню", callback_data="menu:main")

    if not rows:
        await _safe_edit(
            callback.message,
            "📋 <b>Мои заявки</b>\n\nУ вас пока нет заявок.",
            builder.as_markup(),
        )
        return

    lines = ["📋 <b>Ваши заявки:</b>", ""]
    for row in rows:
        status = row["status"] or ""
        emoji = STATUS_EMOJI.get(status, "❓")
        unique_id = str(row["unique_id"] or "")[:100]
        service = str(row["service"] or "")[:300]
        lines.append(f"{emoji} <b>№{_escape(unique_id)}</b> — {_money(row['amount'])} ₽")
        lines.append(f"   {_escape(service)}")
        lines.append(f"   Статус: <i>{_escape(status[:100])}</i>")
        lines.append("")

    text = _truncate_request_lines(lines)
    await _safe_edit(callback.message, text, builder.as_markup())
