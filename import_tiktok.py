import asyncio
import difflib
import logging
from urllib.parse import quote_plus
import re
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
    Эмулирует поиск TikTok через веб-интерфейс и находит прямые ссылки на видео.
    """
    logger.info("Пробуем искать в TikTok (веб-поиск) запрос: «%s»", query)
    
    # Используем публичный поиск или поисковые выдачи, чтобы найти ссылки на видео
    search_url = f"https://www.tiktok.com/search?q={quote_plus(query)}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.9",
    }
    
    try:
        response = await asyncio.to_thread(session.get, search_url, headers=headers, timeout=10)
        if response.status_code != 200:
            return []
            
        # Парсим страницы TikTok в поисках ссылок на видео /video/... или vt.tiktok.com
        soup = BeautifulSoup(response.text, 'html.parser')
        video_links = set()
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            if "/video/" in href or "vt.tiktok.com" in href:
                if not href.startswith("http"):
                    href = "https://www.tiktok.com" + href
                video_links.add(href)
                
        results = []
        for i, link in enumerate(list(video_links)[:max_results]):
            # В качестве названия пока берем запрос + индекс, так как спарсить тайтлы со страницы сложно без Selenium
            display_title = f"{query} (TikTok #{i+1})"
            
            ratio = difflib.SequenceMatcher(None, query.lower(), display_title.lower()).ratio()
            score = int(ratio * 100)
            final_score = max(score, 90 - i * 5) # Искусственный буст для топа выдачи
            
            if final_score >= min_score:
                results.append((final_score, {
                    "title": display_title,
                    "url": link  # Сохраняем саму ссылку на TikTok видео
                }))
                
        return results
    except Exception as e:
        logger.error("Ошибка при эмуляции поиска TikTok: %s", e)
        if bot:
            await mif_core.report_bug(bot, f"⚠️ import_tiktok search error: {e}")
        return []

def download_audio(session: requests.Session, tiktok_page_url: str) -> bytes:
    """
    Берет ссылку на TikTok видео, стучится на ssstik.io, забирает прямую ссылку на MP3 
    (через tikcdn или аналогичные хосты) и скачивает аудиобайты.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "HX-Request": "true",
        "HX-Current-URL": "https://ssstik.io/ru",
        "Origin": "https://ssstik.io",
        "Referer": "https://ssstik.io/ru",
    }
    
    # 1. Запрос к ssstik для получения формы и токенов загрузки
    post_url = "https://ssstik.io/abc?url=dl"
    data = {
        "id": tiktok_page_url,
        "locale": "ru",
        "tt": "" # Параметры сессии ssstik
    }
    
    try:
        resp = session.post(post_url, data=data, headers=headers, timeout=15)
        resp.raise_for_status()
        
        # 2. Парсим HTML-ответ от ssstik, где спрятана ссылка на скачивание MP3/видео
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Ищем кнопку/ссылку на скачивание аудио (mp3)
        mp3_link = None
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Обычно ссылки на аудио содержат маркеры mp3 или ведут на tikcdn / ssstik c аудио
            if "dl" in href or "mp3" in href or "tikcdn" in href:
                if "download" in a.text.lower() or "mp3" in a.text.lower() or "аудио" in a.text.lower():
                    mp3_link = href
                    break
                    
        # Если явную кнопку MP3 не нашли, берем первую попавшуюся прямую ссылку на медиафайл
        if not mp3_link:
            for a in soup.find_all('a', href=True):
                if "tikcdn" in a['href'] or ".mp4" in a['href'] or ".mp3" in a['href']:
                    mp3_link = a['href']
                    break
                    
        if not mp3_link:
            raise NotAudioContentError("Не удалось извлечь прямую ссылку на MP3 из ответов ssstik.")
            
        if not mp3_link.startswith("http"):
            mp3_link = "https:" + mp3_link if mp3_link.startswith("//") else "https://ssstik.io" + mp3_link

        # 3. Скачиваем финальный медиафайл (который пойдет в мясорубку mif_core)
        audio_resp = session.get(mp3_link, timeout=20)
        audio_resp.raise_for_status()
        
        content_type = audio_resp.headers.get("Content-Type", "").lower()
        if "text" in content_type or "html" in content_type:
            raise NotAudioContentError("Скачался не аудиофайл, а страница с ошибкой/капчей.", content_type=content_type)
            
        return audio_resp.content

    except Exception as e:
        logger.error("Ошибка скачивания через ssstik для %s: %s", tiktok_page_url, e)
        raise
         # 3. Скачиваем финальный медиафайл (который пойдет в мясорубку mif_core)
        audio_resp = session.get(mp3_link, timeout=20)
        audio_resp.raise_for_status()
        
        content_type = audio_resp.headers.get("Content-Type", "").lower()
        if "text" in content_type or "html" in content_type:
            raise NotAudioContentError("Скачался не аудиофайл, а страница с ошибкой/капчей.", content_type=content_type)
            
        return audio_resp.content

    except Exception as e:
        logger.error("Ошибка скачивания через ssstik для %s: %s", tiktok_page_url, e)
        raise