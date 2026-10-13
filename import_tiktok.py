import asyncio
import html as html_lib
import logging
import re
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

BING_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
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


def _is_downloadable_tiktok_url(url: str) -> bool:
    if "://" in url:
        candidate = url
    elif url.startswith("//"):
        candidate = f"https:{url}"
    else:
        candidate = f"https://{url}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"vt.tiktok.com", "vm.tiktok.com"}:
        return True
    return (host == "tiktok.com" or host.endswith(".tiktok.com")) and "/video/" in parsed.path


def _extract_tiktok_video_links(page_html: str) -> tuple[list[str], int]:
    soup = BeautifulSoup(page_html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        href = str(anchor["href"])
        if "uddg=" in href:
            href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
        if _is_downloadable_tiktok_url(href) and href not in seen:
            seen.add(href)
            links.append(href)
    return links, len(anchors)


def _extract_bing_video_search_links(page_html: str) -> list[str]:
    """Находит прямые TikTok URLs в Bing Video result metadata."""
    decoded_html = html_lib.unescape(page_html)
    pattern = re.compile(
        r'https?://(?:www\.)?tiktok\.com/@[^/"\\<>\s]+/video/\d+',
        re.IGNORECASE,
    )
    links: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(decoded_html):
        url = match.group(0)
        if _is_downloadable_tiktok_url(url) and url not in seen:
            seen.add(url)
            links.append(url)
    return links


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

        # Сохраняем порядок выдачи. set здесь ломал главный смысл этого
        # провайдера: пользовательский запрос должен идти в первый результат
        # поисковика, а не в случайную ссылку из множества.
        video_links, total_links_found = _extract_tiktok_video_links(response.text)
        for href in video_links:
            logger.info("🎯 TikTok ссылка: %s", href)

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

    Если страница содержит скрытый ``tt``, используем его. В текущей версии
    SSSTik поле может отсутствовать; тогда отправляем пустое значение, которое
    сервис принимает, вместо преждевременного отказа от резервного источника.
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
    """Отменяемый поиск: DuckDuckGo Lite, затем Bing Video как резерв.

    Сохраняет порядок выдачи и принимает только прямые ссылки на ролики,
    которые можно передать TikWM/SSSTik.
    """
    search_query = f"tiktok {query}"
    links: list[str] = []
    try:
        async with session.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": search_query, "kl": ""},
            headers=SEARCH_HEADERS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            if response.status == 200:
                page_html = await response.text()
                links, _ = _extract_tiktok_video_links(page_html)
            else:
                logger.info("DDG Lite POST ответил HTTP %s; пробую GET", response.status)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.debug("DDG Lite POST не удался для %r; пробую GET", query)

    # Некоторые ответы POST содержат только TikTok-теги вместо видео, а
    # DuckDuckGo Lite может вернуть поисковую капчу. GET оставляем как
    # отменяемый резерв и принимаем только прямые ссылки на ролики.
    if not links:
        try:
            async with session.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": search_query, "kl": ""},
                headers=SEARCH_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status != 200:
                    logger.info("DDG Lite GET ответил HTTP %s", response.status)
                else:
                    page_html = await response.text()
                    links, _ = _extract_tiktok_video_links(page_html)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.debug("DDG Lite GET не удался для %r; пробую Bing Video", query)

    if not links:
        logger.info(
            "DDG Lite не вернул прямых TikTok-видео для %r; пробую Bing Video",
            query,
        )
        try:
            async with session.get(
                "https://www.bing.com/videos/search",
                params={"q": f"{query} TikTok"},
                headers=BING_SEARCH_HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    logger.info("Bing Video ответил HTTP %s", response.status)
                    return []
                page_html = await response.text()
                links = _extract_bing_video_search_links(page_html)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.debug("Bing Video поиск не удался для %r", query, exc_info=True)
            return []

    if not links:
        logger.info("DDG Lite и Bing Video не нашли прямых TikTok-видео для %r", query)
        return []

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
    """Точный резервный путь через SSSTik с необязательным токеном ``tt``.

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
        