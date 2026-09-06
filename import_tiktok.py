import asyncio
import difflib
import logging
from urllib.parse import quote_plus, urlparse, parse_qs
import requests
from bs4 import BeautifulSoup
from aiogram import Bot

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
    bot: Bot | None = None
) -> list[tuple[int, dict[str, str]]]:
    logger.info("🟢 [TIKTOK SEARCH START] Запрос: «%s» (min_score=%s)", query, min_score)
    
    search_query = f"tiktok {query}"
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
        logger.info("🌐 Отправляем POST запрос к DuckDuckGo Lite с поиском: '%s'", search_query)
        response = await asyncio.to_thread(session.post, url, data=data, headers=headers, timeout=7)
        logger.info("📥 Ответ от DDG Lite получен. Статус: %s, Размер ответа: %s байт", response.status_code, len(response.text))
        
        if response.status_code != 200:
            logger.warning("❌ DDG Lite вернул плохой статус: %s", response.status_code)
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        video_links = set()
        
        total_links_found = 0
        for a in soup.find_all('a', href=True):
            total_links_found += 1
            href = a['href']
            
            if "uddg=" in href:
                try:
                    parsed_url = parse_qs(urlparse(href).query)
                    if "uddg" in parsed_url:
                        href = parsed_url["uddg"][0]
                except Exception:
                    pass
            
            if "tiktok.com" in href and ("/video/" in href or "vt.tiktok.com" in href or "/@" in href):
                logger.info("🎯 Найдена подходящая ссылка TikTok: %s", href)
                video_links.add(href)
                
        logger.info("📊 Всего просмотрено ссылок на странице: %d. Уникальных TikTok ссылок отобрано: %d", total_links_found, len(video_links))
        
        if not video_links:
            logger.warning("⚠️ Не найдено ни одной ссылки на TikTok в выдаче DDG Lite для запроса: %s", query)
            logger.info("📄 Первые 300 символов ответа DDG: %s", response.text[:300])
            return []

        results = []
        for i, link in enumerate(list(video_links)[:max_results]):
            display_title = f"{query} (TikTok #{i+1})"
            
            ratio = difflib.SequenceMatcher(None, query.lower(), display_title.lower()).ratio()
            score = int(ratio * 100)
            final_score = max(score, 90 - i * 5)
            
            logger.info("✨ Кандидат #%d: title='%s', link='%s', score=%d", i+1, display_title, link, final_score)
            
            if final_score >= min_score:
                results.append((final_score, {
                    "title": display_title,
                    "url": link
                }))
            else:
                logger.info("❌ Кандидат отсечен по score (%d < min_score %d)", final_score, min_score)
                
        logger.info("✅ Итог поиска TikTok: сформировано %d кандидатов", len(results))
        return results
        
    except Exception as e:
        logger.exception("🚨 Ошибка в search_catalog (TikTok): %s", e)
        if bot:
            await mif_core.report_bug(bot, f"⚠️ import_tiktok search error: {e}")
        return []

def download_audio(session: requests.Session, tiktok_page_url: str) -> bytes:
    logger.info("📥 [TIKTOK DOWNLOAD START] Скачивание для ссылки: %s", tiktok_page_url)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "HX-Request": "true",
        "HX-Current-URL": "https://ssstik.io/ru",
        "Origin": "https://ssstik.io",
        "Referer": "https://ssstik.io/ru",
    }
    
    try:
        # ШАГ 1: Заходим на главную и парсим живой токен 'tt'
        logger.info("🌐 Получаем динамический токен защиты с ssstik.io...")
        main_resp = session.get("https://ssstik.io/ru", headers=headers, timeout=7)
        main_resp.raise_for_status()
        
        main_soup = BeautifulSoup(main_resp.text, 'html.parser')
        tt_input = main_soup.find('input', {'name': 'tt'})
        tt_value = tt_input['value'] if tt_input and 'value' in tt_input.attrs else ""
        
        logger.info("🔑 Токен tt получен: '%s'", tt_value)

        # ШАГ 2: Отправляем запрос на парсинг с настоящим токеном
        post_url = "https://ssstik.io/abc?url=dl"
        data = {
            "id": tiktok_page_url,
            "locale": "ru",
            "tt": tt_value
        }
        
        resp = session.post(post_url, data=data, headers=headers, timeout=10)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        mp3_link = None
        # Ищем ссылку на аудио/видео
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.text.lower()
            if "dl" in href or "mp3" in href or "tikcdn" in href:
                if "download" in text or "mp3" in text or "аудио" in text:
                    mp3_link = href
                    break
                    
        if not mp3_link:
            for a in soup.find_all('a', href=True):
                href = a['href']
                if "tikcdn" in href or ".mp4" in href or ".mp3" in href:
                    mp3_link = href
                    break
                    
        if not mp3_link:
            raise NotAudioContentError("ssstik.io не отдала ссылку на скачивание.")
            
        if not mp3_link.startswith("http"):
            mp3_link = "https:" + mp3_link if mp3_link.startswith("//") else "https://ssstik.io" + mp3_link

        # ШАГ 3: Скачиваем сам медиафайл
        logger.info("⬇️ Скачиваем байты: %s", mp3_link)
        audio_resp = session.get(mp3_link, timeout=15)
        audio_resp.raise_for_status()
        
        content_type = audio_resp.headers.get("Content-Type", "").lower()
        if "text" in content_type or "html" in content_type:
            raise NotAudioContentError(f"Скачался HTML вместо аудио (content-type: {content_type}).")
            
        return audio_resp.content

    except Exception as e:
        logger.exception("🚨 Ошибка при скачивании через ssstik: %s", e)
        raise
        