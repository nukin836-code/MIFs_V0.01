"""
Скрипт сверки локального кэша mifs_database.json с историей канала @MIFFFKI.

ИДЕЯ: канал — источник правды. Каждый пост, опубликованный ботом, содержит
служебную подпись с исходным file_id (тот самый file_id, который бот
использует, чтобы пересылать звук в инлайн-поиске). Этот скрипт проходит по
ВСЕЙ истории канала, вытаскивает все такие file_id и добавляет в локальный
JSON-кэш те записи, которых там почему-то нет («потеряшки»).

ВАЖНО про архитектуру: обычным Bot API историю канала прочитать нельзя — там
просто нет такого метода (бот получает только новые посты через
channel_post, отправленные после того как стал админом). Поэтому здесь
используется Pyrogram и вход как ПОЛЬЗОВАТЕЛЬ (аккаунт-админ канала), а не как
бот — у ботов доступ к получению полной истории канала через MTProto
ограничен.

НАСТРОЙКА:
1. Получи API_ID и API_HASH на https://my.telegram.org/apps (там же, где для
   обычных пользовательских Telegram-клиентов).
2. Пропиши их в переменные окружения API_ID и API_HASH проекта (в Replit —
   через вкладку Secrets).
3. Установи зависимость: `pip install pyrogram tgcrypto` (tgcrypto — просто
   ускоряет MTProto, не обязателен).
4. Первый запуск: `python reconcile_channel.py` в Shell — попросит номер
   телефона и код подтверждения прямо в консоли, создаст файл сессии
   admin_session.session рядом со скриптом. Дальше повторные запуски логина
   уже не требуют — сессия переиспользуется.
   Если интерактивной консоли нет (например, при автозапуске по расписанию),
   один раз локально сгенерируй строковую сессию через
   `await client.export_session_string()` и передай её через переменную
   окружения ADMIN_SESSION_STRING — тогда скрипт залогинится без вопросов.
5. Дальше можно просто периодически запускать этот скрипт вручную или по
   расписанию (например, крон-джобой в Replit) — он всегда идемпотентен:
   при повторном запуске просто ничего не найдёт нового.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from pyrogram import Client

logger = logging.getLogger("mif-reconcile")

DATABASE_PATH = Path(__file__).with_name("mifs_database.json")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MIFFFKI")
SESSION_NAME = "admin_session"

# Эти регулярки разбирают ту самую подпись, которую main.py пишет при
# публикации поста в канал (см. post_caption в main.py). Если формат подписи
# там поменяется — поменяй регулярки здесь тоже.
FILE_ID_RE = re.compile(r"file_id:\s*(\S+)")
USER_DESC_RE = re.compile(r"Описание от пользователя:\s*(.+)")
BOT_DESC_RE = re.compile(r"Авто-описание от бота:\s*(.+)")
AUTHOR_RE = re.compile(r"Добавил:\s*(.+)")


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
    """Достаёт file_id и метаданные из подписи к посту в канале.
    Возвращает None, если это не пост от нашего бота (нет file_id в подписи).
    """
    file_id_match = FILE_ID_RE.search(caption)
    if not file_id_match:
        return None

    user_desc_match = USER_DESC_RE.search(caption)
    bot_desc_match = BOT_DESC_RE.search(caption)
    author_match = AUTHOR_RE.search(caption)

    return {
        "file_id": file_id_match.group(1).strip(),
        "user_description": (user_desc_match.group(1).strip() if user_desc_match else ""),
        "bot_description": (bot_desc_match.group(1).strip() if bot_desc_match else ""),
        "author": (author_match.group(1).strip() if author_match else ""),
    }


async def scan_channel(client: Client) -> list[dict[str, Any]]:
    """Проходит по всей истории канала и собирает все записи со звуками."""
    found: list[dict[str, Any]] = []

    async for message in client.get_chat_history(CHANNEL_ID):
        if not (message.voice or message.audio):
            continue
        if not message.caption:
            continue

        parsed = parse_caption(message.caption)
        if parsed is None:
            continue

        parsed["channel_message_id"] = message.id
        parsed["file_type"] = "voice" if message.voice else "audio"
        found.append(parsed)

    return found


def merge_into_database(
    database: list[dict[str, Any]],
    channel_entries: list[dict[str, Any]],
) -> int:
    known_file_ids = {str(mif.get("file_id")) for mif in database}
    added = 0

    for entry in channel_entries:
        if entry["file_id"] in known_file_ids:
            continue

        title = entry["user_description"] or entry["bot_description"] or "Звук"
        new_id = str(next_id(database))
        database.append(
            {
                "id": new_id,
                "title": title,
                "file_id": entry["file_id"],
                "file_type": entry["file_type"],
                "user_description": entry["user_description"],
                "bot_description": entry["bot_description"],
                "user_tags": entry["user_description"].lower(),
                "bot_tags": entry["bot_description"].lower(),
                "tags": entry["user_description"].lower(),
                "channel_message_id": entry["channel_message_id"],
                "restored_from_channel": True,
            }
        )
        known_file_ids.add(entry["file_id"])
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

    async with client:
        logger.info("Сканирую историю канала %s...", CHANNEL_ID)
        channel_entries = await scan_channel(client)
        logger.info("В канале найдено %d постов со звуками", len(channel_entries))

    database = load_database()
    added = merge_into_database(database, channel_entries)

    if added:
        save_database(database)
        logger.info("Добавлено %d записей, которых не хватало в локальной базе.", added)
    else:
        logger.info("Локальная база уже синхронизирована с каналом, новых записей нет.")


if __name__ == "__main__":
    asyncio.run(main())