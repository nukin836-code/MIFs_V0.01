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
import time
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

# Инлайн-запрос улетает боту на КАЖДОЕ изменение текста, пока человек
# печатает буква за буквой — наивно запускать фоновый поиск на каждое из
# них. Вместо кулдауна используем настоящий debounce: на каждое новое
# слабое инлайн-совпадение взводим таймер на DEBOUNCE_DELAY_SECONDS; если
# за это время прилетает более новый запрос от того же человека — старый
# таймер отменяется, взводится новый. Поиск реально стартует только когда
# человек перестал печатать (таймер достоялся до конца). Раз это точнее
# решает саму причину (спам на каждую букву), отдельный кулдаун поверх
# больше не нужен — а параллельные срабатывания от РАЗНЫХ людей на похожий
# запрос уже защищены атомарной проверкой дубликата в
# mif_core.publish_voice_mif (лок), а не через это.
DEBOUNCE_DELAY_SECONDS = 0.3
_pending_debounce_tasks: dict[int, asyncio.Task] = {}


def schedule_background_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    """Взводит (или перезапускает) debounce-таймер для фонового поиска.
    Сама ничего не ищет — только планирует запуск через
    DEBOUNCE_DELAY_SECONDS, если за это время не прилетит более новый
    запрос от того же человека (тогда этот таймер отменяется и заводится
    новый — см. _debounced_lookup)."""
    existing_task = _pending_debounce_tasks.get(requester_id)
    if existing_task is not None and not existing_task.done():
        existing_task.cancel()

    _pending_debounce_tasks[requester_id] = asyncio.create_task(
        _debounced_lookup(bot, requester_id, query_text)
    )


async def _debounced_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_DELAY_SECONDS)
    except asyncio.CancelledError:
        # Прилетел более новый запрос, и его обработчик отменил именно этот
        # таймер специально (см. schedule_background_lookup) — это не
        # ошибка, просто тихо выходим, не запуская поиск для устаревшего
        # текста.
        return

    _pending_debounce_tasks.pop(requester_id, None)
    await background_internet_lookup(bot, requester_id, query_text)


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
    except importer.NotAudioContentError as error:
        await mif_core.report_bug(
            bot,
            f"Автозагрузка: MyInstants вернул не аудио для «{title}» "
            f"(content-type={error.content_type!r}) — похоже на капчу/блокировку "
            f"доступа, а не битый файл: {sound['url']}",
        )
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

    displayed_bot_text = bot_text or "Речь не распознана."
    base_caption = (
        "<b>MIF с MyInstants (автозагрузка)</b>\n\n"
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
            bot, f"Автозагрузка: Telegram отклонил публикацию «{title}»: {error}"
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

    # Раньше почти всегда срабатывал медленный обход категорий (5-15 сек),
    # теперь в норме отвечает JSON API за секунду-две — обход категорий
    # остаётся редким финальным резервом, а не обычным путём. Отдельное
    # "подожди, это долго" сообщение больше не нужно как правило; если
    # запрос всё же провалится до медленного резерва, это будет видно по
    # тому, что ответ просто придёт не сразу.
    await message.answer("🔍Ищу на MyInstants...")

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    try:
        # FUZZY_MATCH_FLOOR, а не строгий FUZZY_MATCH_THRESHOLD: человек сам
        # попросил найти именно это, так что лучше честно показать ближайшую
        # находку, чем молчать при отсутствии идеального совпадения. Обход
        # категорий внутри search_catalog всё равно всегда использует
        # строгий порог независимо от этого — см. докстринг search_catalog.
        candidates = await importer.search_catalog(session, query, min_score=mif_core.FUZZY_MATCH_FLOOR)
    except importer.CatalogBlockedError:
        logger.exception("MyInstants заблокировал доступ при поиске: %s", query)
        await message.answer(
            "⚠️MyInstants ответил 403 (отказ в доступе) — это подтверждает "
            "блокировку, а не случайную сетевую ошибку. Попробуй позже."
        )
        return
    except requests.RequestException:
        logger.exception("Ошибка поиска на MyInstants: %s", query)
        await message.answer("⚠️Не удалось обратиться к MyInstants. Попробуй ещё раз позже.")
        return

    if not candidates:
        await message.answer(
            f"На MyInstants совсем ничего похожего на «{query}» не нашлось "
            "(даже приблизительно) — просканированные категории пустые для этого запроса."
        )
        return

    best_score, sound = candidates[0]
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
        if is_confident_match:
            await message.answer(f"✅Загрузил «{entry['title']}» и добавил в поиск.")
        else:
            await message.answer(
                f"Точного совпадения для «{query}» не нашёл, но вот ближайшее, что "
                f"удалось найти: «{entry['title']}» (балл совпадения {best_score:.0f}/100). "
                "Загрузил и добавил в поиск — если это не то, попробуй другой запрос."
            )
        return

    await message.answer(f"⚠️Не удалось загрузить «{sound['title']}». Попробуй другой запрос.")


async def background_internet_lookup(bot: Bot, requester_id: int, query_text: str) -> None:
    """Локальный инлайн-поиск дал слабое совпадение (см. main.py:search_mifs)
    — пробуем найти похожий звук на MyInstants в фоне и, если получится,
    публикуем его (через тот же import_one_sound/publish_voice_mif, что и
    везде) и присылаем автору запроса личным сообщением.

    Разделено на быструю и медленную части намеренно:
    1. Быстрая: "🔍Ищу в интернете..." → только API-поиск (search_catalog
       с fast_only=True, оба варианта запроса параллельно) →
       "✅Нашёл"/"⚠️Не нашёл". Реалистично 1-3 секунды в обычном случае.
    2. Медленная (только если нашёл): скачать → сконвертировать →
       опубликовать → прислать сам файл. Это реально занимает несколько
       секунд ещё — скачивание, ffmpeg, распознавание речи, загрузка в
       Telegram — тут нечего ускорять без потери в качестве. Разделение
       специально: подтверждение "нашёл/не нашёл" быстрое, а не всё целиком.

    ВАЖНО: результат не может попасть в тот же самый инлайн-ответ — как
    только Telegram показал инлайн-подсказки, дополнить их позже нельзя,
    результат обязательно приходит отдельными сообщениями в личку.

    ВЫЗЫВАТЬ НЕ НАПРЯМУЮ, а через schedule_background_lookup — эта функция
    сама не знает про debounce, ждёт вызова "прямо сейчас, точно финальный
    запрос". Защита от спама на каждую букву во время печати живёт в
    schedule_background_lookup, не здесь.

    ТАКЖЕ ВАЖНО: сработает, только если requester_id уже хотя бы раз писал
    боту (/start) — Telegram не разрешает ботам первыми писать пользователю.
    Если это не так, просто тихо логируем и ничего не ломаем — человек и
    так уже получил обычный (пустой/слабый) инлайн-ответ.
    """
    try:
        await bot.send_message(requester_id, "🔍Ищу в интернете...")
    except TelegramAPIError:
        # requester_id никогда не писал боту — Telegram не даёт ботам
        # первыми начинать личный чат. Дальше пытаться нет смысла: ни одно
        # следующее сообщение всё равно не дойдёт.
        logger.info(
            "Не удалось начать фоновый поиск для %s (возможно, не начинал чат с ботом)",
            requester_id,
        )
        return

    session = requests.Session()
    session.headers.update(importer.MYINSTANTS_HEADERS)

    try:
        # fast_only=True — только API, без HTML-поиска и обхода категорий:
        # это часть, которая должна быть быстрой (см. докстринг выше).
        # Явно строгий порог (не FUZZY_MATCH_FLOOR, как у /loadsSearch) —
        # публикуется в общий канал автоматически, без человека, который мог
        # бы сам решить "ну и ладно, похоже".
        candidates = await importer.search_catalog(
            session,
            query_text,
            max_results=1,
            min_score=mif_core.FUZZY_MATCH_THRESHOLD,
            fast_only=True,
        )
    except Exception:
        logger.exception("Быстрый фоновый поиск на MyInstants упал для запроса «%s»", query_text)
        await _try_send_message(bot, requester_id, f"⚠️Не нашёл: «{query_text}»")
        return

    if not candidates:
        await _try_send_message(bot, requester_id, f"⚠️Не нашёл: «{query_text}»")
        return

    _, sound = candidates[0]

    existing = mif_core.find_duplicate_by_title(sound["title"])
    if existing is not None:
        await _try_send_message(bot, requester_id, f"✅Нашёл: «{query_text}» — уже есть в базе:")
        await _try_send_voice(bot, requester_id, existing["file_id"])
        return

    await _try_send_message(bot, requester_id, f"✅Нашёл: «{query_text}» — публикую в базу...")

    # Дальше — медленная часть: скачивание, конвертация, публикация.
    try:
        status, entry = await import_one_sound(bot, session, sound)
    except Exception:
        logger.exception("Публикация после фонового поиска упала для запроса «%s»", query_text)
        return

    if status != "added" or entry is None:
        return

    await _try_send_voice(bot, requester_id, entry["file_id"])


async def _try_send_message(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        logger.info("Не удалось отправить сообщение пользователю %s", chat_id)


async def _try_send_voice(bot: Bot, chat_id: int, file_id: str) -> None:
    try:
        await bot.send_voice(chat_id, voice=file_id)
    except TelegramAPIError:
        logger.info("Не удалось отправить голосовое пользователю %s", chat_id)


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