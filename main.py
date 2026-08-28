import asyncio
import html
import json
import logging
import os
import speech_recognition as sr
import tempfile
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
DATABASE_PATH = Path(__file__).with_name("mifs_database.json")
LEGACY_DATABASE_PATH = Path(__file__).with_name("mifs.json")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MIFFFKI")
MAX_DESCRIPTION_LENGTH = 700
MAX_CAPTION_TEXT_LENGTH = 300
TRANSCRIPTION_TIMEOUT_SECONDS = 20


DEFAULT_MIFS: list[dict[str, str]] = [
    {
        "id": "1",
        "title": "Звук 1",
        "file_id": "CQACAgIAAxkBAAEuHO9qkZ03pi_mLbWFOsWW01ynB2cZUAACQQ0AAu5m4EjwVpcEQ4qmpz0E",
        "tags": "окс миф 1",
        "media_type": "audio",
        "file_type": "audio",
        "user_tags": "окс миф 1",
        "bot_tags": "",
    },
    {
        "id": "2",
        "title": "Звук 2",
        "file_id": "CQACAgIAAxkBAAEuHPFqkZ0352jEAWpootn6YibYFZpZowACYGIAAt-jkEgtnX7stJl5Lj0E",
        "tags": "мем 2",
        "media_type": "audio",
        "file_type": "audio",
        "user_tags": "мем 2",
        "bot_tags": "",
    },
    {
        "id": "3",
        "title": "Звук 3",
        "file_id": "CQACAgIAAxkBAAEuHPJqkZ031jPFF2-6TwFG4V8JEyKEAgACDUkAAhDEgUqWSpFk4iZf9z0E",
        "tags": "мем 3",
        "media_type": "audio",
        "file_type": "audio",
        "user_tags": "мем 3",
        "bot_tags": "",
    },
    {
        "id": "4",
        "title": "Звук 4",
        "file_id": "CQACAgIAAxkBAAEuHPNqkZ03lpuLxHkyZ92-BqzLJ45uNAACtQoAAmHC0UpMkDlDJZnKTT0E",
        "tags": "мем 4",
        "media_type": "audio",
        "file_type": "audio",
        "user_tags": "мем 4",
        "bot_tags": "",
    },
    {
        "id": "5",
        "title": "Звук 5",
        "file_id": "CQACAgIAAxkBAAEuHPRqkZ03kWvsJw7X42bCEC1T5cOrYwAC_B4AAl0YqUpRRiT9sSu2Hj0E",
        "tags": "мем 5",
        "media_type": "audio",
        "file_type": "audio",
        "user_tags": "мем 5",
        "bot_tags": "",
    },
    {
        "id": "6",
        "title": "Звук 6",
        "file_id": "CQACAgIAAxkBAAEuHPBqkZ034-HvwqfoGRjcXCWQogOW4wACqX8AAiq0IUiw_aQAAVa0QZo9BA",
        "tags": "мем 6",
        "media_type": "audio",
        "file_type": "audio",
        "user_tags": "мем 6",
        "bot_tags": "",
    },
]


def load_mifs() -> list[dict[str, Any]]:
    source_path = DATABASE_PATH
    if not source_path.exists() and LEGACY_DATABASE_PATH.exists():
        source_path = LEGACY_DATABASE_PATH

    if not source_path.exists():
        return [dict(mif) for mif in DEFAULT_MIFS]

    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось прочитать базу MIFов: {source_path}") from error

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


def clip_text(value: str, max_length: int = MAX_CAPTION_TEXT_LENGTH) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 1].rstrip()}…"


async def convert_to_wav(source_path: Path, wav_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-vn",
        str(wav_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    if process.returncode != 0:
        details = stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"ffmpeg завершился с кодом {process.returncode}: {details}")


async def transcribe_audio(
    bot: Bot,
    file_id: str,
) -> tuple[str, str | None]:
    try:
        telegram_file = await bot.get_file(file_id)
        if not telegram_file.file_path:
            raise RuntimeError("Telegram не вернул путь к аудиофайлу")

        with tempfile.TemporaryDirectory(prefix="mif-") as temporary_directory:
            remote_suffix = Path(telegram_file.file_path).suffix or ".audio"
            source_path = Path(temporary_directory) / f"source{remote_suffix}"
            wav_path = Path(temporary_directory) / "converted.wav"

            await bot.download_file(
                telegram_file.file_path,
                destination=source_path,
            )
            await convert_to_wav(source_path, wav_path)

            recognizer = sr.Recognizer()
            recognizer.operation_timeout = TRANSCRIPTION_TIMEOUT_SECONDS

            with sr.AudioFile(str(wav_path)) as audio_source:
                audio_data = recognizer.record(audio_source)

            recognized_text = await asyncio.to_thread(
                recognizer.recognize_google,
                audio_data,
                language="ru-RU",
            )

            return recognized_text.strip(), None
    except sr.UnknownValueError:
        logger.info("Речь в аудиофайле не распознана")
        return "", "Речь в аудиофайле не распознана."
    except sr.RequestError:
        logger.exception("Сервис Speech-to-Text недоступен")
        return "", "Сервис распознавания речи временно недоступен."
    except TelegramAPIError:
        logger.exception("Telegram не дал скачать файл для распознавания")
        return "", "Не удалось скачать аудиофайл из Telegram."
    except (OSError, RuntimeError):
        logger.exception("Не удалось конвертировать аудиофайл через ffmpeg")
        return "", "Не удалось обработать аудиофайл на сервере."
    except Exception:
        logger.exception("Неожиданная ошибка при распознавании аудио")
        return "", "Произошла ошибка при распознавании речи."


MIFS_DATABASE = load_mifs()
if not DATABASE_PATH.exists() and LEGACY_DATABASE_PATH.exists():
    save_mifs()
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
        "⚠️Теперь обязательно отправь описание и теги "
        "(например: Оксимирон мем агрессия).\n"
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

    await message.answer("⏳Распознаю слова в аудиофайле...")
    bot_description, transcription_error = await transcribe_audio(
        message.bot,
        file_id,
    )

    displayed_bot_description = bot_description or "Речь не распознана."
    author = message.from_user
    if author is None:
        author_name = "неизвестный пользователь"
    elif author.username:
        author_name = f"@{author.username}"
    else:
        author_name = author.full_name

    post_caption = (
        "<b>Новый MIF добавлен!</b>\n\n"
        f"<b>Описание от пользователя:</b> "
        f"{html.escape(clip_text(user_description))}\n"
        f"<b>Авто-описание от бота:</b> "
        f"{html.escape(clip_text(displayed_bot_description))}\n"
        f"<b>file_id:</b> <code>{html.escape(file_id)}</code>\n"
        f"<b>Добавил:</b> {html.escape(author_name)}"
    )

    new_mif = {
        "id": next_mif_id(),
        "title": user_description,
        "file_id": file_id,
        "file_type": file_type,
        "media_type": file_type,
        "user_description": user_description,
        "bot_description": bot_description,
        "user_tags": user_description.lower(),
        "bot_tags": bot_description.lower(),
        "tags": user_description.lower(),
    }

    try:
        if file_type == "voice":
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
            "⚠️Не удалось отправить звук в канал. Проверь, что бот добавлен "
            "администратором @MIFFFKI и имеет право публиковать сообщения.\n"
            "Добавление не отменено — можно исправить права и отправить описание ещё раз "
            "или использовать /cancel."
        )
        return
    except OSError:
        logger.exception("Не удалось сохранить базу MIFов")
        await message.answer(
            "⚠️Файл отправлен в канал, но сохранить его в базе не удалось."
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
            f"Авто-описание: {bot_description}"
        )


@dp.message(AddMif.waiting_for_description, F.chat.type == "private")
async def handle_non_text_description(message: Message) -> None:
    await message.answer(
        "⚠️Теперь обязательно отправь текстовое описание и теги "
        "или используй /cancel."
    )


@dp.inline_query()
async def search_mifs(query: InlineQuery) -> None:
    user_input = query.query.lower().strip()
    results = []

    for mif in MIFS_DATABASE:
        user_tags = str(mif.get("user_tags", mif.get("tags", ""))).lower()
        bot_tags = str(
            mif.get("bot_tags", mif.get("bot_description", ""))
        ).lower()
        title = str(mif.get("title", mif.get("user_description", "Звук")))

        match_user = user_input in user_tags
        match_bot = user_input in bot_tags
        if user_input and not (match_user or match_bot):
            continue

        mif_id = str(mif.get("id", ""))
        file_id = str(mif.get("file_id", ""))
        file_type = mif.get("file_type", mif.get("media_type", "audio"))

        if file_type == "voice":
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