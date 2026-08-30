"""
Автозагрузка звуков с MyInstants: /loads, /loadsN, /loadsStop, /loadsSearch.

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
from typing import Any

import requests
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import import_myinstants as importer
import mif_core

logger = logging.getLogger("mif-bot.loader")

# Кому разрешено запускать /loads, /loadsN, /loadsStop. /loadsSearch доступен
# всем — это разовый точечный запрос, а не фоновый цикл.
LOADS_ADMIN_ID = int(os.getenv("LOADS_ADMIN_ID", "1297417116"))

# Пауза между КАЖДОЙ попыткой (успешной, дублем или ошибкой) — не только
# между успешными публикациями. Поднята с 2 до 5 секунд из-за flood control:
# Telegram ограничивает число сообщений В ОДИН И ТОТ ЖЕ чат/канал в минуту, а
# каждая публикация — это ДВА запроса к каналу (send_voice + правка подписи).
# Дополнительно mif_core.publish_voice_mif сам умеет пережидать лимит по
# точной подсказке Telegram, если он всё же сработает несмотря на паузу —
# так что звук не потеряется, просто немного задержится.
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


async def import_one_sound(
    bot: Bot,
    session: requests.Session,
    sound: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    """Скачивает, обрабатывает и публикует один звук с MyInstants через
    mif_core.publish_voice_mif. Возвращает
    ('added' | 'duplicate' | 'error', запись_или_None)."""
    title = sound["title"]

    try:
        audio_bytes = await asyncio.to_thread(importer.download_audio, session, sound["url"])
    except importer.AudioTooLargeError:
        return "error", None
    except requests.RequestException as error:
        await mif_core.report_bug(bot, f"Автозагрузка: не удалось скачать «{title}»: {error}")
        return "error", None

    try:
        bot_text, transcription_error, ogg_bytes, content_hash = (
            await mif_core.prepare_audio_from_bytes(audio_bytes)
        )
    except RuntimeError as error:
        await mif_core.report_bug(bot, f"Автозагрузка: не удалось обработать «{title}»: {error}")
        return "error", None

    if content_hash:
        duplicate = mif_core.find_duplicate_by_hash(content_hash)
        if duplicate is not None:
            return "duplicate", duplicate

    displayed_bot_text = bot_text or "Речь не распознана."
    base_caption = (
        "<b>MIF с MyInstants (автозагрузка)</b>\n\n"
        f"<b>Название и теги пользователя:</b> {html.escape(mif_core.clip_text(title))}\n"
        f"<b>Авто-описание от бота:</b> {html.escape(mif_core.clip_text(displayed_bot_text))}\n"
        f"<b>Источник:</b> {html.escape(sound['url'])}"
    )

    try:
        new_mif = await mif_core.publish_voice_mif(
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
            bot, f"Автозагрузка: Telegram отклонил публикацию «{title}»: {error}"
        )
        return "error", None

    if transcription_error:
        logger.warning("Добавлен без авто-описания: %s — %s", title, transcription_error)

    return "added", new_mif


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
                    # Эта категория на этой странице закончилась — идём в
                    # следующую категорию, а не пытаемся листать бесконечно.
                    pager.advance_category()
                else:
                    await mif_core.report_bug(
                        bot,
                        f"Автозагрузка: категория «{pager.current_category}» "
                        f"не загрузилась: {error}",
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

                # Дешёвая предварительная проверка по названию — не тратим
                # скачивание и ffmpeg на то, что почти наверняка уже есть.
                if mif_core.find_duplicate_by_title(sound["title"]) is not None:
                    await asyncio.sleep(LOADS_STEP_DELAY_SECONDS)
                    continue

                try:
                    status, _ = await import_one_sound(bot, session, sound)
                except Exception as error:  # не даём фоновой задаче умереть молча
                    logger.exception(
                        "Автозагрузка: непредвиденная ошибка на «%s»", sound["title"]
                    )
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
            "⚠️Автозагрузка уже запущена. Останови её через /loadsStop, если нужно "
            "начать заново."
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
        await message.answer(
            "▶️Автозагрузка запущена (бесконечный цикл).\nОстановить — /loadsStop."
        )


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

    # У сайта больше нет рабочего поиска по произвольному тексту (см.
    # комментарий над MYINSTANTS_CATEGORIES в import_myinstants.py) — сканим
    # категории сами, это занимает секунд 5-15, поэтому сразу отвечаем, что
    # не зависли.
    await message.answer("🔍Ищу на MyInstants, это может занять до ~15 секунд...")

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    try:
        candidates = await importer.search_catalog(session, query)
    except requests.RequestException:
        logger.exception("Ошибка поиска на MyInstants: %s", query)
        await message.answer("⚠️Не удалось обратиться к MyInstants. Попробуй ещё раз позже.")
        return

    if not candidates:
        await message.answer(f"На MyInstants ничего похожего на «{query}» не нашлось.")
        return

    sound = candidates[0]

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
        await message.answer(f"✅Загрузил «{entry['title']}» и добавил в поиск.")
        return

    await message.answer(f"⚠️Не удалось загрузить «{sound['title']}». Попробуй другой запрос.")


async def background_internet_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    """Локальный инлайн-поиск дал слабое совпадение (см. main.py:search_mifs)
    — пробуем найти похожий звук на MyInstants в фоне и, если получится,
    публикуем его (через тот же import_one_sound/publish_voice_mif, что и
    везде) и присылаем автору запроса личным сообщением.

    ВАЖНО: это не может попасть в тот же самый инлайн-ответ — Telegram не
    ждёт секунды сканирования+скачивания+конвертации на инлайн-запрос,
    результат обязательно приходит отдельным сообщением позже.

    ТАКЖЕ ВАЖНО: сработает, только если requester_id уже хотя бы раз писал
    боту (/start) — Telegram не разрешает ботам первыми писать пользователю.
    Если это не так, просто тихо логируем и ничего не ломаем — человек и
    так уже получил обычный (пустой/слабый) инлайн-ответ.
    """
    try:
        session = requests.Session()
        session.headers.update(importer.MYINSTANTS_HEADERS)

        candidates = await importer.search_catalog(session, query_text, max_results=1)
        if not candidates:
            return

        sound = candidates[0]

        existing = mif_core.find_duplicate_by_title(sound["title"])
        if existing is not None:
            await bot.send_message(
                requester_id, f"Кстати, по запросу «{query_text}» — «{sound['title']}» уже есть:"
            )
            await bot.send_voice(requester_id, voice=existing["file_id"])
            return

        status, entry = await import_one_sound(bot, session, sound)
        if status != "added" or entry is None:
            return

        await bot.send_message(
            requester_id,
            f"🔎По запросу «{query_text}» не нашлось в архиве, но нашёл на MyInstants "
            f"и добавил: «{entry['title']}». Уже доступен в поиске:",
        )
        await bot.send_voice(requester_id, voice=entry["file_id"])
    except TelegramAPIError:
        # Скорее всего, requester_id никогда не писал боту — Telegram не
        # даёт ботам первыми начинать личный чат. Звук при этом мог уже
        # успеть опубликоваться и попасть в базу — это не потеря, просто
        # человек не узнает о находке лично, увидит её в обычном поиске.
        logger.info(
            "Не удалось написать пользователю %s о находке по запросу «%s» "
            "(возможно, он не начинал чат с ботом)",
            requester_id,
            query_text,
        )
    except Exception:
        logger.exception("Фоновый поиск на MyInstants упал для запроса «%s»", query_text)


async def handle_loads_commands(message: Message) -> None:
    """Точка входа для любого текста, начинающегося с /loads — main.py
    регистрирует хендлер и просто вызывает эту функцию."""
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