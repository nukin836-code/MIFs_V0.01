import asyncio
import logging
from urllib.parse import quote_plus, urlparse, parse_qs

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

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://lite.duckduckgo.com",
        "Referer": "https://lite.duckduckgo.com/",
    }
    data = {"q": search_query, "kl": ""}

    try:
        logger.info("🌐 POST к DuckDuckGo Lite: '%s'", search_query)
        response = await asyncio.to_thread(
            session.post, url, data=data, headers=headers, timeout=7
        )
        logger.info(
            "📥 Ответ DDG Lite: статус %s, %s байт",
            response.status_code, len(response.text),
        )

        if response.status_code != 200:
            logger.warning("❌ DDG Lite вернул статус: %s", response.status_code)
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        video_links: set[str] = set()

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
                video_links.add(href)

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
    logger.info("📥 [TIKTOK DOWNLOAD] Запрос к локальному серверу: %s", tiktok_page_url)

    # Стучимся на локальный FastAPI сервер, запущенный в Termux (tiktok_server.py)
    local_api_url = f"http://127.0.0.1:8000/download?url={quote_plus(tiktok_page_url)}"

    try:
        resp = session.get(local_api_url, timeout=25)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Локальный сервер вернул ошибку {resp.status_code}: {resp.text}"
            )
        if len(resp.content) == 0:
            raise RuntimeError("Локальный сервер отдал пустой файл (0 байт).")

        logger.info("✅ Аудио получено через локальный сервер. Размер: %d байт", len(resp.content))
        return resp.content

    except Exception as e:
        logger.exception("🚨 Ошибка при скачивании через локальный сервер TikTok: %s", e)
        raise
        