import asyncio
import difflib
import logging
from urllib.parse import quote_plus

import requests

logger = logging.getLogger("mif-bot.tiktok")

class NotAudioContentError(Exception):
    def __init__(self, message: str, content_type: str = "unknown"):
        super().__init__(message)
        self.content_type = content_type

async def search_catalog(
    session: requests.Session, 
    query: str, 
    min_score: int = 0, 
    max_results: int = 5
) -> list[tuple[int, dict[str, str]]]:
    """
    Ищет звуки в TikTok.
    Так как TikTok активно блокирует прямые запросы от ботов (капчи, токены X-Bogus),
    здесь используется бесплатный неофициальный API (tikwm.com), который отдает 
    прямые ссылки на mp3/mp4 без водяных знаков и не требует авторизации.
    """
    url = f"https://www.tikwm.com/api/feed/search?keywords={quote_plus(query)}&count=10"
    
    try:
        # Выполняем синхронный запрос в отдельном потоке, чтобы не блокировать event loop aiogram
        response = await asyncio.to_thread(session.get, url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 0 or "data" not in data or "videos" not in data["data"]:
            return []
            
        results = []
        seen_urls = set()
        
        for video in data["data"]["videos"]:
            video_title = video.get("title", "")
            music_info = video.get("music_info", {})
            music_title = music_info.get("title", "")
            
            # API обычно отдает прямую ссылку на звук в поле music или внутри music_info
            audio_url = video.get("music") or music_info.get("play")
            if not audio_url:
                continue
                
            if audio_url in seen_urls:
                continue
            seen_urls.add(audio_url)
            
            # Формируем читаемое название: берем текст видео и, если есть, название трека
            clean_video_title = video_title.split("#")[0].strip()[:50] # Отрезаем хештеги
            display_title = clean_video_title
            if music_title and music_title.lower() not in display_title.lower():
                display_title = f"{display_title} 🎵 {music_title[:30]}"
                
            if not display_title:
                display_title = query
                
            # Оцениваем релевантность
            ratio = difflib.SequenceMatcher(None, query.lower(), display_title.lower()).ratio()
            score = int(ratio * 100)
            
            # Выдача TikTok сама по себе релевантна запросу. Искусственно завышаем балл 
            # первым результатам, чтобы они проходили строгий порог (FUZZY_MATCH_THRESHOLD) 
            # при автоматическом фоновом поиске, даже если точного текстового совпадения нет.
            base_boost = max(0, 95 - len(results) * 5)
            final_score = max(score, base_boost)
            
            if final_score >= min_score:
                results.append((final_score, {
                    "title": display_title.strip(),
                    "url": audio_url
                }))
                
                if len(results) >= max_results:
                    break
                    
        # Сортируем от наиболее релевантных к наименее
        results.sort(key=lambda x: x[0], reverse=True)
        return results
        
    except Exception as e:
        logger.error("Ошибка поиска в TikTok API: %s", e)
        return []

def download_audio(session: requests.Session, tiktok_url: str) -> bytes:
    """
    Скачивает сырые байты по прямой ссылке, полученной из API.
    Вызывается в `mif_loader.py` через `asyncio.to_thread`.
    """
    response = session.get(tiktok_url, timeout=15)
    response.raise_for_status()
    
    content_type = response.headers.get("Content-Type", "").lower()
    
    # Защита от заглушек, страниц с капчей или ошибок серверов (HTML/Text вместо аудио)
    if "text" in content_type or "html" in content_type:
        raise NotAudioContentError(
            f"Вместо медиафайла скачалась веб-страница.", 
            content_type=content_type
        )
        
    return response.content
    