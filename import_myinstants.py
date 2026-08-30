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
from urllib.parse import quote, quote_plus, urljoin

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
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
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


class NotAudioContentError(Exception):
    """Сайт вернул не аудио — скорее всего HTML-страницу (капча/блокировка
    Cloudflare, либо просто страница ошибки), но с кодом 200, так что
    raise_for_status() это не ловит. Проверяем содержимое сами."""

    def __init__(self, content_type: str, preview: bytes) -> None:
        self.content_type = content_type
        self.preview = preview
        super().__init__(
            f"ожидался аудиофайл, получено content-type={content_type!r}, "
            f"начало ответа: {preview[:120]!r}"
        )


class CatalogBlockedError(Exception):
    """MyInstants ответил 403 — явный сигнал блокировки (не 404 "страницы
    нет", а именно отказ в доступе), стоит отличать от прочих сетевых ошибок."""


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


_CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁіІїЇєЄґҐ]")  # + украинские буквы i/ї/є/ґ, которых нет в русском

MYINSTANTS_SEARCH_URL = f"{MYINSTANTS_BASE_URL}/ru/search/?name={{query}}"

# Проверено напрямую: это настоящий JSON API (эндпоинт мобильного
# приложения сайта — у MyInstants есть app в Google Play), возвращает
# структурированные данные {"count":.., "results":[{"name":.., "sound":..,
# "slug":..}]}. Надёжнее HTML-скрейпинга — нет парсинга разметки, нет
# зависимости от того, не поменяли ли на сайте класс div. Теперь это
# первая стратегия в search_catalog; HTML-поиск и обход категорий остаются
# резервом на случай, если у API не окажется чего-то, что видно на сайте
# напрямую.
MYINSTANTS_API_URL = f"{MYINSTANTS_BASE_URL}/api/v1/instants/"


def _translate_to_english(query: str) -> str | None:
    """Если запрос на кириллице (русской ИЛИ украинской — не важно, какой
    именно, определяем сам факт кириллицы) — пробуем перевести на
    английский. source="auto" сам определит язык, не нужно гадать
    заранее — так это работает и для русского, и для украинского, и для
    чего угодно ещё. Тайтлы на MyInstants почти все английские, и нечёткое
    посимвольное сравнение кириллической фразы с ними физически не может
    дать высокий балл без перевода (нет общих букв). При любой ошибке
    перевода просто возвращаем None — вызывающий код тогда работает только
    с оригиналом."""
    if not _CYRILLIC_RE.search(query):
        return None
    try:
        from deep_translator import GoogleTranslator

        translated = GoogleTranslator(source="auto", target="en").translate(query)
        if translated:
            logger.info("Перевёл запрос «%s» → «%s»", query, translated)
            return translated.strip()
        return None
    except Exception:
        logger.warning("Не удалось перевести запрос «%s» на английский", query, exc_info=True)
        return None


def _parse_api_payload(payload: Any, query: str) -> list[dict[str, str]]:
    """Разбирает ответ JSON API в тот же формат {"title":.., "url":..}, что
    и HTML-парсер parse_page — вызывающему коду не важно, откуда пришли
    данные. Максимально защищено: сервер может прислать не совсем то, что
    ожидается (другая структура, отсутствующие поля) — на любой такой
    случай просто пропускаем конкретный элемент или возвращаем пустой
    список, а не падаем с KeyError/TypeError где-то в середине пайплайна."""
    if not isinstance(payload, dict):
        logger.warning("API вернул не JSON-объект для «%s»: %r", query, type(payload))
        return []

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        logger.warning("В ответе API для «%s» нет списка results", query)
        return []

    sounds: list[dict[str, str]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("name")
        mp3_url = item.get("sound")
        # Без названия или прямой ссылки на файл запись бесполезна дальше
        # по пайплайну (download_audio получит пустую строку и упадёт на
        # ровном месте) — пропускаем такие записи сразу здесь.
        if not title or not mp3_url:
            continue
        sounds.append({"title": str(title), "url": str(mp3_url)})

    return sounds


def _fetch_api_search_sync(session: requests.Session, query: str) -> list[dict[str, str]]:
    """Синхронная часть похода в JSON API (вызывается через
    asyncio.to_thread, как и весь остальной requests-код в этом файле —
    сознательно не завожу здесь aiohttp вторым HTTP-стеком параллельно с
    requests, это лишняя сложность без реальной пользы)."""
    url = f"{MYINSTANTS_API_URL}?name={quote_plus(query)}&format=json"
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        logger.warning("API-поиск для «%s»: сетевая ошибка", query, exc_info=True)
        return []

    if response.status_code != 200:
        logger.info("API-поиск для «%s»: HTTP %s, пропускаю этот источник", query, response.status_code)
        return []

    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "API-поиск для «%s»: ответ не распарсился как JSON (content-type=%r)",
            query,
            response.headers.get("content-type"),
        )
        return []

    sounds = _parse_api_payload(payload, query)
    logger.info("API-поиск для «%s»: получено %d записей", query, len(sounds))
    return sounds


async def _fetch_api_search(session: requests.Session, query: str) -> list[dict[str, str]]:
    return await asyncio.to_thread(_fetch_api_search_sync, session, query)


async def _fetch_site_search(session: requests.Session, query: str) -> list[dict[str, str]]:
    """Резерв №1 (если API ничего не дал): HTML-поиск сайта /ru/search/.
    Путь подтверждённо жив (в отличие от /en/search/, который отвечает
    404), но не проверено на 100%, фильтрует ли параметр name на стороне
    сайта. Поэтому дальше всё равно идёт через тот же fuzzy-скоринг, что и
    everything else — нерелевантное отсеется порогом."""
    url = MYINSTANTS_SEARCH_URL.format(query=quote_plus(query))
    try:
        page_html = await asyncio.to_thread(fetch_page, session, url)
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else None
        logger.info("HTML-поиск сайта для «%s»: HTTP %s, пропускаю этот источник", query, status)
        return []
    except requests.RequestException:
        logger.warning("HTML-поиск сайта для «%s» не удался сетевой ошибкой", query, exc_info=True)
        return []

    sounds = parse_page(page_html)
    logger.info(
        "Поиск сайта для «%s»: получено %d записей (до фильтрации по релевантности)",
        query,
        len(sounds),
    )
    return sounds


async def search_catalog(
    session: requests.Session,
    query: str,
    *,
    max_results: int = 10,
    min_score: float = mif_core.FUZZY_MATCH_THRESHOLD,
) -> list[tuple[float, dict[str, str]]]:
    """Ищет звук по произвольному тексту в три каскадных этапа, каждый
    следующий запускается только если предыдущий не дал ни одного
    совпадения ≥ min_score:

    1. JSON API (/api/v1/instants/?name=) — структурированные данные,
       официальный эндпоинт мобильного приложения сайта. Самый быстрый и
       надёжный источник, пробуется первым.
    2. HTML-поиск сайта (/ru/search/?name=) — подтверждённо живой путь
       (в отличие от /en/search/, отвечающего 404), но не проверено на
       100%, фильтрует ли он на стороне сайта.
    3. Обход первых SEARCH_PAGES_PER_CATEGORY страниц каждой категории —
       медленнее (секунд 5-15), но точно рабочий путь, проверенный
       напрямую, финальный резерв.

    На каждом этапе пробуются оба варианта запроса — оригинал и, если он
    на кириллице, английский перевод. Что бы ни вернул любой из трёх
    источников, оно всё равно проходит через один и тот же fuzzy-скоринг
    (mif_core.fuzzy_match_score) — нерелевантное отсеивается порогом
    независимо от того, насколько мы доверяем фильтрации на стороне сайта.

    Возвращает до max_results пар (балл, звук) с баллом >= min_score,
    отсортированных по убыванию релевантности. min_score решает вызывающий
    код для этапов 1-2 (настоящий поиск сайта — там есть хоть какая-то
    осмысленность в том, что вернулось): у /loadsSearch (человек явно
    попросил, можно честно показать "точного нет, но вот ближайшее") и у
    background_internet_lookup (публикует в общий канал сам) разные
    требования к строгости — по умолчанию строгий
    mif_core.FUZZY_MATCH_THRESHOLD, для best-effort передай
    mif_core.FUZZY_MATCH_FLOOR.

    ВАЖНО: этап 3 (обход категорий) ВСЕГДА требует как минимум
    mif_core.FUZZY_MATCH_THRESHOLD, даже если вызывающий код просил более
    низкий min_score. Проверено на практике: категории — это "всё, что там
    лежит", без всякой фильтрации от сайта, и на таком большом
    неотфильтрованном пуле слабый порог начинает случайно цеплять
    что-то просто по совпадающим буквам без всякой связи по смыслу
    (например, "лох пидр" и "мем человек паук" — совершенно разные
    фразы — оба цепляли одно и то же случайное "Error SOUNDSS").

    Кидает CatalogBlockedError, если категории (этап 3) явно отвечают 403
    (не "страницы нет", а "в доступе отказано").
    """
    translated = await asyncio.to_thread(_translate_to_english, query)
    query_variants = [query] + ([translated] if translated else [])
    logger.info("Поиск на MyInstants: запрос=%r, варианты для сравнения=%r", query, query_variants)

    seen_urls: set[str] = set()
    candidates: list[tuple[float, dict[str, str]]] = []

    def consider(sound: dict[str, str], stage_min_score: float) -> None:
        if sound["url"] in seen_urls:
            return
        seen_urls.add(sound["url"])
        best_score = max(
            mif_core.fuzzy_match_score(variant, sound["title"]) for variant in query_variants
        )
        if best_score >= stage_min_score:
            candidates.append((best_score, sound))

    # Этап 1: JSON API.
    for variant in query_variants:
        for sound in await _fetch_api_search(session, variant):
            consider(sound, min_score)

    # Этап 2 (резерв): HTML-поиск сайта — только если API не дал ничего
    # выше порога.
    if not candidates:
        logger.info("API не дал совпадений ≥%.0f для «%s», пробую HTML-поиск сайта", min_score, query)
        for variant in query_variants:
            for sound in await _fetch_site_search(session, variant):
                consider(sound, min_score)

    # Этап 3 (финальный резерв): обход категорий. Строгий порог всегда,
    # см. предупреждение в докстринге выше.
    if not candidates:
        category_scan_min_score = max(min_score, mif_core.FUZZY_MATCH_THRESHOLD)
        logger.info(
            "HTML-поиск тоже не дал совпадений ≥%.0f для «%s», сканирую категории "
            "(порог для этого этапа: ≥%.0f)",
            min_score,
            query,
            category_scan_min_score,
        )
        for category in MYINSTANTS_CATEGORIES:
            for page in range(1, SEARCH_PAGES_PER_CATEGORY + 1):
                url = _category_url(category, page)
                try:
                    page_html = await asyncio.to_thread(fetch_page, session, url)
                    sounds = parse_page(page_html)
                except requests.HTTPError as error:
                    status = error.response.status_code if error.response is not None else None
                    if status in {404, 410}:
                        break
                    if status == 403:
                        raise CatalogBlockedError(
                            f"MyInstants ответил 403 на категории «{category}» — "
                            "похоже на блокировку доступа"
                        ) from error
                    raise
                if not sounds:
                    break
                for sound in sounds:
                    consider(sound, category_scan_min_score)

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    logger.info(
        "Поиск на MyInstants для «%s»: итог %d кандидатов ≥%.0f баллов (лучший: %s)",
        query,
        len(candidates),
        min_score,
        f"{candidates[0][0]:.0f} «{candidates[0][1]['title']}»" if candidates else "нет",
    )
    return candidates[:max_results]


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

        content_type = response.headers.get("content-type", "")

    audio_bytes = b"".join(chunks)

    # raise_for_status() пропускает 200 OK — а капча/блок-страница Cloudflare
    # обычно ПРИХОДИТ именно с кодом 200, просто с HTML вместо файла.
    # Проверяем первые байты содержимого — надёжнее, чем гадать по невнятной
    # ошибке ffmpeg или DOCUMENT_INVALID от Telegram тремя шагами позже.
    head = audio_bytes[:512].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<?xml")) or b"<head" in head[:200]:
        raise NotAudioContentError(content_type, audio_bytes[:200])

    return audio_bytes


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
    except NotAudioContentError as error:
        logger.error(
            "«%s» скачался не как аудио (content-type=%r) — похоже на "
            "капчу/блокировку доступа: %s",
            title,
            error.content_type,
            sound["url"],
        )
        cept requests.RequestException:
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

                logger.info(
                    "Категория «%s», страница: найдено звуков=%s",
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