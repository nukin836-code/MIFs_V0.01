import asyncio
import hashlib
import html
import json
import logging
import os
import re
import speech_recognition as sr
import tempfile
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    InlineQuery,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedVoice,
    Message,
)

import import_myinstants as importer


logger = logging.getLogger("mif-bot")
DATABASE_PATH = Path(__file__).with_name("mifs_database.json")
LEGACY_DATABASE_PATH = Path(__file__).with_name("mifs.json")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MIFFFKI")
MAX_DESCRIPTION_LENGTH = 700
MAX_CAPTION_TEXT_LENGTH = 300
TRANSCRIPTION_TIMEOUT_SECONDS = 20
# Битрейт голосового Opus. 32k с запасом хватает для разборчивой речи/мемов
# и держит файлы маленькими — то, что нужно для голосовых сообщений Telegram.
VOICE_OPUS_BITRATE = "32k"

# Параметры нормализации для хэша дубликатов. ВАЖНО: если поменяешь эти
# значения — поменяй точно так же в reconcile_channel.py и import_myinstants.py,
# иначе хэши, посчитанные разными скриптами, перестанут совпадать.
HASH_WAV_SAMPLE_RATE = "16000"
HASH_WAV_CHANNELS = "1"

# Кому разрешено запускать /loads, /loadsN, /loadsStop. /loadsSearch доступен
# всем — это разовый точечный запрос, а не фоновый цикл.
LOADS_ADMIN_ID = int(os.getenv("LOADS_ADMIN_ID", "1297417116"))

# ПРИМЕЧАНИЕ: у тебя, по твоим словам, уже была своя функция баг-репортов в
# группу — я её не получил, поэтому написал минимальный аналог ниже
# (report_bug). Если у тебя есть готовая — просто замени тело report_bug на
# вызов твоей, остальной код от этого не зависит.
BUG_REPORT_CHAT_ID = os.getenv("BUG_REPORT_CHAT_ID", "-5476127508")

MYINSTANTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
    )
}
# Пауза между КАЖДОЙ попыткой автозагрузки (успешной, дублем или ошибкой) —
# имитирует то, как человек руками перекидывал бы звуки по одному.
LOADS_STEP_DELAY_SECONDS = 2.0


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
        HASH_WAV_SAMPLE_RATE,
        "-ac",
        HASH_WAV_CHANNELS,
        "-vn",
        str(wav_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    if process.returncode != 0:
        details = stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"ffmpeg завершился с кодом {process.returncode}: {details}")


async def convert_to_ogg_voice(source_path: Path, ogg_path: Path) -> None:
    """Перегоняет произвольный аудиофайл в Opus/OGG — формат, который Telegram
    использует для голосовых сообщений. -vn обязателен: у многих mp3 (в том
    числе с MyInstants) есть встроенная обложка, и без -vn ffmpeg пытается
    запихнуть её как видеопоток в ogg, из-за чего Telegram отвечает
    DOCUMENT_INVALID."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "libopus",
        "-b:a",
        VOICE_OPUS_BITRATE,
        "-vbr",
        "on",
        "-application",
        "voip",
        str(ogg_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    if process.returncode != 0:
        details = stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"ffmpeg (opus) завершился с кодом {process.returncode}: {details}")


def compute_content_hash(wav_path: Path) -> str:
    """Хэш нормализованного (16kHz mono) WAV — используется для обнаружения
    повторок независимо от исходного формата/битрейта/контейнера файла.
    ffmpeg-параметры нормализации должны совпадать с reconcile_channel.py
    и import_myinstants.py."""
    hasher = hashlib.sha256()
    with open(wav_path, "rb") as wav_file:
        for chunk in iter(lambda: wav_file.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_duplicate_by_hash(content_hash: str) -> dict[str, Any] | None:
    for mif in MIFS_DATABASE:
        if mif.get("content_hash") == content_hash:
            return mif
    return None


def find_duplicate_by_title(title: str) -> dict[str, Any] | None:
    title_key = title.strip().lower()
    for mif in MIFS_DATABASE:
        if str(mif.get("title", "")).strip().lower() == title_key:
            return mif
    return None


async def analyze_and_convert(
    source_path: Path,
    temporary_directory: Path,
    *,
    convert_voice: bool,
) -> tuple[str, str | None, bytes | None, str | None]:
    """Общее ядро обработки одного аудиофайла, вне зависимости от того,
    откуда он взялся (загружен пользователем в Telegram или скачан с сайта):
    1) распознаёт речь для авто-описания,
    2) считает хэш нормализованного звука — для обнаружения повторок,
    3) если convert_voice=True — перегоняет в Opus/OGG для публикации как
       голосового сообщения (не нужно, если источник уже voice).

    Возвращает (распознанный_текст, ошибка_распознавания, ogg_bytes_или_None,
    content_hash_или_None). Кидает RuntimeError, если понадобилась, но не
    получилась конвертация в Opus — это фатальная ошибка публикации.
    """
    wav_path = temporary_directory / "converted.wav"

    recognized_text = ""
    transcription_error: str | None = None
    content_hash: str | None = None

    try:
        await convert_to_wav(source_path, wav_path)
    except (OSError, RuntimeError):
        logger.exception("Не удалось конвертировать аудиофайл через ffmpeg (WAV)")
        transcription_error = "Не удалось обработать аудио для распознавания речи."
    else:
        content_hash = compute_content_hash(wav_path)
        try:
            recognizer = sr.Recognizer()
            recognizer.operation_timeout = TRANSCRIPTION_TIMEOUT_SECONDS

            with sr.AudioFile(str(wav_path)) as audio_source:
                audio_data = recognizer.record(audio_source)

            recognized_text = await asyncio.to_thread(
                recognizer.recognize_google,
                audio_data,
                language="ru-RU",
            )
            recognized_text = recognized_text.strip()
        except sr.UnknownValueError:
            logger.info("Речь в аудиофайле не распознана")
            transcription_error = "Речь в аудиофайле не распознана."
        except sr.RequestError:
            logger.exception("Сервис Speech-to-Text недоступен")
            transcription_error = "Сервис распознавания речи временно недоступен."
        except Exception:
            logger.exception("Неожиданная ошибка при распознавании аудио")
            transcription_error = "Произошла ошибка при распознавании речи."

    ogg_bytes: bytes | None = None
    if convert_voice:
        ogg_path = temporary_directory / "converted.ogg"
        try:
            await convert_to_ogg_voice(source_path, ogg_path)
        except (OSError, RuntimeError) as error:
            logger.exception("Не удалось перегнать аудио в Opus/OGG")
            raise RuntimeError(
                "Не удалось перегнать аудио в формат голосового сообщения (Opus/OGG)."
            ) from error
        ogg_bytes = ogg_path.read_bytes()

    return recognized_text, transcription_error, ogg_bytes, content_hash


async def prepare_audio(
    bot: Bot,
    file_id: str,
    file_type: str,
) -> tuple[str, str | None, bytes | None, str | None]:
    """Скачивает аудио, ранее загруженное пользователем В САМ TELEGRAM (по
    file_id), и прогоняет через analyze_and_convert."""
    try:
        telegram_file = await bot.get_file(file_id)
    except TelegramAPIError as error:
        raise RuntimeError("Не удалось скачать аудиофайл из Telegram.") from error

    if not telegram_file.file_path:
        raise RuntimeError("Telegram не вернул путь к аудиофайлу")

    with tempfile.TemporaryDirectory(prefix="mif-") as temporary_directory:
        temp_dir_path = Path(temporary_directory)
        remote_suffix = Path(telegram_file.file_path).suffix or ".audio"
        source_path = temp_dir_path / f"source{remote_suffix}"

        try:
            await bot.download_file(telegram_file.file_path, destination=source_path)
        except TelegramAPIError as error:
            raise RuntimeError("Не удалось скачать аудиофайл из Telegram.") from error

        return await analyze_and_convert(
            source_path,
            temp_dir_path,
            convert_voice=(file_type != "voice"),
        )




async def prepare_audio_from_bytes(
    audio_bytes: bytes,
    suffix: str = ".mp3",
) -> tuple[str, str | None, bytes | None, str | None]:
    """То же самое, но для аудио, скачанного НЕ из Telegram (например, с
    MyInstants) — исходник всегда нужно перегонять в Opus/OGG."""
    with tempfile.TemporaryDirectory(prefix="mif-web-") as temporary_directory:
        temp_dir_path = Path(temporary_directory)
        source_path = temp_dir_path / f"source{suffix}"
        source_path.write_bytes(audio_bytes)

        return await analyze_and_convert(source_path, temp_dir_path, convert_voice=True)


async def publish_voice_mif(
    bot: Bot,
    *,
    ogg_bytes: bytes | None,
    existing_voice_file_id: str | None,
    base_caption: str,
    title: str,
    tags_text: str,
    bot_description: str,
    content_hash: str | None,
    source_url: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Единая точка публикации: отправляет голосовое в канал, дозаписывает в
    подпись реальный (боту принадлежащий) file_id и сохраняет запись в базе.
    Используется и ручной загрузкой, и /loads, и /loadsSearch — благодаря
    этому все три пути гарантированно ведут себя одинаково.

    Один из ogg_bytes / existing_voice_file_id должен быть передан:
    existing_voice_file_id — когда источник уже voice-сообщение Telegram
    (конвертация не нужна, просто переиспользуем тот же файл); ogg_bytes —
    когда файл только что перегнан в Opus/OGG.

    Кидает TelegramAPIError, если публикация в канал не удалась — вызывающий
    код должен сам решить, как об этом сообщить.
    """
    voice_source: str | BufferedInputFile
    if existing_voice_file_id is not None:
        voice_source = existing_voice_file_id
    else:
        assert ogg_bytes is not None
        voice_source = BufferedInputFile(ogg_bytes, filename="voice.ogg")

    sent_message = await bot.send_voice(
        chat_id=CHANNEL_ID,
        voice=voice_source,
        caption=base_caption,
        parse_mode="HTML",
    )

    resolved_file_id = sent_message.voice.file_id
    final_caption = f"{base_caption}\n<b>file_id:</b> <code>{html.escape(resolved_file_id)}</code>"
    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=sent_message.message_id,
            caption=final_caption,
            parse_mode="HTML",
        )
    except TelegramAPIError:
        # Кэш всё равно содержит правильный ID. Если подпись не удалось
        # изменить, reconcile_channel.py восстановит его через copy_message.
        logger.exception(
            "Не удалось дописать фактический file_id в подпись поста %s",
            sent_message.message_id,
        )

    new_mif: dict[str, Any] = {
        "id": next_mif_id(),
        "title": title,
        "file_id": resolved_file_id,
        "file_type": "voice",
        "media_type": "voice",
        "user_description": title,
        "bot_description": bot_description,
        "user_tags": tags_text.lower(),
        "bot_tags": bot_description.lower(),
        "tags": tags_text.lower(),
        "channel_message_id": sent_message.message_id,
        "content_hash": content_hash,
    }
    if source_url:
        new_mif["source_url"] = source_url
    if extra_fields:
        new_mif.update(extra_fields)

    MIFS_DATABASE.append(new_mif)
    save_mifs()
    return new_mif
    async def report_bug(bot: Bot, text: str) -> None:
    """Шлёт короткое сообщение об ошибке в группу для баг-репортов."""
    try:
        await bot.send_message(chat_id=int(BUG_REPORT_CHAT_ID), text=clip_text(text, 3500))
    except (TelegramAPIError, ValueError):
        logger.exception("Не удалось отправить баг-репорт в группу")


MIFS_DATABASE = load_mifs()
if not DATABASE_PATH.exists() and LEGACY_DATABASE_PATH.exists():
    save_mifs()
dp = Dispatcher(storage=MemoryStorage())


class AddMif(StatesGroup):
    waiting_for_description = State()


class LoaderState:
    """Состояние фонового цикла /loads. Одно на процесс — параллельно
    запустить второй цикл нельзя (проверяется в handle_loads_start)."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.stop_event: asyncio.Event = asyncio.Event()
        self.added_count: int = 0
        self.target_count: int | None = None


loader_state = LoaderState()


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
        "2. Следующим сообщением напиши название и теги "
        "(например: «Оксимирон мем агрессия»).\n\n"
        "После этого звук попадёт в канал и станет доступен в поиске.\n"
        "Для отмены незавершённого добавления отправь /cancel."
    )


# ⚠️ НАПОМИНАЛКА СЕБЕ: /help — единственное место, где обычные пользователи
# видят список команд. Каждый раз, когда добавляешь новую команду или меняешь
# поведение существующей — обнови текст ниже. Сюда идут ТОЛЬКО команды,
# доступные обычным людям (не /loads, /loadsN, /loadsStop — это админские,
# их в публичном /help быть не должно).
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
    "<code>/loadsSearch \"запрос\"</code> — найду и загружу звук по названию. "
    "Если он уже есть в базе — пришлю уже существующую версию, а не буду "
    "публиковать заново.\n\n"
    "/help — показать это сообщение ещё раз."
)


@dp.message(Command("help"), F.chat.type == "private")
async def show_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


# --- /loads, /loadsN, /loadsStop, /loadsSearch ------------------------------
# Регистрируем ДО хендлера AddMif.waiting_for_description с F.text, чтобы эти
# команды перехватывались, даже если пользователь случайно написал их в
# процессе ручного добавления звука.

LOADS_SEARCH_RE = re.compile(r'^/loadsSearch\s+"?([^"]+?)"?\s*$')
LOADS_COUNT_RE = re.compile(r"^/loads(\d+)$")


async def import_one_sound(
    bot: Bot,
    session: requests.Session,
    sound: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    """Скачивает, обрабатывает и публикует один звук с MyInstants через тот
    же publish_voice_mif, что и ручная загрузка. Возвращает
    ('added' | 'duplicate' | 'error', запись_или_None)."""
    title = sound["title"]

    try:
        audio_bytes = await asyncio.to_thread(importer.download_audio, session, sound["url"])
    except importer.AudioTooLargeError:
        return "error", None
    except requests.RequestException as error:
        await report_bug(bot, f"Автозагрузка: не удалось скачать «{title}»: {error}")
        return "error", None

    try:
        bot_text, transcription_error, ogg_bytes, content_hash = await prepare_audio_from_bytes(
            audio_bytes
        )
    except RuntimeError as error:
        await report_bug(bot, f"Автозагрузка: не удалось обработать «{title}»: {error}")
        return "error", None

    if content_hash:
        duplicate = find_duplicate_by_hash(content_hash)
        if duplicate is not None:
            return "duplicate", duplicate

    displayed_bot_text = bot_text or "Речь не распознана."
    base_caption = (
        "<b>MIF с MyInstants (автозагрузка)</b>\n\n"
        f"<b>Название и теги пользователя:</b> {html.escape(clip_text(title))}\n"
        f"<b>Авто-описание от бота:</b> {html.escape(clip_text(displayed_bot_text))}\n"
        f"<b>Источник:</b> {html.escape(sound['url'])}"
    )

    try:
        new_mif = await publish_voice_mif(
            bot,
            ogg_bytes=ogg_bytes,
            existing_voice_file_id=None,
            base_caption=base_caption,
            title=title,
            tags_text=title,
            bot_description=bot_text,
            content_hash=content_hash,
            source_url=sound["url"],
        )
    except TelegramAPIError as error:
        await report_bug(bot, f"Автозагрузка: Telegram отклонил публикацию «{title}»: {error}")
        return "error", None

    if transcription_error:
        logger.warning("Добавлен без авто-описания: %s — %s", title, transcription_error)

    return "added", new_mif


async def run_loads_loop(bot: Bot, chat_id: int, target_count: int | None) -> None:
    session = requests.Session()
    session.headers.update(MYINSTANTS_HEADERS)

    try:
        page = 1
        empty_page_streak = 0

        while not loader_state.stop_event.is_set():
            if target_count is not None and loader_state.added_count >= target_count:
                break

            page_url = importer.MYINSTANTS_PAGE_URL.format(page=page)
            try:
                page_html = await asyncio.to_thread(importer.fetch_page, session, page_url)
                sounds = importer.parse_page(page_html)
            except requests.HTTPError as error:
                if error.response is not None and error.response.status_code in {404, 410}:
                    # Страницы закончились — начинаем сканирование заново.
                    page = 1
                    empty_page_streak = 0
                else:
                    await report_bug(bot, f"Автозагрузка: страница {page} не загрузилась: {error}")
                    page += 1
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue
            except requests.RequestException as error:
                await report_bug(bot, f"Автозагрузка: страница {page} не загрузилась: {error}")
                page += 1
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue

            if not sounds:
                empty_page_streak += 1
                page = 1 if empty_page_streak >= 2 else page + 1
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue
            empty_page_streak = 0

            for sound in sounds:
                if loader_state.stop_event.is_set():
                    break
                if target_count is not None and loader_state.added_count >= target_count:
                    break

                # Дешёвая предварительная проверка по названию — не тратим
                # скачивание и ffmpeg на то, что почти наверняка уже есть.
                if find_duplicate_by_title(sound["title"]) is not None:
                    await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                    continue

                try:
                    status, _ = await import_one_sound(bot, session, sound)
                except Exception as error:  # не даём фоновой задаче умереть молча
                    logger.exception("Автозагрузка: непредвиденная ошибка на «%s»", sound["title"])
                    await report_bug(
                        bot, f"Автозагрузка: непредвиденная ошибка на «{sound['title']}»: {error}"
                    )
                    status = "error"

                if status == "added":
                    loader_state.added_count += 1

                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)

            page += 1
    finally:
        try:
            await bot.send_message(
                chat_id,
                f"⏹Автозагрузка остановлена. Добавлено новых MIFов: {loader_state.added_count}.",
            )
        except TelegramAPIError:
            logger.exception("Не удалось отправить итоговое сообщение об автозагрузке")
        loader_state.task = None


async def handle_loads_start(message: Message, target_count: int | None) -> None:
    if loader_state.task is not None and not loader_state.task.done():
        await message.answer(
            "⚠️Автозагрузка уже запущена. Останови её через /loadsStop, если нужно "
            "начать заново."
        )
        return

    loader_state.stop_event = asyncio.Event()
    loader_state.added_count = 0
    loader_state.target_count = target_count
    loader_state.task = asyncio.create_task(
        run_loads_loop(message.bot, message.chat.id, target_count)
    )

    if target_count:
        await message.answer(
            f"▶️Автозагрузка запущена — цель: {target_count} новых MIFов.\n"
            "Остановить досрочно — /loadsStop."
        )
    else:
        await message.answer(
            "▶️Автозагрузка запущена (бесконечный цикл).\n"
            "Остановить — /loadsStop."
        )


async def handle_loads_stop(message: Message) -> None:
    if loader_state.task is None or loader_state.task.done():
        await message.answer("Автозагрузка сейчас не запущена.")
        return

    loader_state.stop_event.set()
    await message.answer(
        "⏸Останавливаю автозагрузку — доработаю текущую паузу (до "
        f"{LOADS_STEP_DELAY_SECONDS:.0f} сек) и остановлюсь."
    )


async def handle_loads_search(message: Message, query: str) -> None:
    if not query:
        await message.answer('Укажи запрос: /loadsSearch "текст".')
        return

    session = requests.Session()
    session.headers.update(MYINSTANTS_HEADERS)
    search_url = f"{importer.MYINSTANTS_BASE_URL}/ru/search/?name={quote_plus(query)}"

    try:
        page_html = await asyncio.to_thread(importer.fetch_page, session, search_url)
        sounds = importer.parse_page(page_html)
    except requests.RequestException:
        logger.exception("Ошибка поиска на MyInstants: %s", query)
        await message.answer("⚠️Не удалось обратиться к MyInstants. Попробуй ещё раз позже.")
        return

    if not sounds:
        await message.answer(f"На MyInstants ничего не нашлось по запросу «{query}».")
        return

    sound = sounds[0]

    # Тоже проверяем по названию до скачивания — быстрый путь для явных
    # повторов.
    existing_by_title = find_duplicate_by_title(sound["title"])
    if existing_by_title is not None:
        await message.answer(f"⚠️«{sound['title']}» уже есть в базе. Вот он:")
        try:
            await message.answer_voice(voice=existing_by_title["file_id"])
        except TelegramAPIError:
            logger.exception("Не удалось переслать существующий MIF пользователю")
        return

    status, entry = await import_one_sound(message.bot, session, sound)

    if status == "duplicate" and entry is not None:
        await message.answer(
            f"⚠️«{sound['title']}» по звуку совпадает с уже существующей записью "
            f"«{entry.get('title')}». Вот она:"
        )
        try:
            await message.answer_voice(voice=entry["file_id"])
        except TelegramAPIError:
            logger.exception("Не удалось переслать существующий MIF пользователю")
        return

    if status == "added" and entry is not None:
        await message.answer(f"✅Загрузил «{entry['title']}» и добавил в поиск.")
        return

    await message.answer(f"⚠️Не удалось загрузить «{sound['title']}». Попробуй другой запрос.")


@dp.message(F.chat.type == "private", F.text.startswith("/loads"))
async def handle_loads_commands(message: Message) -> None:
    text = (message.text or "").strip()

    search_match = LOADS_SEARCH_RE.match(text)
    if search_match:
        await handle_loads_search(message, search_match.group(1).strip())
        return

    if message.from_user is None or message.from_user.id != LOADS_ADMIN_ID:
        await message.answer("⛔Эта команда доступна только администратору автозагрузки.")
        return

    if text == "/loadsStop":
        await handle_loads_stop(message)
        return

    if text == "/loads":
        await handle_loads_start(message, target_count=None)
        return

    count_match = LOADS_COUNT_RE.match(text)
    if count_match:
        await handle_loads_start(message, target_count=int(count_match.group(1)))
        return

    await message.answer(
        "Не понял команду. Доступно:\n"
        "/loads — бесконечная автозагрузка\n"
        "/loads5 — загрузить 5 новых MIFов\n"
        "/loadsStop — остановить\n"
        '/loadsSearch "запрос" — найти и загрузить конкретный звук'
    )


# --- Ручное добавление звука через личку бота -------------------------------


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

    await message.answer("⏳Распознаю слова и готовлю голосовое сообщение...")

    try:
        bot_description, transcription_error, ogg_bytes, content_hash = await prepare_audio(
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
        duplicate = find_duplicate_by_hash(content_hash)
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
        f"{html.escape(clip_text(user_description))}\n"
        f"<b>Авто-описание от бота:</b> "
        f"{html.escape(clip_text(displayed_bot_description))}\n"
        f"<b>Добавил:</b> {html.escape(author_name)}"
    )

    try:
        new_mif = await publish_voice_mif(
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
        logger.exception("Не удалось опубликовать MIF в канале %s", CHANNEL_ID)
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
# подошло (в т.ч. если пользователь в процессе AddMif.waiting_for_description
# — тот хендлер матчится раньше и текст туда не попадёт как "неизвестная
# команда"). Смысл — бот никогда не должен молчать в ответ на команду,
# даже если это опечатка.
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
    user_input = query.query.lower().strip()
    # Мульти-поиск: бьём запрос на отдельные слова и требуем, чтобы КАЖДОЕ
    # слово нашлось где-то в тегах (не важно, в каком порядке и в каком
    # конкретно поле — пользователком или авто-описании от бота).
    tokens = [token for token in user_input.split() if token]
    results = []

    for mif in MIFS_DATABASE:
        user_tags = str(mif.get("user_tags", mif.get("tags", ""))).lower()
        bot_tags = str(
            mif.get("bot_tags", mif.get("bot_description", ""))
        ).lower()
        title = str(mif.get("title", mif.get("user_description", "Звук")))

        combined_tags = f"{user_tags} {bot_tags}"
        if tokens and not all(token in combined_tags for token in tokens):
            continue

        mif_id = str(mif.get("id", ""))
        file_id = str(mif.get("file_id", ""))
        file_type = mif.get("file_type", mif.get("media_type", "voice"))

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