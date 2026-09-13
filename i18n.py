"""Единая локализация Telegram-бота и Mini App.

Внутри приложения используются только три кода: ru, en и uk. Telegram
присылает украинский как uk; неизвестные значения намеренно переходят в en.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("ru", "en", "uk")

LANGUAGE_LABELS = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "uk": "🇺🇦 Українська",
}

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "language_prompt": "Выбери язык / Choose language / Обери мову:",
        "language_current": "Текущий язык: {language_name}. Выбери новый:",
        "language_saved": "Язык сохранён: {language_name}.",
        "start": (
            "🔊 <b>MIFki Bot</b>\n\n"
            "Ищи звуки через инлайн: <code>@MIFki_bot запрос</code>\n"
            "Добавь свой — пришли аудиофайл в этот чат.\n\n"
            "/app — открыть все звуки в приложении\n"
            "/help — все команды"
        ),
        "cancel_none": "Сейчас нечего отменять.",
        "cancel_done": "Добавление звука отменено.",
        "mute_on": "🔕 Уведомления отключены. Включить — /unmute.",
        "mute_off": "🔔 Уведомления включены.",
        "popular_empty": "Рейтинг пока пуст.",
        "popular_title": "🏆 <b>Топ-20 популярных звуков:</b>",
        "sound_not_found": "❌ Звук не найден в базе",
        "history_cleared": "🗑 История очищена. Избранное не тронуто.",
        "app_unconfigured": "⚠️ Приложение сейчас не настроено. Спроси администратора бота.",
        "app_intro": "Все звуки канала в одном приложении — ищи, слушай, отправляй в чат.",
        "app_button": "🔊 Открыть MIFki",
        "voice_message": "Voice message",
        "help": (
            "🔊 <b>MIFki</b>\n\n"
            "<code>@MIFki_bot запрос</code> — найти звук в любом чате\n\n"
            "/app — открыть все звуки в приложении\n"
            "/popular — топ-20 популярных звуков\n"
            "/language — сменить язык\n"
            "/clear_history — очистить свою историю\n"
            "/mute · /unmute — уведомления об автопоиске\n"
            "/cancel — отменить добавление звука\n\n"
            "<b>Добавить звук:</b> пришли аудиофайл → напиши описание\n\n"
            '/loadsSearch "текст" — найти и добавить звук из интернета'
        ),
        "info": (
            "ℹ️ <b>MIFki Bot</b>\n\n"
            "Звуков в базе: <b>{count}</b>\n"
            "Канал: {channel}\n"
            "Поиск: @MIFki_bot\n\n"
            "Источники: MyInstants · TikTok\n"
            "Распознавание: Google STT (ru + en)\n"
            "Дедупликация: SHA-256 аудиохэш\n"
            "Защита от дублей: asyncio.Lock"
        ),
        "audio_unknown": "Не удалось определить тип аудио.",
        "audio_received": "✅ Аудио получено!\nТеперь отправь описание и теги. /cancel — отменить.",
        "description_empty": "⚠️ Описание не может быть пустым.",
        "description_too_long": "⚠️ Описание слишком длинное (макс. {max_length} символов).",
        "state_expired": "Срок ожидания истёк. Отправь аудио ещё раз.",
        "processing_audio": "⏳ Распознаю речь и готовлю файл...",
        "prepare_error": "⚠️ {error}\nДобавление не отменено — отправь описание ещё раз или /cancel.",
        "publish_error": "⚠️ Не удалось отправить звук в канал.\nПопробуй ещё раз или /cancel.",
        "duplicate": "⚠️ Такой звук уже есть в базе: «{title}».\nОбрежь или измени файл, если думаешь, что это ошибка.",
        "published_with_warning": "✅ Опубликовано в канале.\n⚠️ Авто-описание: {error}",
        "published": "✅ Опубликовано в канале.\nАвто-описание: {description}",
        "description_required": "⚠️ Отправь текстовое описание или /cancel.",
        "unknown_command": "Не знаю команду: {command}.{hint}\n/help — список команд.",
        "unknown_command_hint": "\nПохоже, опечатка — команда называется <code>/loads</code> (с «s» на конце).",
        "button_info": "ℹ️ /info — о боте",
        "not_configured": "не задано",
        "loads_already": "⚠️ Автозагрузка уже запущена. Останови через /loadsStop.",
        "loads_start_target": "▶️ Запуск (цель: {count}). Остановка — /loadsStop.",
        "loads_start_forever": "▶️ Запуск (бесконечно). Остановка — /loadsStop.",
        "loads_not_running": "Автозагрузка сейчас не запущена.",
        "loads_stopping": "⏸ Останавливаю (доработаю паузу {seconds:.0f} сек).",
        "loads_search_usage": '/loadsSearch "текст" — укажи запрос.',
        "loads_race_status": (
            "🏁 <b>Гонка трёх источников:</b>\n"
            "• 🎬 TikTok (DDG → ssstik)\n"
            "• 🎵 MyInstants (прямое скачивание)\n"
            "• 🔧 MyInstants (yt-dlp)\n\n"
            "Параллельно ищу и скачиваю — победит самый быстрый..."
        ),
        "loads_no_results": "❌ <b>Все источники не нашли ничего.</b>\nПопробуй другой запрос.",
        "loads_winner": "⚡ <b>{source} выиграл!</b>\n«{title}» → конвертирую и публикую...",
        "loads_duplicate_title": "⚠️ <b>Дубликат!</b> «{title}» уже в базе.",
        "loads_duplicate_hash": "⚠️ <b>Дубликат по хэшу!</b> Совпадает с «{title}».",
        "loads_added": "✅ <b>Добавлен!</b>\nИсточник: {source}\nНазвание: {title}",
        "loads_publish_error": "⚠️ Ошибка публикации.",
        "auto_search_status": "🔍 <b>Автопоиск:</b> «{query}»\nTikTok · MyInstants · yt-dlp — параллельно...",
        "auto_search_none": "⚠️ Не нашёл «{query}» нигде.",
        "auto_search_existing": "✅ Уже есть в базе.",
        "auto_search_done": "✅ <b>Готово!</b> «{title}» добавлен.",
        "auto_search_error": "⚠️ Ошибка при конвертации или публикации.",
        "only_admin": "⛔ Только для администратора.",
        "loads_usage": (
            "Не понял. Доступно:\n"
            "/loads — бесконечная автозагрузка\n"
            "/loads5 — загрузить 5 новых MIFов\n"
            "/loadsStop — остановить\n"
            '/loadsSearch "запрос" — найти и загрузить звук'
        ),
        "download": "📥 Скачиваю с MyInstants...",
        "cloudflare": "📥 Прямое скачивание заблокировано — пробую резервный способ...",
        "convert": "⚙️ Конвертирую в Voice OGG + распознаю речь...",
        "loader_stopped": "⏹ Автозагрузка остановлена. Добавлено: {count}.",
    },
    "en": {
        "language_prompt": "Choose language / Выбери язык / Обери мову:",
        "language_current": "Current language: {language_name}. Choose a new one:",
        "language_saved": "Language saved: {language_name}.",
        "start": (
            "🔊 <b>MIFki Bot</b>\n\n"
            "Search sounds inline: <code>@MIFki_bot query</code>\n"
            "Add your own — send an audio file here.\n\n"
            "/app — open all sounds in the app\n"
            "/help — all commands"
        ),
        "cancel_none": "There is nothing to cancel.",
        "cancel_done": "Sound addition cancelled.",
        "mute_on": "🔕 Notifications disabled. Enable them with /unmute.",
        "mute_off": "🔔 Notifications enabled.",
        "popular_empty": "The ranking is empty for now.",
        "popular_title": "🏆 <b>Top 20 popular sounds:</b>",
        "sound_not_found": "❌ Sound not found in the database",
        "history_cleared": "🗑 History cleared. Favorites were not changed.",
        "app_unconfigured": "⚠️ The app is not configured yet. Ask the bot administrator.",
        "app_intro": "All channel sounds in one app — search, listen, and send them to chat.",
        "app_button": "🔊 Open MIFki",
        "voice_message": "Voice message",
        "help": (
            "🔊 <b>MIFki</b>\n\n"
            "<code>@MIFki_bot query</code> — find a sound in any chat\n\n"
            "/app — open all sounds in the app\n"
            "/popular — top 20 popular sounds\n"
            "/language — change language\n"
            "/clear_history — clear your history\n"
            "/mute · /unmute — auto-search notifications\n"
            "/cancel — cancel sound addition\n\n"
            "<b>Add a sound:</b> send an audio file → write a description\n\n"
            '/loadsSearch "text" — find and add a sound from the internet'
        ),
        "info": (
            "ℹ️ <b>MIFki Bot</b>\n\n"
            "Sounds in database: <b>{count}</b>\n"
            "Channel: {channel}\n"
            "Search: @MIFki_bot\n\n"
            "Sources: MyInstants · TikTok\n"
            "Speech recognition: Google STT (ru + en)\n"
            "Deduplication: SHA-256 audio hash\n"
            "Duplicate protection: asyncio.Lock"
        ),
        "audio_unknown": "Could not determine the audio type.",
        "audio_received": "✅ Audio received!\nNow send a description and tags. /cancel — cancel.",
        "description_empty": "⚠️ Description cannot be empty.",
        "description_too_long": "⚠️ Description is too long (max. {max_length} characters).",
        "state_expired": "The waiting period expired. Send the audio again.",
        "processing_audio": "⏳ Recognizing speech and preparing the file...",
        "prepare_error": "⚠️ {error}\nThe addition is not cancelled — send the description again or /cancel.",
        "publish_error": "⚠️ Could not send the sound to the channel.\nTry again or /cancel.",
        "duplicate": "⚠️ This sound is already in the database: “{title}”.\nTrim or change the file if this is unexpected.",
        "published_with_warning": "✅ Published to the channel.\n⚠️ Auto-description: {error}",
        "published": "✅ Published to the channel.\nAuto-description: {description}",
        "description_required": "⚠️ Send a text description or /cancel.",
        "unknown_command": "I don't know this command: {command}.{hint}\n/help — command list.",
        "unknown_command_hint": "\nMaybe this is a typo — the command is <code>/loads</code> (with an “s” at the end).",
        "button_info": "ℹ️ /info — about the bot",
        "not_configured": "not configured",
        "loads_already": "⚠️ Auto-loading is already running. Stop it with /loadsStop.",
        "loads_start_target": "▶️ Starting (target: {count}). Stop with /loadsStop.",
        "loads_start_forever": "▶️ Starting (unlimited). Stop with /loadsStop.",
        "loads_not_running": "Auto-loading is not running.",
        "loads_stopping": "⏸ Stopping (finishing the current {seconds:.0f}-second pause).",
        "loads_search_usage": '/loadsSearch "text" — enter a query.',
        "loads_race_status": (
            "🏁 <b>Three-source race:</b>\n"
            "• 🎬 TikTok (DDG → ssstik)\n"
            "• 🎵 MyInstants (direct download)\n"
            "• 🔧 MyInstants (yt-dlp)\n\n"
            "Searching and downloading in parallel — the fastest wins..."
        ),
        "loads_no_results": "❌ <b>No source found anything.</b>\nTry another query.",
        "loads_winner": "⚡ <b>{source} won!</b>\n“{title}” → converting and publishing...",
        "loads_duplicate_title": "⚠️ <b>Duplicate!</b> “{title}” is already in the database.",
        "loads_duplicate_hash": "⚠️ <b>Hash duplicate!</b> Matches “{title}”.",
        "loads_added": "✅ <b>Added!</b>\nSource: {source}\nTitle: {title}",
        "loads_publish_error": "⚠️ Publication error.",
        "auto_search_status": "🔍 <b>Auto-search:</b> “{query}”\nTikTok · MyInstants · yt-dlp — in parallel...",
        "auto_search_none": "⚠️ Could not find “{query}” anywhere.",
        "auto_search_existing": "✅ Already in the database.",
        "auto_search_done": "✅ <b>Done!</b> “{title}” was added.",
        "auto_search_error": "⚠️ Conversion or publication error.",
        "only_admin": "⛔ Admin only.",
        "loads_usage": (
            "I didn't understand. Available:\n"
            "/loads — unlimited auto-loading\n"
            "/loads5 — load 5 new MIFs\n"
            "/loadsStop — stop\n"
            '/loadsSearch "query" — find and load a sound'
        ),
        "download": "📥 Downloading from MyInstants...",
        "cloudflare": "📥 Direct download blocked — trying the backup method...",
        "convert": "⚙️ Converting to Voice OGG + recognizing speech...",
        "loader_stopped": "⏹ Auto-loading stopped. Added: {count}.",
    },
    "uk": {
        "language_prompt": "Обери мову / Choose language / Выбери язык:",
        "language_current": "Поточна мова: {language_name}. Обери нову:",
        "language_saved": "Мову збережено: {language_name}.",
        "start": (
            "🔊 <b>MIFki Bot</b>\n\n"
            "Шукай звуки через інлайн: <code>@MIFki_bot запит</code>\n"
            "Додай свій — надішли аудіофайл у цей чат.\n\n"
            "/app — відкрити всі звуки в застосунку\n"
            "/help — усі команди"
        ),
        "cancel_none": "Зараз нічого скасовувати.",
        "cancel_done": "Додавання звуку скасовано.",
        "mute_on": "🔕 Сповіщення вимкнено. Увімкнути — /unmute.",
        "mute_off": "🔔 Сповіщення увімкнено.",
        "popular_empty": "Рейтинг поки порожній.",
        "popular_title": "🏆 <b>Топ-20 популярних звуків:</b>",
        "sound_not_found": "❌ Звук не знайдено в базі",
        "history_cleared": "🗑 Історію очищено. Обране не змінено.",
        "app_unconfigured": "⚠️ Застосунок ще не налаштовано. Запитай адміністратора бота.",
        "app_intro": "Усі звуки каналу в одному застосунку — шукай, слухай і надсилай у чат.",
        "app_button": "🔊 Відкрити MIFki",
        "voice_message": "Голосове повідомлення",
        "help": (
            "🔊 <b>MIFki</b>\n\n"
            "<code>@MIFki_bot запит</code> — знайти звук у будь-якому чаті\n\n"
            "/app — відкрити всі звуки в застосунку\n"
            "/popular — топ-20 популярних звуків\n"
            "/language — змінити мову\n"
            "/clear_history — очистити історію\n"
            "/mute · /unmute — сповіщення автопошуку\n"
            "/cancel — скасувати додавання звуку\n\n"
            "<b>Додати звук:</b> надішли аудіофайл → напиши опис\n\n"
            '/loadsSearch "текст" — знайти й додати звук з інтернету'
        ),
        "info": (
            "ℹ️ <b>MIFki Bot</b>\n\n"
            "Звуків у базі: <b>{count}</b>\n"
            "Канал: {channel}\n"
            "Пошук: @MIFki_bot\n\n"
            "Джерела: MyInstants · TikTok\n"
            "Розпізнавання: Google STT (ru + en)\n"
            "Дедуплікація: SHA-256 аудіохеш\n"
            "Захист від дублів: asyncio.Lock"
        ),
        "audio_unknown": "Не вдалося визначити тип аудіо.",
        "audio_received": "✅ Аудіо отримано!\nТепер надішли опис і теги. /cancel — скасувати.",
        "description_empty": "⚠️ Опис не може бути порожнім.",
        "description_too_long": "⚠️ Опис задовгий (макс. {max_length} символів).",
        "state_expired": "Час очікування минув. Надішли аудіо ще раз.",
        "processing_audio": "⏳ Розпізнаю мовлення та готую файл...",
        "prepare_error": "⚠️ {error}\nДодавання не скасовано — надішли опис ще раз або /cancel.",
        "publish_error": "⚠️ Не вдалося надіслати звук у канал.\nСпробуй ще раз або /cancel.",
        "duplicate": "⚠️ Такий звук уже є в базі: «{title}».\nОбріж або зміни файл, якщо це помилка.",
        "published_with_warning": "✅ Опубліковано в каналі.\n⚠️ Автоопис: {error}",
        "published": "✅ Опубліковано в каналі.\nАвтоопис: {description}",
        "description_required": "⚠️ Надішли текстовий опис або /cancel.",
        "unknown_command": "Не знаю цієї команди: {command}.{hint}\n/help — список команд.",
        "unknown_command_hint": "\nМожливо, це помилка — команда називається <code>/loads</code> (з «s» наприкінці).",
        "button_info": "ℹ️ /info — про бота",
        "not_configured": "не налаштовано",
        "loads_already": "⚠️ Автозавантаження вже запущено. Зупини через /loadsStop.",
        "loads_start_target": "▶️ Запуск (ціль: {count}). Зупинка — /loadsStop.",
        "loads_start_forever": "▶️ Запуск (без обмеження). Зупинка — /loadsStop.",
        "loads_not_running": "Автозавантаження зараз не запущено.",
        "loads_stopping": "⏸ Зупиняю (завершу поточну паузу {seconds:.0f} с).",
        "loads_search_usage": '/loadsSearch "текст" — вкажи запит.',
        "loads_race_status": (
            "🏁 <b>Перегони трьох джерел:</b>\n"
            "• 🎬 TikTok (DDG → ssstik)\n"
            "• 🎵 MyInstants (пряме завантаження)\n"
            "• 🔧 MyInstants (yt-dlp)\n\n"
            "Шукаю та завантажую паралельно — переможе найшвидший..."
        ),
        "loads_no_results": "❌ <b>Жодне джерело нічого не знайшло.</b>\nСпробуй інший запит.",
        "loads_winner": "⚡ <b>{source} переміг!</b>\n«{title}» → конвертую та публікую...",
        "loads_duplicate_title": "⚠️ <b>Дублікат!</b> «{title}» уже є в базі.",
        "loads_duplicate_hash": "⚠️ <b>Дублікат за хешем!</b> Збігається з «{title}».",
        "loads_added": "✅ <b>Додано!</b>\nДжерело: {source}\nНазва: {title}",
        "loads_publish_error": "⚠️ Помилка публікації.",
        "auto_search_status": "🔍 <b>Автопошук:</b> «{query}»\nTikTok · MyInstants · yt-dlp — паралельно...",
        "auto_search_none": "⚠️ Нічого не знайшов за запитом «{query}».",
        "auto_search_existing": "✅ Уже є в базі.",
        "auto_search_done": "✅ <b>Готово!</b> «{title}» додано.",
        "auto_search_error": "⚠️ Помилка конвертації або публікації.",
        "only_admin": "⛔ Лише для адміністратора.",
        "loads_usage": (
            "Не зрозумів. Доступно:\n"
            "/loads — безперервне автозавантаження\n"
            "/loads5 — завантажити 5 нових MIFів\n"
            "/loadsStop — зупинити\n"
            '/loadsSearch "запит" — знайти й завантажити звук'
        ),
        "download": "📥 Завантажую з MyInstants...",
        "cloudflare": "📥 Пряме завантаження заблоковано — пробую резервний спосіб...",
        "convert": "⚙️ Конвертую у Voice OGG та розпізнаю мовлення...",
        "loader_stopped": "⏹ Автозавантаження зупинено. Додано: {count}.",
    },
}


def normalize_language(language: str | None) -> str:
    code = (language or "").lower().replace("_", "-").split("-", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def language_label(language: str | None) -> str:
    return LANGUAGE_LABELS[normalize_language(language)]


def t(key: str, language: str | None = None, **params: Any) -> str:
    lang = normalize_language(language)
    template = TEXTS.get(lang, TEXTS[DEFAULT_LANGUAGE]).get(key)
    if template is None:
        template = TEXTS[DEFAULT_LANGUAGE].get(key, key)
    try:
        return template.format(**params)
    except (KeyError, ValueError):
        return template
