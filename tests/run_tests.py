"""
Інтеграційні тести переходу MIFki з JSON на SQLite.

Запуск:  python tests/run_tests.py [шлях_до_старого_проєкту]

Старий проєкт (версія на JSON) береться з каталогу, переданого аргументом
(за замовчуванням /tmp/old_proj). Усі перевірки виконуються в ізольованих
тимчасових копіях і підпроцесах, реальні дані не змінюються.

Зовнішні залежності, яких немає в тестовому середовищі (aiogram,
speech_recognition, rapidfuzz), підміняються легкими заглушками з
tests/stubs; для порівняння «старе проти нового» заглушки однакові.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = ROOT / "tests" / "stubs"
OLD = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/old_proj")
RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append({"test": name, "ok": ok, "detail": detail})
    print(("PASS" if ok else "FAIL"), "|", name, "|", detail)


def make_copy(src: Path) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="mifki-"))
    for item in src.iterdir():
        if item.is_file() and item.suffix in {".py", ".json"} and not item.name.startswith("test"):
            shutil.copy(item, dst / item.name)
    return dst


def run_py(code: str, workdir: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": f"{workdir}{os.pathsep}{STUBS}", "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------- 1. міграція
def test_migration() -> None:
    work = make_copy(ROOT)
    db = work / "t1.db"
    out = run_py(
        "import json, database as d\n"
        "con = d.get_connection()\n"
        "r = {t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in "
        "('sounds','tags','sound_tags','users','favorites','usage')}\n"
        "r['fk_violations'] = len(con.execute('PRAGMA foreign_key_check').fetchall())\n"
        "r['user_version'] = con.execute('PRAGMA user_version').fetchone()[0]\n"
        "r['journal'] = con.execute('PRAGMA journal_mode').fetchone()[0]\n"
        "r['distinct_hash'] = con.execute('SELECT COUNT(DISTINCT content_hash) FROM sounds').fetchone()[0]\n"
        "r['popular_rows'] = con.execute('SELECT COUNT(*) FROM v_popularity').fetchone()[0]\n"
        "print(json.dumps(r))", work, {"MIFKI_DB_PATH": str(db)})
    r = json.loads(out.stdout)
    json_sounds = len(json.loads((work / "mifs_database.json").read_text(encoding="utf-8")))
    ok = (r["sounds"] == json_sounds and r["fk_violations"] == 0 and r["user_version"] == 1
          and r["journal"] == "wal" and r["distinct_hash"] == r["sounds"])
    record("T1 міграція реальних JSON-даних", ok,
           f"звуків {r['sounds']}/{json_sounds}, тегів {r['tags']}, зв'язків {r['sound_tags']}, "
           f"користувачів {r['users']}, порушень FK {r['fk_violations']}, журнал {r['journal']}")
    shutil.rmtree(work)


# ---------------------------------------------------- 2. паритет пошуку і API
SCENARIO = r"""
import json, sys, asyncio
import mif_core, db_manager
out = {}
queries = ['sweaty', 'крокодил', 'ballerina', 'ui ai', 'брэйк кор', 'best programmer',
           'fart', 'мем', 'bark', 'zzzzqq', 'птички']
out['search'] = {}
for q in queries:
    res, best = mif_core.find_matching_mifs(q)
    out['search'][q] = [m['id'] for m in res]
res, best = mif_core.find_matching_mifs('')
out['empty_query'] = [m['id'] for m in res]
out['count'] = len(mif_core.MIFS_DATABASE)
first = mif_core.MIFS_DATABASE[0]
out['get_by_id'] = mif_core.get_mif_by_id(first['id'])['title']
out['get_missing'] = mif_core.get_mif_by_id(99999)
out['dup_hash'] = mif_core.find_duplicate_by_hash(first['content_hash'])['id']
out['dup_title_case'] = mif_core.find_duplicate_by_title(first['title'].upper() if first['title'].isascii() else first['title'].capitalize())
out['dup_title_exact'] = mif_core.find_duplicate_by_title(first['title'])['id']
out['dup_missing'] = mif_core.find_duplicate_by_title('такого звуку нема')
ids = [m['id'] for m in mif_core.MIFS_DATABASE]
U1, U2, U3 = 777001, 777002, 777003
for i in ids[:12]:
    db_manager.add_to_favorites(U1, i)
out['fav_ids_u1'] = [m['id'] for m in db_manager.get_favorites(U1)]
out['fav_set_u1'] = sorted(db_manager.get_favorite_ids(U1), key=int)
db_manager.add_to_favorites(U1, ids[5])
out['fav_after_bump'] = [m['id'] for m in db_manager.get_favorites(U1)][:2]
out['removed'] = db_manager.remove_from_favorites(U1, ids[5])
out['removed_again'] = db_manager.remove_from_favorites(U1, ids[5])
for uid in (U1, U2):
    db_manager.record_usage(uid, ids[2])
db_manager.record_usage(U1, ids[2])
db_manager.record_usage(U1, ids[3])
out['popular'] = [m['id'] for m in db_manager.get_popular_sounds(5)]
out['menu_u1'] = [m['id'] for m in db_manager.get_personal_menu(U1)]
db_manager.clear_history(U1)
out['menu_after_clear'] = [m['id'] for m in db_manager.get_personal_menu(U1)]
out['popular_after_clear'] = [m['id'] for m in db_manager.get_popular_sounds(5)]
out['menu_new_user'] = [m['id'] for m in db_manager.get_personal_menu(424242)]
out['lang_first'] = db_manager.ensure_user_language(U3, 'uk')
db_manager.set_user_language(U3, 'ru')
out['lang_manual_kept'] = db_manager.ensure_user_language(U3, 'en')
out['lang_get'] = db_manager.get_user_language(U3)
out['lang_unknown_user'] = db_manager.get_user_language(31337)
mif_core.mute_user(U1)
out['muted'] = mif_core.is_muted(U1)
mif_core.unmute_user(U1)
out['unmuted'] = mif_core.is_muted(U1)
print(json.dumps(out, ensure_ascii=False))
"""


def run_scenario(project: Path, tag: str) -> dict:
    work = make_copy(project)
    extra = {"MIFKI_DB_PATH": str(work / f"{tag}.db")}
    proc = run_py(SCENARIO, work, extra)
    if proc.returncode != 0:
        print(proc.stderr[-1500:])
        raise SystemExit(f"сценарій {tag} впав")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    shutil.rmtree(work)
    return data


def test_parity() -> None:
    old, new = run_scenario(OLD, "old"), run_scenario(ROOT, "new")
    keys = list(old)
    # Звуки з однаковим рейтингом у топі можуть стояти в різному порядку
    # (старий код брав порядок вставки в словник, новий — за id): порівнюємо
    # їх як множину з однаковим першим елементом.
    tie_keys = {"popular", "popular_after_clear", "menu_new_user"}
    same = [k for k in keys if old[k] == new[k] or (k in tie_keys and old[k][0] == new[k][0]
                                                    and set(old[k]) == set(new[k]))]
    diff = [k for k in keys if k not in same]
    for k in diff:
        print("   відмінність:", k, "\n      старе:", old[k], "\n      нове :", new[k])
    record("T2 паритет пошуку та функцій db_manager/mif_core (старе JSON vs нове SQLite)",
           not diff, f"збіг {len(same)} з {len(keys)} показників; пошукових запитів {len(old['search'])}")
    s_ok = all(old["search"][q] == new["search"][q] for q in old["search"])
    record("T2a однаковий порядок результатів пошуку", s_ok,
           f"{sum(old['search'][q] == new['search'][q] for q in old['search'])}/{len(old['search'])} запитів")
    record("T2b обране: ліміт 10 та порядок «нові першими»",
           new["fav_ids_u1"] == old["fav_ids_u1"] and len(new["fav_ids_u1"]) == 10, f"{new['fav_ids_u1'][:4]}…")
    record("T2c мова: ручний вибір не перезаписується",
           new["lang_manual_kept"] == "ru" and new["lang_first"] == "uk", f"{new['lang_first']} → {new['lang_manual_kept']}")
    record("T2d очищення історії не зменшує рейтинг",
           new["popular_after_clear"] == new["popular"], f"топ до/після: {new['popular'][:2]}/{new['popular_after_clear'][:2]}")


# ------------------------------------------- 3. два процеси (реальні модулі)
WORKER = r"""
import sys, db_manager
base, n = int(sys.argv[1]), int(sys.argv[2])
for i in range(n):
    db_manager.ensure_user_language(base + i, 'uk')
"""


def count_users_after_two_workers(project: Path, tag: str, n: int = 60) -> int:
    work = make_copy(project)
    env = {**os.environ, "PYTHONPATH": f"{work}{os.pathsep}{STUBS}", "MIFKI_DB_PATH": str(work / f"{tag}.db"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(base), str(n)], cwd=work, env=env)
             for base in (1000, 2000)]
    [p.wait() for p in procs]
    if (work / "users_database.json").exists() and tag == "old":
        count = len(json.loads((work / "users_database.json").read_text(encoding="utf-8")))
        shutil.rmtree(work)
        return count
    out = run_py("import database; con = database.get_connection(); "
                 "print(con.execute('SELECT COUNT(*) FROM users WHERE user_id BETWEEN 1000 AND 2999').fetchone()[0])",
                 work, {"MIFKI_DB_PATH": str(work / f"{tag}.db")})
    shutil.rmtree(work)
    return int(out.stdout.strip())


def test_two_processes() -> None:
    results = {"old": [], "new": []}
    for run in range(3):
        results["old"].append(count_users_after_two_workers(OLD, "old") - 1)  # мінус 1 реальний користувач
        results["new"].append(count_users_after_two_workers(ROOT, "new"))
    record("T3 два процеси створюють по 60 користувачів (очікується 120)",
           all(x == 120 for x in results["new"]) and all(x < 120 for x in results["old"]),
           f"JSON (старе): {results['old']}; SQLite (нове): {results['new']}")


# ------------------------------------------- 4. актуальність між процесами
def test_cross_process_visibility() -> None:
    new_work = make_copy(ROOT)
    env_new = {"MIFKI_DB_PATH": str(new_work / "vis.db")}
    reader = ("import subprocess, sys, json, mif_core\n"
              "before = len(mif_core.MIFS_DATABASE)\n"
              "subprocess.run([sys.executable, '-c', \"import database; database.insert_sound(title='новий звук', file_id='F', "
              "file_type='voice', bot_description='', content_hash='h-new', channel_message_id=1, source_url=None, "
              "tags_text='новий звук')\"], check=True)\n"
              "print(json.dumps([before, len(mif_core.MIFS_DATABASE)]))")
    out = run_py(reader, new_work, env_new)
    new_before, new_after = json.loads(out.stdout.strip().splitlines()[-1])
    old_work = make_copy(OLD)
    reader_old = ("import subprocess, sys, json, mif_core\n"
                  "before = len(mif_core.MIFS_DATABASE)\n"
                  "subprocess.run([sys.executable, '-c', \"import mif_core as m; m.MIFS_DATABASE.append({'id': '999', 'title': 'x', "
                  "'file_id': 'F', 'content_hash': 'h-new', 'user_tags': 'x', 'bot_tags': ''}); m.save_mifs()\"], check=True)\n"
                  "print(json.dumps([before, len(mif_core.MIFS_DATABASE)]))")
    out = run_py(reader_old, old_work)
    old_before, old_after = json.loads(out.stdout.strip().splitlines()[-1])
    record("T4 Mini App одразу бачить звук, доданий ботом (інший процес)",
           new_after == new_before + 1 and old_after == old_before,
           f"SQLite: {new_before} → {new_after}; JSON (старе): {old_before} → {old_after}")
    shutil.rmtree(new_work); shutil.rmtree(old_work)


# ----------------------------------------- 5. одночасний старт і міграція
def test_concurrent_startup() -> None:
    work = make_copy(ROOT)
    env = {**os.environ, "PYTHONPATH": f"{work}{os.pathsep}{STUBS}", "MIFKI_DB_PATH": str(work / "race.db"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    procs = [subprocess.Popen([sys.executable, "-c", "import database; database.get_connection()"],
                              cwd=work, env=env, stderr=subprocess.PIPE) for _ in range(5)]
    errors = [p.communicate()[1].decode()[-200:] for p in procs]
    out = run_py("import database; c = database.get_connection(); "
                 "print(c.execute('SELECT COUNT(*) FROM sounds').fetchone()[0])", work, {"MIFKI_DB_PATH": str(work / "race.db")})
    n_json = len(json.loads((work / "mifs_database.json").read_text(encoding="utf-8")))
    record("T5 п'ять процесів стартують одночасно — міграція виконується один раз",
           int(out.stdout.strip()) == n_json and not any(errors), f"у БД {out.stdout.strip()} звуків, у JSON {n_json}")
    shutil.rmtree(work)


# ------------------------------- 6. публікація звуку (publish_voice_mif)
PUBLISH = r"""
import asyncio, json, types, mif_core
class FakeBot:
    def __init__(self): self.mid = 500
    async def send_voice(self, **kw):
        self.mid += 1
        return types.SimpleNamespace(message_id=self.mid, voice=types.SimpleNamespace(file_id=f'FILE{self.mid}'))
    async def edit_message_caption(self, **kw): return None
async def main():
    bot = FakeBot()
    before = len(mif_core.MIFS_DATABASE)
    kw = dict(ogg_bytes=b'x', existing_voice_file_id=None, base_caption='c', title='Тестовий мем',
              tags_text='Тестовий мем test', bot_description='привіт', content_hash='hash-publish-1')
    s1, m1 = await mif_core.publish_voice_mif(bot, **kw)
    s2, m2 = await mif_core.publish_voice_mif(bot, **kw)
    found = mif_core.find_matching_mifs('тестовий')[0]
    print(json.dumps({'s1': s1, 's2': s2, 'same_id': m1['id'] == m2['id'], 'before': before,
                      'after': len(mif_core.MIFS_DATABASE), 'found': [m['id'] for m in found][:1] == [m1['id']],
                      'tags': m1['user_tags'], 'channel_message_id': m1['channel_message_id']}, ensure_ascii=False))
asyncio.run(main())
"""


def test_publish() -> None:
    work = make_copy(ROOT)
    out = run_py(PUBLISH, work, {"MIFKI_DB_PATH": str(work / "pub.db")})
    if out.returncode != 0:
        print(out.stderr[-1500:])
    r = json.loads(out.stdout.strip().splitlines()[-1])
    ok = (r["s1"] == "added" and r["s2"] == "duplicate" and r["same_id"] and r["after"] == r["before"] + 1 and r["found"])
    record("T6 publish_voice_mif: додавання, відхилення дубліката за хешем, пошук нового звуку", ok,
           f"{r['s1']}, потім {r['s2']}; рядків {r['before']} → {r['after']}; теги «{r['tags']}»")
    shutil.rmtree(work)


if __name__ == "__main__":
    if not OLD.exists():
        raise SystemExit(f"не знайдено старий проєкт: {OLD}")
    for fn in (test_migration, test_parity, test_two_processes, test_cross_process_visibility,
               test_concurrent_startup, test_publish):
        fn()
    Path(ROOT / "tests" / "test_results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\nПідсумок: {len(RESULTS) - len(failed)} з {len(RESULTS)} перевірок пройдено")
    sys.exit(1 if failed else 0)
