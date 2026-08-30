"""
Общая "инженерная" часть бота: локальная база MIFов, обработка аудио
(транскрипция, конвертация в Opus/OGG, хэш для дедупликации) и единая точка
публикации звука в канал.

Здесь нет ничего специфичного для Telegram-хендлеров (FSM, команды) — это
переиспользуют и main.py (ручная загрузка), и mif_loader.py (автозагрузка с
MyInstants), и import_myinstants.py (batch-режим). Благодаря этому все пути
публикации гарантированно ведут себя одинаково, независимо от источника
звука — именно раздвоение этой логики раньше и порождало баги.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import tempfile
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import speech_recognition as sr
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import BufferedInputFile
from rapidfuzz import fuzz

logger = logging.getLogger("mif-bot.core")

DATABASE_PATH = Path(__file__).with_name("mifs_database.json")
LEGACY_DATABASE_PATH = Path(__file__).with_name("mifs.json")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MIFFFKI")
MAX_CAPTION_TEXT_LENGTH = 300
TRANSCRIPTION_TIMEOUT_SECONDS = 20
# Битрейт голосового Opus. 32k с запасом хватает для разборчивой речи/мемов
# и держит файлы маленькими — то, что нужно для голосовых сообщений Telegram.
VOICE_OPUS_BITRATE = "32k"

# Параметры нормализации для хэша дубликатов. ВАЖНО: если поменяешь эти
# значения — поменяй точно так же в reconcile_channel.py (там своя копия,
# т.к. этот скрипт работает независимо от бота), иначе хэши перестанут
# совпадать между скриптами.
HASH_WAV_SAMPLE_RATE = "16000"
HASH_WAV_CHANNELS = "1"

# ПРИМЕЧАНИЕ: минимальный аналог функции баг-репортов — у тебя, по твоим
# словам, была своя, я её не получил. Если найдёшь/пришлёшь — просто замени
# тело report_bug() ниже на вызов твоей, остальной код от этого не зависит.
BUG_REPORT_CHAT_ID = os.getenv("BUG_REPORT_CHAT_ID", "-5476127508")

# Сколько раз повторить запрос к Telegram, если он ответил flood control
# (429 Too Many Requests), прежде чем сдаться и поднять исключение выше.
MAX_RATE_LIMIT_RETRIES = 3


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


# --- Поиск (точное вхождение + нечёткий рейтинг через rapidfuzz) -----------

# Инлайн-ответ Telegram не может содержать больше 50 результатов — если
# отдать больше, answerInlineQuery падает с ошибкой. Раньше при пустом или
# однобуквенном запросе под фильтр попадала вся база (пустой tokens-список
# ничего не отсеивал), и это тянуло на 50+ при росте базы через /loads.
MAX_INLINE_RESULTS = 50

# Название/теги, которые ввёл сам человек, весят больше, чем шумное
# авто-распознавание речи (Google STT нередко ошибается в словах и фоне).
TITLE_SOURCE_WEIGHT = 1.0
BOT_DESCRIPTION_SOURCE_WEIGHT = 0.65

# Порог применяется к УЖЕ ВЗВЕШЕННОМУ баллу — значит, чтобы совпадение по
# одному только bot_description (без поддержки со стороны user_tags) прошло
# порог, оно должно быть почти точным (0.65 × 100 ≈ 65 — как раз впритык).
# Это осознанно: раз STT часто ошибается, не даём шумному тексту тянуть на
# себя нечёткие совпадения так же легко, как проверенным пользовательским
# тегам.
FUZZY_MATCH_THRESHOLD = 65.0

# Более низкая планка — "хоть что-то похожее", а не "уверенное совпадение".
# Используется только для /loadsSearch, и ТОЛЬКО для результатов настоящего
# поиска сайта (API/HTML) — см. import_myinstants.search_catalog. Обход
# категорий (когда сайт вообще ничего не нашёл) всегда требует
# FUZZY_MATCH_THRESHOLD, даже в best-effort режиме: там нет фильтрации от
# сайта вообще, просто "всё, что лежит в категории", и слабый порог на
# таком большом неотфильтрованном пуле начинает случайно цеплять что-то
# просто по совпадающим буквам, без всякой связи по смыслу (проверено на
# практике — "лох пидр" и "мем человек паук" оба цеплялись за одно и то же
# случайное "Error SOUNDSS").
FUZZY_MATCH_FLOOR = 60.0


def fuzzy_match_score(query: str, text: str) -> float:
    """Простое (без разбивки по источникам и весам) нечёткое сравнение —
    для сопоставления с ВНЕШНИМИ данными вроде заголовков MyInstants, а не
    с нашей размеченной локальной базой. Точное вхождение подстроки — сразу
    100, иначе rapidfuzz.token_set_ratio (не зависит от порядка слов внутри
    text, терпим к мелким опечаткам)."""
    query = query.strip().lower()
    text = text.strip().lower()
    if not query or not text:
        return 0.0
    if query in text:
        return 100.0
    return fuzz.token_set_ratio(query, text)


def _token_match_score(token: str, text: str) -> float:
    if not text:
        return 0.0
    if token in text:
        return 100.0
    return fuzz.token_set_ratio(token, text)


def _score_mif_tokens(tokens: list[str], user_tags: str, bot_tags: str) -> float | None:
    """Взвешенный нечёткий поиск по токенам запроса. Для каждого токена
    берём лучший ВЗВЕШЕННЫЙ результат среди user_tags и bot_tags. Если хотя
    бы один токен не дотягивает до FUZZY_MATCH_THRESHOLD — запись не
    подходит (None), сохраняя поведение "каждое слово должно найтись
    где-то", просто теперь с нечёткостью вместо жёсткой подстроки. Итоговый
    рейтинг (для сортировки) — среднее по токенам."""
    total_weighted = 0.0
    for token in tokens:
        weighted = max(
            _token_match_score(token, user_tags) * TITLE_SOURCE_WEIGHT,
            _token_match_score(token, bot_tags) * BOT_DESCRIPTION_SOURCE_WEIGHT,
        )
        if weighted < FUZZY_MATCH_THRESHOLD:
            return None
        total_weighted += weighted
    return total_weighted / len(tokens)


def find_matching_mifs(query_text: str) -> tuple[list[dict[str, Any]], float]:
    """Ищет по локальной базе. Возвращает (топ-MAX_INLINE_RESULTS по
    убыванию релевантности, лучший_балл_среди_всех). Лучший балл нужен
    вызывающему коду (main.py), чтобы решить, стоит ли запускать фоновый
    поиск на MyInstants — см. mif_loader.background_internet_lookup.

    Пустой запрос: не фильтруем и не сортируем (нечего ранжировать),
    отдаём первые MAX_INLINE_RESULTS — именно это чинит падение при
    пустом/однобуквенном запросе, а не "фильтрация в один шаг".
    """
    tokens = [token for token in query_text.lower().strip().split() if token]

    if not tokens:
        # Пустой запрос — не запускаем фоновый интернет-поиск (нечего
        # искать), поэтому best_score = 100.0, а не 0.0.
        return list(MIFS_DATABASE[:MAX_INLINE_RESULTS]), 100.0

    scored: list[tuple[float, dict[str, Any]]] = []
    for mif in MIFS_DATABASE:
        user_tags = str(mif.get("user_tags", mif.get("tags", ""))).lower()
        bot_tags = str(mif.get("bot_tags", mif.get("bot_description", ""))).lower()

        score = _score_mif_tokens(tokens, user_tags, bot_tags)
        if score is None:
            continue
        scored.append((score, mif))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = scored[0][0] if scored else 0.0
    return [mif for _, mif in scored[:MAX_INLINE_RESULTS]], best_score


# Загружается один раз при первом импорте этого модуля и переиспользуется
# всеми остальными модулями (main.py, mif_loader.py, import_myinstants.py) —
# Python кеширует модули, так что это гарантированно один и тот же список.
MIFS_DATABASE: list[dict[str, Any]] = load_mifs()
if not DATABASE_PATH.exists() and LEGACY_DATABASE_PATH.exists():
    save_mifs()


# --- ffmpeg / хэш / распознавание речи --------------------------------------


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
    повторок независимо от исходного формата/битрейта/контейнера файла."""
    hasher = hashlib.sha256()
    with open(wav_path, "rb") as wav_file:
        for chunk in iter(lambda: wav_file.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def analyze_and_convert(
    source_path: Path,
    temporary_directory: Path,
    *,
    convert_voice: bool,
) -> tuple[str, str | None, bytes | None, str | None]:
    """Общее ядро обработки одного аудиофайла вне зависимости от того, откуда
    он взялся (загружен пользователем в Telegram или скачан с сайта):
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
    """Скачивает аудио, ранее загруженное пользователем в сам Telegram (по
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


# --- Публикация с защитой от flood control ----------------------------------

_T = TypeVar("_T")


async def _call_with_flood_retry(action: Callable[[], Awaitable[_T]]) -> _T:
    """Вызывает Telegram API-запрос; если Telegram отвечает flood control
    (429 Too Many Requests — aiogram поднимает TelegramRetryAfter), ждёт
    ровно столько, сколько он просит, и пробует ещё раз. Это дополняет паузу
    между попытками в mif_loader.py: пауза снижает вероятность лимита, а это
    — подчищает случаи, когда лимит всё равно сработал."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return await action()
        except TelegramRetryAfter as error:
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            wait_seconds = error.retry_after + 1
            logger.warning(
                "Flood control от Telegram, жду %.0f сек (попытка %d/%d)",
                wait_seconds,
                attempt + 1,
                MAX_RATE_LIMIT_RETRIES,
            )
            await asyncio.sleep(wait_seconds)
    raise RuntimeError("unreachable")  # pragma: no cover


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
    Используется и ручной загрузкой (main.py), и /loads/loadsSearch
    (mif_loader.py), и batch-импортом (import_myinstants.py) — благодаря
    этому все пути публикации ведут себя гарантированно одинаково.

    Один из ogg_bytes / existing_voice_file_id должен быть передан:
    existing_voice_file_id — когда источник уже voice-сообщение Telegram
    (конвертация не нужна, просто переиспользуем тот же файл); ogg_bytes —
    когда файл только что перегнан в Opus/OGG.

    Кидает TelegramAPIError, если публикация в канал не удалась даже после
    повторов при flood control — вызывающий код сам решает, как об этом
    сообщить.
    """
    voice_source: str | BufferedInputFile
    if existing_voice_file_id is not None:
        voice_source = existing_voice_file_id
    else:
        assert ogg_bytes is not None
        voice_source = BufferedInputFile(ogg_bytes, filename="voice.ogg")

    sent_message = await _call_with_flood_retry(
        lambda: bot.send_voice(
            chat_id=CHANNEL_ID,
            voice=voice_source,
            caption=base_caption,
            parse_mode="HTML",
        )
    )

    resolved_file_id = sent_message.voice.file_id
    final_caption = f"{base_caption}\n<b>file_id:</b> <code>{html.escape(resolved_file_id)}</code>"
    try:
        await _call_with_flood_retry(
            lambda: bot.edit_message_caption(
                chat_id=CHANNEL_ID,
                message_id=sent_message.message_id,
                caption=final_caption,
                parse_mode="HTML",
            )
        )
    except TelegramAPIError:
        # Кэш всё равно содержит правильный ID. Если подпись не удалось
        # изменить, reconcile_channel.py восстановит её через copy_message.
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