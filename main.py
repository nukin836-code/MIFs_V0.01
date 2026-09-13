"""
Точка входа бота. Только Telegram-хендлеры (команды, FSM, инлайн-поиск).
Вся базовая логика — в mif_core.py, работа с базами пользователей и рейтинга — в db_manager.py.
Mini App (веб-витрина звуков канала) — в webapp_api.py, здесь только команда
для её открытия и настройка кнопки меню бота.
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
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

import mif_core
import mif_loader
import db_manager
import i18n

logger = logging.getLogger("mif-bot")

MAX_DESCRIPTION_LENGTH = 700

# URL Mini App (веб-витрины звуков). Задаётся в .env/переменных окружения —
# см. webapp_api.py и start_miniapp.sh. Пока не задан — команда /app и
# кнопка меню просто молчат вместо падения, чтобы бот работал и без
# развёрнутого Mini App backend'а.
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip()

dp = Dispatcher(storage=MemoryStorage())


class AddMif(StatesGroup):
    waiting_for_description = State()


# --- Язык пользователя и /start ---------------------------------------------


def _message_language(message: Message) -> str:
    user = message.from_user
    if user is None:
        return i18n.DEFAULT_LANGUAGE
    return db_manager.ensure_user_language(user.id, user.language_code)


def _callback_language(callback: CallbackQuery) -> str:
    return db_manager.ensure_user_language(
        callback.from_user.id,
        callback.from_user.language_code,
    )


def _language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=i18n.LANGUAGE_LABELS["ru"], callback_data="lang_ru"),
            InlineKeyboardButton(text=i18n.LANGUAGE_LABELS["en"], callback_data="lang_en"),
            InlineKeyboardButton(text=i18n.LANGUAGE_LABELS["uk"], callback_data="lang_uk"),
        ]]
    )


@dp.message(Command("start"), F.chat.type == "private")
async def start_private_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    lang = _message_language(message)
    await message.answer(
        f"👋 {i18n.t('language_prompt', lang)}\n\n{i18n.t('start', lang)}",
        parse_mode="HTML",
        reply_markup=_language_keyboard(),
    )


@dp.message(Command("language"), F.chat.type == "private")
async def language_command(message: Message) -> None:
    lang = _message_language(message)
    await message.answer(
        i18n.t("language_current", lang, language_name=i18n.language_label(lang)),
        reply_markup=_language_keyboard(),
    )


@dp.callback_query(F.data.startswith("lang_"))
async def handle_language_choice(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    lang = i18n.normalize_language((callback.data or "lang_en").split("_", 1)[-1])
    db_manager.set_user_language(callback.from_user.id, lang)
    await callback.message.edit_text(i18n.t("start", lang), parse_mode="HTML")
    await callback.answer()


# --- /cancel ------------------------------------------------------------------

@dp.message(Command("cancel"), F.chat.type == "private")
async def cancel_addition(message: Message, state: FSMContext) -> None:
    lang = _message_language(message)
    if await state.get_state() is None:
        await message.answer(i18n.t("cancel_none", lang))
        return
    await state.clear()
    await message.answer(i18n.t("cancel_done", lang))


# --- /mute / /unmute ----------------------------------------------------------

@dp.message(Command("mute"), F.chat.type == "private")
async def mute_command(message: Message) -> None:
    if message.from_user is None:
        return
    lang = _message_language(message)
    mif_core.mute_user(message.from_user.id)
    await message.answer(i18n.t("mute_on", lang))

@dp.message(Command("unmute"), F.chat.type == "private")
async def unmute_command(message: Message) -> None:
    if message.from_user is None:
        return
    lang = _message_language(message)
    mif_core.unmute_user(message.from_user.id)
    await message.answer(i18n.t("mute_off", lang))


# --- /popular / /clear_history ------------------------------------------------

@dp.message(Command("popular"), F.chat.type == "private")
async def popular_command(message: Message) -> None:
    lang = _message_language(message)
    top_sounds = db_manager.get_popular_sounds(limit=20)
    if not top_sounds:
        await message.answer(i18n.t("popular_empty", lang))
        return

    keyboard_buttons = []
    for mif in top_sounds:
        mif_id = str(mif.get("id", ""))
        title = str(mif.get("title", mif.get("user_description", "Звук")))
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"🔊 {title[:32]}", callback_data=f"play_{mif_id}")
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer(
        i18n.t("popular_title", lang),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("play_"))
async def play_popular_sound(callback: CallbackQuery) -> None:
    lang = _callback_language(callback)
    mif_id = callback.data.split("play_", 1)[-1]
    # Используем правильную функцию вызова из mif_core:
    mif = mif_core.get_mif_by_id(mif_id)

    if not mif:
        await callback.answer(i18n.t("sound_not_found", lang), show_alert=True)
        return

    file_id = mif.get("file_id")
    file_type = mif.get("file_type", mif.get("media_type", "voice"))

    await callback.answer()
    if file_type == "audio":
        await callback.message.answer_audio(audio=file_id, caption=mif.get("title"))
    else:
        await callback.message.answer_voice(voice=file_id, caption=mif.get("title"))


@dp.message(Command("clear_history"), F.chat.type == "private")
async def clear_history_command(message: Message) -> None:
    if message.from_user is None:
        return
    lang = _message_language(message)
    db_manager.clear_history(message.from_user.id)
    await message.answer(i18n.t("history_cleared", lang))


# --- /app — открыть Mini App ---------------------------------------------------

@dp.message(Command("app"), F.chat.type == "private")
async def open_miniapp(message: Message) -> None:
    lang = _message_language(message)
    if not MINIAPP_URL:
        await message.answer(i18n.t("app_unconfigured", lang))
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=i18n.t("app_button", lang),
                web_app=WebAppInfo(url=MINIAPP_URL),
            )
        ]]
    )
    await message.answer(
        i18n.t("app_intro", lang),
        reply_markup=keyboard,
    )


# --- /help --------------------------------------------------------------------

def _help_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=i18n.t("button_info", lang),
                callback_data="show_info",
            ),
        ]]
    )


@dp.message(Command("help"), F.chat.type == "private")
async def show_help(message: Message) -> None:
    lang = _message_language(message)
    await message.answer(
        i18n.t("help", lang),
        parse_mode="HTML",
        reply_markup=_help_keyboard(lang),
    )


# --- /info --------------------------------------------------------------------
def _build_info_text(lang: str) -> str:
    count = len(mif_core.MIFS_DATABASE)
    return i18n.t("info", lang, count=count, channel=mif_core.CHANNEL_ID)


@dp.message(Command("info"), F.chat.type == "private")
async def show_info(message: Message) -> None:
    await message.answer(
        _build_info_text(_message_language(message)),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "show_info")
async def handle_info_button(callback: CallbackQuery) -> None:
    await callback.message.answer(
        _build_info_text(_callback_language(callback)),
        parse_mode="HTML",
    )
    await callback.answer()


# --- /loads* ------------------------------------------------------------------

@dp.message(F.chat.type == "private", F.text.startswith("/loads"))
async def loads_commands(message: Message) -> None:
    await mif_loader.handle_loads_commands(message)


# --- Загрузка аудио пользователем --------------------------------------------

@dp.message(F.chat.type == "private", F.audio | F.voice)
async def handle_audio_upload(message: Message, state: FSMContext) -> None:
    lang = _message_language(message)
    if message.audio is not None:
        file_id = message.audio.file_id
        file_type = "audio"
    elif message.voice is not None:
        file_id = message.voice.file_id
        file_type = "voice"
    else:
        await message.answer(i18n.t("audio_unknown", lang))
        return

    await state.update_data(file_id=file_id, file_type=file_type)
    await state.set_state(AddMif.waiting_for_description)
    await message.answer(i18n.t("audio_received", lang))


@dp.message(
    AddMif.waiting_for_description,
    F.chat.type == "private",
    F.text,
)
async def handle_description(message: Message, state: FSMContext) -> None:
    lang = _message_language(message)
    user_description = (message.text or "").strip()

    if not user_description:
        await message.answer(i18n.t("description_empty", lang))
        return

    if len(user_description) > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            i18n.t("description_too_long", lang, max_length=MAX_DESCRIPTION_LENGTH)
        )
        return

    user_data = await state.get_data()
    file_id = user_data.get("file_id")
    file_type = user_data.get("file_type", "audio")

    if not isinstance(file_id, str) or file_type not in {"audio", "voice"}:
        await state.clear()
        await message.answer(i18n.t("state_expired", lang))
        return

    await message.answer(i18n.t("processing_audio", lang))

    try:
        bot_description, transcription_error, ogg_bytes, content_hash = (
            await mif_core.prepare_audio(message.bot, file_id, file_type)
        )
    except RuntimeError as error:
        logger.exception("Не удалось подготовить аудио к публикации")
        await message.answer(
            i18n.t("prepare_error", lang, error=error)
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
            i18n.t("publish_error", lang)
        )
        return

    await state.clear()

    if status == "duplicate":
        duplicate_title = new_mif.get("title") or "без названия"
        await message.answer(
            i18n.t("duplicate", lang, title=duplicate_title)
        )
        return

    if transcription_error:
        await message.answer(
            i18n.t("published_with_warning", lang, error=transcription_error)
        )
    else:
        await message.answer(
            i18n.t("published", lang, description=new_mif["bot_description"])
        )


@dp.message(AddMif.waiting_for_description, F.chat.type == "private")
async def handle_non_text_description(message: Message) -> None:
    await message.answer(i18n.t("description_required", _message_language(message)))


# --- Неизвестные команды (ловушка, стоит последней) --------------------------

@dp.message(F.chat.type == "private", F.text.startswith("/"))
async def handle_unknown_command(message: Message) -> None:
    lang = _message_language(message)
    text = (message.text or "").strip()
    hint = (
        i18n.t("unknown_command_hint", lang)
        if text.lower().startswith("/load") and not text.lower().startswith("/loads")
        else ""
    )
    await message.answer(
        i18n.t("unknown_command", lang, command=html.escape(text), hint=hint),
        parse_mode="HTML",
    )


# --- Инлайн-поиск ------------------------------------------------------------

@dp.inline_query()
async def search_mifs(query: InlineQuery) -> None:
    db_manager.ensure_user_language(query.from_user.id, query.from_user.language_code)
    query_text = query.query.strip()

    if not query_text:
        # Пустой запрос: отдаем личное меню из db_manager (избранное + история или топ)
        matches = db_manager.get_personal_menu(query.from_user.id)
        best_score = 100.0
    else:
        matches, best_score = mif_core.find_matching_mifs(query_text)

    results = []
    for mif in matches:
        mif_id   = str(mif.get("id", ""))
        file_id  = str(mif.get("file_id", ""))
        file_type = mif.get("file_type", mif.get("media_type", "voice"))
        title    = str(mif.get("title", mif.get("user_description", "Sound")))

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
                    title=title[:64] or i18n.t("voice_message", db_manager.get_user_language(query.from_user.id)),
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

        if MINIAPP_URL:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="🔊 MIFki", web_app=WebAppInfo(url=MINIAPP_URL))
                )
                logger.info("Кнопка меню настроена на Mini App: %s", MINIAPP_URL)
            except TelegramAPIError:
                # Не фатально — /app как команда всё ещё работает, даже если
                # кнопку меню почему-то не удалось выставить (например, URL
                # ещё не HTTPS в момент рестарта туннеля).
                logger.exception("Не удалось выставить кнопку меню на Mini App")
        else:
            logger.info("MINIAPP_URL не задан — кнопка меню Mini App не настроена, доступна только /app")

        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())