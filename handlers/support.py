import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS
from database import db_manager


logger = logging.getLogger(__name__)
router = Router()


class SupportStates(StatesGroup):
    chatting = State()


@router.callback_query(F.data == "support")
async def start_support(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(SupportStates.chatting)

    message = callback.message
    if not isinstance(message, Message):
        logger.warning(
            "Не удалось открыть поддержку: сообщение недоступно (user_id=%s).",
            callback.from_user.id,
        )
        return

    keyboard = (
        InlineKeyboardBuilder()
        .button(text="⬅️ Выйти", callback_data="menu:main")
        .as_markup()
    )

    try:
        await message.edit_text(
            "💬 <b>Поддержка</b>\n\nНапишите ваш вопрос.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return

        logger.warning(
            "Не удалось отредактировать сообщение поддержки для user_id=%s: %s",
            callback.from_user.id,
            exc,
        )
        try:
            await message.answer(
                "💬 <b>Поддержка</b>\n\nНапишите ваш вопрос.",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except TelegramAPIError:
            logger.exception(
                "Не удалось отправить новое сообщение поддержки user_id=%s.",
                callback.from_user.id,
            )
    except TelegramAPIError:
        logger.exception(
            "Ошибка Telegram при открытии поддержки user_id=%s.",
            callback.from_user.id,
        )
        try:
            await message.answer(
                "💬 <b>Поддержка</b>\n\nНапишите ваш вопрос.",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except TelegramAPIError:
            logger.exception(
                "Не удалось отправить новое сообщение поддержки user_id=%s.",
                callback.from_user.id,
            )


@router.message(SupportStates.chatting, F.text)
async def send_support_msg(message: Message, bot: Bot) -> None:
    user = message.from_user
    text = message.text

    if user is None or not text:
        await message.answer("❌ Не удалось определить отправителя или текст обращения.")
        return

    # Сначала надёжно сохраняем обращение. Уведомление администраторам —
    # отдельный шаг: ошибка Telegram не должна отменять сохранённую запись.
    try:
        async with db_manager.transaction() as db:
            await db.execute(
                """
                INSERT INTO support_msgs (user_id, text, is_admin)
                VALUES (?, ?, 0)
                """,
                (user.id, text),
            )
    except Exception:
        logger.exception(
            "Не удалось сохранить обращение в поддержку от user_id=%s.",
            user.id,
        )
        await message.answer(
            "❌ Не удалось сохранить обращение. Попробуйте отправить его ещё раз позже."
        )
        return

    keyboard = (
        InlineKeyboardBuilder()
        .button(
            text="Ответить",
            callback_data=f"sup_reply:{user.id}",
        )
        .as_markup()
    )

    safe_name = html.escape(user.full_name or "Пользователь")
    # Оставляем запас под заголовок и HTML-разметку Telegram (лимит 4096 символов).
    notification_text = text[:3000]
    if len(text) > len(notification_text):
        notification_text += "\n\n[Сообщение сокращено в уведомлении; полный текст сохранён в БД.]"

    admin_message = (
        "💬 <b>Поддержка</b>\n"
        f"От: {safe_name} (<code>{user.id}</code>)\n\n"
        f"{html.escape(notification_text)}"
    )

    delivered = 0
    failed = 0

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_message,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            delivered += 1
        except TelegramAPIError:
            failed += 1
            logger.exception(
                "Не удалось отправить обращение от user_id=%s администратору %s.",
                user.id,
                admin_id,
            )
        except Exception:
            failed += 1
            logger.exception(
                "Неожиданная ошибка при уведомлении администратора %s "
                "об обращении user_id=%s.",
                admin_id,
                user.id,
            )

    if delivered == 0:
        await message.answer(
            "⚠️ Обращение сохранено, но сейчас не удалось уведомить администраторов. "
            "Они могут не увидеть его сразу. Попробуйте обратиться позже."
        )
    elif failed:
        await message.answer(
            f"✅ Обращение отправлено {delivered} из {len(ADMIN_IDS)} администраторов."
        )
    else:
        await message.answer("✅ Обращение отправлено администратору.")
