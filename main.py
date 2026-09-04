"""
Точка входа бота. Здесь только Telegram-хендлеры (регистрация команд, FSM,
инлайн-поиск) и запуск polling'а. Вся реальная логика — в mif_core.py
(обработка аудио, база, публикация, избранное) и mif_loader.py
(автозагрузка с MyInstants).

Избранное:
- ⭐ в конце inline-запроса означает добавить найденный звук в избранное;
- пустой inline-запрос показывает до 10 избранных + до 10 последних глобальных
  звуков;
- один и тот же звук в выдаче не дублируется;
- сам аудиофайл никогда не копируется — избранное хранит только связь
  user_id -> sound_id в базе.
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

# Ограничения inline-выдачи по умолчанию.
MAX_FAVORITES_RESULTS = 10
MAX_RECENT_RESULTS = 10
MAX_DEFAULT_RESULTS = MAX_FAVORITES_RESULTS + MAX_RECENT_RESULTS

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
        "Привет! Я бот для поиска и загрузки голосовых мем-звуков (MIF).\n\n"
        "Что я умею:\n"
        "🔍 Умный поиск: Пиши в любом чате через инлайн-режим (@MIFki_bot) "
        "— мгновенно поищу звук в базе, а если его нет — автоматически "
        "сбегаю на MyInstants и добавлю. О статусе напишу в личку.\n"
        "⭐ Избранное: добавь найденный звук в избранное, поставив ⭐ "
        "в конце запроса.\n"
        "➕ Добавление своих звуков:\n"
        "1. Отправь мне аудиофайл или голосовое.\n"
        "2. Напиши название и теги следующим сообщением.\n"
        "Звук сразу улетит в канал и станет доступен всем.\n\n"
        "Не хочешь получать уведомления об автопоиске — /mute "
        "(обратно — /unmute).\n"
        "Для отмены загрузки в любой момент — /cancel."
    )


@dp.message(Command("mute"), F.chat.type == "private")
async def mute_command(message: Message) -> None:
    if message.from_user is None:
        return

    mif_core.mute_user(message.from_user.id)

    await message.answer(
        "🔕Уведомления об автопоиске отключены — искать и добавлять звуки "
        "буду по-прежнему, просто больше не буду писать тебе об этом. "
        "Включить обратно — /unmute."
    )


@dp.message(Command("unmute"), F.chat.type == "private")
async def unmute_command(message: Message) -> None:
    if message.from_user is None:
        return

    mif_core.unmute_user(message.from_user.id)

    await message.answer("🔔Уведомления об автопоиске снова включены.")


# ⚠️ НАПОМИНАЛКА СЕБЕ: /help — единственное место, где обычные пользователи
# видят список команд. Каждый раз, когда добавляешь новую команду или
# меняешь поведение существующей — обнови текст ниже. Сюда идут ТОЛЬКО
# команды, доступные обычным людям. Админские (/loads, /loadsN, /loadsStop)
# сюда НЕ добавлять.
HELP_TEXT = (
    "🔊 <b>MIFs — звуковые мемы</b>\n\n"
    "<b>Найти звук:</b>\n"
    "В любом чате набери <code>@MIFki_bot запрос</code> — появится список "
    "подходящих звуков. Можно вводить несколько слов в любом порядке "
    "(например: «котик мем»).\n"
    "Если в базе ничего не нашлось, бот сам поищет на MyInstants. Статус "
    "придёт в личку: 🔍 ищу → ➖ нашёл, публикую → ✅ готово (или ⚠️ не "
    "нашёл). Сам файл в личку не присылается — как будет готово, найдёшь "
    "его тем же инлайн-поиском.\n\n"
    "<b>Избранное:</b>\n"
    "Чтобы добавить найденный звук в избранное, поставь ⭐ в конце запроса. "
    "Например: <code>@MIFki_bot мяу⭐</code>.\n"
    "Пустой запрос <code>@MIFki_bot</code> показывает до 10 твоих "
    "избранных звуков и до 10 последних глобальных звуков.\n\n"
    "<b>Добавить свой звук:</b>\n"
    "1. Пришли мне аудиофайл или голосовое сообщение.\n"
    "2. Следующим сообщением напиши название и теги.\n"
    "Звук опубликуется в @MIFFFKI и станет доступен в поиске.\n"
    "Отменить незавершённое добавление — /cancel.\n\n"
    "<b>Загрузить конкретный звук с MyInstants:</b>\n"
    '<code>/loadsSearch "запрос"</code> — найду и загружу звук по названию. '
    "Если он уже есть в базе — пришлю уже существующую версию, а не буду "
    "публиковать заново.\n\n"
    "<b>Уведомления:</b>\n"
    "/mute — отключить сообщения об автопоиске в личке.\n"
    "/unmute — включить обратно.\n\n"
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
        await message.answer(
            "Срок ожидания описания истёк. Отправь аудио ещё раз."
        )
        return

    await message.answer("⏳Распознаю слова и готовлю голосовое сообщение...")

    try:
        (
            bot_description,
            transcription_error,
            ogg_bytes,
            content_hash,
        ) = await mif_core.prepare_audio(
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
        logger.exception(
            "Не удалось опубликовать MIF в канале %s",
            mif_core.CHANNEL_ID,
        )
        await message.answer(
            "⚠️Не удалось отправить звук в канал. Проверь, что бот добавлен "
            "администратором @MIFFFKI и имеет право публиковать сообщения.\n"
            "Добавление не отменено — можно исправить права и отправить "
            "описание ещё раз или использовать /cancel."
        )
        return

    await state.clear()

    if status == "duplicate":
        # Проверка дубликата атомарна внутри publish_voice_mif.
        duplicate_title = new_mif.get("title") or "без названия"

        await message.answer(
            f"⚠️Такой звук уже есть в базе: «{duplicate_title}». "
            "Повторно не публикую.\n"
            "Если тебе кажется, что это ошибка — обрежь/измени файл немного "
            "и пришли ещё раз."
        )
        return

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


# Ловушка на любую нераспознанную команду.
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


def _build_inline_result(mif: dict, favorite: bool = False):
    """
    Преобразует запись MIF из базы в Telegram inline result.

    favorite=True меняет только отображаемое название.
    Данные самого MIF в базе не изменяются.
    """
    mif_id = str(mif.get("id", ""))
    file_id = str(mif.get("file_id", ""))
    file_type = mif.get(
        "file_type",
        mif.get("media_type", "voice"),
    )

    title = str(
        mif.get(
            "title",
            mif.get("user_description", "Звук"),
        )
    )

    if favorite:
        title = f"⭐ {title}"

    if file_type == "audio":
        return InlineQueryResultCachedAudio(
            id=mif_id,
            audio_file_id=file_id,
        )

    return InlineQueryResultCachedVoice(
        id=mif_id,
        voice_file_id=file_id,
        title=title[:64] or "Голосовое сообщение",
    )


def _merge_default_results(
    favorites: list[dict],
    recent: list[dict],
) -> list[tuple[dict, bool]]:
    """
    Формирует стандартную выдачу:

    1. максимум 10 избранных;
    2. максимум 10 последних глобальных;
    3. дубликаты между блоками удаляются;
    4. общий максимум — 20 результатов.

    Возвращаем (mif, is_favorite), чтобы ⭐ отображалась только
    у избранных результатов.
    """
    results: list[tuple[dict, bool]] = []
    already_added: set[str] = set()

    for mif in favorites[:MAX_FAVORITES_RESULTS]:
        mif_id = str(mif.get("id", ""))

        if not mif_id or mif_id in already_added:
            continue

        already_added.add(mif_id)
        results.append((mif, True))

    recent_added = 0

    for mif in recent:
        if recent_added >= MAX_RECENT_RESULTS:
            break

        mif_id = str(mif.get("id", ""))

        if not mif_id or mif_id in already_added:
            continue

        already_added.add(mif_id)
        results.append((mif, False))
        recent_added += 1

    return results[:MAX_DEFAULT_RESULTS]


@dp.inline_query()
async def search_mifs(query: InlineQuery) -> None:
    """
    Inline-поиск.

    Обычный запрос:
        @MIFki_bot мяу

    Добавление в избранное:
        @MIFki_bot мяу⭐

    Пустой запрос:
        @MIFki_bot

    Пустой запрос показывает:
        - до 10 избранных пользователя;
        - до 10 последних глобальных;
        - без дублей;
        - максимум 20 результатов.
    """
    raw_query = query.query.strip()

    # ⭐ считается специальным суффиксом только если находится
    # непосредственно в конце запроса.
    add_to_favorites = raw_query.endswith("⭐")

    if add_to_favorites:
        search_text = raw_query[:-1].strip()
    else:
        search_text = raw_query

    # ---------------------------------------------------------
    # ПУСТОЙ ЗАПРОС
    # ---------------------------------------------------------
    if not search_text:
        try:
            favorites = mif_core.get_favorite_mifs(
                query.from_user.id,
                limit=MAX_FAVORITES_RESULTS,
            )

            recent = mif_core.get_recent_mifs(
                limit=MAX_RECENT_RESULTS + MAX_FAVORITES_RESULTS,
            )
        except Exception:
            logger.exception(
                "Не удалось получить избранные/последние MIF для user_id=%s",
                query.from_user.id,
            )

            # Не ломаем inline-режим полностью, если БД временно дала ошибку.
            favorites = []
            recent = []

        merged_results = _merge_default_results(
            favorites,
            recent,
        )

        results = [
            _build_inline_result(
                mif,
                favorite=is_favorite,
            )
            for mif, is_favorite in merged_results
        ]

        await query.answer(
            results=results,
            cache_time=1,
            is_personal=True,
        )
        return

    # ---------------------------------------------------------
    # ОБЫЧНЫЙ ПОИСК
    # ---------------------------------------------------------
    matches, best_score = mif_core.find_matching_mifs(search_text)

    # Если это запрос с ⭐ — сохраняем лучший найденный звук.
    if add_to_favorites and matches:
        favorite_mif = matches[0]
        favorite_id = favorite_mif.get("id")

        if favorite_id is not None:
            try:
                mif_core.add_favorite(
                    query.from_user.id,
                    int(favorite_id),
                )
            except Exception:
                logger.exception(
                    "Не удалось добавить MIF id=%s в избранное пользователя %s",
                    favorite_id,
                    query.from_user.id,
                )

    results = [
        _build_inline_result(mif)
        for mif in matches
    ]

    await query.answer(
        results=results,
        cache_time=1,
        is_personal=True,
    )

    # ⭐ не должен мешать обычному fallback-поиску:
    # MyInstants ищется по очищенному запросу, без ⭐.
    if search_text and best_score < mif_core.FUZZY_MATCH_THRESHOLD:
        mif_loader.schedule_background_lookup(
            query.bot,
            query.from_user.id,
            search_text,
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

        logger.info(
            "MIF bot started as @%s",
            bot_info.username,
        )

        logger.info(
            "Publishing new MIFs to %s",
            mif_core.CHANNEL_ID,
        )

        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())