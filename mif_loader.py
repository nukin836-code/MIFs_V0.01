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

def is_valid_audio_bytes(data: bytes) -> bool:
    """Проверяет байты на реальные сигнатуры аудио/видео форматов."""
    if len(data) < 5000:
        return False

    # Если в первых 1000 байтах есть символы HTML — это веб-страница/капча
    head = data[:1000].lower()
    if b"<html" in head or b"<!doctype" in head or b"<head" in head or b'{"error"' in head or b"<script" in head:
        return False

    # Проверка Magic Bytes (заголовков настоящих медиафайлов)
    is_mp3 = data.startswith(b"ID3") or data.startswith(b"\xff\xfb") or data.startswith(b"\xff\xf3")
    is_ogg = data.startswith(b"OggS")
    is_wav = data.startswith(b"RIFF") and b"WAVE" in data[:16]
    is_flac = data.startswith(b"fLaC")
    is_mp4 = len(data) > 8 and data[4:8] == b"ftyp"
    is_webm = data.startswith(b"\x1a\x45\xdf\xa3")

    # Если совпал хотя бы один сигнатурный заголовок медиа
    return is_mp3 or is_ogg or is_wav or is_flac or is_mp4 or is_webm
    


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
    notify_func=None,
) -> tuple[str, dict[str, Any] | None]:
    """Скачивает, обрабатывает и публикует звук с уведомлением пользователя на каждом шаге."""
    title = sound["title"]
    source_type = sound.get("source_type", "myinstants")
    source_label = "TikTok" if source_type == "tiktok" else "MyInstants"

    try:
        if source_type == "tiktok":
            if notify_func:
                await notify_func(f"📥 [Шаг 2/3] Запускаю парсинг <b>ssstik.io</b> и скачивание MP3 из TikTok...")
            session = requests.Session()
            audio_bytes = await asyncio.to_thread(import_tiktok.download_audio, session, sound["url"])
        else:
            if notify_func:
                await notify_func(f"📥 [Шаг 2/3] Скачиваю аудиофайл с MyInstants через <b>yt-dlp</b>...")
            audio_bytes = await _download_audio_ytdlp(sound["url"])
            
    except yt_dlp.utils.DownloadError as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): yt-dlp не смог скачать «{title}»: {error}")
        return "error", None
    except import_tiktok.NotAudioContentError as error:
        await mif_core.report_bug(bot, f"Автозагрузка (TikTok): ssstik отдал не аудио «{title}»: {error}")
        return "error", None
    except Exception as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): ошибка скачивания «{title}»: {error}")
        return "error", None

    if notify_func:
        await notify_func("⚙️ [Шаг 3/3] Обрабатываю звук: конвертация в Voice OGG + распознавание речи...")

    try:
        bot_text, transcription_error, ogg_bytes, content_hash = (
            await mif_core.prepare_audio_from_bytes(audio_bytes)
        )
    except RuntimeError as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): ошибка конвертации «{title}»: {error}")
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

    status_msg = await message.answer("🔍 <b>[Шаг 1/3]</b> Запускаю параллельный поиск:\n• 🎬 <b>TikTok</b> (таймаут 2.0 сек)\n• 🎵 <b>MyInstants</b>...")

    async def update_status(text: str):
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    start_time = asyncio.get_running_loop().time()

    # Поиск TikTok с ловлей таймаута
    async def fetch_tiktok():
        try:
            return await asyncio.wait_for(
                import_tiktok.search_catalog(session, query, min_score=mif_core.FUZZY_MATCH_FLOOR),
                timeout=2.0
            )
        except asyncio.TimeoutError:
            return "TIMEOUT"
        except Exception:
            return []

    # Поиск MyInstants
    async def fetch_mi():
        try:
            return await importer.search_catalog(session, query, min_score=mif_core.FUZZY_MATCH_FLOOR)
        except Exception:
            return []

    tt_res, mi_res = await asyncio.gather(fetch_tiktok(), fetch_mi())

    candidates = []
    source_type = ""

    # Развилка логики: выбор источника
    if isinstance(tt_res, list) and tt_res:
        candidates = tt_res
        source_type = "tiktok"
        elapsed = round(asyncio.get_running_loop().time() - start_time, 2)
        await update_status(
            f"⚡ <b>Развилка: Выбран TikTok!</b>\n"
            f"⏱ Ответ получен за {elapsed} сек.\n"
            f"📌 Найдено: «<i>{candidates[0][1]['title']}</i>»\n\n"
            f"Перехожу к скачиванию..."
        )
    elif mi_res:
        candidates = mi_res
        source_type = "myinstants"
        reason = "⏱ TikTok не успел за 2 сек" if tt_res == "TIMEOUT" else "❌ В TikTok ничего не найдено"
        await update_status(
            f"⏳ <b>Развилка: Выбран MyInstants!</b>\n"
            f"Причина: {reason}.\n"
            f"📌 Найдено: «<i>{candidates[0][1]['title']}</i>»\n\n"
            f"Перехожу к скачиванию..."
        )
    else:
        await update_status(f"❌ <b>Ничего не найдено</b> ни в TikTok (таймаут/пусто), ни в MyInstants.")
        return

    best_score, sound = candidates[0]
    sound["source_type"] = source_type

    # Проверка на дубликат по названию
    existing_by_title = mif_core.find_duplicate_by_title(sound["title"])
    if existing_by_title:
        await update_status(f"⚠️ <b>Дубликат!</b> Звук «{sound['title']}» уже сохранен в базе.")
        await message.answer_voice(voice=existing_by_title["file_id"])
        return

    # Запуск загрузки и обработки
    status, entry = await import_one_sound(message.bot, sound, notify_func=update_status)

    if status == "duplicate" and entry:
        await update_status(f"⚠️ <b>Дубликат по аудио-хэшу!</b> Совпадает с «{entry.get('title')}».")
        await message.answer_voice(voice=entry["file_id"])
        return

    if status == "added" and entry:
        source_label = "TikTok (ssstik.io)" if source_type == "tiktok" else "MyInstants (yt-dlp)"
        await update_status(
            f"✅ <b>Звук успешно добавлен!</b>\n\n"
            f"<b>Источник:</b> {source_label}\n"
            f"<b>Название:</b> {entry['title']}"
        )
        return

    await update_status("⚠️ Ошибка на этапе скачивания или обработки файла.")


async def background_internet_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    muted = mif_core.is_muted(requester_id)
    can_message = True

    if not muted:
        try:
            await bot.send_message(requester_id, f"🔍 Ищу в интернете «{query_text}» (параллельный поиск)...")
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

    # Обертки для фонового поиска
    async def fetch_tiktok():
        try:
            return await asyncio.wait_for(
                import_tiktok.search_catalog(session, query_text, max_results=1, min_score=mif_core.FUZZY_MATCH_THRESHOLD),
                timeout=2.0
            )
        except Exception:
            return []

    async def fetch_mi():
        try:
            return await importer.search_catalog(session, query_text, max_results=1, min_score=mif_core.FUZZY_MATCH_THRESHOLD, fast_only=True)
        except Exception:
            return []

    tt_cands, mi_cands = await asyncio.gather(fetch_tiktok(), fetch_mi())

    candidates = []
    source_type = "tiktok"

    if tt_cands:
        candidates = tt_cands
    elif mi_cands:
        candidates = mi_cands
        source_type = "myinstants"

    if not candidates:
        await notify(f"⚠️ Не нашёл: «{query_text}»")
        return

    _, sound = candidates[0]
    sound["source_type"] = source_type

    if mif_core.find_duplicate_by_title(sound["title"]):
        await notify("✅ Готово")
        return

    source_label = "TikTok" if source_type == "tiktok" else "MyInstants"
    await notify(f"➖ Нашёл: «{query_text}» в {source_label} — публикую...")
    
    try:
        status, entry = await import_one_sound(bot, sound)
        if status in ("added", "duplicate") and entry:
            await notify("✅ Готово")
    except Exception:
        pass

async def background_internet_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    muted = mif_core.is_muted(requester_id)
    can_message = True

    status_msg = None
    if not muted:
        try:
            status_msg = await bot.send_message(
                requester_id, 
                f"🔍 <b>Автопоиск в интернете:</b> «{query_text}»\nПараллельно опрашиваю TikTok (2с) и MyInstants..."
            )
        except TelegramAPIError:
            can_message = False

    async def notify(text: str) -> None:
        if not muted and can_message and status_msg:
            try:
                await status_msg.edit_text(text, parse_mode="HTML")
            except TelegramAPIError:
                pass

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    start_time = asyncio.get_running_loop().time()

    async def fetch_tiktok():
        try:
            return await asyncio.wait_for(
                import_tiktok.search_catalog(session, query_text, max_results=1, min_score=mif_core.FUZZY_MATCH_THRESHOLD),
                timeout=2.0
            )
        except asyncio.TimeoutError:
            return "TIMEOUT"
        except Exception:
            return []

    async def fetch_mi():
        try:
            return await importer.search_catalog(session, query_text, max_results=1, min_score=mif_core.FUZZY_MATCH_THRESHOLD, fast_only=True)
        except Exception:
            return []

    tt_res, mi_res = await asyncio.gather(fetch_tiktok(), fetch_mi())

    candidates = []
    source_type = ""

    if isinstance(tt_res, list) and tt_res:
        candidates = tt_res
        source_type = "tiktok"
        elapsed = round(asyncio.get_running_loop().time() - start_time, 2)
        await notify(f"⚡ <b>TikTok успел за {elapsed}с!</b>\nНашел: «{candidates[0][1]['title']}»\nЗапускаю ssstik.io...")
    elif mi_res:
        candidates = mi_res
        source_type = "myinstants"
        reason = "⏱ TikTok превысил 2 сек" if tt_res == "TIMEOUT" else "TikTok не нашел совпадений"
        await notify(f"⏳ <b>{reason}.</b>\nБеру MyInstants: «{candidates[0][1]['title']}»\nЗапускаю yt-dlp...")
    else:
        await notify(f"⚠️ Не нашел «{query_text}» ни в TikTok, ни в MyInstants.")
        return

    _, sound = candidates[0]
    sound["source_type"] = source_type

    if mif_core.find_duplicate_by_title(sound["title"]):
        await notify("✅ Звук с таким названием уже есть в базе.")
        return

    try:
        status, entry = await import_one_sound(bot, sound, notify_func=notify)
        if status in ("added", "duplicate") and entry:
            await notify(f"✅ <b>Готово!</b> Звук «{entry['title']}» добавлен.")
    except Exception:
        await notify("⚠️ Ошибка при обработке звука.")
        

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
    