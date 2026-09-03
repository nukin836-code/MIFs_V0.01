import asyncio
import difflib
import logging
from urllib.parse import quote_plus

import requests
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
    Ищет звуки в TikTok через tikwm API и кидает баг-репорты при любых отклонениях.
    """
    url = f"https://www.tikwm.com/api/feed/search?keywords={quote_plus(query)}&count=10"
    
    try:
        response = await asyncio.to_thread(session.get, url, timeout=10)
        
        # 1. Проверка HTTP статуса
        if response.status_code != 200:
            error_text = f"TikTok API ответил кодом {response.status_code}. Тело: {response.text[:100]}"
            logger.error(error_text)
            if bot:
                await mif_core.report_bug(bot, f"⚠️ import_tiktok: {error_text}\nURL: {url}")
            return []

        # 2. Проверка валидности JSON
        try:
            data = response.json()
        except Exception as e:
            error_text = f"TikTok API вернул не JSON (возможно, капча Cloudflare): {response.text[:100]}"
            logger.error(error_text)
            if bot:
                await mif_core.report_bug(bot, f"⚠️ import_tiktok: {error_text}")
            return []
        
        # 3. Проверка внутренней структуры ответа
        if data.get("code") != 0 or "data" not in data or "videos" not in data["data"]:
            error_text = f"Неожиданный формат ответа: {data.get('msg', 'отсутствует ключ videos')}"
            logger.warning(error_text)
            if bot:
                await mif_core.report_bug(bot, f"⚠️ import_tiktok: {error_text}\nЗапрос: {query}")
            return []
            
        results = []
        seen_urls = set()
        
        for video in data["data"]["videos"]:
            video_title = video.get("title", "")
            music_info = video.get("music_info", {})
            music_title = music_info.get("title", "")
            
            audio_url = video.get("music") or music_info.get("play")
            if not audio_url:
                continue
                
            if audio_url in seen_urls:
                continue
            seen_urls.add(audio_url)
            
            clean_video_title = video_title.split("#")[0].strip()[:50]
            display_title = clean_video_title
            if music_title and music_title.lower() not in display_title.lower():
                display_title = f"{display_title} 🎵 {music_title[:30]}"
                
            if not display_title:
                display_title = query
                
            ratio = difflib.SequenceMatcher(None, query.lower(), display_title.lower()).ratio()
            score = int(ratio * 100)
            
            base_boost = max(0, 95 - len(results) * 5)
            final_score = max(score, base_boost)
            
            if final_score >= min_score:
                results.append((final_score, {
                    "title": display_title.strip(),
                    "url": audio_url
                }))
                
                if len(results) >= max_results:
                    break
                    
        results.sort(key=lambda x: x[0], reverse=True)
        return results
        
    except Exception as e:
        logger.exception("Критическая ошибка поиска в TikTok API: %s", e)
        if bot:
            await mif_core.report_bug(bot, f"🚨 Критическая ошибка в import_tiktok.search_catalog:\nЗапрос: {query}\nОшибка: {type(e).__name__} - {e}")
        return []

def download_audio(session: requests.Session, tiktok_url: str) -> bytes:
    """
    Здесь баг-репорты отправлять напрямую нельзя (функция синхронная для asyncio.to_thread).
    Мы просто прокидываем ошибку наверх — mif_loader.py сам перехватит её в import_one_sound 
    и отправит баг-репорт с точным URL.
    """
    response = session.get(tiktok_url, timeout=15)
    response.raise_for_status()
    
    content_type = response.headers.get("Content-Type", "").lower()
    
    if "text" in content_type or "html" in content_type:
        raise NotAudioContentError(
            f"Вместо аудио пришла веб-страница. TikTok заблокировал скачивание.", 
            content_type=content_type
        )
        
    return response.content
    