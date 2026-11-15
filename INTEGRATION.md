# Перехід MIFki з JSON на SQLite — що змінено і як запустити

Замінити у проєкті (поруч із main.py) файли з цього архіву:

| Файл | Що це |
|---|---|
| `database.py` | НОВИЙ: з'єднання, схема, автоматична міграція, транзакції |
| `db_manager.py` | переписано на SQL, публічні функції ті самі |
| `mif_core.py` | база звуків і /mute тепер у SQLite (`MIFS_DATABASE` — «віконце» на таблицю) |
| `reconcile_chennel.py` | працює з БД замість mifs_database.json |
| `tests/` | інтеграційні тести (заглушки aiogram/rapidfuzz — у `tests/stubs`) |

`main.py`, `webapp_api.py`, `mif_loader.py`, `import_*.py` НЕ змінювалися.

## Перший запуск
1. Залиште старі `mifs_database.json`, `users_database.json`, `muted_users.json` поруч із кодом.
2. Запустіть бота й Mini App як зазвичай. При першому старті з'явиться файл `mifki.db`,
   а в журналі — повідомлення «Міграція JSON → SQLite». JSON-файли не змінюються.
3. Бот і Mini App мають працювати в одному каталозі (або з однаковим `MIFKI_DB_PATH`).

## Важливо для Replit
За документацією Replit файлова система опублікованих (Deployments) застосунків не зберігається
між публікаціями. `.replit` проєкту має `deploymentTarget = "autoscale"`, тож `mifki.db` там
буде втрачатися. Запускайте як workflows у робочому просторі, на Termux, або винесіть БД
на постійне сховище / PostgreSQL. Додайте у `.gitignore`: `mifki.db*`, `users_database.json`,
`popular_database.json`, `muted_users.json`.

## Тести
`python tests/run_tests.py /шлях/до/старої/версії` — порівнює стару (JSON) і нову (SQLite) версії.
Тестується шар даних і логіка модулів; запити до реального Telegram імітуються.
