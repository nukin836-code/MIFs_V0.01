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
from urllib.parse import urljoin

import requests
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from bs4 import BeautifulSoup


logger = logging.getLogger("mif-importer")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MIFFFKI")
DB_FILE = Path(__file__).with_name("mifs_database.json")
LEGACY_DB_FILE = Path(__file__).with_name("mifs.json")

# На одной странице MyInstants обычно около 70 звуков.
PAGES_TO_PARSE = 2
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
RATE_LIMIT_SECONDS = 2.5
TRANSCRIPTION_TIMEOUT_SECONDS = 20
# Битрейт голосового Opus — держим в точности как в main.py, чтобы все MIFы
# были одинакового качества независимо от того, как они попали в базу.
VOICE_OPUS_BITRATE = "32k"

# ВАЖНО: эти параметры нормализации должны точно совпадать с main.py и
# reconcile_channel.py — иначе хэши для обнаружения повторок не совпадут
# между скриптами.
HASH_WAV_SAMPLE_RATE = "16000"
HASH_WAV_CHANNELS = "1"

MYINSTANTS_BASE_URL = "https://www.myinstants.com"
MYINSTANTS_PAGE_URL = f"{MYINSTANTS_BASE_URL}/ru/index/ru/?page={{page}}"
REQUEST_TIMEOUT = (15, 60)


class AudioTooLargeError(Exception):
    pass


def load_db() -> list[dict[str, Any]]:
    source_path = DB_FILE
    if not source_path.exists() and LEGACY_DB_FILE.exists():
        source_path = LEGACY_DB_FILE

    if not source_path.exists():
        return []

    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось прочитать базу {source_path}") from error

    if not isinstance(data, list):
        raise RuntimeError(f"Файл {source_path} должен содержать JSON-массив")

    return data


def save_db(db_data: list[dict[str, Any]]) -> None:
    temporary_path = DB_FILE.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(db_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(DB_FILE)


def next_mif_id(db_data: list[dict[str, Any]]) -> str:
    numeric_ids = []
    for item in db_data:
        try:
            numeric_ids.append(int(str(item["id"])))
        except (KeyError, TypeError, ValueError):
            continue
    return str(max(numeric_ids, default=0) + 1)


def find_duplicate_by_hash(db_data: list[dict[str, Any]], content_hash: str) -> dict[str, Any] | None:
    for item in db_data:
        if item.get("content_hash") == content_hash:
            return item
    return None


def fetch_page(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def extract_mp3_url(button: Any) -> str | None:
    onclick = str(button.get("onclick", ""))
    match = re.search(r"""play\(\s*['"]([^'"]+)['"]""", onclick)
    if match:
        return urljoin(MYINSTANTS_BASE_URL, html.unescape(match.group(1)))

    for attribute in ("data-url", "data-audio", "data-mp3"):
        value = button.get(attribute)
        if value:
            return urljoin(MYINSTANTS_BASE_URL, html.unescape(str(value)))

    return None


def parse_page(page_html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    sounds: list[dict[str, str]] = []

    for item in soup.find_all("div", class_="instant"):
        link_tag = item.find("a", class_="instant-link")
        button = item.find("button", class_="small-button")
        if link_tag is None or button is None:
            continue

        title = link_tag.get_text(" ", strip=True)
        mp3_url = extract_mp3_url(button)
        if title and mp3_url:
            sounds.append({"title": title, "url": mp3_url})

    return sounds


def download_audio(session: requests.Session, url: str) -> bytes:
    with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()

        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > MAX_FILE_SIZE_BYTES:
            raise AudioTooLargeError

        chunks: list[bytes] = []
        total_size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > MAX_FILE_SIZE_BYTES:
                raise AudioTooLargeError
            chunks.append(chunk)

    return b"".join(chunks)


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
    """Перегоняет mp3 в Opus/OGG — формат голосовых сообщений Telegram.
    -vn обязателен: у многих mp3 с MyInstants есть встроенная обложка, ffmpeg
    иначе пытается запихнуть её как видеопоток в ogg, и Telegram отвечает
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
    hasher = hashlib.sha256()
    with open(wav_path, "rb") as wav_file:
        for chunk in iter(lambda: wav_file.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def prepare_audio(
    audio_bytes: bytes,
    title: str,
) -> tuple[str, str | None, bytes, str | None]:
    """Из сырых mp3-байтов готовит: распознанный текст, ogg-байты для
    публикации как voice и хэш для обнаружения повторок. Скачивание уже
    произошло раньше — здесь только локальная обработка через ffmpeg."""
    with tempfile.TemporaryDirectory(prefix="mif-import-") as temporary_directory:
        source_path = Path(temporary_directory) / "source.mp3"
        wav_path = Path(temporary_directory) / "converted.wav"
        ogg_path = Path(temporary_directory) / "converted.ogg"
        source_path.write_bytes(audio_bytes)

        recognized_text = ""
        transcription_error: str | None = None
        content_hash: str | None = None

        try:
            await convert_to_wav(source_path, wav_path)
        except (OSError, RuntimeError):
            logger.exception("Не удалось конвертировать в WAV: %s", title)
            transcription_error = "Не удалось обработать звук через ffmpeg."
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
                logger.info("Речь не распознана: %s", title)
                transcription_error = "Речь не распознана."
            except sr.RequestError:
                logger.exception("Сервис распознавания недоступен: %s", title)
                transcription_error = "Сервис распознавания временно недоступен."
            except Exception:
                logger.exception("Неожиданная ошибка при распознавании: %s", title)
                transcription_error = "Произошла ошибка при распознавании речи."

        try:
            await convert_to_ogg_voice(source_path, ogg_path)
        except (OSError, RuntimeError) as error:
            logger.exception("Не удалось перегнать в Opus/OGG: %s", title)
            raise RuntimeError(f"Не удалось перегнать звук в голосовой формат: {title}") from error

        return recognized_text, transcription_error, ogg_path.read_bytes(), content_hash


def clip_text(value: str, max_length: int = 300) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 1].rstrip()}…"


async def import_sound(
    bot: Bot,
    session: requests.Session,
    db: list[dict[str, Any]],
    existing_titles: set[str],
    sound: dict[str, str],
) -> bool:
    title = sound["title"]

    try:
        audio_bytes = await asyncio.to_thread(download_audio, session, sound["url"])
        logger.info("Скачан: %s (%.1f КБ)", title, len(audio_bytes) / 1024)

        try:
            bot_text, transcription_error, ogg_bytes, content_hash = await prepare_audio(
                audio_bytes, title
            )
        except RuntimeError:
            logger.exception("Пропускаю (не удалось подготовить аудио): %s", title)
            return False

        # Обнаружение повторок по содержимому — ловит тот же звук даже если
        # он раньше был добавлен вручную под другим названием.
        if content_hash:
            duplicate = find_duplicate_by_hash(db, content_hash)
            if duplicate is not None:
                logger.info(
                    "Пропуск повторки по хэшу: «%s» совпадает с «%s»",
                    title,
                    duplicate.get("title", "без названия"),
                )
                existing_titles.add(title.lower())
                return False

        displayed_bot_text = bot_text or "Речь не распознана."
        post_caption = (
            "<b>MIF с MyInstants</b>\n\n"
            f"<b>Название и теги пользователя:</b> "
            f"{html.escape(clip_text(title))}\n"
            f"<b>Авто-описание от бота:</b> "
            f"{html.escape(clip_text(displayed_bot_text))}\n"
            f"<b>Источник:</b> {html.escape(sound['url'])}"
        )

        telegram_message = await bot.send_voice(
            chat_id=CHANNEL_ID,
            voice=BufferedInputFile(ogg_bytes, filename="voice.ogg"),
            caption=post_caption,
            parse_mode="HTML",
        )
        if telegram_message.voice is None:
            raise RuntimeError("Telegram не вернул объект voice после загрузки")

        file_id = telegram_message.voice.file_id

        # file_id узнаём только ПОСЛЕ отправки — дописываем его в подпись
        # поста постфактум. Это то, чего не хватало раньше: без этой строки
        # reconcile_channel.py не мог напрямую забрать пост, приходилось
        # выпрашивать file_id у бота отдельным шагом (copy_message).
        final_caption = (
            f"{post_caption}\n<b>file_id:</b> <code>{html.escape(file_id)}</code>"
        )
        try:
            await bot.edit_message_caption(
                chat_id=CHANNEL_ID,
                message_id=telegram_message.message_id,
                caption=final_caption,
                parse_mode="HTML",
            )
        except TelegramAPIError:
            logger.exception(
                "Не удалось дописать file_id в подпись для «%s» — публикация "
                "прошла, но при сверке этот пост попадёт в 'осиротевшие'",
                title,
            )

        db.append(
            {
                "id": next_mif_id(db),
                "title": title,
                "file_id": file_id,
                "file_type": "voice",
                "media_type": "voice",
                "user_description": title,
                "bot_description": bot_text,
                "user_tags": title.lower(),
                "bot_tags": bot_text.lower(),
                "tags": title.lower(),
                "source_url": sound["url"],
                "channel_message_id": telegram_message.message_id,
                "content_hash": content_hash,
            }
        )
        save_db(db)
        existing_titles.add(title.lower())

        if transcription_error:
            logger.warning("Добавлен без авто-описания: %s — %s", title, transcription_error)
        else:
            logger.info("Авто-описание получено для: %s", title)
        logger.info("Добавлен в базу: %s, file_id получен от Telegram", title)
        return True
    except AudioTooLargeError:
        logger.info("Пропущен, больше 20 МБ: %s", title)
    except requests.RequestException:
        logger.exception("Не удалось скачать звук: %s", title)
    except TelegramAPIError:
        logger.exception("Telegram не принял звук для канала %s: %s", CHANNEL_ID, title)
    except OSError:
        logger.exception("Не удалось сохранить базу после импорта: %s", title)
    except Exception:
        logger.exception("Ошибка при обработке: %s", title)

    return False


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    db = load_db()
    if not DB_FILE.exists() and LEGACY_DB_FILE.exists():
        save_db(db)

    existing_titles = {
        str(item.get("title", "")).strip().lower()
        for item in db
        if item.get("title")
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        )
    }
    total_added = 0

    logger.info("Начинаем импорт с MyInstants: страниц=%s, канал=%s", PAGES_TO_PARSE, CHANNEL_ID)

    with requests.Session() as session:
        session.headers.update(headers)
        async with Bot(token=BOT_TOKEN) as bot:
            for page in range(1, PAGES_TO_PARSE + 1):
                page_url = MYINSTANTS_PAGE_URL.format(page=page)
                try:
                    page_html = await asyncio.to_thread(fetch_page, session, page_url)
                    sounds = parse_page(page_html)
                except requests.RequestException:
                    logger.exception("Не удалось загрузить страницу: %s", page_url)
                    continue

                logger.info("Страница %s: найдено звуков=%s", page, len(sounds))

                for sound in sounds:
                    title_key = sound["title"].lower()
                    if title_key in existing_titles:
                        logger.info("Пропуск дубликата по названию: %s", sound["title"])
                        continue

                    if await import_sound(
                        bot,
                        session,
                        db,
                        existing_titles,
                        sound,
                    ):
                        total_added += 1

                    await asyncio.sleep(RATE_LIMIT_SECONDS)

    logger.info("Импорт завершён. Добавлено новых MIFов: %s", total_added)


if __name__ == "__main__":
    asyncio.run(main())