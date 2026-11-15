"""
Шар доступу до реляційної бази даних MIFki (SQLite).

Єдина точка, де проєкт торкається файлу бази. Замінює чотири JSON-файли
(mifs_database.json, users_database.json, popular_database.json,
muted_users.json): бот, Mini App і завантажувачі тепер працюють зі спільним
файлом БД, кожна зміна — окрема транзакція, а не переписування всього файлу.

Основні рішення:
  * режим WAL: читачі не блокують запис, запис не блокує читачів;
  * PRAGMA foreign_keys = ON на КОЖНОМУ з'єднанні (у SQLite вона вимкнена
    за замовчуванням і діє лише для одного з'єднання);
  * одне з'єднання на потік (threading.local): бот працює в одному потоці
    asyncio, а FastAPI виконує синхронні ендпоїнти в пулі потоків;
  * перша ініціалізація автоматично переносить старі JSON-файли (один раз,
    під блокуванням BEGIN IMMEDIATE, тому бот і Mini App, що стартують
    одночасно, не зроблять міграцію двічі).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("mif-bot.database")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("MIFKI_DB_PATH") or BASE_DIR / "mifki.db")

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    language    TEXT    NOT NULL DEFAULT 'en'
                CHECK (language IN ('ru', 'en', 'uk')),
    is_muted    INTEGER NOT NULL DEFAULT 0 CHECK (is_muted IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sounds (
    sound_id           INTEGER PRIMARY KEY,
    title              TEXT    NOT NULL,
    file_id            TEXT    NOT NULL,
    file_type          TEXT    NOT NULL DEFAULT 'voice'
                       CHECK (file_type IN ('voice', 'audio')),
    bot_description    TEXT    NOT NULL DEFAULT '',
    content_hash       TEXT    NOT NULL UNIQUE,
    channel_message_id INTEGER,
    source_url         TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tags (
    tag_id  INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS sound_tags (
    sound_id INTEGER NOT NULL REFERENCES sounds(sound_id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(tag_id)     ON DELETE CASCADE,
    PRIMARY KEY (sound_id, tag_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    user_id  INTEGER NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
    sound_id INTEGER NOT NULL REFERENCES sounds(sound_id) ON DELETE CASCADE,
    added_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, sound_id)
);

CREATE TABLE IF NOT EXISTS usage (
    user_id    INTEGER NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
    sound_id   INTEGER NOT NULL REFERENCES sounds(sound_id) ON DELETE CASCADE,
    use_count  INTEGER NOT NULL DEFAULT 1 CHECK (use_count >= 1),
    first_used TEXT    NOT NULL,
    last_used  TEXT    NOT NULL,
    in_history INTEGER NOT NULL DEFAULT 1 CHECK (in_history IN (0, 1)),
    PRIMARY KEY (user_id, sound_id)
);

CREATE INDEX IF NOT EXISTS idx_usage_sound ON usage(sound_id);
CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id, added_at DESC);
CREATE INDEX IF NOT EXISTS idx_sound_tags_tag ON sound_tags(tag_id);

CREATE VIEW IF NOT EXISTS v_popularity AS
SELECT sound_id, COUNT(*) AS unique_users
FROM usage
GROUP BY sound_id;
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized_path: Path | None = None


# --------------------------------------------------------------------------
# З'єднання та транзакції
# --------------------------------------------------------------------------


def _open_connection(path: Path) -> sqlite3.Connection:
    # isolation_level=None — "автокоміт": транзакціями керуємо явно
    # (див. transaction()), а не неявними BEGIN модуля sqlite3.
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA synchronous = NORMAL")
    # Python-версії lower()/casefold(): вбудований lower() у SQLite
    # працює лише з ASCII і не зрозумів би кирилицю.
    con.create_function("pylower", 1, _py_lower, deterministic=True)
    return con


def _py_lower(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip().lower()


def get_connection() -> sqlite3.Connection:
    """З'єднання поточного потоку (створюється й налаштовується один раз)."""
    init_db()
    con = getattr(_local, "con", None)
    if con is None or getattr(_local, "path", None) != DB_PATH:
        con = _open_connection(DB_PATH)
        _local.con = con
        _local.path = DB_PATH
    return con


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Транзакція із негайним блокуванням запису (BEGIN IMMEDIATE): два
    процеси не зможуть одночасно виконати цикл «прочитати — змінити», а
    SQLite сам поставить другого в чергу до busy_timeout."""
    con = get_connection()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


# --------------------------------------------------------------------------
# Ініціалізація та міграція зі старих JSON-файлів
# --------------------------------------------------------------------------


def _retry_locked(action, attempts: int = 60):
    """Перемикання журналу в WAL і створення схеми на новому файлі БД SQLite
    може повернути «database is locked» одразу, не чекаючи busy_timeout, якщо
    кілька процесів стартують одночасно. Тоді повторюємо дію з паузою."""
    for attempt in range(attempts):
        try:
            return action()
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) and "busy" not in str(error):
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.05 * (attempt + 1), 0.5))


def init_db() -> None:
    global _initialized_path
    if _initialized_path == DB_PATH:
        return
    with _init_lock:
        if _initialized_path == DB_PATH:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = _open_connection(DB_PATH)
        try:
            _retry_locked(lambda: con.execute("PRAGMA journal_mode = WAL"))
            _retry_locked(lambda: con.executescript(SCHEMA_SQL))
            con.execute("BEGIN IMMEDIATE")
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version < SCHEMA_VERSION:
                    migrated = _migrate_from_json(con)
                    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    if migrated:
                        logger.info("Міграція JSON → SQLite: %s", migrated)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
        _initialized_path = DB_PATH


def _read_json(name: str, default: Any) -> Any:
    path = BASE_DIR / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Не вдалося прочитати %s під час міграції", path)
        return default


def _migrate_from_json(con: sqlite3.Connection) -> dict[str, int]:
    """Одноразовий перенос старих JSON-файлів. Нічого в них не змінює і не
    видаляє — їх можна залишити як резервну копію."""
    sounds = _read_json("mifs_database.json", [])
    users = _read_json("users_database.json", {})
    muted = {int(x) for x in _read_json("muted_users.json", []) if str(x).lstrip("-").isdigit()}
    stats = {"sounds": 0, "users": 0, "favorites": 0, "usage": 0}
    if not isinstance(sounds, list) or not isinstance(users, dict):
        return stats

    known_ids: set[int] = set()
    for mif in sounds:
        try:
            sound_id = int(str(mif["id"]))
            file_id = str(mif["file_id"])
        except (KeyError, TypeError, ValueError):
            continue
        title = str(mif.get("title") or mif.get("user_description") or "Звук")
        content_hash = mif.get("content_hash") or f"nohash:{sound_id}"
        file_type = mif.get("file_type") or mif.get("media_type") or "voice"
        if file_type not in ("voice", "audio"):
            file_type = "voice"
        try:
            con.execute(
                "INSERT INTO sounds(sound_id, title, file_id, file_type, bot_description,"
                " content_hash, channel_message_id, source_url) VALUES (?,?,?,?,?,?,?,?)",
                (sound_id, title, file_id, file_type,
                 str(mif.get("bot_description") or ""), content_hash,
                 mif.get("channel_message_id"), mif.get("source_url") or None),
            )
        except sqlite3.IntegrityError:
            logger.warning("Міграція: пропущено дублікат звуку %s", sound_id)
            continue
        _link_tags(con, sound_id, str(mif.get("user_tags") or mif.get("tags") or title))
        known_ids.add(sound_id)
        stats["sounds"] += 1

    for raw_user_id, user in users.items():
        try:
            user_id = int(raw_user_id)
        except ValueError:
            continue
        language = user.get("language") if isinstance(user, dict) else None
        con.execute(
            "INSERT OR IGNORE INTO users(user_id, language, is_muted) VALUES (?,?,?)",
            (user_id, language if language in ("ru", "en", "uk") else "en", int(user_id in muted)),
        )
        stats["users"] += 1
        if not isinstance(user, dict):
            continue
        for sound_id in user.get("favorites", []):
            if int(sound_id) in known_ids:
                con.execute("INSERT OR IGNORE INTO favorites(user_id, sound_id) VALUES (?,?)",
                            (user_id, int(sound_id)))
                stats["favorites"] += 1
        for sound_id, record in user.get("history", {}).items():
            if int(sound_id) in known_ids:
                last = record.get("last_used", "")
                con.execute("INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?,1)",
                            (user_id, int(sound_id), max(1, int(record.get("count", 1))), last, last))
                stats["usage"] += 1
        for sound_id in user.get("used_sound_ids", []):
            if int(sound_id) in known_ids:
                con.execute(
                    "INSERT OR IGNORE INTO usage VALUES (?,?,1,datetime('now'),datetime('now'),0)",
                    (user_id, int(sound_id)))
    for user_id in muted:  # заглушені, яких не було в users_database
        con.execute("INSERT OR IGNORE INTO users(user_id, is_muted) VALUES (?, 1)", (user_id,))
    return stats


# --------------------------------------------------------------------------
# Звуки
# --------------------------------------------------------------------------


def _split_tags(tags_text: str) -> list[str]:
    seen: dict[str, None] = {}
    for token in str(tags_text).lower().split():
        seen.setdefault(token, None)
    return list(seen)


def _link_tags(con: sqlite3.Connection, sound_id: int, tags_text: str) -> None:
    for name in _split_tags(tags_text):
        con.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
        con.execute(
            "INSERT OR IGNORE INTO sound_tags(sound_id, tag_id) "
            "SELECT ?, tag_id FROM tags WHERE name = ?", (sound_id, name))


def _rows_to_mifs(con: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Рядки таблиці sounds → словники у форматі, якого очікує решта коду
    (поля title/user_tags/bot_tags тощо збережено для сумісності)."""
    if not rows:
        return []
    ids = [row["sound_id"] for row in rows]
    tags_by_sound: dict[int, list[str]] = {sid: [] for sid in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for tag_row in con.execute(
            f"SELECT st.sound_id, t.name FROM sound_tags st JOIN tags t USING (tag_id) "
            f"WHERE st.sound_id IN ({marks}) ORDER BY st.rowid", chunk):
            tags_by_sound[tag_row["sound_id"]].append(tag_row["name"])
    result = []
    for row in rows:
        tags = " ".join(tags_by_sound[row["sound_id"]])
        mif: dict[str, Any] = {
            "id": str(row["sound_id"]),
            "title": row["title"],
            "file_id": row["file_id"],
            "file_type": row["file_type"],
            "media_type": row["file_type"],
            "user_description": row["title"],
            "bot_description": row["bot_description"],
            "user_tags": tags,
            "tags": tags,
            "bot_tags": row["bot_description"].lower(),
            "channel_message_id": row["channel_message_id"],
            "content_hash": row["content_hash"],
        }
        if row["source_url"]:
            mif["source_url"] = row["source_url"]
        result.append(mif)
    return result


def fetch_sounds(where: str = "", params: tuple = (), limit: int | None = None) -> list[dict[str, Any]]:
    con = get_connection()
    sql = "SELECT * FROM sounds"
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY sound_id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return _rows_to_mifs(con, con.execute(sql, params).fetchall())


def count_sounds() -> int:
    return get_connection().execute("SELECT COUNT(*) FROM sounds").fetchone()[0]


def get_sound(sound_id: int | str) -> dict[str, Any] | None:
    try:
        key = int(str(sound_id))
    except ValueError:
        return None
    found = fetch_sounds("sound_id = ?", (key,), limit=1)
    return found[0] if found else None


def find_by_hash(content_hash: str) -> dict[str, Any] | None:
    found = fetch_sounds("content_hash = ?", (content_hash,), limit=1)
    return found[0] if found else None


def find_by_title(title: str) -> dict[str, Any] | None:
    found = fetch_sounds("pylower(title) = ?", (title.strip().lower(),), limit=1)
    return found[0] if found else None


def insert_sound(
    *,
    title: str,
    file_id: str,
    file_type: str,
    bot_description: str,
    content_hash: str,
    channel_message_id: int | None,
    source_url: str | None,
    tags_text: str,
) -> dict[str, Any]:
    """Додає звук і його теги в одній транзакції. Ідентифікатор присвоює
    сама СКБД (INTEGER PRIMARY KEY) — два процеси не можуть отримати один
    номер. Повторний content_hash порушує UNIQUE → sqlite3.IntegrityError."""
    with transaction() as con:
        cursor = con.execute(
            "INSERT INTO sounds(title, file_id, file_type, bot_description, content_hash,"
            " channel_message_id, source_url) VALUES (?,?,?,?,?,?,?)",
            (title, file_id, file_type, bot_description, content_hash,
             channel_message_id, source_url or None),
        )
        sound_id = cursor.lastrowid
        _link_tags(con, sound_id, tags_text or title)
    created = get_sound(sound_id)
    assert created is not None
    return created


def sync_sounds(entries: list[dict[str, Any]]) -> int:
    """Додає записи (у «старому» форматі словника), яких ще немає в БД за
    content_hash. Використовує reconcile_channel.py. Повертає кількість доданих."""
    added = 0
    for entry in entries:
        content_hash = entry.get("content_hash")
        if not content_hash or find_by_hash(str(content_hash)) is not None:
            continue
        file_type = entry.get("file_type") or "voice"
        try:
            insert_sound(
                title=str(entry.get("title") or "Звук"),
                file_id=str(entry["file_id"]),
                file_type=file_type if file_type in ("voice", "audio") else "voice",
                bot_description=str(entry.get("bot_description") or ""),
                content_hash=str(content_hash),
                channel_message_id=entry.get("channel_message_id"),
                source_url=entry.get("source_url"),
                tags_text=str(entry.get("user_tags") or entry.get("title") or ""),
            )
            added += 1
        except sqlite3.IntegrityError:
            continue
    return added


class SoundsView:
    """Сумісна з list «віконце» на таблицю sounds. Код проєкту, який раніше
    ітерував mif_core.MIFS_DATABASE (список у пам'яті), працює без змін, але
    щоразу бачить актуальний вміст БД — зокрема звуки, додані іншим процесом."""

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(fetch_sounds())

    def __len__(self) -> int:
        return count_sounds()

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice) and key.start in (None, 0) and key.step is None \
                and isinstance(key.stop, int) and key.stop >= 0:
            return fetch_sounds(limit=key.stop)
        return fetch_sounds()[key]

    def __bool__(self) -> bool:
        return count_sounds() > 0


# --------------------------------------------------------------------------
# Користувачі
# --------------------------------------------------------------------------


def get_user_language(user_id: int, default: str) -> str:
    row = get_connection().execute(
        "SELECT language FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
    return row["language"] if row else default


def ensure_user(user_id: int, language: str) -> str:
    """Створює користувача з визначеною мовою, якщо його ще немає; існуючого
    не змінює (ручний вибір мови не перезаписується). Повертає мову з БД."""
    with transaction() as con:
        con.execute("INSERT OR IGNORE INTO users(user_id, language) VALUES (?, ?)",
                    (int(user_id), language))
        return con.execute("SELECT language FROM users WHERE user_id = ?",
                           (int(user_id),)).fetchone()["language"]


def set_user_language(user_id: int, language: str) -> None:
    with transaction() as con:
        con.execute(
            "INSERT INTO users(user_id, language) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET language = excluded.language",
            (int(user_id), language))


def is_muted(user_id: int) -> bool:
    row = get_connection().execute(
        "SELECT is_muted FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
    return bool(row["is_muted"]) if row else False


def set_muted(user_id: int, muted: bool) -> None:
    with transaction() as con:
        con.execute(
            "INSERT INTO users(user_id, is_muted) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET is_muted = excluded.is_muted",
            (int(user_id), int(muted)))
