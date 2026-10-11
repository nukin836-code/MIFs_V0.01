import asyncio
import logging
from urllib.parse import urlparse, parse_qs

import aiohttp
import requests
from bs4 import BeautifulSoup
from aiogram import Bot

import mif_bugs   # FIX: report_bug живёт здесь, не в mif_core
import mif_core

logger = logging.getLogger("mif-bot.tiktok")
logger.setLevel(logging.INFO)


class NotAudioContentError(Exception):
    def __init__(self, message: str, content_type: str = "unknown"):
        super().__init__(message)
        self.content_type = content_type


SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Origin": "https://lite.duckduckgo.com",
    "Referer": "https://lite.duckduckgo.com/",
}

SSSTIK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "HX-Request": "true",
    "HX-Current-URL": "https://ssstik.io/ru",
    "Origin": "https://ssstik.io",
    "Referer": "https://ssstik.io/ru",
}


async def search_catalog(
    session: requests.Session,
    query: str,
    min_score: int = 0,
    max_results: int = 5,
    bot: Bot | None = None,
) -> list[tuple[int, dict[str, str]]]:
    logger.info("🟢 [TIKTOK SEARCH START] Запрос: «%s» (min_score=%s)", query, min_score)

    search_query = f"tiktok {query}"
    url = "https://lite.duckduckgo.com/lite/"

    data = {"q": search_query, "kl": ""}

    try:
        logger.info("🌐 POST к DuckDuckGo Lite: '%s'", search_query)
        response = await asyncio.to_thread(
            session.post, url, data=data, headers=SEARCH_HEADERS, timeout=7
        )
        logger.info(
            "📥 Ответ DDG Lite: статус %s, %s байт",
            response.status_code, len(response.text),
        )

        if response.status_code != 200:
            logger.warning("❌ DDG Lite вернул статус: %s", response.status_code)
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        # Сохраняем порядок выдачи. set здесь ломал главный смысл этого
        # провайдера: пользовательский запрос должен идти в первый результат
        # поисковика, а не в случайную ссылку из множества.
        video_links: list[str] = []
        seen_links: set[str] = set()

        total_links_found = 0
        for a in soup.find_all("a", href=True):
            total_links_found += 1
            href = a["href"]

            if "uddg=" in href:
                try:
                    parsed_url = parse_qs(urlparse(href).query)
                    if "uddg" in parsed_url:
                        href = parsed_url["uddg"][0]
                except Exception:
                    pass

            if "tiktok.com" in href and (
                "/video/" in href or "vt.tiktok.com" in href or "/@" in href
            ):
                logger.info("🎯 TikTok ссылка: %s", href)
                if href not in seen_links:
                    seen_links.add(href)
                    video_links.append(href)

        logger.info(
            "📊 Просмотрено ссылок: %d. TikTok ссылок: %d",
            total_links_found, len(video_links),
        )

        if not video_links:
            logger.warning("⚠️ Нет TikTok ссылок для запроса: %s", query)
            logger.info("📄 Первые 300 символов DDG: %s", response.text[:300])
            return []

        results = []
        for i, link in enumerate(list(video_links)[:max_results]):
            display_title = f"{query} (TikTok #{i+1})"

            # FIX: убрана накрутка через difflib — она давала score ≥ 90 всегда,
            # потому что query буквально входит в display_title как подстрока.
            # Честный убывающий score: DDG уже отфильтровал по релевантности,
            # первый результат наиболее подходящий. Реального названия ролика
            # мы пока не знаем, поэтому не претендуем на 90+.
            final_score = max(0, 80 - i * 10)  # 80, 70, 60, 50...

            logger.info(
                "✨ Кандидат #%d: title='%s', link='%s', score=%d",
                i + 1, display_title, link, final_score,
            )

            if final_score >= min_score:
                results.append((final_score, {"title": display_title, "url": link}))
            else:
                logger.info(
                    "❌ Отсечён по score (%d < min_score %d)", final_score, min_score
                )

        logger.info("✅ Итог TikTok: %d кандидатов", len(results))
        return results

    except Exception as e:
        logger.exception("🚨 Ошибка в search_catalog (TikTok): %s", e)
        if bot:
            await mif_bugs.report_bug(bot, f"⚠️ import_tiktok search error: {e}")
        return []


def download_audio(session: requests.Session, tiktok_page_url: str) -> bytes:
    """Совместимый sync-интерфейс старого пути, теперь напрямую через SSSTik."""
    return download_audio_via_ssstik(session, tiktok_page_url)


def _extract_ssstik_download_link(html: str) -> str | None:
    """Извлекает прямую ссылку из ответа ssstik.io.

    SSSTik иногда меняет подписи кнопок, поэтому сначала ищем ссылку по
    смыслу кнопки, а затем используем более широкий резерв по доменам/расширениям.
    """
    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        text = link.get_text(" ", strip=True).lower()
        if (
            ("download" in text or "mp3" in text or "аудио" in text)
            and ("dl" in href or "mp3" in href or "tikcdn" in href)
        ):
            return href

    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if "tikcdn" in href or ".mp4" in href or ".mp3" in href:
            return href

    return None


def download_audio_via_ssstik(
    session: requests.Session,
    tiktok_page_url: str,
) -> bytes:
    """Медленный, но точный путь: TikTok URL → SSSTik → прямой медиафайл.

    Сначала забираем свежий скрытый токен ``tt`` со страницы SSSTik. Это
    важно: отправка старого пустого токена периодически возвращает HTML
    ошибки вместо ссылки на скачивание.
    """
    logger.info("📥 [SSSTIK DOWNLOAD START] Ссылка: %s", tiktok_page_url)

    landing = session.get(
        "https://ssstik.io/ru",
        headers=SSSTIK_HEADERS,
        timeout=10,
    )
    landing.raise_for_status()
    token_node = BeautifulSoup(landing.text, "html.parser").find(
        "input", {"name": "tt"}
    )
    token = str(token_node.get("value", "")) if token_node else ""
    if not token:
        raise RuntimeError("SSSTik не вернул защитный токен tt")

    parsed = session.post(
        "https://ssstik.io/abc?url=dl",
        data={"id": tiktok_page_url, "locale": "ru", "tt": token},
        headers=SSSTIK_HEADERS,
        timeout=15,
    )
    parsed.raise_for_status()

    media_url = _extract_ssstik_download_link(parsed.text)
    if not media_url:
        raise NotAudioContentError(
            "SSSTik не вернул прямую ссылку на медиафайл."
        )
    if media_url.startswith("//"):
        media_url = f"https:{media_url}"
    elif media_url.startswith("/"):
        media_url = f"https://ssstik.io{media_url}"
    elif not media_url.startswith(("http://", "https://")):
        media_url = f"https://ssstik.io/{media_url}"

    audio_response = session.get(media_url, timeout=25)
    audio_response.raise_for_status()
    content_type = audio_response.headers.get("Content-Type", "").lower()
    if "text" in content_type or "html" in content_type:
        raise NotAudioContentError(
            "SSSTik вернул HTML вместо аудио.",
            content_type=content_type,
        )
    if not audio_response.content:
        raise NotAudioContentError("SSSTik вернул пустой файл.")

    logger.info(
        "✅ [SSSTIK DOWNLOAD DONE] Получено %d байт",
        len(audio_response.content),
    )
    return audio_response.content


async def search_catalog_async(
    session: aiohttp.ClientSession,
    query: str,
    min_score: int = 0,
    max_results: int = 5,
) -> list[tuple[int, dict[str, str]]]:
    """Отменяемый asyncio-вариант DDG Lite поиска TikTok.

    Сохраняет точный порядок выдачи: первый TikTok URL — главный кандидат,
    как в прежнем пользовательском сценарии.
    """
    search_query = f"tiktok {query}"
    try:
        async with session.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": search_query, "kl": ""},
            headers=SEARCH_HEADERS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            if response.status != 200:
                logger.info("DDG Lite ответил HTTP %s", response.status)
                return []
            page_html = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.debug("DDG Lite поиск не удался для %r", query, exc_info=True)
        return []

    soup = BeautifulSoup(page_html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "uddg=" in href:
            href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
        if (
            "tiktok.com" in href.lower()
            and ("/video/" in href or "vt.tiktok.com" in href or "/@" in href)
            and href not in seen
        ):
            seen.add(href)
            links.append(href)

    results: list[tuple[int, dict[str, str]]] = []
    for index, link in enumerate(links[:max_results]):
        score = max(0, 80 - index * 10)
        if score >= min_score:
            results.append(
                (score, {"title": f"{query} (TikTok #{index + 1})", "url": link})
            )
    return results


async def download_audio_via_tikwm_async(
    session: aiohttp.ClientSession,
    tiktok_page_url: str,
) -> bytes:
    """Быстрый TikWM путь; вызывающий код включает yt-dlp как резерв."""
    headers = {"User-Agent": SEARCH_HEADERS["User-Agent"]}
    async with session.get(
        "https://www.tikwm.com/api/",
        params={"url": tiktok_page_url},
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=12),
    ) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)

    data = payload.get("data") if isinstance(payload, dict) else None
    media_url = (data or {}).get("music") or (data or {}).get("play")
    if not media_url:
        raise RuntimeError("TikWM не вернул ссылку на аудио")
    if media_url.startswith("/"):
        media_url = f"https://www.tikwm.com{media_url}"

    async with session.get(
        media_url,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        content = await response.read()
    if not content or "text" in content_type or "html" in content_type:
        raise NotAudioContentError(
            "TikWM вернул пустой ответ или HTML вместо аудио.",
            content_type=content_type,
        )
    return content


async def download_audio_via_ssstik_async(
    session: aiohttp.ClientSession,
    tiktok_page_url: str,
) -> bytes:
    """Точный резервный путь через SSSTik с динамическим токеном ``tt``.

    Использует cancellable aiohttp-запросы, поэтому при победе другого
    источника незавершённые SSSTik-запросы закрываются вместе с задачей.
    """
    async with session.get(
        "https://ssstik.io/ru",
        headers=SSSTIK_HEADERS,
        timeout=aiohttp.ClientTimeout(total=12),
    ) as response:
        response.raise_for_status()
        landing_html = await response.text()

    token_node = BeautifulSoup(landing_html, "html.parser").find(
        "input", {"name": "tt"}
    )
    token = str(token_node.get("value", "")) if token_node else ""
    if not token:
        raise RuntimeError("SSSTik не вернул защитный токен tt")

    async with session.post(
        "https://ssstik.io/abc?url=dl",
        data={"id": tiktok_page_url, "locale": "ru", "tt": token},
        headers=SSSTIK_HEADERS,
        timeout=aiohttp.ClientTimeout(total=18),
    ) as response:
        response.raise_for_status()
        result_html = await response.text()

    media_url = _extract_ssstik_download_link(result_html)
    if not media_url:
        raise NotAudioContentError("SSSTik не вернул прямую ссылку на медиафайл.")
    if media_url.startswith("//"):
        media_url = f"https:{media_url}"
    elif media_url.startswith("/"):
        media_url = f"https://ssstik.io{media_url}"
    elif not media_url.startswith(("http://", "https://")):
        media_url = f"https://ssstik.io/{media_url}"

    async with session.get(
        media_url,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        content = await response.read()
    if not content or "text" in content_type or "html" in content_type:
        raise NotAudioContentError(
            "SSSTik вернул пустой ответ или HTML вместо аудио.",
            content_type=content_type,
        )
    return content
        