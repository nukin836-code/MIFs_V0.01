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
from urllib.parse import quote, urljoin

import requests
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from bs4 import BeautifulSoup

import mif_core

logger = logging.getLogger("mif-importer")

BOT_TOKEN = os.getenv("BOT_TOKEN")

MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
# Пауза между звуками. Поднята с 2.5 до 5 сек по той же причине, что и в
# mif_loader.py: публикация — это два запроса к каналу подряд (send_voice +
# правка подписи), и Telegram ограничивает частоту сообщений в один и тот же
# чат/канал (flood control).
RATE_LIMIT_SECONDS = 5.0
PAGE_RETRIES = 4

MYINSTANTS_BASE_URL = "https://www.myinstants.com"
REQUEST_TIMEOUT = (15, 60)

MYINSTANTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
    )
}

# ВАЖНО, прочитай перед тем как трогать эти URL:
# Раньше здесь были /ru/search/?name= (поиск) и /ru/index/ru/?page=
# (пагинация). Проверено напрямую на живом сайте: ОБА мертвы — 404. Даже
# /en/search/, который использует старый сторонний бот-скрейпер, 404-ит, и
# даже голая ссылка /en/search/, которая до сих пор лежит в собственной
# навигации сайта, тоже 404 (протухшая ссылка у них самих). Похоже, сайт
# убрал серверный полнотекстовый поиск в пользу JS/AJAX-виджета, который
# просто так не воспроизвести.
#
# Единственное, что подтверждено рабочим прямо сейчас — страницы категорий:
# https://www.myinstants.com/en/categories/<название>/
# Этот список категорий и есть прямо из текущей навигации сайта.
MYINSTANTS_CATEGORIES: list[str] = [
    "anime & manga",
    "games",
    "memes",
    "movies",
    "music",
    "politics",
    "pranks",
    "reactions",
    "sound effects",
    "sports",
    "television",
    "tiktok trends",
    "viral",
    "whatsapp audios",
]
MYINSTANTS_CATEGORY_URL = f"{MYINSTANTS_BASE_URL}/en/categories/{{category}}/"

# Сколько страниц внутри ОДНОЙ категории просматривать при /loadsSearch —
# сайт больше не даёт искать по всему каталогу разом, так что это честный
# компромисс: покрываем популярное в каждой категории, а не гоняемся за
# буквально любым из "миллионов звуков", которые сайт рекламирует.
SEARCH_PAGES_PER_CATEGORY = 1


class AudioTooLargeError(Exception):
    pass


def _category_url(category: str, page: int) -> str:
    url = MYINSTANTS_CATEGORY_URL.format(category=quote(category, safe="&"))
    if page > 1:
        url = f"{url}?page={page}"
    return url


class CatalogPager:
    """Отслеживает позицию (категория + номер страницы) при бесконечном
    обходе каталога MyInstants для /loads. Сам не делает сетевых запросов —
    только говорит, какой URL запросить дальше, и как реагировать на
    результат. Так весь блокирующий I/O (requests) остаётся на стороне
    вызывающего кода (обычно под asyncio.to_thread), а эта штука — чистая,
    синхронная бухгалтерия."""

    def __init__(self) -> None:
        self._category_index = 0
        self._page = 1

    @property
    def current_url(self) -> str:
        return _category_url(MYINSTANTS_CATEGORIES[self._category_index], self._page)

    @property
    def current_category(self) -> str:
        return MYINSTANTS_CATEGORIES[self._category_index]

    def advance_page(self) -> None:
        self._page += 1

    def advance_category(self) -> None:
        self._category_index = (self._category_index + 1) % len(MYINSTANTS_CATEGORIES)
        self._page = 1


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


async def search_catalog(
    session: requests.Session,
    query: str,
    *,
    max_results: int = 10,
) -> list[dict[str, str]]:
    """Раз у сайта больше нет рабочего поиска по произвольному тексту —
    ищем сами: проходим по первым SEARCH_PAGES_PER_CATEGORY страницам
    каждой категории, собираем названия и нечётко сравниваем с query через
    mif_core.fuzzy_match_score. Возвращает до max_results совпадений,
    отсортированных по убыванию релевантности (только те, что прошли
    mif_core.FUZZY_MATCH_THRESHOLD).

    Это ~14 HTTP-запросов подряд (по одному на категорию) — секунд 5-15.
    Приемлемо для команды типа /loadsSearch (не инлайн-запрос, Telegram не
    ограничивает время ответа на обычное сообщение), но не для чего-то,
    что должно быть мгновенным.
    """
    candidates: list[tuple[float, dict[str, str]]] = []

    for category in MYINSTANTS_CATEGORIES:
        for page in range(1, SEARCH_PAGES_PER_CATEGORY + 1):
            url = _category_url(category, page)
            try:
                page_html = await asyncio.to_thread(fetch_page, session, url)
                sounds = parse_page(page_html)
            except requests.HTTPError as error:
                if error.response is not None and error.response.status_code in {404, 410}:
                    break
                raise
            if not sounds:
                break

            for sound in sounds:
                score = mif_core.fuzzy_match_score(query, sound["title"])
                if score >= mif_core.FUZZY_MATCH_THRESHOLD:
                    candidates.append((score, sound))

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [sound for _, sound in candidates[:max_results]]


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
    total_added = 0

    logger.info(
        "Начинаем импорт с MyInstants по категориям (%d шт.), канал=%s",
        len(MYINSTANTS_CATEGORIES),
        mif_core.CHANNEL_ID,
    )

    pager = CatalogPager()
    categories_without_new_content_in_a_row = 0

    with requests.Session() as session:
        session.headers.update(MYINSTANTS_HEADERS)
        async with Bot(token=BOT_TOKEN) as bot:
            # Обходим все категории по кругу. Останавливаемся сами, если
            # прошли полный круг категорий и нигде не нашли ничего нового —
            # иначе это будет буквально бесконечный процесс, что для
            # ручного batch-запуска (в отличие от /loads в самом боте)
            # неожиданно.
            while categories_without_new_content_in_a_row < len(MYINSTANTS_CATEGORIES):
                page_url = pager.current_url
                try:
                    page_html = await asyncio.to_thread(fetch_page, session, page_url)
                    sounds = parse_page(page_html)
                except requests.HTTPError as error:
                    if error.response is not None and error.response.status_code in {404, 410}:
                        pager.advance_category()
                        continue
                    logger.exception("Не удалось загрузить страницу: %s", page_url)
                    pager.advance_category()
                    continue
                except requests.RequestException:
                    logger.exception("Не удалось загрузить страницу: %s", page_url)
                    pager.advance_category()
                    continue

                logger.info("Категория «%s», страница: найдено звуков=%s",
                    pager.current_category,
                    len(sounds),
                )
                if not sounds:
                    categories_without_new_content_in_a_row += 1
                    pager.advance_category()
                    continue

                found_new_here = False
                for sound in sounds:
                    title_key = sound["title"].lower()
                    if title_key in existing_titles:
                        continue

                    found_new_here = True
                    if await import_sound(bot, session, sound, existing_titles):
                        total_added += 1

                    await asyncio.sleep(RATE_LIMIT_SECONDS)

                categories_without_new_content_in_a_row = (
                    0 if found_new_here else categories_without_new_content_in_a_row + 1
                )
                pager.advance_page()

    logger.info("Импорт завершён. Добавлено новых MIFов: %s", total_added)


if __name__ == "__main__":
    asyncio.run(main())