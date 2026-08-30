"""
Точка входа бота. Здесь только Telegram-хендлеры (регистрация команд, FSM,
инлайн-поиск) и запуск polling'а. Вся реальная логика — в mif_core.py
(обработка аудио, база, публикация) и mif_loader.py (автозагрузка с
MyInstants). Если тебе нужно поменять ЧТО происходит при публикации/хэшах —
правь mif_core.py, а не этот файл.
"""

import asyncio
import html
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineQuery,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedVoice,
    Message,
)

import mif_core
import mif_loader

logger = logging.getLogger("mif-bot")

MAX_DESCRIPTION_LENGTH = 700

dp = Dispatcher(storage=MemoryStorage())


class AddMif(StatesGroup):
    waiting_for_description = State()


@dp.message(Command("cancel"), F.chat.type == "private")
async def cancel_addition(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Сейчас нечего отменять.")
        return

    await state.clear()
    await message.answer("Добавление звука отменено.")


@dp.message(Command("start"), F.chat.type == "private")
async def start_private_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я ищу MIF-звуки через inline-режим.\n\n"
        "Чтобы добавить звук:\n"
        "1. Отправь мне аудиофайл или голосовое сообщение.\n"
        "2. Следующим сообщением напиши название и теги.\n\n"
        "После этого звук попадёт в канал и станет доступен в поиске.\n"
        "Для отмены незавершённого добавления отправь /cancel."
    )


# ⚠️ НАПОМИНАЛКА СЕБЕ: /help — единственное место, где обычные пользователи
# видят список команд. Каждый раз, когда добавляешь новую команду или
# меняешь поведение существующей — обнови текст ниже. Сюда идут ТОЛЬКО
# команды, доступные обычным людям. Админские (/loads, /loadsN, /loadsStop)
# сюда НЕ добавлять — их не должно быть видно в общем /help.
HELP_TEXT = (
    "🔊 <b>MIFs — звуковые мемы</b>\n\n"
    "<b>Найти звук:</b>\n"
    "В любом чате набери <code>@MIFki_bot запрос</code> — появится список "
    "подходящих звуков. Можно вводить несколько слов в любом порядке "
    "(например: «окси мем»).\n\n"
    "<b>Добавить свой звук:</b>\n"
    "1. Пришли мне аудиофайл или голосовое сообщение.\n"
    "2. Следующим сообщением напиши название и теги.\n"
    "Звук опубликуется в @MIFFFKI и станет доступен в поиске.\n"
    "Отменить незавершённое добавление — /cancel.\n\n"
    "<b>Загрузить конкретный звук с MyInstants:</b>\n"
    '<code>/loadsSearch "запрос"</code> — найду и загружу звук по названию. '
    "Если он уже есть в базе — пришлю уже существующую версию, а не буду "
    "публиковать заново.\n\n"
    "/help — показать это сообщение ещё раз."
)


@dp.message(Command("help"), F.chat.type == "private")
async def show_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text.startswith("/loads"))
async def loads_commands(message: Message) -> None:
    await mif_loader.handle_loads_commands(message)


@dp.message(F.chat.type == "private", F.audio | F.voice)
async def handle_audio_upload(message: Message, state: FSMContext) -> None:
    if message.audio is not None:
        file_id = message.audio.file_id
        file_type = "audio"
    elif message.voice is not None:
        file_id = message.voice.file_id
        file_type = "voice"
    else:
        await message.answer("Не удалось определить тип аудио.")
        return

    await state.update_data(file_id=file_id, file_type=file_type)
    await state.set_state(AddMif.waiting_for_description)

    await message.answer(
        "✅Аудио получено!\n"
        "⚠️Теперь обязательно отправь описание и теги.\n"
        "Для отмены отправь /cancel."
    )


@dp.message(
    AddMif.waiting_for_description,
    F.chat.type == "private",
    F.text,
)
async def handle_description(message: Message, state: FSMContext) -> None:
    user_description = (message.text or "").strip()

    if not user_description:
        await message.answer("⚠️Описание не может быть пустым.")
        return

    if len(user_description) > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            f"⚠️Описание слишком длинное. Используй не более "
            f"{MAX_DESCRIPTION_LENGTH} символов."
        )
        return

    user_data = await state.get_data()
    file_id = user_data.get("file_id")
    file_type = user_data.get("file_type", "audio")

    if not isinstance(file_id, str) or file_type not in {"audio", "voice"}:
        await state.clear()
        await message.answer("Срок ожидания описания истёк. Отправь аудио ещё раз.")
        return

    await message.answer("⏳Распознаю слова и готовлю голосовое сообщение...")

    try:
        bot_description, transcription_error, ogg_bytes, content_hash = await mif_core.prepare_audio(
            message.bot,
            file_id,
            file_type,
        )
    except RuntimeError as error:
        logger.exception("Не удалось подготовить аудио к публикации")
        await message.answer(
            f"⚠️{error}\n"
            "Добавление не отменено — можно отправить описание ещё раз "
            "или использовать /cancel."
        )
        return

    # Обнаружение повторок: сравниваем хэш нормализованного звука со всеми
    # уже сохранёнными. Разные битрейты/форматы одного и того же звука дадут
    # одинаковый хэш, потому что хэшируем уже нормализованный WAV.
    if content_hash:
        duplicate = mif_core.find_duplicate_by_hash(content_hash)
        if duplicate is not None:
            await state.clear()
            duplicate_title = duplicate.get("title") or "без названия"
            await message.answer(
                f"⚠️Такой звук уже есть в базе: «{duplicate_title}». "
                "Повторно не публикую.\n"
                "Если тебе кажется, что это ошибка — обрежь/измени файл немного "
                "и пришли ещё раз."
            )
            return

    displayed_bot_description = bot_description or "Речь не распознана."
    author = message.from_user
    if author is None:
        author_name = "неизвестный пользователь"
    elif author.username:
        author_name = f"@{author.username}"
    else:
        author_name = author.full_name

    base_caption = (
        "<b>Новый MIF добавлен!</b>\n\n"
        f"<b>Описание от пользователя:</b> "
        f"{html.escape(mif_core.clip_text(user_description))}\n"
        f"<b>Авто-описание от бота:</b> "
        f"{html.escape(mif_core.clip_text(displayed_bot_description))}\n"
        f"<b>Добавил:</b> {html.escape(author_name)}"
    )

    try:
        new_mif = await mif_core.publish_voice_mif(
            message.bot,
            ogg_bytes=ogg_bytes,
            existing_voice_file_id=file_id if file_type == "voice" else None,
            base_caption=base_caption,
            title=user_description,
            tags_text=user_description,
            bot_description=bot_description,
            content_hash=content_hash,
        )
    except TelegramAPIError:
        logger.exception("Не удалось опубликовать MIF в канале %s", mif_core.CHANNEL_ID)
        await message.answer(
            "⚠️Не удалось отправить звук в канал. Проверь, что бот добавлен "
            "администратором @MIFFFKI и имеет право публиковать сообщения.\n"
            "Добавление не отменено — можно исправить права и отправить описание ещё раз "
            "или использовать /cancel."
        )
        return

    await state.clear()
    if transcription_error:
        await message.answer(
            "✅Файл опубликован в @MIFFFKI и добавлен в поиск.\n"
            f"⚠️Авто-описание: {transcription_error}\n"
            "Описание и теги пользователя сохранены."
        )
    else:
        await message.answer(
            "✅Файл опубликован в @MIFFFKI и добавлен в поиск.\n"
            f"Авто-описание: {new_mif['bot_description']}"
        )


@dp.message(AddMif.waiting_for_description, F.chat.type == "private")
async def handle_non_text_description(message: Message) -> None:
    await message.answer(
        "⚠️Теперь обязательно отправь текстовое описание и теги "
        "или используй /cancel."
    )


# Ловушка на любую нераспознанную команду. Стоит ПОСЛЕДНЕЙ среди хендлеров
# приватного чата — сработает только если ничего более специфичное выше не
# подошло. Смысл — бот никогда не должен молчать в ответ на команду, даже
# если это опечатка.
@dp.message(F.chat.type == "private", F.text.startswith("/"))
async def handle_unknown_command(message: Message) -> None:
    text = (message.text or "").strip()
    hint = ""
    if text.lower().startswith("/load") and text.lower() != "/loads":
        hint = (
            "\nПохоже, это опечатка в команде автозагрузки — она называется "
            "именно <code>/loads</code> (с «s» на конце)."
        )
    await message.answer(
        f"Не знаю такую команду: {html.escape(text)}.{hint}\n"
        "Список доступных команд — /help.",
        parse_mode="HTML",
    )


@dp.inline_query()
async def search_mifs(query: InlineQuery) -> None:
    matches, best_score = mif_core.find_matching_mifs(query.query)
    results = []

    for mif in matches:
        mif_id = str(mif.get("id", ""))
        file_id = str(mif.get("file_id", ""))
        file_type = mif.get("file_type", mif.get("media_type", "voice"))
        title = str(mif.get("title", mif.get("user_description", "Звук")))

        # Пересылаем чистый звук без подписи, авторства и лишнего текста —
        # ровно то, что просили: файл берётся напрямую из "базы" канала.
        if file_type == "audio":
            results.append(
                InlineQueryResultCachedAudio(
                    id=mif_id,
                    audio_file_id=file_id,
                )
            )
        else:
            results.append(
                InlineQueryResultCachedVoice(
                    id=mif_id,
                    voice_file_id=file_id,
                    title=title[:64] or "Голосовое сообщение",
                )
            )

    await query.answer(
        results=results,
        cache_time=1,
        is_personal=True,
    )

    # Локальный поиск дал слабое совпадение (или вообще ничего) — пробуем
    # найти звук на MyInstants в фоне. НЕ делаем это до query.answer() и не
    # ждём результата здесь: инлайн-ответ Telegram должен прийти быстро, а
    # скачивание+конвертация+публикация занимают секунды. Если получится —
    # mif_loader сам пришлёт находку личным сообщением автору запроса.
    query_text = query.query.strip()
    if query_text and best_score < mif_core.FUZZY_MATCH_THRESHOLD:
        asyncio.create_task(
            mif_loader.background_internet_lookup(query.bot, query.from_user.id, query_text)
        )


async def main() -> None:
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not configured")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    async with Bot(token=bot_token) as bot:
        bot_info = await bot.get_me()
        logger.info("MIF bot started as @%s", bot_info.username)
        logger.info("Publishing new MIFs to %s", mif_core.CHANNEL_ID)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())