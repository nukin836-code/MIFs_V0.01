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
    Ищет TikTok через поисковик (DuckDuckGo HTML), находя реальные индексированные 
    ссылки на ролики по запросу пользователя.
    """
    logger.info("Ищем TikTok через поисковик для запроса: «%s»", query)
    
    # Ищем с приставкой tiktok, как ты и предложил
    search_query = f"tiktok {query}"
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = await asyncio.to_thread(session.get, url, headers=headers, timeout=10)
        if response.status_code != 200:
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        video_links = set()
        
        # Парсим результаты поисковой выдачи и достаем оттуда ссылки на TikTok
        for a in soup.find_all('a', href=True):
            href = a['href']
            
            # DuckDuckGo оборачивает ссылки в редиректы, вытаскиваем чистый URL
            if "uddg=" in href:
                parsed_url = parse_qs(urlparse(href).query)
                if "uddg" in parsed_url:
                    href = parsed_url["uddg"][0]
            
            if "tiktok.com" in href and ("/video/" in href or "vt.tiktok.com" in href or "/@" in href):
                video_links.add(href)
                
        results = []
        for i, link in enumerate(list(video_links)[:max_results]):
            display_title = f"{query} (TikTok #{i+1})"
            
            ratio = difflib.SequenceMatcher(None, query.lower(), display_title.lower()).ratio()
            score = int(ratio * 100)
            final_score = max(score, 90 - i * 5)
            
            if final_score >= min_score:
                results.append((final_score, {
                    "title": display_title,
                    "url": link  # Та самая чистая ссылка на TikTok
                }))
                
        return results
    except Exception as e:
        logger.error("Ошибка при поиске TikTok через поисковик: %s", e)
        if bot:
            await mif_core.report_bug(bot, f"⚠️ import_tiktok search error: {e}")
        return []

def download_audio(session: requests.Session, tiktok_page_url: str) -> bytes:
    """
    Принимает найденную ссылку на TikTok, прогоняет через ssstik.io, 
    вытаскивает прямую ссылку на медиафайл и скачивает его.
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
        resp = session.post(post_url, data=data, headers=headers, timeout=15)
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

        audio_resp = session.get(mp3_link, timeout=20)
        audio_resp.raise_for_status()
        
        content_type = audio_resp.headers.get("Content-Type", "").lower()
        if "text" in content_type or "html" in content_type:
            raise NotAudioContentError("Скачался не аудиофайл, а страница с ошибкой/капчей.", content_type=content_type)
            
        return audio_resp.content

    except Exception as e:
        logger.error("Ошибка скачивания через ssstik для %s: %s", tiktok_page_url, e)
        raise
        