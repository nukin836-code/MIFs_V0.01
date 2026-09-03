import asyncio
import difflib
import logging
from urllib.parse import quote_plus, urlparse, parse_qs
import requests
from bs4 import BeautifulSoup
from aiogram import Bot

import mif_core

logger = logging.getLogger("mif-bot.tiktok")

class NotAudioContentError(Exception):
    def __init__(self, message: str, content_type: str = "unknown"):
        super().__init__(message)
        self.content_type = content_type

async def search_catalog(
    session: requests.Session, 
    query: str, 
    min_score: int = 0, 
    max_results: int = 5,
    bot: Bot | None = None
) -> list[tuple[int, dict[str, str]]]:
    """
    Ищет TikTok через DuckDuckGo Lite (без капч и блокировок).
    """
    logger.info("Ищем TikTok через DuckDuckGo Lite для запроса: «%s»", query)
    
    search_query = f"tiktok {query}"
    # Используем lite-версию, она создана для простых клиентов и не требует сложных заголовков
    url = "https://lite.duckduckgo.com/lite/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://lite.duckduckgo.com",
        "Referer": "https://lite.duckduckgo.com/",
    }
    data = {
        "q": search_query,
        "kl": ""
    }
    
    try:
        response = await asyncio.to_thread(session.post, url, data=data, headers=headers, timeout=7)
        if response.status_code != 200:
            logger.warning("DuckDuckGo Lite ответил статусом %s", response.status_code)
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        video_links = set()
        
        # Парсим ссылки из lite-версии поисковика
        for a in soup.find_all('a', href=True):
            href = a['href']
            
            # В DuckDuckGo ссылки идут через редирект /l/?uddg=...
            if "uddg=" in href:
                parsed_url = parse_qs(urlparse(href).query)
                if "uddg" in parsed_url:
                    href = parsed_url["uddg"][0]
            
            if "tiktok.com" in href and ("/video/" in href or "vt.tiktok.com" in href or "/@" in href):
                video_links.add(href)
                
        logger.info("Найдено TikTok ссылок через поиск: %d", len(video_links))
        
        results = []
        for i, link in enumerate(list(video_links)[:max_results]):
            display_title = f"{query} (TikTok #{i+1})"
            
            ratio = difflib.SequenceMatcher(None, query.lower(), display_title.lower()).ratio()
            score = int(ratio * 100)
            final_score = max(score, 90 - i * 5)
            
            if final_score >= min_score:
                results.append((final_score, {
                    "title": display_title,
                    "url": link
                }))
                
        return results
    except Exception as e:
        logger.error("Ошибка при поиске TikTok через DDG Lite: %s", e)
        if bot:
            await mif_core.report_bug(bot, f"⚠️ import_tiktok search error: {e}")
        return []

def download_audio(session: requests.Session, tiktok_page_url: str) -> bytes:
    """
    Скачивает аудиодорожку по найденной ссылке TikTok через ssstik.io.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "HX-Request": "true",
        "HX-Current-URL": "https://ssstik.io/ru",
        "Origin": "https://ssstik.io",
        "Referer": "https://ssstik.io/ru",
    }
    
    post_url = "https://ssstik.io/abc?url=dl"
    data = {
        "id": tiktok_page_url,
        "locale": "ru",
        "tt": ""
    }
    
    try:
        resp = session.post(post_url, data=data, headers=headers, timeout=10)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        mp3_link = None
        for a in soup.find_all('a', href=True):
            href = a['href']
            if "dl" in href or "mp3" in href or "tikcdn" in href:
                if "download" in a.text.lower() or "mp3" in a.text.lower() or "аудио" in a.text.lower():
                    mp3_link = href
                    break
                    
        if not mp3_link:
            for a in soup.find_all('a', href=True):
                if "tikcdn" in a['href'] or ".mp4" in a['href'] or ".mp3" in a['href']:
                    mp3_link = a['href']
                    break
                    
        if not mp3_link:
            raise NotAudioContentError("Не удалось извлечь прямую ссылку на аудио из ответов ssstik.")
            
        if not mp3_link.startswith("http"):
            mp3_link = "https:" + mp3_link if mp3_link.startswith("//") else "https://ssstik.io" + mp3_link

        audio_resp = session.get(mp3_link, timeout=15)
        audio_resp.raise_for_status()
        
        content_type = audio_resp.headers.get("Content-Type", "").lower()
        if "text" in content_type or "html" in content_type:
            raise NotAudioContentError("Скачался не аудиофайл, а страница с ошибкой/капчей.", content_type=content_type)
            
        return audio_resp.content

    except Exception as e:
        logger.error("Ошибка скачивания через ssstik для %s: %s", tiktok_page_url, e)
        raise
        