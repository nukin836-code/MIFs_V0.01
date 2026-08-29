"""
MyInstants: функции скрейпинга (используются mif_loader.py для /loads и
/loadsSearch) + необязательный самостоятельный batch-импортёр
(`python import_myinstants.py`).

Обработка звука (ffmpeg/хэш/распознавание) и публикация теперь полностью
идут через mif_core.py — здесь этой логики больше нет. Раньше она была
задублирована и со временем разъехалась с тем, что делает сам бот
(разные форматы публикации, разный дедуп) — вынос в общий модуль это
исключает: и живой /loads, и этот batch-скрипт гарантированно ведут себя
одинаково.

/loads в самом боте делает то же самое, что этот скрипт, но по команде в
чате — гонять этот файл вручную больше не обязательно, он оставлен как
самостоятельный вариант (например, для разового прогона без бота).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urljoin

import requests
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

import mif_core

logger = logging.getLogger("mif-importer")

BOT_TOKEN = os.getenv("BOT_TOKEN")

# 0 означает идти по страницам до первого настоящего 404.
MAX_PAGES = int(os.getenv("MYINSTANTS_MAX_PAGES", "0"))
START_PAGE = max(1, int(os.getenv("MYINSTANTS_START_PAGE", "1")))
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
# Пауза между звуками. Поднята с 2.5 до 5 сек по той же причине, что и в
# mif_loader.py: публикация — это два запроса к каналу подряд (send_voice +
# правка подписи), и Telegram ограничивает частоту сообщений в один и тот же
# чат/канал (flood control).
RATE_LIMIT_SECONDS = 5.0
PAGE_RETRIES = 4

MYINSTANTS_BASE_URL = "https://www.myinstants.com"
MYINSTANTS_PAGE_URL = f"{MYINSTANTS_BASE_URL}/ru/index/ru/?page={{page}}"
REQUEST_TIMEOUT = (15, 60)


class AudioTooLargeError(Exception):
    pass


def fetch_page(session: requests.Session, url: str) -> str:
    last_error: requests.RequestException | None = None
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text
        except requests.HTTPError as error:
            # 404 означает, что страницы закончились. Повторять такой запрос
            # бессмысленно; вызывающий код обработает его отдельно.
            if error.response is not None and error.response.status_code in {404, 410}:
                raise
            last_error = error
        except requests.RequestException as error:
            last_error = error

        if attempt < PAGE_RETRIES:
            delay = min(30.0, float(2 ** (attempt - 1)))
            logger.warning(
                "Ошибка загрузки страницы (попытка %d/%d), повтор через %.0f с: %s",
                attempt,
                PAGE_RETRIES,
                delay,
                url,
            )
            time.sleep(delay)

    assert last_error is not None
    raise last_error


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
    from bs4 import BeautifulSoup

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


async def import_sound(
    bot: Bot,
    session: requests.Session,
    sound: dict[str, str],
    existing_titles: set[str],
) -> bool:
    """Batch-версия того же пути, что mif_loader.import_one_sound — обе идут
    через mif_core.prepare_audio_from_bytes / mif_core.publish_voice_mif."""
    title = sound["title"]

    try:
        audio_bytes = await asyncio.to_thread(download_audio, session, sound["url"])
        logger.info("Скачан: %s (%.1f КБ)", title, len(audio_bytes) / 1024)

        try:
            bot_text, transcription_error, ogg_bytes, content_hash = (
                await mif_core.prepare_audio_from_bytes(audio_bytes)
            )
        except RuntimeError:
            logger.exception("Пропускаю (не удалось подготовить аудио): %s", title)
            return False

        if content_hash:
            duplicate = mif_core.find_duplicate_by_hash(content_hash)
            if duplicate is not None:
                logger.info(
                    "Пропуск повторки по хэшу: «%s» совпадает с «%s»",
                    title,
                    duplicate.get("title", "без названия"),
                )
                existing_titles.add(title.lower())
                return False

        displayed_bot_text = bot_text or "Речь не распознана."
        base_caption = (
            "<b>MIF с MyInstants</b>\n\n"
            f"<b>Название и теги пользователя:</b> "
            f"{html.escape(mif_core.clip_text(title))}\n"
            f"<b>Авто-описание от бота:</b> "
            f"{html.escape(mif_core.clip_text(displayed_bot_text))}\n"
            f"<b>Источник:</b> {html.escape(sound['url'])}"
        )

        await mif_core.publish_voice_mif(
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
        existing_titles.add(title.lower())

        if transcription_error:
            logger.warning("Добавлен без авто-описания: %s — %s", title, transcription_error)
        else:
            logger.info("Авто-описание получено для: %s", title)
        logger.info("Добавлен в базу: %s", title)
        return True
    except AudioTooLargeError:
        logger.info("Пропущен, больше 20 МБ: %s", title)
    except requests.RequestException:
        logger.exception("Не удалось скачать звук: %s", title)
    except TelegramAPIError:
        logger.exception("Telegram не принял звук для канала %s: %s", mif_core.CHANNEL_ID, title)
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

    existing_titles = {
        str(item.get("title", "")).strip().lower()
        for item in mif_core.MIFS_DATABASE
        if item.get("title")
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        )
    }
    total_added = 0

    page_limit = MAX_PAGES or "до конца пагинации"
    logger.info(
        "Начинаем импорт с MyInstants: стартовая страница=%s, лимит=%s, канал=%s",
        START_PAGE,
        page_limit,
        mif_core.CHANNEL_ID,
    )

    with requests.Session() as session:
        session.headers.update(headers)
        async with Bot(token=BOT_TOKEN) as bot:
            page = START_PAGE
            while MAX_PAGES == 0 or page < START_PAGE + MAX_PAGES:
                page_url = MYINSTANTS_PAGE_URL.format(page=page)
                try:
                    page_html = await asyncio.to_thread(fetch_page, session, page_url)
                    sounds = parse_page(page_html)
                except requests.HTTPError as error:
                    if error.response is not None and error.response.status_code in {404, 410}:
                        logger.info("Страницы закончились на странице %s.", page)
                        break
                    logger.exception("Не удалось загрузить страницу: %s", page_url)
                    page += 1
                    continue
                except requests.RequestException:
                    logger.exception("Не удалось загрузить страницу: %s", page_url)
                    page += 1
                    continue

                logger.info("Страница %s: найдено звуков=%s", page, len(sounds))
                if not sounds:
                    logger.warning(
                        "На странице %s не найдено звуков. Останавливаюсь, "
                        "чтобы не публиковать непредсказуемые страницы.",
                        page,
                    )
                    break

                for sound in sounds:
                    title_key = sound["title"].lower()
                    if title_key in existing_titles:
                        logger.info("Пропуск дубликата по названию: %s", sound["title"])
                        continue

                    if await import_sound(bot, session, sound, existing_titles):
                        total_added += 1

                    await asyncio.sleep(RATE_LIMIT_SECONDS)
                page += 1

    logger.info("Импорт завершён. Добавлено новых MIFов: %s", total_added)


if __name__ == "__main__":
    asyncio.run(main())