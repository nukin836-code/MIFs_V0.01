"""
Автозагрузка звуков через универсальный движок yt-dlp: /loads, /loadsN, /loadsStop, /loadsSearch.

Логика вынесена сюда отдельно от main.py. Парсеры (import_myinstants, import_tiktok) 
используются исключительно для поиска ссылок и названий. Само скачивание делегировано yt-dlp.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import tempfile
from typing import Any

import requests
import yt_dlp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import import_myinstants as importer
import import_tiktok
import mif_core

logger = logging.getLogger("mif-bot.loader")

LOADS_ADMIN_ID = int(os.getenv("LOADS_ADMIN_ID", "1297417116"))
LOADS_STEP_DELAY_SECONDS = 5.0

LOADS_SEARCH_RE = re.compile(r'^/loadsSearch\s+"?([^"]+?)"?\s*$')
LOADS_COUNT_RE = re.compile(r"^/loads(\d+)$")


class LoaderState:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.stop_event: asyncio.Event = asyncio.Event()
        self.added_count: int = 0
        self.target_count: int | None = None


loader_state = LoaderState()

DEBOUNCE_DELAY_SECONDS = 0.3


class _UserLookupState:
    def __init__(self) -> None:
        self.debounce_task: asyncio.Task | None = None
        self.in_flight: bool = False
        self.latest_pending_query: str | None = None


_user_lookup_states: dict[int, _UserLookupState] = {}


def schedule_background_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    state = _user_lookup_states.setdefault(requester_id, _UserLookupState())

    if state.in_flight:
        state.latest_pending_query = query_text
        return

    if state.debounce_task is not None and not state.debounce_task.done():
        state.debounce_task.cancel()

    state.debounce_task = asyncio.create_task(_debounced_lookup(bot, requester_id, query_text))


async def _debounced_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    state = _user_lookup_states[requester_id]
    state.debounce_task = None
    state.in_flight = True
    try:
        await background_internet_lookup(bot, requester_id, query_text)
    finally:
        state.in_flight = False

    next_query = state.latest_pending_query
    state.latest_pending_query = None
    if next_query is not None:
        schedule_background_lookup(bot, requester_id, next_query)


async def _download_audio_ytdlp(url: str) -> bytes:
    """Универсальная загрузка аудио через yt-dlp. Возвращает сырые байты скачанного файла."""
    def _extract():
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
                
                files = os.listdir(tmpdir)
                if not files:
                    raise RuntimeError("yt-dlp не смог извлечь аудио файл")
                    
                with open(os.path.join(tmpdir, files[0]), 'rb') as f:
                    return f.read()

    return await asyncio.to_thread(_extract)


async def import_one_sound(
    bot: Bot,
    sound: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    """Скачивает, обрабатывает и публикует звук, полагаясь на yt-dlp."""
    title = sound["title"]
    source_type = sound.get("source_type", "myinstants")
    source_label = "TikTok" if source_type == "tiktok" else "MyInstants"

    try:
        audio_bytes = await _download_audio_ytdlp(sound["url"])
    except yt_dlp.utils.DownloadError as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): yt-dlp не смог скачать «{title}»: {error}")
        return "error", None
    except Exception as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): непредвиденная ошибка yt-dlp «{title}»: {error}")
        return "error", None

    try:
        bot_text, transcription_error, ogg_bytes, content_hash = (
            await mif_core.prepare_audio_from_bytes(audio_bytes)
        )
    except RuntimeError as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): не удалось конвертировать «{title}»: {error}")
        return "error", None

    displayed_bot_text = bot_text or "Речь не распознана."
    base_caption = (
        f"<b>MIF с {source_label} (автозагрузка)</b>\n\n"
        f"<b>Название и теги:</b> {html.escape(mif_core.clip_text(title))}\n"
        f"<b>Авто-описание:</b> {html.escape(mif_core.clip_text(displayed_bot_text))}\n"
        f"<b>Источник:</b> {html.escape(sound['url'])}"
    )

    try:
        status, new_mif = await mif_core.publish_voice_mif(
            bot,
            ogg_bytes=ogg_bytes,
            existing_voice_file_id=None,
            base_caption=base_caption,
            title=title,
            tags_text=title,
            bot_description=bot_text,
            content_hash=content_hash,
            source_url=sound["url"],
        )
    except TelegramAPIError as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): Telegram отклонил «{title}»: {error}")
        return "error", None

    if status == "added" and transcription_error:
        logger.warning("Добавлен без авто-описания: %s — %s", title, transcription_error)

    return status, new_mif


async def run_loads_loop(bot: Bot, chat_id: int, target_count: int | None) -> None:
    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)
    pager = importer.CatalogPager()

    try:
        while not loader_state.stop_event.is_set():
            if target_count is not None and loader_state.added_count >= target_count:
                break

            page_url = pager.current_url
            try:
                page_html = await asyncio.to_thread(importer.fetch_page, session, page_url)
                sounds = importer.parse_page(page_html)
            except requests.RequestException as error:
                await mif_core.report_bug(bot, f"Автозагрузка: ошибка загрузки категории: {error}")
                pager.advance_category()
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue

            if not sounds:
                pager.advance_category()
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue

            for sound in sounds:
                if loader_state.stop_event.is_set() or (target_count is not None and loader_state.added_count >= target_count):
                    break

                if mif_core.find_duplicate_by_title(sound["title"]) is not None:
                    await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                    continue

                sound["source_type"] = "myinstants"
                try:
                    status, _ = await import_one_sound(bot, sound)
                except Exception as error:
                    logger.exception("Автозагрузка: критическая ошибка на «%s»", sound["title"])
                    status = "error"

                if status == "added":
                    loader_state.added_count += 1

                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)

            pager.advance_page()
    finally:
        try:
            await bot.send_message(chat_id, f"⏹Автозагрузка остановлена. Добавлено: {loader_state.added_count}.")
        except TelegramAPIError:
            pass
        loader_state.task = None


async def handle_loads_start(message: Message, target_count: int | None) -> None:
    if loader_state.task is not None and not loader_state.task.done():
        await message.answer("⚠️Автозагрузка уже запущена. Останови через /loadsStop.")
        return

    loader_state.stop_event = asyncio.Event()
    loader_state.added_count = 0
    loader_state.target_count = target_count
    loader_state.task = asyncio.create_task(run_loads_loop(message.bot, message.chat.id, target_count))

    if target_count:
        await message.answer(f"▶️Запуск (цель: {target_count}). Досрочная остановка — /loadsStop.")
    else:
        await message.answer("▶️Запуск (бесконечно). Остановка — /loadsStop.")


async def handle_loads_stop(message: Message) -> None:
    if loader_state.task is None or loader_state.task.done():
        await message.answer("Автозагрузка сейчас не запущена.")
        return

    loader_state.stop_event.set()
    await message.answer(f"⏸Останавливаю (доработаю паузу {LOADS_STEP_DELAY_SECONDS:.0f} сек).")


async def handle_loads_search(message: Message, query: str) -> None:
    if not query:
        await message.answer('Укажи запрос: /loadsSearch "текст".')
        return

    await message.answer("🔍Ищу ссылки (TikTok / MyInstants)...")

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    candidates = []
    source_type = "tiktok"

    try:
        candidates = await import_tiktok.search_catalog(session, query, min_score=mif_core.FUZZY_MATCH_FLOOR)
    except Exception:
        pass

    if not candidates:
        source_type = "myinstants"
        try:
            candidates = await importer.search_catalog(session, query, min_score=mif_core.FUZZY_MATCH_FLOOR)
        except Exception:
            await message.answer("⚠️Оба источника поиска недоступны. Попробуй позже.")
            return

    if not candidates:
        await message.answer(f"Ничего похожего на «{query}» не нашлось.")
        return

    best_score, sound = candidates[0]
    sound["source_type"] = source_type
    is_confident_match = best_score >= mif_core.FUZZY_MATCH_THRESHOLD

    existing_by_title = mif_core.find_duplicate_by_title(sound["title"])
    if existing_by_title:
        await message.answer(f"⚠️«{sound['title']}» уже в базе.")
        await message.answer_voice(voice=existing_by_title["file_id"])
        return

    status, entry = await import_one_sound(message.bot, sound)

    if status == "duplicate" and entry:
        await message.answer(f"⚠️Звук совпадает с «{entry.get('title')}».")
        await message.answer_voice(voice=entry["file_id"])
        return

    if status == "added" and entry:
        source_label = "TikTok" if source_type == "tiktok" else "MyInstants"
        if is_confident_match:
            await message.answer(f"✅Загрузил с {source_label} «{entry['title']}».")
        else:
            await message.answer(f"Точного совпадения нет, скачал ближайшее: «{entry['title']}».")
        return

    await message.answer("⚠️Не удалось загрузить аудио.")


async def background_internet_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    muted = mif_core.is_muted(requester_id)
    can_message = True

    if not muted:
        try:
            await bot.send_message(requester_id, f"🔍Ищу в интернете «{query_text}»...")
        except TelegramAPIError:
            can_message = False

    async def notify(text: str) -> None:
        if not muted and can_message:
            try:
                await bot.send_message(requester_id, text)
            except TelegramAPIError:
                pass

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    candidates = []
    source_type = "tiktok"

    try:
        candidates = await import_tiktok.search_catalog(session, query_text, max_results=1, min_score=mif_core.FUZZY_MATCH_THRESHOLD)
    except Exception:
        pass

    if not candidates:
        source_type = "myinstants"
        try:
            candidates = await importer.search_catalog(session, query_text, max_results=1, min_score=mif_core.FUZZY_MATCH_THRESHOLD, fast_only=True)
        except Exception:
            pass

    if not candidates:
        await notify(f"⚠️Не нашёл: «{query_text}»")
        return

    _, sound = candidates[0]
    sound["source_type"] = source_type

    if mif_core.find_duplicate_by_title(sound["title"]):
        await notify("✅Готово")
        return

    await notify(f"➖Нашёл: «{query_text}» — публикую через yt-dlp...")
    
    try:
        status, entry = await import_one_sound(bot, sound)
        if status in ("added", "duplicate") and entry:
            await notify("✅Готово")
    except Exception:
        pass


async def handle_loads_commands(message: Message) -> None:
    text = (message.text or "").strip()

    search_match = LOADS_SEARCH_RE.match(text)
    if search_match:
        await handle_loads_search(message, search_match.group(1).strip())
        return

    if message.from_user is None or message.from_user.id != LOADS_ADMIN_ID:
        await message.answer("⛔Только для администратора.")
        return

    if text == "/loadsStop":
        await handle_loads_stop(message)
        return
    if text == "/loads":
        await handle_loads_start(message, target_count=None)
        return

    count_match = LOADS_COUNT_RE.match(text)
    if count_match:
        await handle_loads_start(message, target_count=int(count_match.group(1)))
        return

    await message.answer("Не понял команду. Доступно: /loads, /loads5, /loadsStop, /loadsSearch \"запрос\"")
    