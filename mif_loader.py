"""
Автозагрузка звуков с TikTok и MyInstants: /loads, /loadsN, /loadsStop, /loadsSearch.

Для /loadsSearch и background_internet_lookup используется гонка четырёх
загрузчиков (TikTok fast/SSSTik и MyInstants direct/yt-dlp). Побеждает
первая ветка, которая вернула аудиобайты.

Цепочки:
  1. TikTok DDG/Bing Video → TikWM → yt-dlp
  2. TikTok DDG/Bing Video → SSSTik (точный резервный downloader)
  3. MyInstants API → прямое скачивание
  4. MyInstants API → yt-dlp

Поиск TikTok и MyInstants выполняется один раз и шарится между их
загрузчиками. При победе остальные HTTP-запросы и yt-dlp процессы отменяются.

Для /loads (фоновый цикл по категориям) гонка не нужна — источник всегда
MyInstants, используется import_one_sound напрямую.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
from typing import Any

import aiohttp
import requests
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import import_myinstants as importer
import import_tiktok
import mif_bugs   # report_bug живёт здесь, не в mif_core
import mif_core
import db_manager
import i18n

logger = logging.getLogger("mif-bot.loader")

LOADS_ADMIN_ID = int(os.getenv("LOADS_ADMIN_ID", "1297417116"))
LOADS_STEP_DELAY_SECONDS = 5.0

LOADS_SEARCH_RE = re.compile(r'^/loadsSearch\s+"?([^"]+?)"?\s*$')
LOADS_COUNT_RE  = re.compile(r"^/loads(\d+)$")


# ---------------------------------------------------------------------------
# Состояние /loads цикла и debounce для background_internet_lookup
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _mi_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(importer.MYINSTANTS_HEADERS)
    return s


def schedule_background_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    state = _user_lookup_states.setdefault(requester_id, _UserLookupState())
    if state.in_flight:
        state.latest_pending_query = query_text
        return
    if state.debounce_task is not None and not state.debounce_task.done():
        state.debounce_task.cancel()
    state.debounce_task = asyncio.create_task(
        _debounced_lookup(bot, requester_id, query_text)
    )


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


async def _download_via_ytdlp(url: str) -> bytes:
    """Скачивает yt-dlp в stdout без создания файлов в проекте.

    Запуск отдельным процессом позволяет завершить yt-dlp при победе другого
    источника; отмена coroutine вокруг to_thread этого не делает.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "yt_dlp",
        "--format",
        "bestaudio/best",
        "--output",
        "-",
        "--quiet",
        "--no-warnings",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=75)
    except asyncio.CancelledError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        raise
    except asyncio.TimeoutError as error:
        process.kill()
        await process.wait()
        raise RuntimeError("yt-dlp превысил лимит ожидания 75 секунд") from error

    if process.returncode != 0 or not stdout:
        details = stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"yt-dlp завершился с кодом {process.returncode}: {details}")
    return stdout


# ---------------------------------------------------------------------------
# Конкурентная гонка провайдеров (/loadsSearch и background lookup)
# ---------------------------------------------------------------------------

async def _race_all_sources(
    query: str,
    *,
    timeout: float = 75.0,
) -> tuple[bytes, dict[str, str]] | None:
    """
    Одновременно запускает поиски и четыре загрузчика.
    Возвращает (audio_bytes, sound_info) первой успешной цепочки.
    При победе отменяет HTTP-запросы и завершает yt-dlp процессы.

    Цепочки:
      1. TikTok DDG/Bing Video → TikWM → yt-dlp
      2. TikTok DDG/Bing Video → SSSTik (точный резервный downloader)
      3. MyInstants API → прямое скачивание
      4. MyInstants API → yt-dlp
    Каждый поиск выполняется один раз и отдаёт кандидата двум загрузчикам.
    """
    mi_result: list[tuple[float, dict]] = []
    mi_done = asyncio.Event()
    tiktok_result: list[tuple[int, dict[str, str]]] = []
    tiktok_done = asyncio.Event()

    won = asyncio.Event()
    winner: list[tuple[bytes, dict]] = []

    def try_claim(audio: bytes, sound: dict) -> bool:
        if won.is_set():
            return False
        won.set()
        winner.append((audio, sound))
        return True

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=75),
        connector=aiohttp.TCPConnector(limit=20),
    ) as http:
        async def do_mi_search() -> None:
            try:
                results = await importer.search_catalog_async(
                    http,
                    query,
                    min_score=mif_core.FUZZY_MATCH_FLOOR,
                    max_results=1,
                )
                mi_result.extend(results)
            except Exception as error:
                logger.debug("race: поиск MyInstants упал: %s", error)
            finally:
                mi_done.set()

        async def do_tiktok_search() -> None:
            try:
                results = await import_tiktok.search_catalog_async(
                    http,
                    query,
                    min_score=int(mif_core.FUZZY_MATCH_FLOOR),
                    max_results=1,
                )
                tiktok_result.extend(results)
            except Exception as error:
                logger.debug("race: поиск TikTok упал: %s", error)
            finally:
                tiktok_done.set()

        async def tiktok_candidate() -> dict[str, str] | None:
            await tiktok_done.wait()
            if won.is_set() or not tiktok_result:
                return None
            return tiktok_result[0][1]

        async def pipeline_tiktok_fast() -> None:
            try:
                found = await tiktok_candidate()
                if found is None:
                    return
                sound = {**found, "source_type": "tiktok"}
                try:
                    audio = await import_tiktok.download_audio_via_tikwm_async(
                        http, sound["url"]
                    )
                except Exception as tikwm_error:
                    if won.is_set():
                        return
                    logger.debug(
                        "race: TikWM не сработал (%s), запускаю yt-dlp",
                        tikwm_error,
                    )
                    audio = await _download_via_ytdlp(sound["url"])
                if try_claim(audio, sound):
                    logger.info("race: TikTok fast ПОБЕДИЛ «%s»", sound["title"])
            except Exception as error:
                logger.debug("race: TikTok fast упал: %s", error)

        async def pipeline_tiktok_ssstik() -> None:
            try:
                found = await tiktok_candidate()
                if found is None or won.is_set():
                    return
                sound = {**found, "source_type": "tiktok_ssstik"}
                logger.info("race: SSSTik начал загрузку «%s»", sound["title"])
                audio = await import_tiktok.download_audio_via_ssstik_async(
                    http, sound["url"]
                )
                if try_claim(audio, sound):
                    logger.info("race: SSSTik ПОБЕДИЛ «%s»", sound["title"])
            except Exception as error:
                logger.debug("race: SSSTik pipeline упал: %s", error)

        async def pipeline_mi_direct() -> None:
            await mi_done.wait()
            if not mi_result or won.is_set():
                return
            try:
                _, found = mi_result[0]
                sound = {**found, "source_type": "myinstants"}
                audio = await importer.download_audio_async(http, sound["url"])
                if try_claim(audio, sound):
                    logger.info("race: MI прямое ПОБЕДИЛО «%s»", sound["title"])
            except importer.AudioTooLargeError:
                logger.debug("race: MI прямое: файл >20 МБ")
            except Exception as error:
                logger.debug("race: MI прямое упало: %s", error)

        async def pipeline_mi_ytdlp() -> None:
            await mi_done.wait()
            if not mi_result or won.is_set():
                return
            try:
                _, found = mi_result[0]
                sound = {**found, "source_type": "myinstants"}
                audio = await _download_via_ytdlp(sound["url"])
                if try_claim(audio, sound):
                    logger.info("race: MI yt-dlp ПОБЕДИЛ «%s»", sound["title"])
            except Exception as error:
                logger.debug("race: MI yt-dlp упал: %s", error)

        # Все источники и загрузчики стартуют одновременно. SSSTik не ждёт
        # провала быстрых источников — он получает тот же результат поиска.
        tasks = [
            asyncio.create_task(do_mi_search(), name="mif-mi-search"),
            asyncio.create_task(do_tiktok_search(), name="mif-tiktok-search"),
            asyncio.create_task(pipeline_mi_direct(), name="mif-mi-direct"),
            asyncio.create_task(pipeline_mi_ytdlp(), name="mif-mi-ytdlp"),
            asyncio.create_task(pipeline_tiktok_fast(), name="mif-tiktok-fast"),
            asyncio.create_task(pipeline_tiktok_ssstik(), name="mif-tiktok-ssstik"),
        ]

        remaining = set(tasks)
        deadline = asyncio.get_running_loop().time() + timeout
        while remaining and not won.is_set():
            time_left = deadline - asyncio.get_running_loop().time()
            if time_left <= 0:
                break
            _, remaining = await asyncio.wait(
                remaining,
                timeout=time_left,
                return_when=asyncio.FIRST_COMPLETED,
            )

        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return winner[0] if winner else None


# ---------------------------------------------------------------------------
# Конвертация + публикация (общая для loadsSearch и background_lookup)
# ---------------------------------------------------------------------------

async def _convert_and_publish(
    bot: Bot,
    audio_bytes: bytes,
    sound: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    """
    Принимает сырые байты победителя гонки, конвертирует и публикует.
    Возвращает ('added' | 'duplicate' | 'error', запись_или_None).
    """
    title = sound.get("title", "")
    source_type = sound.get("source_type", "myinstants")
    source_label = "TikTok" if source_type.startswith("tiktok") else "MyInstants"

    try:
        bot_text, transcription_error, ogg_bytes, content_hash = (
            await mif_core.prepare_audio_from_bytes(audio_bytes)
        )
    except RuntimeError as error:
        await mif_bugs.report_bug(bot, f"race: конвертация не удалась для «{title}»: {error}")
        return "error", None

    displayed = bot_text or "Речь не распознана."
    base_caption = (
        f"<b>MIF с {source_label} (автозагрузка)</b>\n\n"
        f"<b>Название:</b> {html.escape(mif_core.clip_text(title))}\n"
        f"<b>Авто-описание:</b> {html.escape(mif_core.clip_text(displayed))}\n"
        f"<b>Источник:</b> {html.escape(sound.get('url', ''))}"
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
            source_url=sound.get("url", ""),
        )
    except TelegramAPIError as error:
        await mif_bugs.report_bug(bot, f"race: Telegram отклонил «{title}»: {error}")
        return "error", None

    if status == "added" and transcription_error:
        logger.warning("Добавлен без авто-описания: %s — %s", title, transcription_error)

    return status, new_mif


# ---------------------------------------------------------------------------
# import_one_sound — используется только в /loads цикле (MyInstants batch)
# ---------------------------------------------------------------------------

async def import_one_sound(
    bot: Bot,
    session: requests.Session,
    sound: dict[str, str],
    notify_func=None,
    language: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """
    Для /loads цикла: источник уже известен (всегда MyInstants).
    Прямое скачивание → yt-dlp как резерв.
    """
    title = sound["title"]

    if notify_func:
        await notify_func(i18n.t("download", language))

    try:
        audio_bytes = await asyncio.to_thread(importer.download_audio, session, sound["url"])
    except importer.AudioTooLargeError:
        logger.info("Пропущен (>20 МБ): %s", title)
        return "error", None
    except Exception as direct_err:
        logger.info("Прямое скачивание не вышло (%s) → yt-dlp: %s", type(direct_err).__name__, title)
        if notify_func:
            await notify_func(i18n.t("cloudflare", language))
        try:
            audio_bytes = await _download_via_ytdlp(sound["url"])
        except Exception as ytdlp_err:
            await mif_bugs.report_bug(
                bot,
                f"Автозагрузка (MyInstants): оба метода не сработали для «{title}»\n"
                f"  прямое: {direct_err}\n  yt-dlp: {ytdlp_err}",
            )
            return "error", None

    if notify_func:
        await notify_func(i18n.t("convert", language))

    return await _convert_and_publish(bot, audio_bytes, {**sound, "source_type": "myinstants"})


# ---------------------------------------------------------------------------
# /loads — фоновый цикл по категориям MyInstants
# ---------------------------------------------------------------------------

async def run_loads_loop(bot: Bot, chat_id: int, target_count: int | None) -> None:
    session = _mi_session()
    pager = importer.CatalogPager()
    language = db_manager.get_user_language(chat_id)

    try:
        while not loader_state.stop_event.is_set():
            if target_count is not None and loader_state.added_count >= target_count:
                break

            page_url = pager.current_url
            try:
                page_html = await asyncio.to_thread(importer.fetch_page, session, page_url)
                sounds = importer.parse_page(page_html)
            except requests.HTTPError as error:
                code = error.response.status_code if error.response is not None else None
                if code not in {404, 410}:
                    await mif_bugs.report_bug(
                        bot,
                        f"Автозагрузка: «{pager.current_category}» не загрузилась: {error}",
                    )
                pager.advance_category()
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue
            except requests.RequestException as error:
                await mif_bugs.report_bug(bot, f"Автозагрузка: ошибка загрузки категории: {error}")
                pager.advance_category()
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue

            if not sounds:
                pager.advance_category()
                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                continue

            for sound in sounds:
                if loader_state.stop_event.is_set() or (
                    target_count is not None and loader_state.added_count >= target_count
                ):
                    break
                if mif_core.find_duplicate_by_title(sound["title"]) is not None:
                    await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                    continue

                sound["source_type"] = "myinstants"
                try:
                    status, _ = await import_one_sound(bot, session, sound)
                except Exception:
                    logger.exception("Автозагрузка: критическая ошибка на «%s»", sound["title"])
                    status = "error"

                if status == "added":
                    loader_state.added_count += 1

                await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)

            pager.advance_page()
    finally:
        try:
            await bot.send_message(
                chat_id,
                i18n.t("loader_stopped", language, count=loader_state.added_count),
            )
        except TelegramAPIError:
            pass
        loader_state.task = None


async def handle_loads_start(message: Message, target_count: int | None) -> None:
    language = db_manager.ensure_user_language(
        message.from_user.id if message.from_user else message.chat.id,
        message.from_user.language_code if message.from_user else None,
    )
    if loader_state.task is not None and not loader_state.task.done():
        await message.answer(i18n.t("loads_already", language))
        return

    loader_state.stop_event = asyncio.Event()
    loader_state.added_count = 0
    loader_state.target_count = target_count
    loader_state.task = asyncio.create_task(
        run_loads_loop(message.bot, message.chat.id, target_count)
    )

    if target_count:
        await message.answer(i18n.t("loads_start_target", language, count=target_count))
    else:
        await message.answer(i18n.t("loads_start_forever", language))


async def handle_loads_stop(message: Message) -> None:
    language = db_manager.get_user_language(
        message.from_user.id if message.from_user else message.chat.id
    )
    if loader_state.task is None or loader_state.task.done():
        await message.answer(i18n.t("loads_not_running", language))
        return

    loader_state.stop_event.set()
    await message.answer(
        i18n.t("loads_stopping", language, seconds=LOADS_STEP_DELAY_SECONDS)
    )


# ---------------------------------------------------------------------------
# /loadsSearch — ручной поиск с гонкой трёх источников
# ---------------------------------------------------------------------------

async def handle_loads_search(message: Message, query: str) -> None:
    language = db_manager.ensure_user_language(
        message.from_user.id if message.from_user else message.chat.id,
        message.from_user.language_code if message.from_user else None,
    )
    if not query:
        await message.answer(i18n.t("loads_search_usage", language))
        return

    status_msg = await message.answer(
        i18n.t("loads_race_status", language),
        parse_mode="HTML",
    )

    async def upd(text: str) -> None:
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    result = await _race_all_sources(query)

    if result is None:
        await upd(i18n.t("loads_no_results", language))
        return

    audio_bytes, sound = result
    title = sound.get("title", query)
    source_type = sound.get("source_type", "myinstants")
    if source_type == "tiktok_ssstik":
        source_label = "TikTok (SSSTik)"
    elif source_type.startswith("tiktok"):
        source_label = "TikTok"
    else:
        source_label = "MyInstants"

    # Проверка дубликата по названию
    existing = mif_core.find_duplicate_by_title(title)
    if existing:
        await upd(i18n.t("loads_duplicate_title", language, title=title))
        await message.answer_voice(voice=existing["file_id"])
        return

    await upd(i18n.t("loads_winner", language, source=source_label, title=title))

    status, entry = await _convert_and_publish(message.bot, audio_bytes, sound)

    if status == "duplicate" and entry:
        await upd(i18n.t("loads_duplicate_hash", language, title=entry.get("title")))
        await message.answer_voice(voice=entry["file_id"])
        return

    if status == "added" and entry:
        await upd(
            i18n.t("loads_added", language, source=source_label, title=entry["title"])
        )
        return

    await upd(i18n.t("loads_publish_error", language))


# ---------------------------------------------------------------------------
# Фоновый поиск (после слабого инлайн-совпадения) — тоже гонка трёх
# ---------------------------------------------------------------------------

async def background_internet_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    """Инлайн-поиск дал слабый результат — пробуем найти в фоне.
    Один статусный messages, редактируется. Те же 3 параллельные цепочки.
    """
    muted = mif_core.is_muted(requester_id)
    language = db_manager.get_user_language(requester_id)
    can_message = True

    status_msg = None
    if not muted:
        try:
            status_msg = await bot.send_message(
                requester_id,
                i18n.t("auto_search_status", language, query=query_text),
                parse_mode="HTML",
            )
        except TelegramAPIError:
            can_message = False

    async def notify(text: str) -> None:
        if not muted and can_message and status_msg:
            try:
                await status_msg.edit_text(text, parse_mode="HTML")
            except TelegramAPIError:
                pass

    result = await _race_all_sources(query_text)

    if result is None:
        await notify(i18n.t("auto_search_none", language, query=query_text))
        return

    audio_bytes, sound = result
    title = sound.get("title", query_text)
    source_type = sound.get("source_type", "myinstants")
    source_label = "TikTok" if source_type.startswith("tiktok") else "MyInstants"

    await notify(i18n.t("loads_winner", language, source=source_label, title=title))

    if mif_core.find_duplicate_by_title(title):
        await notify(i18n.t("auto_search_existing", language))
        return

    status, entry = await _convert_and_publish(bot, audio_bytes, sound)

    if status in ("added", "duplicate") and entry:
        await notify(i18n.t("auto_search_done", language, title=entry["title"]))
    elif status == "error":
        await notify(i18n.t("auto_search_error", language))


# ---------------------------------------------------------------------------
# Диспетчер /loads* команд
# ---------------------------------------------------------------------------

async def handle_loads_commands(message: Message) -> None:
    language = db_manager.ensure_user_language(
        message.from_user.id if message.from_user else message.chat.id,
        message.from_user.language_code if message.from_user else None,
    )
    text = (message.text or "").strip()

    search_match = LOADS_SEARCH_RE.match(text)
    if search_match:
        await handle_loads_search(message, search_match.group(1).strip())
        return

    if message.from_user is None or message.from_user.id != LOADS_ADMIN_ID:
        await message.answer(i18n.t("only_admin", language))
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

    await message.answer(i18n.t("loads_usage", language))
    