"""
Автозагрузка звуков с TikTok и MyInstants: /loads, /loadsN, /loadsStop, /loadsSearch.

Для /loadsSearch и background_internet_lookup используется гонка трёх
параллельных цепочек (поиск+скачивание). Побеждает первая вернувшая байты.

Цепочки:
  1. TikTok DDG → ssstik (локальный сервер)
  2. MyInstants API → прямое скачивание (importer.download_audio)
  3. MyInstants API → yt-dlp

MI поиск выполняется ОДИН РАЗ и шарится между цепочками 2 и 3.
Победитель конвертируется и публикуется. Проигравшие отменяются без следа.

Для /loads (фоновый цикл по категориям) гонка не нужна — источник всегда
MyInstants, используется import_one_sound напрямую.
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
import mif_bugs   # report_bug живёт здесь, не в mif_core
import mif_core

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
    """Скачивает через yt-dlp (резерв когда прямой HTTP заблокирован)."""
    def _run() -> bytes:
        with tempfile.TemporaryDirectory() as tmpdir:
            opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
                files = os.listdir(tmpdir)
                if not files:
                    raise RuntimeError("yt-dlp не извлёк файл")
                with open(os.path.join(tmpdir, files[0]), "rb") as f:
                    return f.read()

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Гонка трёх источников (core логика /loadsSearch и background_lookup)
# ---------------------------------------------------------------------------

async def _race_all_sources(
    query: str,
    *,
    timeout: float = 45.0,
) -> tuple[bytes, dict[str, str]] | None:
    """
    Запускает три параллельные цепочки (поиск + скачивание).
    Возвращает (audio_bytes, sound_info) первой успешной цепочки.
    Остальные отменяются немедленно после победителя.

    Цепочки:
      1. TikTok DDG → ssstik (локальный FastAPI)
      2. MyInstants API → прямое скачивание
      3. MyInstants API → yt-dlp
    MI поиск выполняется один раз, результат шарится между 2 и 3.
    """
    # --- Общий кэш MI поиска ---
    mi_result: list[tuple[float, dict]] = []
    mi_done = asyncio.Event()

    async def do_mi_search() -> None:
        try:
            cands = await importer.search_catalog(
                _mi_session(), query,
                min_score=mif_core.FUZZY_MATCH_FLOOR, max_results=1,
            )
            mi_result.extend(cands)
        except Exception:
            pass
        finally:
            mi_done.set()

    # --- Победный слот (атомарен в asyncio — нет await внутри try_claim) ---
    won = asyncio.Event()
    winner: list[tuple[bytes, dict]] = []

    def try_claim(audio: bytes, sound: dict) -> bool:
        if won.is_set():
            return False
        won.set()
        winner.append((audio, sound))
        return True

    # --- Цепочка 1: TikTok ---
    async def pipeline_tiktok() -> None:
        try:
            cands = await asyncio.wait_for(
                import_tiktok.search_catalog(
                    requests.Session(), query,
                    min_score=mif_core.FUZZY_MATCH_FLOOR, max_results=1,
                ),
                timeout=8.0,
            )
            if not cands or won.is_set():
                return
            _, sound = cands[0]
            sound = {**sound, "source_type": "tiktok"}
            logger.debug("race: TikTok нашёл «%s», запускаю ssstik", sound["title"])
            audio = await asyncio.to_thread(
                import_tiktok.download_audio, requests.Session(), sound["url"]
            )
            if try_claim(audio, sound):
                logger.debug("race: TikTok ПОБЕДИЛ «%s»", sound["title"])
        except Exception as e:
            logger.debug("race: TikTok pipeline упал: %s", e)

    # --- Цепочка 2: MyInstants прямое скачивание ---
    async def pipeline_mi_direct() -> None:
        await mi_done.wait()
        if not mi_result or won.is_set():
            return
        try:
            _, sound = mi_result[0]
            sound = {**sound, "source_type": "myinstants"}
            logger.debug("race: MI прямое → «%s»", sound["title"])
            audio = await asyncio.to_thread(importer.download_audio, _mi_session(), sound["url"])
            if try_claim(audio, sound):
                logger.debug("race: MI прямое ПОБЕДИЛО «%s»", sound["title"])
        except importer.AudioTooLargeError:
            logger.debug("race: MI прямое: файл >20 МБ")
        except Exception as e:
            logger.debug("race: MI прямое упало: %s", e)

    # --- Цепочка 3: MyInstants + yt-dlp ---
    async def pipeline_mi_ytdlp() -> None:
        await mi_done.wait()
        if not mi_result or won.is_set():
            return
        try:
            _, sound = mi_result[0]
            sound = {**sound, "source_type": "myinstants"}
            logger.debug("race: yt-dlp → «%s»", sound["title"])
            audio = await _download_via_ytdlp(sound["url"])
            if try_claim(audio, sound):
                logger.debug("race: yt-dlp ПОБЕДИЛ «%s»", sound["title"])
        except Exception as e:
            logger.debug("race: yt-dlp упал: %s", e)

    # --- Запуск ---
    mi_task = asyncio.create_task(do_mi_search())
    pipe_tasks = [
        asyncio.create_task(pipeline_tiktok()),
        asyncio.create_task(pipeline_mi_direct()),
        asyncio.create_task(pipeline_mi_ytdlp()),
    ]

    # Ждём первого победителя ИЛИ пока все не завершатся
    remaining: set[asyncio.Task] = set(pipe_tasks)
    deadline = asyncio.get_running_loop().time() + timeout

    while remaining and not won.is_set():
        time_left = deadline - asyncio.get_running_loop().time()
        if time_left <= 0:
            break
        done, remaining = await asyncio.wait(
            remaining,
            timeout=time_left,
            return_when=asyncio.FIRST_COMPLETED,
        )

    # Отменяем всё лишнее
    mi_task.cancel()
    for t in pipe_tasks:
        t.cancel()
    await asyncio.gather(mi_task, *pipe_tasks, return_exceptions=True)

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
    source_label = "TikTok" if source_type == "tiktok" else "MyInstants"

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
) -> tuple[str, dict[str, Any] | None]:
    """
    Для /loads цикла: источник уже известен (всегда MyInstants).
    Прямое скачивание → yt-dlp как резерв.
    """
    title = sound["title"]

    if notify_func:
        await notify_func("📥 Скачиваю с MyInstants...")

    try:
        audio_bytes = await asyncio.to_thread(importer.download_audio, session, sound["url"])
    except importer.AudioTooLargeError:
        logger.info("Пропущен (>20 МБ): %s", title)
        return "error", None
    except Exception as direct_err:
        logger.info("Прямое скачивание не вышло (%s) → yt-dlp: %s", type(direct_err).__name__, title)
        if notify_func:
            await notify_func("📥 Cloudflare блок → пробую yt-dlp...")
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
        await notify_func("⚙️ Конвертирую в Voice OGG + распознаю речь...")

    return await _convert_and_publish(bot, audio_bytes, {**sound, "source_type": "myinstants"})


# ---------------------------------------------------------------------------
# /loads — фоновый цикл по категориям MyInstants
# ---------------------------------------------------------------------------

async def run_loads_loop(bot: Bot, chat_id: int, target_count: int | None) -> None:
    session = _mi_session()
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
                f"⏹ Автозагрузка остановлена. Добавлено: {loader_state.added_count}.",
            )
        except TelegramAPIError:
            pass
        loader_state.task = None


async def handle_loads_start(message: Message, target_count: int | None) -> None:
    if loader_state.task is not None and not loader_state.task.done():
        await message.answer("⚠️ Автозагрузка уже запущена. Останови через /loadsStop.")
        return

    loader_state.stop_event = asyncio.Event()
    loader_state.added_count = 0
    loader_state.target_count = target_count
    loader_state.task = asyncio.create_task(
        run_loads_loop(message.bot, message.chat.id, target_count)
    )

    if target_count:
        await message.answer(f"▶️ Запуск (цель: {target_count}). Остановка — /loadsStop.")
    else:
        await message.answer("▶️ Запуск (бесконечно). Остановка — /loadsStop.")


async def handle_loads_stop(message: Message) -> None:
    if loader_state.task is None or loader_state.task.done():
        await message.answer("Автозагрузка сейчас не запущена.")
        return

    loader_state.stop_event.set()
    await message.answer(f"⏸ Останавливаю (доработаю паузу {LOADS_STEP_DELAY_SECONDS:.0f} сек).")


# ---------------------------------------------------------------------------
# /loadsSearch — ручной поиск с гонкой трёх источников
# ---------------------------------------------------------------------------

async def handle_loads_search(message: Message, query: str) -> None:
    if not query:
        await message.answer('/loadsSearch "текст" — укажи запрос.')
        return

    status_msg = await message.answer(
        "🏁 <b>Гонка трёх источников:</b>\n"
        "• 🎬 TikTok (DDG → ssstik)\n"
        "• 🎵 MyInstants (прямое скачивание)\n"
        "• 🔧 MyInstants (yt-dlp)\n\n"
        "Параллельно ищу и скачиваю — победит самый быстрый...",
        parse_mode="HTML",
    )

    async def upd(text: str) -> None:
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    result = await _race_all_sources(query)

    if result is None:
        await upd("❌ <b>Все источники не нашли ничего.</b>\nПопробуй другой запрос.")
        return

    audio_bytes, sound = result
    title = sound.get("title", query)
    source_type = sound.get("source_type", "myinstants")
    source_label = "TikTok (ssstik)" if source_type == "tiktok" else "MyInstants"

    # Проверка дубликата по названию
    existing = mif_core.find_duplicate_by_title(title)
    if existing:
        await upd(f"⚠️ <b>Дубликат!</b> «{title}» уже в базе.")
        await message.answer_voice(voice=existing["file_id"])
        return

    await upd(
        f"⚡ <b>{source_label} выиграл!</b>\n"
        f"«{title}» → конвертирую и публикую..."
    )

    status, entry = await _convert_and_publish(message.bot, audio_bytes, sound)

    if status == "duplicate" and entry:
        await upd(f"⚠️ <b>Дубликат по хэшу!</b> Совпадает с «{entry.get('title')}».")
        await message.answer_voice(voice=entry["file_id"])
        return

    if status == "added" and entry:
        await upd(
            f"✅ <b>Добавлен!</b>\n"
            f"Источник: {source_label}\n"
            f"Название: {entry['title']}"
        )
        return

    await upd("⚠️ Ошибка публикации.")


# ---------------------------------------------------------------------------
# Фоновый поиск (после слабого инлайн-совпадения) — тоже гонка трёх
# ---------------------------------------------------------------------------

async def background_internet_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    """Инлайн-поиск дал слабый результат — пробуем найти в фоне.
    Один статусный messages, редактируется. Те же 3 параллельные цепочки.
    """
    muted = mif_core.is_muted(requester_id)
    can_message = True

    status_msg = None
    if not muted:
        try:
            status_msg = await bot.send_message(
                requester_id,
                f"🔍 <b>Автопоиск:</b> «{query_text}»\n"
                "TikTok · MyInstants · yt-dlp — параллельно...",
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
        await notify(f"⚠️ Не нашёл «{query_text}» нигде.")
        return

    audio_bytes, sound = result
    title = sound.get("title", query_text)
    source_type = sound.get("source_type", "myinstants")
    source_label = "TikTok" if source_type == "tiktok" else "MyInstants"

    await notify(f"⚡ <b>{source_label} выиграл!</b>\n«{title}» → публикую...")

    if mif_core.find_duplicate_by_title(title):
        await notify("✅ Уже есть в базе.")
        return

    status, entry = await _convert_and_publish(bot, audio_bytes, sound)

    if status in ("added", "duplicate") and entry:
        await notify(f"✅ <b>Готово!</b> «{entry['title']}» добавлен.")
    elif status == "error":
        await notify("⚠️ Ошибка при конвертации или публикации.")


# ---------------------------------------------------------------------------
# Диспетчер /loads* команд
# ---------------------------------------------------------------------------

async def handle_loads_commands(message: Message) -> None:
    text = (message.text or "").strip()

    search_match = LOADS_SEARCH_RE.match(text)
    if search_match:
        await handle_loads_search(message, search_match.group(1).strip())
        return

    if message.from_user is None or message.from_user.id != LOADS_ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
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
        "Не понял. Доступно:\n"
        "/loads — бесконечная автозагрузка\n"
        "/loads5 — загрузить 5 новых MIFов\n"
        "/loadsStop — остановить\n"
        '/loadsSearch "запрос" — найти и загрузить звук'
    )
    