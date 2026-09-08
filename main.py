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
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedVoice,
    Message,
)

import mif_core
import mif_loader
import db_manager

logger = logging.getLogger("mif-bot")

MAX_DESCRIPTION_LENGTH = 700

dp = Dispatcher(storage=MemoryStorage())


class AddMif(StatesGroup):
    waiting_for_description = State()


# --- /start с выбором языка ---------------------------------------------------

_LANG_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский",    callback_data="lang_ru"),
        InlineKeyboardButton(text="🇬🇧 English",    callback_data="lang_en"),
        InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang_ua"),
    ]]
)

_START_TEXTS: dict[str, str] = {
    "ru": (
        "🔊 <b>MIFki Bot</b>\n\n"
        "Ищи звуки через инлайн: <code>@MIFki_bot запрос</code>\n"
        "Добавь свой — пришли аудиофайл в этот чат.\n\n"
        "/help — все команды"
    ),
    "en": (
        "🔊 <b>MIFki Bot</b>\n\n"
        "Search sounds via inline: <code>@MIFki_bot query</code>\n"
        "Add your own — send an audio file here.\n\n"
        "/help — all commands"
    ),
    "ua": (
        "🔊 <b>MIFki Bot</b>\n\n"
        "Шукай звуки через інлайн: <code>@MIFki_bot запит</code>\n"
        "Додай свій — надішли аудіофайл у цей чат.\n\n"
        "/help — всі команди"
    ),
}


@dp.message(Command("start"), F.chat.type == "private")
async def start_private_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Выбери язык / Choose language / Обери мову:",
        reply_markup=_LANG_KEYBOARD,
    )


@dp.callback_query(F.data.startswith("lang_"))
async def handle_language_choice(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    lang = (callback.data or "lang_ru").split("_", 1)[-1]
    text = _START_TEXTS.get(lang, _START_TEXTS["ru"])
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


# --- /cancel ------------------------------------------------------------------

@dp.message(Command("cancel"), F.chat.type == "private")
async def cancel_addition(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять.")
        return
    await state.clear()
    await message.answer("Добавление звука отменено.")


# --- /mute / /unmute ----------------------------------------------------------

@dp.message(Command("mute"), F.chat.type == "private")
async def mute_command(message: Message) -> None:
    if message.from_user is None:
        return
    mif_core.mute_user(message.from_user.id)
    await message.answer("🔕 Уведомления отключены. Включить — /unmute.")


@dp.message(Command("unmute"), F.chat.type == "private")
async def unmute_command(message: Message) -> None:
    if message.from_user is None:
        return
    mif_core.unmute_user(message.from_user.id)
    await message.answer("🔔 Уведомления включены.")


# --- /popular / /clear_history ------------------------------------------------

@dp.message(Command("popular"), F.chat.type == "private")
async def popular_command(message: Message) -> None:
    top_sounds = db_manager.get_popular_sounds(limit=10)
    if not top_sounds:
        await message.answer("Рейтинг пока пуст.")
        return

    keyboard_buttons = []
    for mif in top_sounds:
        mif_id = str(mif.get("id", ""))
        title = str(mif.get("title", "Звук"))
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"🔊 {title}", callback_data=f"play_{mif_id}")
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer("🏆 <b>Топ популярных звуков:</b>", parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("play_"))
async def play_popular_sound(callback: CallbackQuery) -> None:
    mif_id = callback.data.split("play_", 1)[-1]
    mif = db_manager.get_mif_by_id(mif_id) if hasattr(db_manager, "get_mif_by_id") else None

    if not mif:
        await callback.answer("❌ Звук не найден в базе", show_alert=True)
        return

    file_id = mif.get("file_id")
    file_type = mif.get("file_type", "voice")

    await callback.answer()
    if file_type == "audio":
        await callback.message.answer_audio(audio=file_id, caption=mif.get("title"))
    else:
        await callback.message.answer_voice(voice=file_id, caption=mif.get("title"))


@dp.message(Command("clear_history"), F.chat.type == "private")
async def clear_history_command(message: Message) -> None:
    if message.from_user is None:
        return
    db_manager.clear_history(message.from_user.id)
    await message.answer("🗑 История очищена. Избранное не тронуто.")


# --- /help --------------------------------------------------------------------

HELP_TEXT = (
    "🔊 <b>MIFki</b>\n\n"
    "<code>@MIFki_bot запрос</code> — найти звук в любом чате\n\n"
    "/popular — топ-20 популярных звуков\n"
    "/clear_history — очистить свою историю\n"
    "/mute · /unmute — уведомления об автопоиске\n"
    "/cancel — отменить добавление звука\n\n"
    "<b>Добавить звук:</b> пришли аудиофайл → напиши описание\n\n"
    '/loadsSearch "текст" — найти и добавить звук из интернета'
)

_HELP_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text="ℹ️ /info — о боте", callback_data="show_info"),
    ]]
)


@dp.message(Command("help"), F.chat.type == "private")
async def show_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=_HELP_KEYBOARD)


# --- /info --------------------------------------------------------------------

def _build_info_text() -> str:
    count = len(mif_core.MIFS_DATABASE)
    return (
        "ℹ️ <b>MIFki Bot</b>\n\n"
        f"Звуков в базе: <b>{count}</b>\n"
        "Канал: @MIFFFKI\n"
        "Поиск: @MIFki_bot\n\n"
        "Источники: MyInstants · TikTok\n"
        "Распознавание: Google STT (ru + en)\n"
        "Дедупликация: SHA-256 аудиохэш\n"
        "Защита от дублей: asyncio.Lock"
    )


@dp.message(Command("info"), F.chat.type == "private")
async def show_info(message: Message) -> None:
    await message.answer(_build_info_text(), parse_mode="HTML")


@dp.callback_query(F.data == "show_info")
async def handle_info_button(callback: CallbackQuery) -> None:
    await callback.message.answer(_build_info_text(), parse_mode="HTML")
    await callback.answer()


# --- /loads* ------------------------------------------------------------------

@dp.message(F.chat.type == "private", F.text.startswith("/loads"))
async def loads_commands(message: Message) -> None:
    await mif_loader.handle_loads_commands(message)


# --- Загрузка аудио пользователем --------------------------------------------

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
        "✅ Аудио получено!\n"
        "Теперь отправь описание и теги. /cancel — отменить."
    )


@dp.message(
    AddMif.waiting_for_description,
    F.chat.type == "private",
    F.text,
)
async def handle_description(message: Message, state: FSMContext) -> None:
    user_description = (message.text or "").strip()

    if not user_description:
        await message.answer("⚠️ Описание не может быть пустым.")
        return

    if len(user_description) > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            f"⚠️ Описание слишком длинное (макс. {MAX_DESCRIPTION_LENGTH} символов)."
        )
        return

    user_data = await state.get_data()
    file_id = user_data.get("file_id")
    file_type = user_data.get("file_type", "audio")

    if not isinstance(file_id, str) or file_type not in {"audio", "voice"}:
        await state.clear()
        await message.answer("Срок ожидания истёк. Отправь аудио ещё раз.")
        return

    await message.answer("⏳ Распознаю речь и готовлю файл...")

    try:
        bot_description, transcription_error, ogg_bytes, content_hash = (
            await mif_core.prepare_audio(message.bot, file_id, file_type)
        )
    except RuntimeError as error:
        logger.exception("Не удалось подготовить аудио к публикации")
        await message.answer(
            f"⚠️ {error}\n"
            "Добавление не отменено — отправь описание ещё раз или /cancel."
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
        status, new_mif = await mif_core.publish_voice_mif(
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
            "⚠️ Не удалось отправить в канал.\n"
            "Добавление не отменено — попробуй ещё раз или /cancel."
        )
        return

    await state.clear()

    if status == "duplicate":
        duplicate_title = new_mif.get("title") or "без названия"
        await message.answer(
            f"⚠️ Такой звук уже есть в базе: «{duplicate_title}».\n"
            "Обрежь или измени файл, если думаешь, что это ошибка."
        )
        return

    if transcription_error:
        await message.answer(
            "✅ Опубликовано в @MIFFFKI.\n"
            f"⚠️ Авто-описание: {transcription_error}"
        )
    else:
        await message.answer(
            "✅ Опубликовано в @MIFFFKI.\n"
            f"Авто-описание: {new_mif['bot_description']}"
        )


@dp.message(AddMif.waiting_for_description, F.chat.type == "private")
async def handle_non_text_description(message: Message) -> None:
    await message.answer("⚠️ Отправь текстовое описание или /cancel.")


# --- Неизвестные команды (ловушка, стоит последней) --------------------------

@dp.message(F.chat.type == "private", F.text.startswith("/"))
async def handle_unknown_command(message: Message) -> None:
    text = (message.text or "").strip()
    hint = ""
    if text.lower().startswith("/load") and not text.lower().startswith("/loads"):
        hint = (
            "\nПохоже, опечатка — команда называется "
            "<code>/loads</code> (с «s» на конце)."
        )
    await message.answer(
        f"Не знаю команду: {html.escape(text)}.{hint}\n/help — список команд.",
        parse_mode="HTML",
    )


# --- Инлайн-поиск ------------------------------------------------------------

@dp.inline_query()
async def search_mifs(query: InlineQuery) -> None:
    query_text = query.query.strip()

    if not query_text:
        matches = db_manager.get_personal_menu(query.from_user.id)
        best_score = 100.0
    else:
        matches, best_score = mif_core.find_matching_mifs(query_text)

    results = []
    for mif in matches:
        mif_id   = str(mif.get("id", ""))
        file_id  = str(mif.get("file_id", ""))
        file_type = mif.get("file_type", mif.get("media_type", "voice"))
        title    = str(mif.get("title", mif.get("user_description", "Звук")))

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

    await query.answer(results=results, cache_time=1, is_personal=True)

    if query_text and best_score < mif_core.FUZZY_MATCH_THRESHOLD:
        mif_loader.schedule_background_lookup(query.bot, query.from_user.id, query_text)


@dp.chosen_inline_result()
async def track_chosen_result(chosen: ChosenInlineResult) -> None:
    db_manager.record_usage(chosen.from_user.id, chosen.result_id)


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
    