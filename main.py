import asyncio
import html
import json
import logging
import os
from json import JSONDecodeError
from pathlib import Path
from typing import Any

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


logger = logging.getLogger("mif-bot")
DATABASE_PATH = Path(__file__).with_name("mifs.json")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MiFFFki")
MAX_DESCRIPTION_LENGTH = 700


DEFAULT_MIFS: list[dict[str, str]] = [
    {
        "id": "1",
        "title": "Звук 1",
        "file_id": "CQACAgIAAxkBAAEuHO9qkZ03pi_mLbWFOsWW01ynB2cZUAACQQ0AAu5m4EjwVpcEQ4qmpz0E",
        "tags": "окс миф 1",
        "media_type": "audio",
    },
    {
        "id": "2",
        "title": "Звук 2",
        "file_id": "CQACAgIAAxkBAAEuHPFqkZ0352jEAWpootn6YibYFZpZowACYGIAAt-jkEgtnX7stJl5Lj0E",
        "tags": "мем 2",
        "media_type": "audio",
    },
    {
        "id": "3",
        "title": "Звук 3",
        "file_id": "CQACAgIAAxkBAAEuHPJqkZ031jPFF2-6TwFG4V8JEyKEAgACDUkAAhDEgUqWSpFk4iZf9z0E",
        "tags": "мем 3",
        "media_type": "audio",
    },
    {
        "id": "4",
        "title": "Звук 4",
        "file_id": "CQACAgIAAxkBAAEuHPNqkZ03lpuLxHkyZ92-BqzLJ45uNAACtQoAAmHC0UpMkDlDJZnKTT0E",
        "tags": "мем 4",
        "media_type": "audio",
    },
    {
        "id": "5",
        "title": "Звук 5",
        "file_id": "CQACAgIAAxkBAAEuHPRqkZ03kWvsJw7X42bCEC1T5cOrYwAC_B4AAl0YqUpRRiT9sSu2Hj0E",
        "tags": "мем 5",
        "media_type": "audio",
    },
    {
        "id": "6",
        "title": "Звук 6",
        "file_id": "CQACAgIAAxkBAAEuHPBqkZ034-HvwqfoGRjcXCWQogOW4wACqX8AAiq0IUiw_aQAAVa0QZo9BA",
        "tags": "мем 6",
        "media_type": "audio",
    },
]


def load_mifs() -> list[dict[str, Any]]:
    if not DATABASE_PATH.exists():
        return [dict(mif) for mif in DEFAULT_MIFS]

    try:
        data = json.loads(DATABASE_PATH.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось прочитать базу MIFов: {DATABASE_PATH}") from error

    if not isinstance(data, list):
        raise RuntimeError("База MIFов должна содержать JSON-массив")

    return data


def save_mifs() -> None:
    temporary_path = DATABASE_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(MIFS_DATABASE, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(DATABASE_PATH)


def next_mif_id() -> str:
    numeric_ids = []
    for mif in MIFS_DATABASE:
        try:
            numeric_ids.append(int(str(mif["id"])))
        except (KeyError, TypeError, ValueError):
            continue
    return str(max(numeric_ids, default=0) + 1)


MIFS_DATABASE = load_mifs()
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


@dp.message(F.chat.type == "private", F.audio | F.voice)
async def handle_audio_upload(message: Message, state: FSMContext) -> None:
    if message.audio is not None:
        file_id = message.audio.file_id
        media_type = "audio"
    elif message.voice is not None:
        file_id = message.voice.file_id
        media_type = "voice"
    else:
        await message.answer("Не удалось определить тип аудио.")
        return

    await state.update_data(file_id=file_id, media_type=media_type)
    await state.set_state(AddMif.waiting_for_description)

    await message.answer(
        "Аудио получено.\n"
        "Я автоматически определил file ID.\n\n"
        "Теперь обязательно отправь текстовое описание и теги "
        "(например: Оксимирон мем агрессия).\n"
        "Чтобы отменить добавление, отправь /cancel."
    )


@dp.message(
    AddMif.waiting_for_description,
    F.chat.type == "private",
    F.text,
)
async def handle_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()

    if not description:
        await message.answer("Описание не может быть пустым. Отправь текстовое описание.")
        return

    if len(description) > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            f"Описание слишком длинное. Используй не более {MAX_DESCRIPTION_LENGTH} символов."
        )
        return

    user_data = await state.get_data()
    file_id = user_data.get("file_id")
    media_type = user_data.get("media_type", "audio")

    if not isinstance(file_id, str) or media_type not in {"audio", "voice"}:
        await state.clear()
        await message.answer("Срок ожидания описания истёк. Отправь аудио ещё раз.")
        return

    author = message.from_user
    if author is None:
        author_name = "неизвестный пользователь"
    elif author.username:
        author_name = f"@{author.username}"
    else:
        author_name = author.full_name

    post_caption = (
        "<b>Новый MIF добавлен!</b>\n\n"
        f"<b>Описание:</b> {html.escape(description)}\n"
        f"<b>file_id:</b> <code>{html.escape(file_id)}</code>\n"
        f"<b>Добавил:</b> {html.escape(author_name)}"
    )

    new_mif = {
        "id": next_mif_id(),
        "title": description,
        "file_id": file_id,
        "tags": description.lower(),
        "media_type": media_type,
    }

    try:
        if media_type == "voice":
            await message.bot.send_voice(
                chat_id=CHANNEL_ID,
                voice=file_id,
                caption=post_caption,
                parse_mode="HTML",
            )
        else:
            await message.bot.send_audio(
                chat_id=CHANNEL_ID,
                audio=file_id,
                caption=post_caption,
                parse_mode="HTML",
            )

        MIFS_DATABASE.append(new_mif)
        save_mifs()
    except TelegramAPIError:
        logger.exception("Не удалось опубликовать MIF в канале %s", CHANNEL_ID)
        await message.answer(
            "Не удалось отправить звук в канал. Проверь, что бот добавлен "
            "администратором канала и имеет право публиковать сообщения.\n"
            "Текущее добавление не отменено — после исправления можно отправить "
            "описание ещё раз или использовать /cancel."
        )
        return
    except OSError:
        logger.exception("Не удалось сохранить базу MIFов")
        await message.answer(
            "Файл отправлен в канал, но сохранить его в локальную базу не удалось. "
            "Обратись к администратору бота."
        )
        return

    await state.clear()
    await message.answer(
        "Готово. Звук опубликован в канале, file ID и описание записаны, "
        "а MIF добавлен в inline-поиск."
    )


@dp.message(AddMif.waiting_for_description, F.chat.type == "private")
async def handle_non_text_description(message: Message) -> None:
    await message.answer(
        "Для добавления обязательно нужно текстовое описание. "
        "Отправь его обычным текстовым сообщением или используй /cancel."
    )


@dp.inline_query()
async def search_mifs(query: InlineQuery) -> None:
    user_input = query.query.lower().strip()
    results = []

    for mif in MIFS_DATABASE:
        title = str(mif.get("title", ""))
        tags = str(mif.get("tags", ""))
        if user_input and user_input not in title.lower() and user_input not in tags.lower():
            continue

        mif_id = str(mif.get("id", ""))
        file_id = str(mif.get("file_id", ""))
        media_type = mif.get("media_type", "audio")

        if media_type == "voice":
            results.append(
                InlineQueryResultCachedVoice(
                    id=mif_id,
                    voice_file_id=file_id,
                    title=title[:64] or "Голосовое сообщение",
                    caption=title,
                )
            )
        else:
            results.append(
                InlineQueryResultCachedAudio(
                    id=mif_id,
                    audio_file_id=file_id,
                    caption=title,
                )
            )

    await query.answer(
        results=results,
        cache_time=1,
        is_personal=True,
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
        logger.info("Publishing new MIFs to %s", CHANNEL_ID)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())