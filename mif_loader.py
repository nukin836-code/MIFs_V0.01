"""
Автозагрузка звуков с TikTok и MyInstants: /loads, /loadsN, /loadsStop, /loadsSearch.

Логика вынесена сюда отдельно от main.py, чтобы не раздувать файл с
Telegram-хендлерами. main.py регистрирует @dp.message-хендлеры и просто
вызывает функции отсюда (см. handle_loads_commands).

Публикация звука всегда идёт через mif_core.publish_voice_mif — ту же самую
функцию, что использует и ручная загрузка в main.py. Здесь НЕТ своей копии
логики публикации/конвертации.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from typing import Any

import requests
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import import_myinstants as importer
import import_tiktok
import mif_core

logger = logging.getLogger("mif-bot.loader")

# Кому разрешено запускать /loads, /loadsN, /loadsStop. /loadsSearch доступен
# всем — это разовый точечный запрос, а не фоновый цикл.
LOADS_ADMIN_ID = int(os.getenv("LOADS_ADMIN_ID", "1297417116"))

# Пауза между КАЖДОЙ попыткой (успешной, дублем или ошибкой) — не только
# между успешными публикациями.
LOADS_STEP_DELAY_SECONDS = 5.0

LOADS_SEARCH_RE = re.compile(r'^/loadsSearch\s+"?([^"]+?)"?\s*$')
LOADS_COUNT_RE = re.compile(r"^/loads(\d+)$")


class LoaderState:
    """Состояние фонового цикла /loads. Одно на процесс — параллельно
    запустить второй цикл нельзя (проверяется в handle_loads_start)."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.stop_event: asyncio.Event = asyncio.Event()
        self.added_count: int = 0
        self.target_count: int | None = None


loader_state = LoaderState()

DEBOUNCE_DELAY_SECONDS = 0.7


class _UserLookupState:
    def __init__(self) -> None:
        self.debounce_task: asyncio.Task | None = None
        self.in_flight: bool = False
        self.latest_pending_query: str | None = None


_user_lookup_states: dict[int, _UserLookupState] = {}


def schedule_background_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    """Планирует фоновый поиск с учётом двух слоёв защиты (debounce и in-flight)."""
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


async def import_one_sound(
    bot: Bot,
    session: requests.Session,
    sound: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    """Скачивает, обрабатывает и публикует один звук с TikTok или MyInstants
    через mif_core.publish_voice_mif. Возвращает ('added' | 'duplicate' | 'error', запись_или_None)."""
    title = sound["title"]
    source_type = sound.get("source_type", "myinstants")
    source_label = "TikTok" if source_type == "tiktok" else "MyInstants"

    try:
        if source_type == "tiktok":
            audio_bytes = await asyncio.to_thread(import_tiktok.download_audio, session, sound["url"])
        else:
            audio_bytes = await asyncio.to_thread(importer.download_audio, session, sound["url"])
    except getattr(importer, "AudioTooLargeError", Exception):
        return "error", None
    except getattr(importer, "NotAudioContentError", Exception) as error:
        await mif_core.report_bug(
            bot,
            f"Автозагрузка ({source_label}): вернул не аудио для «{title}» "
            f"(content-type={getattr(error, 'content_type', 'unknown')!r}) — {sound['url']}",
        )
        return "error", None
    except Exception as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): не удалось скачать «{title}»: {error}")
        return "error", None

    try:
        bot_text, transcription_error, ogg_bytes, content_hash = (
            await mif_core.prepare_audio_from_bytes(audio_bytes)
        )
    except RuntimeError as error:
        await mif_core.report_bug(bot, f"Автозагрузка ({source_label}): не удалось обработать «{title}»: {error}")
        return "error", None

    displayed_bot_text = bot_text or "Речь не распознана."
    base_caption = (
        f"<b>MIF с {source_label} (автозагрузка)</b>\n\n"
        f"<b>Название и теги пользователя:</b> {html.escape(mif_core.clip_text(title))}\n"
        f"<b>Авто-описание от бота:</b> {html.escape(mif_core.clip_text(displayed_bot_text))}\n"
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
        await mif_core.report_bug(
            bot, f"Автозагрузка ({source_label}): Telegram отклонил публикацию «{title}»: {error}"
        )
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
            except requests.HTTPError as error:
                if error.response is not None and error.response.status_code in {404, 410}:
                    pager.advance_category()
                else:
                    await mif_core.report_bug(
                        bot,
                        f"Автозагрузка: категория «{pager.current_category}» не загрузилась: {error}",
                    )
                    pager.advance_category()
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue
            except requests.RequestException as error:
                await mif_core.report_bug(
                    bot,
                    f"Автозагрузка: категория «{pager.current_category}» не загрузилась: {error}",
                )
                pager.advance_category()
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue

            if not sounds:
                pager.advance_category()
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue

            for sound in sounds:
                if loader_state.stop_event.is_set():
                    break
                if target_count is not None and loader_state.added_count >= target_count:
                    break

                if mif_core.find_duplicate_by_title(sound["title"]) is not None:
                    await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                    continue

                sound["source_type"] = "myinstants"
                try:
                    status, _ = await import_one_sound(bot, session, sound)
                except Exception as error:
                    logger.exception("Автозагрузка: непредвиденная ошибка на «%s»", sound["title"])
                    await mif_core.report_bug(
                        bot,
                        f"Автозагрузка: непредвиденная ошибка на «{sound['title']}»: {error}",
                    )
                    status = "error"

                if status == "added":
                    loader_state.added_count += 1

                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)

            pager.advance_page()
    finally:
        try:
            await bot.send_message(
                chat_id,
                f"⏹Автозагрузка остановлена. Добавлено новых MIFов: {loader_state.added_count}.",
            )
        except TelegramAPIError:
            logger.exception("Не удалось отправить итоговое сообщение об автозагрузке")
        loader_state.task = None


async def handle_loads_start(message: Message, target_count: int | None) -> None:
    if loader_state.task is not None and not loader_state.task.done():
        await message.answer(
            "⚠️Автозагрузка уже запущена. Останови её через /loadsStop, если нужно начать заново."
        )
        return

    loader_state.stop_event = asyncio.Event()
    loader_state.added_count = 0
    loader_state.target_count = target_count
    loader_state.task = asyncio.create_task(
        run_loads_loop(message.bot, message.chat.id, target_count)
    )

    if target_count:
        await message.answer(
            f"▶️Автозагрузка запущена — цель: {target_count} новых MIFов.\n"
            "Остановить досрочно — /loadsStop."
        )
    else:
        await message.answer("▶️Автозагрузка запущена (бесконечный цикл).\nОстановить — /loadsStop.")


async def handle_loads_stop(message: Message) -> None:
    if loader_state.task is None or loader_state.task.done():
        await message.answer("Автозагрузка сейчас не запущена.")
        return

    loader_state.stop_event.set()
    await message.answer(
        "⏸Останавливаю автозагрузку — доработаю текущую паузу (до "
        f"{LOADS_STEP_DELAY_SECONDS:.0f} сек) и остановлюсь."
    )


async def handle_loads_search(message: Message, query: str) -> None:
    if not query:
        await message.answer('Укажи запрос: /loadsSearch "текст".')
        return

    await message.answer("🔍Ищу в интернете (TikTok / MyInstants)...")

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    candidates = []
    source_type = "tiktok"

    # 1. Поиск в TikTok
    try:
        candidates = await import_tiktok.search_catalog(
            session, query, min_score=mif_core.FUZZY_MATCH_FLOOR
        )
    except Exception:
        logger.exception("Ошибка поиска в TikTok для /loadsSearch: %s", query)

    # 2. Фолбэк на MyInstants
    if not candidates:
        source_type = "myinstants"
        try:
            candidates = await importer.search_catalog(session, query, min_score=mif_core.FUZZY_MATCH_FLOOR)
        except getattr(importer, "CatalogBlockedError", Exception):
            await message.answer("⚠️MyInstants ответил 403 (отказ в доступе). Попробуй позже.")
            return
        except requests.RequestException:
            await message.answer("⚠️Не удалось обратиться к источникам поиска. Попробуй позже.")
            return

    if not candidates:
        await message.answer(f"Ни в TikTok, ни на MyInstants ничего похожего на «{query}» не нашлось.")
        return

    best_score, sound = candidates[0]
    sound["source_type"] = source_type
    is_confident_match = best_score >= mif_core.FUZZY_MATCH_THRESHOLD

    existing_by_title = mif_core.find_duplicate_by_title(sound["title"])
    if existing_by_title is not None:
        await message.answer(f"⚠️«{sound['title']}» уже есть в базе. Вот он:")
        try:
            await message.answer_voice(voice=existing_by_title["file_id"])
        except TelegramAPIError:
            logger.exception("Не удалось переслать существующий MIF пользователю")
        return

    status, entry = await import_one_sound(message.bot, session, sound)

    if status == "duplicate" and entry is not None:
        await message.answer(
            f"⚠️«{sound['title']}» по звуку совпадает с уже существующей записью "
            f"«{entry.get('title')}». Вот она:"
        )
        try:
            await message.answer_voice(voice=entry["file_id"])
        except TelegramAPIError:
            logger.exception("Не удалось переслать существующий MIF пользователю")
        return

    if status == "added" and entry is not None:
        source_label = "TikTok" if source_type == "tiktok" else "MyInstants"
        if is_confident_match:
            await message.answer(f"✅Загрузил с {source_label} «{entry['title']}» и добавил в поиск.")
        else:
            await message.answer(
                f"Точного совпадения для «{query}» не нашёл, но вот ближайшее с {source_label}: "
                f"«{entry['title']}» (балл {best_score:.0f}/100). Загрузил и добавил в поиск."
            )
        return

    await message.answer(f"⚠️Не удалось загрузить «{sound['title']}». Попробуй другой запрос.")


async def background_internet_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    """Инлайн-поиск дал слабое совпадение — пробуем найти похожий звук в фоне (сначала TikTok, затем MyInstants)."""
    muted = mif_core.is_muted(requester_id)
    can_message = True

    if not muted:
        try:
            await bot.send_message(requester_id, f"🔍Ищу в интернете «{query_text}»...")
        except TelegramAPIError:
            logger.info(
                "Не удалось написать пользователю %s — поиск продолжен без статусов",
                requester_id,
            )
            can_message = False

    async def notify(text: str) -> None:
        if not muted and can_message:
            await _try_send_message(bot, requester_id, text)

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    candidates = []
    source_type = "tiktok"

    # --- 1. Пробуем TikTok ---
    try:
        candidates = await import_tiktok.search_catalog(
            session,
            query_text,
            max_results=1,
            min_score=mif_core.FUZZY_MATCH_THRESHOLD,
        )
    except Exception:
        logger.exception("Быстрый фоновый поиск в TikTok упал для запроса «%s»", query_text)

    # --- 2. Если TikTok пуст/упал — пробуем MyInstants ---
    if not candidates:
        source_type = "myinstants"
        try:
            candidates = await importer.search_catalog(
                session,
                query_text,
                max_results=1,
                min_score=mif_core.FUZZY_MATCH_THRESHOLD,
                fast_only=True,
            )
        except Exception:
            logger.exception("Быстрый фоновый поиск на MyInstants упал для запроса «%s»", query_text)

    if not candidates:
        await notify(f"⚠️Не нашёл: «{query_text}»")
        return

    _, sound = candidates[0]
    sound["source_type"] = source_type

    existing = mif_core.find_duplicate_by_title(sound["title"])
    if existing is not None:
        await notify("✅Готово")
        return

    source_label = "TikTok" if source_type == "tiktok" else "MyInstants"
    await notify(f"➖Нашёл в {source_label}: «{query_text}» — публикую в базу...")

    try:
        status, entry = await import_one_sound(bot, session, sound)
    except Exception:
        logger.exception("Публикация после фонового поиска упала для запроса «%s»", query_text)
        return

    if status in ("added", "duplicate") and entry is not None:
        await notify("✅Готово")


async def _try_send_message(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        logger.info("Не удалось отправить сообщение пользователю %s", chat_id)


async def handle_loads_commands(message: Message) -> None:
    """Точка входа для любого текста, начинающегося с /loads."""
    text = (message.text or "").strip()

    search_match = LOADS_SEARCH_RE.match(text)
    if search_match:
        await handle_loads_search(message, search_match.group(1).strip())
        return

    if message.from_user is None or message.from_user.id != LOADS_ADMIN_ID:
        await message.answer("⛔Эта команда доступна только администратору автозагрузки.")
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

    await message.answer(
        "Не понял команду. Доступно:\n"
        "/loads — бесконечная автозагрузка\n"
        "/loads5 — загрузить 5 новых MIFов\n"
        "/loadsStop — остановить\n"
        '/loadsSearch "запрос" — найти и загрузить конкретный звук'
    )
    