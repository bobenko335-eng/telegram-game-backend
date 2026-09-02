"""
Логика Telegram-бота (aiogram 3.x).

Сценарий авторизации:
1. Игрок в игре нажимает "Войти через Telegram" -> backend создаёт
   link_token и отдаёт клиенту deep-link вида
   https://t.me/<bot_username>?start=<link_token>
2. Игрок переходит по ссылке, у бота срабатывает /start <link_token>.
3. Бот запоминает telegram_id игрока для этого токена и просит
   поделиться контактом через штатную Telegram-кнопку.
4. Игрок жмёт кнопку -> бот получает contact (телефон, имя, id).
5. Backend привязывает telegram_id к game_user_id, сохраняет только то,
   что реально прислал Telegram, и оповещает игру через WebSocket.
6. Дальше все текстовые сообщения от этого telegram_id уходят в игру
   И в общую публичную ленту, которую видят все игроки.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from storage import storage
from ws_manager import ws_manager

logger = logging.getLogger("bot")

router = Router()

CONTACT_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Поделиться контактом", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


@router.message(CommandStart(deep_link=True))
async def start_with_token(message: Message, command: CommandObject):
    link_token = (command.args or "").strip()
    link = storage.get_link(link_token)

    if not link:
        await message.answer(
            "Ссылка недействительна или устарела. Запросите новую ссылку входа в игре."
        )
        return

    if link["status"] == "linked":
        await message.answer("Этот аккаунт уже авторизован в игре.")
        return

    storage.mark_pending_telegram_id(link_token, message.from_user.id)
    await message.answer(
        "Пожалуйста, авторизуйтесь: нажмите кнопку ниже, чтобы поделиться "
        "данными профиля с игрой.",
        reply_markup=CONTACT_KEYBOARD,
    )


@router.message(CommandStart())
async def start_without_token(message: Message):
    await message.answer(
        "Привет! Чтобы связать аккаунт с игрой, откройте игру и нажмите "
        "«Войти через Telegram» — оттуда придёт персональная ссылка."
    )


@router.message(F.contact)
async def contact_received(message: Message):
    telegram_id = message.from_user.id
    link_token = storage.find_pending_link_by_telegram_id(telegram_id)

    if not link_token:
        await message.answer(
            "Не нашёл ожидающую авторизацию для вашего аккаунта. "
            "Начните вход заново из игры.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    contact = message.contact
    profile = {
        "telegram_id": telegram_id,
        "username": message.from_user.username,
        "first_name": contact.first_name or message.from_user.first_name,
        "last_name": contact.last_name or message.from_user.last_name,
        "phone_number": contact.phone_number,
    }

    game_user_id = storage.confirm_link(link_token, telegram_id, profile)

    await message.answer(
        "Спасибо! Профиль передан в игру. Можете вернуться в игру — "
        "она уже видит эти данные.",
        reply_markup=ReplyKeyboardRemove(),
    )

    await ws_manager.send(
        game_user_id,
        {"type": "linked", "profile": profile},
    )


@router.message(F.text)
async def text_message(message: Message):
    telegram_id = message.from_user.id
    game_user_id = storage.get_game_user_by_telegram_id(telegram_id)

    if not game_user_id:
        await message.answer(
            "Ваш аккаунт ещё не связан с игрой. Начните вход из игры."
        )
        return

    user = storage.get_user(game_user_id)
    if user and user.get("blocked"):
        return

    msg = storage.add_message(game_user_id, "in", message.text)
    await ws_manager.send(game_user_id, {"type": "message", "message": msg})
    await ws_manager.broadcast_feed(
        {
            "type": "feed_message",
            "message": {
                "game_user_id": game_user_id,
                "display_name": storage.display_name(game_user_id),
                "direction": "in",
                "text": message.text,
                "ts": msg["ts"],
            },
        }
    )


async def send_to_telegram(bot: Bot, game_user_id: str, text: str) -> bool:
    user = storage.get_user(game_user_id)
    if not user or not user.get("telegram_id"):
        return False
    if user.get("blocked"):
        return False
    await bot.send_message(user["telegram_id"], text)
    msg = storage.add_message(game_user_id, "out", text)
    await ws_manager.broadcast_feed(
        {
            "type": "feed_message",
            "message": {
                "game_user_id": game_user_id,
                "display_name": storage.display_name(game_user_id),
                "direction": "out",
                "text": text,
                "ts": msg["ts"],
            },
        }
    )
    return True


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def run_polling(bot: Bot, dp: Dispatcher):
    await dp.start_polling(bot)
