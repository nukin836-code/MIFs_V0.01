"""
Backend Telegram Mini App «MIFki» — веб-витрина звуков из mif_core.MIFS_DATABASE.

Никакой новой базы данных здесь нет и не должно быть: этот файл — тонкая
HTTP-обёртка поверх уже существующих mif_core.py (сама база звуков, поиск)
и db_manager.py (избранное/история/рейтинг). Ровно тот же принцип, что и во
всём остальном проекте — одна точка правды, никакого раздвоения логики
между ботом и веб-приложением.

Реальные аудиофайлы лежат в Telegram (у бота есть file_id на каждый звук),
поэтому:
  - для проигрывания в приложении используется прокси-эндпоинт /api/audio/*,
    который один раз качает файл через Bot API и дальше отдаёт его из
    локального кэша на диске (см. AUDIO_CACHE_DIR);
  - "Открыть на канале" в интерфейсе — обычная ссылка t.me/<канал>/<msg_id>,
    подгружать её содержимое самим тут не нужно.

Авторизация: Telegram Mini Apps присылают подписанную строку initData
(Telegram.WebApp.initData) — сервер обязан сам проверить подпись HMAC, а не
доверять user_id, который просто пришёл бы в теле запроса. Без этой проверки
кто угодно мог бы дёргать /api/favorites или /api/send от чужого имени.
См. validate_init_data() и https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Запуск (для разработки):
    uvicorn webapp_api:app --host 127.0.0.1 --port 8010 --reload
В проде — см. start_miniapp.sh (Termux + cloudflared).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from json import JSONDecodeError
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db_manager
import i18n
import mif_core

logger = logging.getLogger("mif-bot.miniapp")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = (
    mif_core.CHANNEL_ID.lstrip("@")
    if str(mif_core.CHANNEL_ID).startswith("@")
    else ""
)

# Сколько секунд считаем initData ещё свежей. Telegram сам не ограничивает
# срок жизни, ограничение — наша защита от повторного использования
# перехваченной когда-то строки. Сутки с запасом хватает для одной сессии
# использования приложения.
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60

AUDIO_CACHE_DIR = Path(__file__).with_name("audio_cache")
AUDIO_CACHE_DIR.mkdir(exist_ok=True)

WEBAPP_STATIC_DIR = Path(__file__).with_name("webapp")
if not WEBAPP_STATIC_DIR.exists():
    # Старый файл в проекте был создан с опечаткой wabapp. Поддерживаем его
    # как fallback, чтобы API не падал до переименования каталога.
    WEBAPP_STATIC_DIR = Path(__file__).with_name("wabapp")

# Список отдаётся вкладками, а не одним махом — на большой базе присылать
# клиенту сразу все тысячи записей ни к чему, да и Mini App живёт в WebView
# на телефоне.
DEFAULT_PAGE_LIMIT = 60
MAX_PAGE_LIMIT = 200


_bot: Bot | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _bot
    if BOT_TOKEN:
        _bot = Bot(token=BOT_TOKEN)
        logger.info("Mini App backend: бот инициализирован, аудио-прокси и /api/send доступны")
    else:
        _bot = None
        logger.warning(
            "BOT_TOKEN не задан — /api/audio и /api/send будут отвечать 500. "
            "Список и поиск звуков при этом работают."
        )
    try:
        yield
    finally:
        if _bot is not None:
            await _bot.session.close()


app = FastAPI(title="MIFki Mini App API", lifespan=lifespan)


# --- Проверка Telegram.WebApp.initData -------------------------------------


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int) -> dict[str, str]:
    """Проверяет HMAC-подпись initData по алгоритму из документации Telegram.
    Кидает ValueError с человекочитаемой причиной, если что-то не так —
    вызывающий код превращает это в HTTP 401."""
    if not init_data:
        raise ValueError("initData пустая")

    pairs = parse_qsl(init_data, strict_parsing=True, keep_blank_values=True)
    parsed = dict(pairs)
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("В initData нет поля hash")

    auth_date = parsed.get("auth_date")
    if auth_date:
        try:
            age = time.time() - int(auth_date)
        except ValueError:
            age = None
        if age is not None and age > max_age_seconds:
            raise ValueError("initData устарела, открой приложение заново")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("Подпись initData не совпадает")

    return parsed


def _extract_user(parsed_init_data: dict[str, str]) -> dict:
    raw_user = parsed_init_data.get("user")
    if not raw_user:
        raise ValueError("В initData нет поля user")
    try:
        user = json.loads(raw_user)
        if not isinstance(user, dict):
            raise ValueError
        return user
    except (JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Не удалось прочитать user из initData") from error


def _extract_user_id(parsed_init_data: dict[str, str]) -> int:
    try:
        return int(_extract_user(parsed_init_data)["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Не удалось прочитать user.id из initData") from error


def get_user_id_required(
    x_telegram_init_data: str = Header(..., alias="X-Telegram-Init-Data"),
) -> int:
    """Для эндпоинтов, где авторизация обязательна (избранное, отправка в чат)."""
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не настроен на сервере")
    try:
        parsed = validate_init_data(x_telegram_init_data, BOT_TOKEN, INIT_DATA_MAX_AGE_SECONDS)
        user = _extract_user(parsed)
        user_id = int(user["id"])
        db_manager.ensure_user_language(user_id, user.get("language_code"))
        return user_id
    except ValueError as error:
        raise HTTPException(401, str(error)) from error


def get_user_id_optional(
    x_telegram_init_data: str | None = Header(None, alias="X-Telegram-Init-Data"),
) -> int | None:
    """Для списков: если initData нет или она не прошла проверку — просто
    показываем список без ★-разметки, а не роняем запрос. Так список звуков
    остаётся доступен, даже если фронт открыли вне Telegram при отладке."""
    if not x_telegram_init_data or not BOT_TOKEN:
        return None
    try:
        parsed = validate_init_data(x_telegram_init_data, BOT_TOKEN, INIT_DATA_MAX_AGE_SECONDS)
        user = _extract_user(parsed)
        user_id = int(user["id"])
        db_manager.ensure_user_language(user_id, user.get("language_code"))
        return user_id
    except ValueError:
        return None


# --- Сериализация звука ------------------------------------------------------


def serialize_sound(mif: dict, favorite_ids: set[str]) -> dict:
    mif_id = str(mif.get("id", ""))
    title = str(mif.get("title") or mif.get("user_description") or "Без названия")
    tags_source = str(mif.get("user_tags") or mif.get("tags") or "")
    channel_message_id = mif.get("channel_message_id")

    return {
        "id": mif_id,
        "title": title,
        "description": str(mif.get("bot_description") or ""),
        "tags": [tag for tag in tags_source.split() if tag][:6],
        "media_type": mif.get("file_type") or mif.get("media_type") or "voice",
        "channel_url": (
            f"https://t.me/{CHANNEL_USERNAME}/{channel_message_id}" if channel_message_id else None
        ),
        "is_favorite": mif_id in favorite_ids,
    }


def _text_filter(records: list[dict], query_text: str) -> list[dict]:
    needle = query_text.strip().lower()
    if not needle:
        return records
    filtered = []
    for mif in records:
        haystack = " ".join(
            str(mif.get(field, ""))
            for field in ("user_tags", "tags", "bot_tags", "bot_description", "title")
        ).lower()
        if needle in haystack:
            filtered.append(mif)
    return filtered


# --- Эндпоинты ---------------------------------------------------------------


@app.get("/api/config")
def get_config(user_id: int | None = Depends(get_user_id_optional)) -> dict:
    return {
        "channel_username": CHANNEL_USERNAME,
        "language": db_manager.get_user_language(user_id) if user_id is not None else i18n.DEFAULT_LANGUAGE,
    }


@app.get("/api/sounds")
def list_sounds(
    q: str = Query("", description="поисковый запрос"),
    tab: str = Query("all", pattern="^(all|popular|mine)$"),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    user_id: int | None = Depends(get_user_id_optional),
) -> dict:
    favorite_ids = db_manager.get_favorite_ids(user_id) if user_id is not None else set()

    if tab == "mine":
        if user_id is None:
            raise HTTPException(401, "Открой приложение через бота, чтобы увидеть своё избранное")
        records = db_manager.get_favorites(user_id)
        records = _text_filter(records, q)
    elif tab == "popular":
        records = db_manager.get_popular_sounds(limit=500)
        records = _text_filter(records, q)
    elif q.strip():
        records, _best_score = mif_core.find_matching_mifs(q)
    else:
        records = list(mif_core.MIFS_DATABASE)

    total = len(records)
    page = records[offset : offset + limit]
    return {
        "total": total,
        "offset": offset,
        "items": [serialize_sound(mif, favorite_ids) for mif in page],
    }


class FavoriteRequest(BaseModel):
    sound_id: str
    action: str  # "add" | "remove"


class LanguageRequest(BaseModel):
    language: str


@app.post("/api/language")
def set_language(
    payload: LanguageRequest,
    user_id: int = Depends(get_user_id_required),
) -> dict:
    language = i18n.normalize_language(payload.language)
    db_manager.set_user_language(user_id, language)
    return {
        "ok": True,
        "language": language,
        "label": i18n.language_label(language),
    }


@app.post("/api/favorites")
def toggle_favorite(payload: FavoriteRequest, user_id: int = Depends(get_user_id_required)) -> dict:
    if mif_core.get_mif_by_id(payload.sound_id) is None:
        raise HTTPException(404, "Такого звука нет в базе")

    if payload.action == "add":
        db_manager.add_to_favorites(user_id, payload.sound_id)
    elif payload.action == "remove":
        db_manager.remove_from_favorites(user_id, payload.sound_id)
    else:
        raise HTTPException(400, "action должен быть 'add' или 'remove'")

    return {"ok": True, "is_favorite": payload.sound_id in db_manager.get_favorite_ids(user_id)}


class SendRequest(BaseModel):
    sound_id: str


@app.post("/api/send")
async def send_to_chat(payload: SendRequest, user_id: int = Depends(get_user_id_required)) -> dict:
    """Отправляет звук пользователю в личку с ботом (Mini App всегда
    открыта из этого же приватного чата — см. MenuButtonWebApp в main.py —
    поэтому chat_id == user_id гарантированно валиден, ЕСЛИ пользователь
    хоть раз нажимал /start)."""
    if _bot is None:
        raise HTTPException(500, "Бот не настроен на сервере")

    mif = mif_core.get_mif_by_id(payload.sound_id)
    if mif is None:
        raise HTTPException(404, "Такого звука нет в базе")

    file_id = mif.get("file_id")
    file_type = mif.get("file_type") or mif.get("media_type") or "voice"

    try:
        if file_type == "audio":
            await _bot.send_audio(chat_id=user_id, audio=file_id, caption=mif.get("title"))
        else:
            await _bot.send_voice(chat_id=user_id, voice=file_id, caption=mif.get("title"))
    except TelegramAPIError as error:
        # Самая частая причина — пользователь никогда не писал боту /start,
        # у бота просто нет чата, куда слать. Отдаём это текстом наружу,
        # фронт покажет tg.showAlert с понятной причиной.
        raise HTTPException(502, f"Telegram отказал: {error}") from error

    db_manager.record_usage(user_id, payload.sound_id)
    return {"ok": True}


@app.get("/api/audio/{sound_id}")
async def stream_audio(sound_id: str) -> FileResponse:
    """Отдаёт сам файл для проигрывания в <audio>. Публично (без initData) —
    это то же самое аудио, что и так открыто любому в самом канале, поэтому
    отдельная авторизация тут не нужна, важна была только для операций от
    имени конкретного пользователя (избранное, отправка)."""
    if _bot is None:
        raise HTTPException(500, "Бот не настроен на сервере")

    mif = mif_core.get_mif_by_id(sound_id)
    if mif is None:
        raise HTTPException(404, "Такого звука нет в базе")

    cache_path = AUDIO_CACHE_DIR / f"{sound_id}.ogg"
    if not cache_path.exists():
        file_id = mif.get("file_id")
        if not file_id:
            raise HTTPException(404, "У записи нет file_id")
        try:
            telegram_file = await _bot.get_file(file_id)
            if telegram_file.file_path is None:
                raise HTTPException(502, "Telegram не вернул путь к файлу")
            buffer = await _bot.download_file(telegram_file.file_path)
        except TelegramAPIError as error:
            raise HTTPException(502, f"Не удалось скачать файл у Telegram: {error}") from error
        if buffer is None:
            raise HTTPException(502, "Telegram не отдал содержимое файла")
        cache_path.write_bytes(buffer.read())

    return FileResponse(cache_path, media_type="audio/ogg")


# Статика (index.html, css, js) — монтируется последней, чтобы /api/* маршруты
# выше имели приоритет над "поймай всё" статик-сервером.
app.mount("/", StaticFiles(directory=WEBAPP_STATIC_DIR, html=True), name="webapp")
