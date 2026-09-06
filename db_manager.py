"""
Куратор баз данных (DB Curator) — единая точка входа для Users DB и
Popular DB. Sounds DB по-прежнему живёт в mif_core.py (MIFS_DATABASE) — она
уже используется десятком мест в проекте и имеет свой рабочий интерфейс
(find_duplicate_by_hash, find_matching_mifs и т.д.), поэтому здесь не
дублируется, а переиспользуется через mif_core.get_mif_by_id (см. отдельный
маленький патч для mif_core.py).

Все синхронные операции (без await внутри) — в asyncio с одним потоком
это само по себе атомарно, отдельный лок не нужен (в отличие от
mif_core.publish_voice_mif, где есть настоящие сетевые await).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import mif_core

logger = logging.getLogger("mif-bot.db")

USERS_DB_PATH = Path(__file__).with_name("users_database.json")
POPULAR_DB_PATH = Path(__file__).with_name("popular_database.json")

FAVORITES_LIMIT = 10
HISTORY_LIMIT = 20


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError):
        logger.exception("Не удалось прочитать %s, использую значение по умолчанию", path)
        return default


def _save_json(path: Path, data: Any) -> None:
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(path)


USERS_DB: dict[str, dict[str, Any]] = _load_json(USERS_DB_PATH, {})
POPULAR_DB: dict[str, int] = _load_json(POPULAR_DB_PATH, {})


def _save_users_db() -> None:
    _save_json(USERS_DB_PATH, USERS_DB)


def _save_popular_db() -> None:
    _save_json(POPULAR_DB_PATH, POPULAR_DB)


def _get_or_create_user(user_id: str) -> dict[str, Any]:
    if user_id not in USERS_DB:
        USERS_DB[user_id] = {"favorites": [], "history": {}, "used_sound_ids": []}
    return USERS_DB[user_id]


# --- Избранное -----------------------------------------------------------


def add_to_favorites(user_id: int | str, sound_id: int | str) -> None:
    """Добавляет звук в избранное. Если уже там — просто поднимает наверх
    (список хранится "самое новое первым"). Лимит FAVORITES_LIMIT — старые
    вытесняются с конца."""
    user_id, sound_id = str(user_id), str(sound_id)
    user = _get_or_create_user(user_id)

    if sound_id in user["favorites"]:
        user["favorites"].remove(sound_id)
    user["favorites"].insert(0, sound_id)
    del user["favorites"][FAVORITES_LIMIT:]

    _save_users_db()


def remove_from_favorites(user_id: int | str, sound_id: int | str) -> bool:
    user_id, sound_id = str(user_id), str(sound_id)
    user = _get_or_create_user(user_id)
    if sound_id in user["favorites"]:
        user["favorites"].remove(sound_id)
        _save_users_db()
        return True
    return False


# --- История + глобальный рейтинг -----------------------------------------


def record_usage(user_id: int | str, sound_id: int | str) -> None:
    """Вызывать при каждом реальном использовании звука (см. пункт про
    chosen_inline_result выше — без него сюда просто ничего не прилетит).

    Обновляет личную историю (счётчик + время последнего использования,
    вытеснение по LRU при превышении HISTORY_LIMIT) и, если это ПЕРВОЕ
    использование этого звука этим пользователем за всё время — плюсует
    глобальный рейтинг ровно на 1. Повторные использования одним и тем же
    человеком на глобальный рейтинг не влияют (антиспам)."""
    user_id, sound_id = str(user_id), str(sound_id)
    user = _get_or_create_user(user_id)

    if sound_id not in user["used_sound_ids"]:
        user["used_sound_ids"].append(sound_id)
        POPULAR_DB[sound_id] = POPULAR_DB.get(sound_id, 0) + 1
        _save_popular_db()

    history = user["history"]
    now = datetime.now(timezone.utc).isoformat()
    if sound_id in history:
        history[sound_id]["count"] += 1
        history[sound_id]["last_used"] = now
    else:
        history[sound_id] = {"count": 1, "last_used": now}
        if len(history) > HISTORY_LIMIT:
            oldest_id = min(history, key=lambda sid: history[sid]["last_used"])
            del history[oldest_id]

    _save_users_db()


def clear_history(user_id: int | str) -> None:
    """Чистит только отображаемую историю. used_sound_ids (антиспам-учёт
    для глобального рейтинга) НЕ трогается намеренно — иначе можно было бы
    почистить историю и заново накручивать глобальную популярность теми же
    звуками."""
    user_id = str(user_id)
    user = _get_or_create_user(user_id)
    user["history"] = {}
    _save_users_db()


# --- Выдача ----------------------------------------------------------------


def get_popular_sounds(limit: int = 20) -> list[dict[str, Any]]:
    """Топ по глобальному рейтингу (Popular DB), возвращает полные записи
    звуков через mif_core.get_mif_by_id, а не голые ID."""
    top_ids = sorted(POPULAR_DB, key=lambda sid: POPULAR_DB[sid], reverse=True)[:limit]
    result = []
    for sound_id in top_ids:
        sound = mif_core.get_mif_by_id(sound_id)
        if sound is not None:
            result.append(sound)
    return result


def get_personal_menu(user_id: int | str) -> list[dict[str, Any]]:
    """Избранное (до FAVORITES_LIMIT) + история по убыванию частоты (до
    HISTORY_LIMIT). Если у пользователя пусто и там, и там — отдаёт
    глобальный топ (онбординг для новых)."""
    user_id = str(user_id)
    user = USERS_DB.get(user_id)

    if not user or (not user["favorites"] and not user["history"]):
        return get_popular_sounds(limit=20)

    favorite_ids = user["favorites"][:FAVORITES_LIMIT]
    history_ids = sorted(
        user["history"], key=lambda sid: user["history"][sid]["count"], reverse=True
    )[:HISTORY_LIMIT]

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for sound_id in favorite_ids + history_ids:
        if sound_id in seen:
            continue
        seen.add(sound_id)
        sound = mif_core.get_mif_by_id(sound_id)
        if sound is not None:
            result.append(sound)

    return result
    