"""
Куратор персональних даних користувачів: мова, обране, історія, глобальний
рейтинг. Публічні функції збережено без змін, щоб main.py, webapp_api.py і
mif_loader.py працювали як раніше, але замість users_database.json та
popular_database.json (що читалися в пам'ять один раз і переписувалися
цілком) дані лежать у таблицях SQLite (див. database.py):

    users      — мова, ознака «сповіщення вимкнено»
    favorites  — обране (до FAVORITES_LIMIT, нові першими)
    usage      — історія використання; рядок існує, доки користувач хоч раз
                 використав звук (звідси антиспам-рейтинг), прапорець
                 in_history керує лише відображуваною історією

Глобальний рейтинг — не окремий файл, а представлення v_popularity:
кількість користувачів, які колись використали звук. Його неможливо
«накрутити» повторами, а очищення історії рейтинг не зменшує.

Кожна функція змінення даних — окрема транзакція (BEGIN IMMEDIATE), тому
бот і веб-сервер Mini App можуть працювати одночасно без втрати змін.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import database
import i18n

logger = logging.getLogger("mif-bot.db")

FAVORITES_LIMIT = 10
HISTORY_LIMIT = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(value: int | str) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


def _ensure_user_row(con, user_id: int) -> None:
    con.execute("INSERT OR IGNORE INTO users(user_id, language) VALUES (?, ?)",
                (user_id, i18n.DEFAULT_LANGUAGE))


def _sound_exists(con, sound_id: int) -> bool:
    return con.execute("SELECT 1 FROM sounds WHERE sound_id = ?", (sound_id,)).fetchone() is not None


def _sounds_in_order(ids: list[int]) -> list[dict[str, Any]]:
    """Повні записи звуків у заданому порядку (один запит замість N)."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    by_id = {int(m["id"]): m for m in database.fetch_sounds(f"sound_id IN ({marks})", tuple(ids))}
    return [by_id[sid] for sid in ids if sid in by_id]


# --- Мова ----------------------------------------------------------------


def get_user_language(user_id: int | str) -> str:
    """Повертає збережену мову користувача, не змінюючи його вибір."""
    uid = _to_int(user_id)
    if uid is None:
        return i18n.DEFAULT_LANGUAGE
    return i18n.normalize_language(database.get_user_language(uid, i18n.DEFAULT_LANGUAGE))


def ensure_user_language(user_id: int | str, telegram_language: str | None = None) -> str:
    """Створює користувача й визначає мову лише при першому контакті.

    Якщо користувач уже обрав мову вручну, наступні language_code від
    Telegram його вибір не перезаписують (INSERT OR IGNORE у database.ensure_user).
    """
    uid = _to_int(user_id)
    detected = i18n.normalize_language(telegram_language)
    if uid is None:
        return detected
    return i18n.normalize_language(database.ensure_user(uid, detected))


def set_user_language(user_id: int | str, language: str) -> str:
    normalized = i18n.normalize_language(language)
    uid = _to_int(user_id)
    if uid is not None:
        database.set_user_language(uid, normalized)
    return normalized


# --- Обране ----------------------------------------------------------------


def add_to_favorites(user_id: int | str, sound_id: int | str) -> None:
    """Додає звук в обране. Якщо він уже там — піднімає нагору (нові першими).
    Зайві старі записи (понад FAVORITES_LIMIT) видаляються в тій самій
    транзакції."""
    uid, sid = _to_int(user_id), _to_int(sound_id)
    if uid is None or sid is None:
        return
    with database.transaction() as con:
        if not _sound_exists(con, sid):
            return
        _ensure_user_row(con, uid)
        con.execute(
            "INSERT INTO favorites(user_id, sound_id, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, sound_id) DO UPDATE SET added_at = excluded.added_at",
            (uid, sid, _now()))
        con.execute(
            "DELETE FROM favorites WHERE user_id = ? AND sound_id NOT IN ("
            "SELECT sound_id FROM favorites WHERE user_id = ? "
            "ORDER BY added_at DESC, rowid DESC LIMIT ?)",
            (uid, uid, FAVORITES_LIMIT))


def remove_from_favorites(user_id: int | str, sound_id: int | str) -> bool:
    uid, sid = _to_int(user_id), _to_int(sound_id)
    if uid is None or sid is None:
        return False
    with database.transaction() as con:
        cursor = con.execute("DELETE FROM favorites WHERE user_id = ? AND sound_id = ?", (uid, sid))
        return cursor.rowcount > 0


def _favorite_ids(user_id: int | str, limit: int = FAVORITES_LIMIT) -> list[int]:
    uid = _to_int(user_id)
    if uid is None:
        return []
    rows = database.get_connection().execute(
        "SELECT sound_id FROM favorites WHERE user_id = ? "
        "ORDER BY added_at DESC, rowid DESC LIMIT ?", (uid, limit)).fetchall()
    return [row["sound_id"] for row in rows]


def get_favorites(user_id: int | str) -> list[dict[str, Any]]:
    """Тільки обране (без історії), повні записи, нові першими."""
    return _sounds_in_order(_favorite_ids(user_id))


def get_favorite_ids(user_id: int | str) -> set[str]:
    """Множина ID обраного — для ★ у довільному списку (Mini App)."""
    return {str(sid) for sid in _favorite_ids(user_id)}


# --- Історія + глобальний рейтинг ---------------------------------------------


def record_usage(user_id: int | str, sound_id: int | str) -> None:
    """Викликається при кожному реальному використанні звуку.

    Один атомарний UPSERT: або створює рядок usage (перше використання —
    саме це й збільшує глобальний рейтинг, бо v_popularity рахує рядки), або
    збільшує лічильник. Якщо звук було прибрано з історії командою
    /clear_history, лічильник починається знову з 1. Відображувана історія
    обмежена HISTORY_LIMIT найновіших записів."""
    uid, sid = _to_int(user_id), _to_int(sound_id)
    if uid is None or sid is None:
        return
    now = _now()
    with database.transaction() as con:
        if not _sound_exists(con, sid):
            return
        _ensure_user_row(con, uid)
        con.execute(
            "INSERT INTO usage(user_id, sound_id, first_used, last_used) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, sound_id) DO UPDATE SET "
            "use_count = CASE WHEN in_history = 1 THEN use_count + 1 ELSE 1 END, "
            "last_used = excluded.last_used, in_history = 1",
            (uid, sid, now, now))
        con.execute(
            "UPDATE usage SET in_history = 0 WHERE user_id = ? AND in_history = 1 "
            "AND sound_id NOT IN (SELECT sound_id FROM usage WHERE user_id = ? "
            "AND in_history = 1 ORDER BY last_used DESC LIMIT ?)",
            (uid, uid, HISTORY_LIMIT))


def clear_history(user_id: int | str) -> None:
    """Чистить лише відображувану історію (in_history = 0). Рядки usage
    лишаються, тому рейтинг не зменшується й не можна «накрутити» його
    повторно тими самими звуками."""
    uid = _to_int(user_id)
    if uid is None:
        return
    with database.transaction() as con:
        con.execute("UPDATE usage SET in_history = 0 WHERE user_id = ?", (uid,))


# --- Видача ----------------------------------------------------------------


def get_popular_sounds(limit: int = 20) -> list[dict[str, Any]]:
    """Топ за глобальним рейтингом (представлення v_popularity)."""
    rows = database.get_connection().execute(
        "SELECT sound_id FROM v_popularity ORDER BY unique_users DESC, sound_id LIMIT ?",
        (limit,)).fetchall()
    return _sounds_in_order([row["sound_id"] for row in rows])


def get_personal_menu(user_id: int | str) -> list[dict[str, Any]]:
    """Обране (до FAVORITES_LIMIT) + історія за спаданням частоти (до
    HISTORY_LIMIT). Якщо порожньо й там, і там — глобальний топ (онбординг)."""
    uid = _to_int(user_id)
    favorite_ids = _favorite_ids(user_id) if uid is not None else []
    history_ids: list[int] = []
    if uid is not None:
        rows = database.get_connection().execute(
            "SELECT sound_id FROM usage WHERE user_id = ? AND in_history = 1 "
            "ORDER BY use_count DESC, last_used DESC LIMIT ?", (uid, HISTORY_LIMIT)).fetchall()
        history_ids = [row["sound_id"] for row in rows]

    if not favorite_ids and not history_ids:
        return get_popular_sounds(limit=20)

    ordered: list[int] = []
    for sound_id in favorite_ids + history_ids:
        if sound_id not in ordered:
            ordered.append(sound_id)
    return _sounds_in_order(ordered)
