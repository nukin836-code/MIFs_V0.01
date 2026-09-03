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
    
    post_url = "https://ssstik.io/abc?url=dl"
    data = {
        "id": tiktok_page_url,
        "locale": "ru",
        "tt": ""
    }
    
    try:
        logger.info("🌐 Стучимся в ssstik.io для парсинга медиа...")
        resp = session.post(post_url, data=data, headers=headers, timeout=10)
        logger.info("📥 Ответ от ssstik.io получен. Статус: %s, Размер: %s байт", resp.status_code, len(resp.text))
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        mp3_link = None
        all_links = [a['href'] for a in soup.find_all('a', href=True)]
        logger.info("🔗 Найдено всего ссылок в ответе ssstik: %d", len(all_links))
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            if "dl" in href or "mp3" in href or "tikcdn" in href:
                if "download" in a.text.lower() or "mp3" in a.text.lower() or "аудио" in a.text.lower():
                    mp3_link = href
                    logger.info("🎵 Найдена явная аудио-ссылка в ssstik: %s", mp3_link)
                    break
                    
        if not mp3_link:
            logger.info("⚠️ Явная аудио-ссылка не найдена, ищем любую медиа-ссылку (.mp4 / .mp3 / tikcdn)...")
            for a in soup.find_all('a', href=True):
                href = a['href']
                if "tikcdn" in href or ".mp4" in href or ".mp3" in href:
                    mp3_link = href
                    logger.info("🎵 Найдена альтернативная медиа-ссылка: %s", mp3_link)
                    break
                    
        if not mp3_link:
            logger.error("❌ Не удалось найти прямую ссылку на медиа в ответе ssstik. HTML ответ: %s", resp.text[:500])
            raise NotAudioContentError("Не удалось извлечь прямую ссылку на аудио из ответов ssstik.")
            
        if not mp3_link.startswith("http"):
            mp3_link = "https:" + mp3_link if mp3_link.startswith("//") else "https://ssstik.io" + mp3_link

        logger.info("⬇️ Скачиваем медиафайл по ссылке: %s", mp3_link)
        audio_resp = session.get(mp3_link, timeout=15)
        logger.info("📥 Ответ медиасервера получен. Статус: %s, Размер: %s байт", audio_resp.status_code, len(audio_resp.content))
        audio_resp.raise_for_status()
        
        content_type = audio_resp.headers.get("Content-Type", "").lower()
        logger.info("📄 Content-Type скачанного файла: %s", content_type)
        
        if "text" in content_type or "html" in content_type:
            raise NotAudioContentError(f"Скачался не аудиофайл, а страница с ошибкой/капчей (content-type: {content_type}).", content_type=content_type)
            
        logger.info("✅ Аудио успешно скачано из TikTok! Передаем в мясорубку.")
        return audio_resp.content

    except Exception as e:
        logger.exception("🚨 Ошибка при скачивании аудио через ssstik: %s", e)
        raise
        