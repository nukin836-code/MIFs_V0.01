"""
Скрипт сверки локального кэша mifs_database.json с историей канала @MIFFFKI.

ИДЕЯ: канал — источник правды. Этот скрипт проходит по ВСЕЙ истории канала и
добавляет в локальный JSON-кэш те записи, которых там почему-то нет.

Понимает два формата подписи:
  1) обычные посты бота — "Описание от пользователя: ..." + "file_id: ...";
  2) посты из import_myinstants.py — "Название и теги пользователя: ..." —
     БЕЗ строки file_id (в этом скрипте раньше был баг, из-за которого такие
     посты просто пропускались).

ВАЖНОЕ ТЕХНИЧЕСКОЕ ЗАМЕЧАНИЕ ПРО file_id:
file_id, который видно через личный Telegram-аккаунт (Pyrogram-сессию), НЕ
совпадает с file_id, которым может пользоваться бот — это разные
идентификаторы для одного и того же файла. Поэтому просто взять
`message.voice.file_id`, увиденный из-под пользователя, и положить его в базу
для бота — сломает пересылку (бот получит ошибку "wrong file identifier").

Поэтому для постов без явного `file_id:` в подписи скрипт работает в три
этапа:
  Фаза 1 (Pyrogram, читаем как пользователь): сканируем историю, парсим
          подписи. Если file_id явно есть в тексте — он уже был выдан боту,
          можно использовать напрямую.
  Фаза 2 (дедупликация): для постов без явного file_id скачиваем аудио и
          считаем хэш нормализованного звука. Если такой хэш уже есть в базе
          (например, звук раньше добавили вручную через бота под другим
          file_id) — пропускаем, это не новая запись.
  Фаза 3 (aiogram, от имени бота): для реально новых "осиротевших" постов бот
          сам копирует сообщение из канала себе в личку (copy_message) — это
          выдаёт новый, уже боту принадлежащий, file_id. Только после этого
          запись можно безопасно добавлять в базу.

НАСТРОЙКА:
1. API_ID и API_HASH с https://my.telegram.org/apps → в Secrets проекта.
2. BOT_TOKEN — тот же токен, что и у main.py.
3. ADMIN_CHAT_ID — числовой chat_id личного чата между тобой и ботом (нужен
   для Фазы 3). Проще всего: напиши боту /start в личку, затем узнай свой
   числовой id, например через @userinfobot, и пропиши его сюда.
4. `pip install pyrogram tgcrypto`.
5. Первый запуск: `python reconcile_channel.py` в Shell — попросит номер
   телефона и код в консоли, создаст файл сессии admin_session.session.
   Дальше повторные запуски логина не требуют.
   Для запусков без интерактивной консоли один раз сгенерируй строковую
   сессию (`await client.export_session_string()`) и передай её через
   переменную окружения ADMIN_SESSION_STRING.

Скрипт идемпотентен — повторные запуски просто не найдут новых записей.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from pyrogram import Client

logger = logging.getLogger("mif-reconcile")

DATABASE_PATH = Path(__file__).with_name("mifs_database.json")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MIFFFKI")
SESSION_NAME = "admin_session"

# ВАЖНО: эти параметры нормализации должны точно совпадать с
# HASH_WAV_SAMPLE_RATE / HASH_WAV_CHANNELS в main.py — иначе хэши,
# посчитанные ботом и этим скриптом, не будут совпадать.
HASH_WAV_SAMPLE_RATE = "16000"
HASH_WAV_CHANNELS = "1"

# Оба формата подписи, которые может писать наш бот/импортёр.
USER_DESC_RE = re.compile(
    r"(?:Описание от пользователя|Название и теги пользователя):\s*(.+)"
)
BOT_DESC_RE = re.compile(r"Авто-описание от бота:\s*(.+)")
FILE_ID_RE = re.compile(r"file_id:\s*(\S+)")
SOURCE_RE = re.compile(r"Источник:\s*(\S+)")


def load_database() -> list[dict[str, Any]]:
    if not DATABASE_PATH.exists():
        return []
    try:
        data = json.loads(DATABASE_PATH.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось прочитать базу MIFов: {DATABASE_PATH}") from error
    if not isinstance(data, list):
        raise RuntimeError("База MIFов должна содержать JSON-массив")
    return data


def save_database(database: list[dict[str, Any]]) -> None:
    temporary_path = DATABASE_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(database, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(DATABASE_PATH)


def next_id(database: list[dict[str, Any]]) -> int:
    numeric_ids = []
    for mif in database:
        try:
            numeric_ids.append(int(str(mif["id"])))
        except (KeyError, TypeError, ValueError):
            continue
    return max(numeric_ids, default=0) + 1


def parse_caption(caption: str) -> dict[str, str] | None:
    """Достаёт метаданные из подписи. Понимает оба шаблона (бот / MyInstants).
    Возвращает None, если это не пост нашего формата вообще."""
    user_desc_match = USER_DESC_RE.search(caption)
    bot_desc_match = BOT_DESC_RE.search(caption)

    if not user_desc_match and not bot_desc_match:
        return None

    file_id_match = FILE_ID_RE.search(caption)
    source_match = SOURCE_RE.search(caption)

    return {
        "file_id": file_id_match.group(1).strip() if file_id_match else "",
        "user_description": (user_desc_match.group(1).strip() if user_desc_match else ""),
        "bot_description": (bot_desc_match.group(1).strip() if bot_desc_match else ""),
        "source_url": source_match.group(1).strip() if source_match else "",
    }


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


def compute_content_hash(wav_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(wav_path, "rb") as wav_file:
        for chunk in iter(lambda: wav_file.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def download_and_hash(client: Client, message: Any) -> str | None:
    """Скачивает медиа сообщения через Pyrogram (это можно делать даже без
    боту-совместимого file_id) и считает хэш нормализованного звука."""
    with tempfile.TemporaryDirectory(prefix="mif-reconcile-") as temporary_directory:
        target_path = Path(temporary_directory) / f"orphan_{message.id}"
        try:
            downloaded = await client.download_media(message, file_name=str(target_path))
        except Exception:
            logger.exception("Не удалось скачать медиа сообщения %s для хэширования", message.id)
            return None

        if not downloaded:
            return None

        wav_path = Path(temporary_directory) / f"orphan_{message.id}.wav"
        try:
            await convert_to_wav(Path(downloaded), wav_path)
        except (OSError, RuntimeError):
            logger.exception("ffmpeg не смог нормализовать файл сообщения %s", message.id)
            return None

        return compute_content_hash(wav_path)


def build_entry(
    *,
    database: list[dict[str, Any]],
    file_id: str,
    file_type: str,
    user_description: str,
    bot_description: str,
    channel_message_id: int,
    content_hash: str | None,
    restored: bool = True,
) -> dict[str, Any]:
    title = user_description or bot_description or "Звук"
    return {
        "id": str(next_id(database)),
        "title": title,
        "file_id": file_id,
        "file_type": file_type,
        "user_description": user_description,
        "bot_description": bot_description,
        "user_tags": user_description.lower(),
        "bot_tags": bot_description.lower(),
        "tags": user_description.lower(),
        "channel_message_id": channel_message_id,
        "content_hash": content_hash,
        "restored_from_channel": restored,
    }


async def scan_and_merge(
    client: Client,
    database: list[dict[str, Any]],
    bot: Bot,
) -> tuple[int, list[dict[str, Any]]]:
    """Фаза 1 + Фаза 2. Возвращает (сколько добавлено напрямую, список
    "осиротевших" кандидатов, которым нужно фазу 3 — минтинг file_id ботом)."""
    known_file_ids = {str(mif.get("file_id")) for mif in database if mif.get("file_id")}
    known_channel_message_ids = {
        int(mif["channel_message_id"])
        for mif in database
        if str(mif.get("channel_message_id", "")).isdigit()
    }
    known_hashes = {str(mif.get("content_hash")) for mif in database if mif.get("content_hash")}

    added_direct = 0
    orphans: list[dict[str, Any]] = []

    async for message in client.get_chat_history(CHANNEL_ID):
        if not (message.voice or message.audio):
            continue
        if not message.caption:
            continue

        parsed = parse_caption(message.caption)
        if parsed is None:
            continue

        if message.id in known_channel_message_ids:
            continue

        explicit_file_id = parsed["file_id"]
        if explicit_file_id and explicit_file_id in known_file_ids:
            continue  # уже в базе, ничего скачивать не нужно

        content_hash = await download_and_hash(client, message)
        if content_hash and content_hash in known_hashes:
            logger.info(
                "Пропускаю сообщение %s — звук уже есть в базе (совпал хэш)",
                message.id,
            )
            continue

        file_type = "voice" if message.voice else "audio"

        if explicit_file_id:
            try:
                await bot.get_file(explicit_file_id)
            except TelegramAPIError:
                logger.warning(
                    "file_id в подписи сообщения %s не принадлежит боту или устарел; "
                    "восстановлю через copy_message",
                    message.id,
                )
                explicit_file_id = ""

        if explicit_file_id:
            entry = build_entry(
                database=database,
                file_id=explicit_file_id,
                file_type=file_type,
                user_description=parsed["user_description"],
                bot_description=parsed["bot_description"],
                channel_message_id=message.id,
                content_hash=content_hash,
            )
            database.append(entry)
            known_file_ids.add(explicit_file_id)
            if content_hash:
                known_hashes.add(content_hash)
            known_channel_message_ids.add(message.id)
            save_database(database)
            added_direct += 1
        else:
            orphans.append(
                {
                    "channel_message_id": message.id,
                    "file_type": file_type,
                    "user_description": parsed["user_description"],
                    "bot_description": parsed["bot_description"],
                    "content_hash": content_hash,
                }
            )
            known_channel_message_ids.add(message.id)

    return added_direct, orphans


async def mint_file_ids_for_orphans(
    orphans: list[dict[str, Any]],
    database: list[dict[str, Any]],
    bot: Bot,
) -> int:
    """Фаза 3. Для постов без file_id в подписи бот копирует сообщение из
    канала себе в личный чат с админом — это выдаёт боту собственный,
    рабочий file_id для того же файла."""
    if not orphans:
        return 0

    admin_chat_id = os.getenv("ADMIN_CHAT_ID")

    if not admin_chat_id:
        logger.warning(
            "Нашлось %d постов без file_id (например, из import_myinstants.py), "
            "но ADMIN_CHAT_ID не задан — пропускаю восстановление этих записей. "
            "Добавь секрет ADMIN_CHAT_ID и запусти скрипт ещё раз.",
            len(orphans),
        )
        return 0

    added = 0
    known_hashes = {str(mif.get("content_hash")) for mif in database if mif.get("content_hash")}

    for orphan in orphans:
        try:
            sent_message = await bot.copy_message(
                chat_id=int(admin_chat_id),
                from_chat_id=CHANNEL_ID,
                message_id=orphan["channel_message_id"],
            )
        except TelegramAPIError:
            logger.exception(
                "Не удалось скопировать сообщение %s для минтинга file_id "
                "(проверь, что боту написали /start и ADMIN_CHAT_ID верный)",
                orphan["channel_message_id"],
            )
            continue

        minted_file_id = None
        if sent_message.voice:
            minted_file_id = sent_message.voice.file_id
        elif sent_message.audio:
            minted_file_id = sent_message.audio.file_id

        if not minted_file_id:
            logger.warning(
                "Скопированное сообщение %s не содержит аудио — пропускаю",
                orphan["channel_message_id"],
            )
            continue

        content_hash = orphan["content_hash"]
        if content_hash and content_hash in known_hashes:
            continue

        entry = build_entry(
            database=database,
            file_id=minted_file_id,
            file_type=orphan["file_type"],
            user_description=orphan["user_description"],
            bot_description=orphan["bot_description"],
            channel_message_id=orphan["channel_message_id"],
            content_hash=content_hash,
        )
        database.append(entry)
        if content_hash:
            known_hashes.add(content_hash)
        save_database(database)
        added += 1

    return added


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("Заданы не все переменные окружения: API_ID, API_HASH")

    session_string = os.getenv("ADMIN_SESSION_STRING")
    client_kwargs: dict[str, Any] = {"api_id": int(api_id), "api_hash": api_hash}

    if session_string:
        client = Client(
            "admin_session_inmemory",
            in_memory=True,
            session_string=session_string,
            **client_kwargs,
        )
    else:
        client = Client(SESSION_NAME, **client_kwargs)

    database = load_database()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not configured")

    async with Bot(token=bot_token) as bot:
        try:
            channel = await bot.get_chat(CHANNEL_ID)
        except TelegramAPIError as error:
            raise RuntimeError(
                f"Бот не видит канал {CHANNEL_ID}. Проверь CHANNEL_ID, "
                "публичность канала и права бота-администратора."
            ) from error

        logger.info(
            "Канал доступен: %s (id=%s, type=%s)",
            channel.title or channel.username or CHANNEL_ID,
            channel.id,
            channel.type,
        )

        async with client:
            logger.info("Сканирую историю канала %s...", CHANNEL_ID)
            added_direct, orphans = await scan_and_merge(client, database, bot)

        logger.info(
            "Из истории канала напрямую добавлено %d записей, найдено %d "
            "'осиротевших' постов без рабочего file_id.",
            added_direct,
            len(orphans),
        )

        added_minted = await mint_file_ids_for_orphans(orphans, database, bot)

    total_added = added_direct + added_minted
    if total_added:
        save_database(database)
        logger.info("Итого добавлено %d новых записей.", total_added)
    else:
        logger.info("Локальная база уже синхронизирована с каналом, новых записей нет.")


if __name__ == "__main__":
    asyncio.run(main())
    