"""
Ljubičasto — бэкенд календаря репетитора.
Один процесс: Telegram-бот (webhook) + HTTP API для Mini App + уведомления.

Деплоится на Bothost из GitHub. Перед деплоем заполни блок CONFIG ниже.
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from contextlib import closing

import pytz
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import (Message, InlineKeyboardMarkup,
                           InlineKeyboardButton, WebAppInfo, FSInputFile, BotCommand)
from aiogram.filters import CommandStart, Command
from aiogram.utils.web_app import safe_parse_webapp_init_data
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======================= CONFIG =======================
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
MINIAPP_URL = "https://openopq.github.io/ljubicasto/?v=16"
ALLOWED_IDS = [7653945813, 6571313515]
DEV_ID      = 7653945813          # только мне: бэкапы, статус, меню разработчика
NOTIFY_HOUR = 8
NOTIFY_MIN  = 0
TZ          = "Europe/Moscow"
DB_PATH     = os.environ.get("DATA_DIR", "/app/data") + "/ljubicasto.db"
PORT        = int(os.environ.get("PORT", 3000))
# ======================================================

logging.basicConfig(level=logging.INFO)
tz = pytz.timezone(TZ)
bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

# ---------------------- DB ----------------------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with closing(db()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS lessons(
            id TEXT PRIMARY KEY, date TEXT, time TEXT,
            name TEXT, phone TEXT, tg TEXT, price TEXT, level TEXT, about TEXT,
            paid INTEGER DEFAULT 0, groupId TEXT,
            contacts TEXT, note TEXT, trial INTEGER DEFAULT 0,
            studentId TEXT, color TEXT)""")
        for col, decl in [("contacts","TEXT"), ("note","TEXT"),
                          ("trial","INTEGER DEFAULT 0"), ("studentId","TEXT"),
                          ("color","TEXT")]:
            try: c.execute(f"ALTER TABLE lessons ADD COLUMN {col} {decl}")
            except Exception: pass
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            chat_id INTEGER PRIMARY KEY, notify INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS students(
            id TEXT PRIMARY KEY, name TEXT, phone TEXT, contacts TEXT,
            price TEXT, duration TEXT, level TEXT, about TEXT,
            color TEXT, trialUsed INTEGER DEFAULT 0,
            created INTEGER, archived INTEGER DEFAULT 0)""")
        try: c.execute("ALTER TABLE students ADD COLUMN archived INTEGER DEFAULT 0")
        except Exception: pass
        # ---- конструктор заданий ----
        c.execute("""CREATE TABLE IF NOT EXISTS task_levels(
            id TEXT PRIMARY KEY, name TEXT, sort INTEGER DEFAULT 0, created INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS task_groups(
            id TEXT PRIMARY KEY, name TEXT, level_id TEXT, sort INTEGER DEFAULT 0, created INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, type TEXT, word TEXT, translation TEXT,
            choices TEXT, correct INTEGER DEFAULT 0,
            audio_file_id TEXT, group_id TEXT, level_id TEXT, created INTEGER)""")
        c.commit()

# ---------------------- DB helpers ----------------------
def day_lessons(date):
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM lessons WHERE date=? ORDER BY time", (date,)).fetchall()
        return [dict(r) for r in rows]

def replace_day(date, lessons):
    with closing(db()) as c:
        c.execute("DELETE FROM lessons WHERE date=?", (date,))
        for L in lessons:
            c.execute("""INSERT OR REPLACE INTO lessons
                (id,date,time,name,phone,tg,price,level,about,paid,groupId,
                 contacts,note,trial,studentId,color)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (L.get("id"), date, L.get("time",""), L.get("name",""),
                 L.get("phone",""), L.get("tg",""), L.get("price",""),
                 L.get("level",""), L.get("about",""),
                 1 if L.get("paid") else 0, L.get("groupId"),
                 L.get("contacts",""), L.get("note",""),
                 1 if L.get("trial") else 0,
                 L.get("studentId"), L.get("color","")))
        c.commit()

def move_lesson(lesson_id, old_date, new_date, new_time):
    """Атомарный перенос урока: всё в одной транзакции."""
    with closing(db()) as c:
        row = c.execute("SELECT * FROM lessons WHERE id=? AND date=?",
                        (lesson_id, old_date)).fetchone()
        if not row:
            return False
        L = dict(row)
        c.execute("DELETE FROM lessons WHERE id=? AND date=?", (lesson_id, old_date))
        L["date"] = new_date
        L["time"] = new_time
        c.execute("""INSERT OR REPLACE INTO lessons
            (id,date,time,name,phone,tg,price,level,about,paid,groupId,
             contacts,note,trial,studentId,color)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (L["id"], L["date"], L["time"], L.get("name",""),
             L.get("phone",""), L.get("tg",""), L.get("price",""),
             L.get("level",""), L.get("about",""),
             L.get("paid",0), L.get("groupId"),
             L.get("contacts",""), L.get("note",""),
             L.get("trial",0), L.get("studentId"), L.get("color","")))
        c.commit()
        return True

def all_day_keys():
    with closing(db()) as c:
        rows = c.execute("SELECT DISTINCT date FROM lessons").fetchall()
        keys = []
        for r in rows:
            y, m, d = r["date"].split("-")
            keys.append(f"d_{y}_{m}_{d}")
        return keys

def list_students():
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM students ORDER BY created").fetchall()
        return [dict(r) for r in rows]

def save_student(s):
    with closing(db()) as c:
        c.execute("""INSERT OR REPLACE INTO students
            (id,name,phone,contacts,price,duration,level,about,color,
             trialUsed,created,archived)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s.get("id"), s.get("name",""), s.get("phone",""),
             s.get("contacts",""), s.get("price",""), s.get("duration",""),
             s.get("level",""), s.get("about",""), s.get("color",""),
             1 if s.get("trialUsed") else 0, s.get("created") or 0,
             1 if s.get("archived") else 0))
        c.commit()

def delete_student(sid):
    with closing(db()) as c:
        c.execute("UPDATE students SET archived=1 WHERE id=?", (sid,))
        c.commit()

def delete_student_forever(sid):
    with closing(db()) as c:
        c.execute("DELETE FROM students WHERE id=?", (sid,))
        c.commit()

# ---- конструктор заданий ----
def list_levels():
    with closing(db()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM task_levels ORDER BY sort,created").fetchall()]

def save_level(lv):
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO task_levels(id,name,sort,created) VALUES(?,?,?,?)",
                  (lv["id"], lv["name"], lv.get("sort",0), lv.get("created",0)))
        c.commit()

def delete_level(lid):
    with closing(db()) as c:
        c.execute("DELETE FROM task_levels WHERE id=?", (lid,))
        # группы и задания этого уровня тоже удаляем
        gids=[r["id"] for r in c.execute("SELECT id FROM task_groups WHERE level_id=?", (lid,)).fetchall()]
        c.execute("DELETE FROM task_groups WHERE level_id=?", (lid,))
        for gid in gids:
            c.execute("DELETE FROM tasks WHERE group_id=?", (gid,))
        c.execute("DELETE FROM tasks WHERE level_id=? AND group_id IS NULL", (lid,))
        c.commit()

def list_groups(level_id=None):
    with closing(db()) as c:
        if level_id:
            rows=c.execute("SELECT * FROM task_groups WHERE level_id=? ORDER BY sort,created",(level_id,)).fetchall()
        else:
            rows=c.execute("SELECT * FROM task_groups ORDER BY sort,created").fetchall()
        return [dict(r) for r in rows]

def save_group(g):
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO task_groups(id,name,level_id,sort,created) VALUES(?,?,?,?,?)",
                  (g["id"], g["name"], g.get("level_id",""), g.get("sort",0), g.get("created",0)))
        c.commit()

def count_tasks_in_group(gid):
    with closing(db()) as c:
        return c.execute("SELECT COUNT(*) FROM tasks WHERE group_id=?", (gid,)).fetchone()[0]

def delete_group(gid, force=False):
    with closing(db()) as c:
        count = c.execute("SELECT COUNT(*) FROM tasks WHERE group_id=?", (gid,)).fetchone()[0]
        if count > 0 and not force:
            return {"ok": False, "count": count}
        c.execute("DELETE FROM tasks WHERE group_id=?", (gid,))
        c.execute("DELETE FROM task_groups WHERE id=?", (gid,))
        c.commit()
        return {"ok": True}

def list_tasks(group_id=None, level_id=None):
    with closing(db()) as c:
        if group_id:
            rows=c.execute("SELECT * FROM tasks WHERE group_id=? ORDER BY created",(group_id,)).fetchall()
        elif level_id:
            rows=c.execute("SELECT * FROM tasks WHERE level_id=? ORDER BY created",(level_id,)).fetchall()
        else:
            rows=c.execute("SELECT * FROM tasks ORDER BY created").fetchall()
        return [dict(r) for r in rows]

def save_task(t):
    with closing(db()) as c:
        c.execute("""INSERT OR REPLACE INTO tasks
            (id,type,word,translation,choices,correct,audio_file_id,group_id,level_id,created)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (t["id"], t.get("type","choice"), t.get("word",""), t.get("translation",""),
             t.get("choices",""), t.get("correct",0), t.get("audio_file_id",""),
             t.get("group_id",""), t.get("level_id",""), t.get("created",0)))
        c.commit()

def move_task_group(task_id, new_group_id):
    with closing(db()) as c:
        # level_id берём из новой группы
        row=c.execute("SELECT level_id FROM task_groups WHERE id=?", (new_group_id,)).fetchone()
        new_level=row["level_id"] if row else ""
        c.execute("UPDATE tasks SET group_id=?, level_id=? WHERE id=?",
                  (new_group_id, new_level, task_id))
        c.commit()

def delete_task(tid):
    with closing(db()) as c:
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        c.commit()

def register_user(chat_id):
    with closing(db()) as c:
        c.execute("INSERT OR IGNORE INTO users(chat_id,notify) VALUES(?,1)", (chat_id,))
        c.commit()

def notify_users():
    with closing(db()) as c:
        return [r["chat_id"] for r in
                c.execute("SELECT chat_id FROM users WHERE notify=1").fetchall()]

def get_setting(k, default=""):
    with closing(db()) as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return row["value"] if row else default

def set_setting(k, v):
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))
        c.commit()

def get_history_mode():
    return get_setting("history_mode", "0") == "1"

def set_history_mode(on: bool):
    set_setting("history_mode", "1" if on else "0")

def db_stats():
    with closing(db()) as c:
        students = c.execute("SELECT COUNT(*) FROM students WHERE archived=0").fetchone()[0]
        lessons  = c.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        markers  = c.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'reminded_%'").fetchone()[0]
    size_kb = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0
    return students, lessons, markers, size_kb

def clean_old_markers():
    """Удаляем маркеры напоминаний для уроков, дата которых уже прошла (>2 дней назад)."""
    cutoff = (datetime.now(tz) - timedelta(days=2)).strftime("%Y-%m-%d")
    with closing(db()) as c:
        # получаем все маркеры
        rows = c.execute(
            "SELECT key FROM settings WHERE key LIKE 'reminded_%'").fetchall()
        ids_to_check = [r["key"].replace("reminded_", "") for r in rows]
        stale = []
        for lid in ids_to_check:
            row = c.execute(
                "SELECT date FROM lessons WHERE id=?", (lid,)).fetchone()
            # если урока нет совсем или его дата раньше cutoff — маркер лишний
            if not row or row["date"] < cutoff:
                stale.append("reminded_" + lid)
        if stale:
            c.executemany("DELETE FROM settings WHERE key=?", [(k,) for k in stale])
            c.commit()
            logging.info("clean_markers: удалено %d маркеров", len(stale))

# ---------------------- auth ----------------------
def check_init(request):
    init = request.headers.get("X-Init-Data", "")
    if not init:
        return None
    try:
        data = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=init)
    except Exception:
        return None
    uid = data.user.id if data.user else None
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        return None
    return uid

# ---------------------- API ----------------------
@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

async def api_day(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    date = request.query.get("d", "")
    return web.json_response({"lessons": day_lessons(date)})

async def api_save_day(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    replace_day(body["d"], body.get("lessons", []))
    return web.json_response({"ok": True})

async def api_move_lesson(request):
    """Атомарный перенос урока — одна транзакция, нет риска потери данных."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    ok = move_lesson(
        body.get("id"), body.get("old_date"),
        body.get("new_date"), body.get("new_time","")
    )
    if not ok:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})

async def api_keys(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    return web.json_response({"keys": all_day_keys()})

async def api_students(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    return web.json_response({"students": list_students()})

async def api_save_student(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    try:
        save_student(body)
    except Exception as e:
        logging.exception("save_student failed")
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True})

async def api_delete_student(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    delete_student(body.get("id"))
    return web.json_response({"ok": True})

async def api_delete_student_forever(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    delete_student_forever(body.get("id"))
    return web.json_response({"ok": True})

async def api_history(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    return web.json_response({"history": get_history_mode()})

# ---- конструктор заданий ----
async def api_levels(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    if request.method=="POST":
        save_level(await request.json()); return web.json_response({"ok":True})
    return web.json_response({"levels": list_levels()})

async def api_delete_level(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    body=await request.json(); delete_level(body["id"])
    return web.json_response({"ok":True})

async def api_groups(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    if request.method=="POST":
        save_group(await request.json()); return web.json_response({"ok":True})
    level_id=request.query.get("level_id")
    return web.json_response({"groups": list_groups(level_id)})

async def api_delete_group(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    body=await request.json()
    result=delete_group(body["id"], force=body.get("force",False))
    return web.json_response(result)

async def api_tasks(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    if request.method=="POST":
        save_task(await request.json()); return web.json_response({"ok":True})
    group_id=request.query.get("group_id"); level_id=request.query.get("level_id")
    return web.json_response({"tasks": list_tasks(group_id, level_id)})

async def api_delete_task(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    body=await request.json(); delete_task(body["id"])
    return web.json_response({"ok":True})

async def api_move_task(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    body=await request.json(); move_task_group(body["task_id"], body["group_id"])
    return web.json_response({"ok":True})

async def api_audio(request):
    """Возвращает прямую ссылку на аудио-файл по file_id."""
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    file_id=request.query.get("file_id","")
    if not file_id: return web.json_response({"error":"no file_id"},status=400)
    try:
        f=await bot.get_file(file_id)
        url=f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
        return web.json_response({"url":url})
    except Exception as e:
        return web.json_response({"error":str(e)},status=500)

async def health(request):
    return web.Response(text="Ljubičasto OK")

# ---------------------- bot handlers ----------------------
def dev_menu_text():
    hist = "включён 📖" if get_history_mode() else "выключён 📕"
    return (
        f"\n\n🛠 Панель разработчика:\n"
        f"/history — режим истории ({hist})\n"
        f"/backup — скачать бэкап базы\n"
        f"/status — статистика и состояние базы\n"
        f"/resetwebhook — переустановить вебхук\n"
        f"/tasks — конструктор заданий"
    )

@dp.message(CommandStart())
async def start(m: Message):
    if ALLOWED_IDS and m.from_user.id not in ALLOWED_IDS:
        await m.answer("Этот календарь только для преподавателя.")
        return
    register_user(m.chat.id)
    if m.from_user.id == DEV_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📅 Расписание",
                                 web_app=WebAppInfo(url=MINIAPP_URL)),
            InlineKeyboardButton(text="📚 Задания",
                                 web_app=WebAppInfo(url=TASKS_URL)),
        ]])
        await m.answer(
            "Љубичица 🌸 — запущена." + dev_menu_text(),
            reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📅 Открыть расписание",
                                 web_app=WebAppInfo(url=MINIAPP_URL)),
            InlineKeyboardButton(text="📚 Задания",
                                 web_app=WebAppInfo(url=TASKS_URL)),
        ]])
        await m.answer(
            "Привет, Миляна! Я Љубичица 🌸\n"
            "Буду присылать расписание по утрам. "
            "Открыть календарь или конструктор заданий — кнопками ниже.",
            reply_markup=kb)

@dp.message(Command("tasks"))
async def cmd_tasks(m: Message):
    if ALLOWED_IDS and m.from_user.id not in ALLOWED_IDS:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📚 Открыть конструктор",
                             web_app=WebAppInfo(url=TASKS_URL))
    ]])
    await m.answer("Конструктор заданий:", reply_markup=kb)

@dp.message(Command("status"))
async def cmd_status(m: Message):
    if m.from_user.id != DEV_ID:
        return
    students, lessons, markers, size_kb = db_stats()
    hist = "включён 📖" if get_history_mode() else "выключён 📕"
    await m.answer(
        f"📊 Статус Ljubičasto\n\n"
        f"👥 Учеников: {students}\n"
        f"📅 Занятий в базе: {lessons}\n"
        f"🔔 Маркеров напоминаний: {markers}\n"
        f"💾 Размер базы: {size_kb} КБ\n"
        f"📖 Режим истории: {hist}")

@dp.message(Command("resetwebhook"))
async def cmd_resetwebhook(m: Message):
    if m.from_user.id != DEV_ID:
        return
    try:
        await bot.set_webhook(PUBLIC_URL + WEBHOOK_PATH, drop_pending_updates=False)
        await m.answer("✅ Вебхук переустановлен.")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@dp.message(lambda m: m.voice or m.audio)
async def handle_audio(m: Message):
    """Принимает голосовое или аудио и возвращает file_id для конструктора заданий."""
    if ALLOWED_IDS and m.from_user.id not in ALLOWED_IDS:
        return
    file_id = m.voice.file_id if m.voice else m.audio.file_id
    await m.answer(
        f"✅ Аудио получено!\n\n"
        f"<code>{file_id}</code>\n\n"
        f"Скопируй этот file_id и вставь в поле «Аудио файл» в конструкторе заданий.",
        parse_mode="HTML"
    )

@dp.message(Command("backup"))
async def cmd_backup(m: Message):
    if m.from_user.id != DEV_ID:
        return
    try:
        await m.answer_document(
            FSInputFile(DB_PATH, filename="ljubicasto.db"),
            caption=f"💾 Бэкап — {datetime.now(tz).strftime('%d.%m.%Y %H:%M')}")
    except Exception as e:
        await m.answer(f"Не удалось отправить файл: {e}")

@dp.message(Command("history"))
async def cmd_history(m: Message):
    if m.from_user.id != DEV_ID:
        return
    cur = get_history_mode()
    set_history_mode(not cur)
    if not cur:
        await m.answer("📖 Режим истории включён.\nТеперь можно назначать уроки в прошлое и помечать пробным любой урок.")
    else:
        await m.answer("📕 Режим истории выключён.\nНазначать уроки в прошлое больше нельзя.")

# ---------------------- hour-before reminder ----------------------
async def hour_reminder():
    now   = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    for L in day_lessons(today):
        t = L.get("time", "")
        if not t or ":" not in t:
            continue
        try:
            hh, mm = map(int, t.split(":")[:2])
        except Exception:
            continue
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        diff  = (start - now).total_seconds() / 60.0
        if 55 <= diff <= 65:
            marker = f"reminded_{L['id']}"
            if get_setting(marker, "0") == "1":
                continue
            txt = f"⏰ Через час урок — {L.get('name') or 'занятие'} в {t}."
            for chat_id in notify_users():
                try:
                    await bot.send_message(chat_id, txt)
                except Exception as e:
                    logging.warning("reminder fail %s: %s", chat_id, e)
            set_setting(marker, "1")

# ---------------------- morning notifications ----------------------
async def morning():
    today   = datetime.now(tz).strftime("%Y-%m-%d")
    lessons = day_lessons(today)
    if not lessons:
        return
    lines = [f"• {L['time']} — {L['name'] or 'занятие'}"
             + (f" ({L['price']} ₽)" if L.get("price") else "")
             for L in lessons]
    text = f"Доброе утро! Сегодня занятий: {len(lessons)}\n\n" + "\n".join(lines)
    sep  = "&" if "?" in MINIAPP_URL else "?"
    kb   = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть расписание",
                             web_app=WebAppInfo(url=f"{MINIAPP_URL}{sep}d={today}"))
    ]])
    for chat_id in notify_users():
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:
            logging.warning("notify fail %s: %s", chat_id, e)

# ---------------------- auto backup (раз в 3 дня) ----------------------
async def auto_backup():
    try:
        await bot.send_document(
            DEV_ID,
            FSInputFile(DB_PATH, filename="ljubicasto.db"),
            caption=f"💾 Авто-бэкап — {datetime.now(tz).strftime('%d.%m.%Y %H:%M')}")
    except Exception as e:
        logging.warning("auto_backup fail: %s", e)

# ---------------------- startup ----------------------
PUBLIC_URL   = "https://bot-1781087941-4553-ruserb.bothost.tech"
WEBHOOK_PATH = "/webhook"

TASKS_URL = "https://openopq.github.io/ljubicasto/tasks.html"

async def on_startup(app):
    init_db()
    await bot.set_webhook(PUBLIC_URL + WEBHOOK_PATH, drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start",        description="Открыть расписание / панель управления"),
        BotCommand(command="tasks",        description="Конструктор заданий"),
        BotCommand(command="backup",       description="Скачать бэкап базы"),
        BotCommand(command="history",      description="Включить/выключить режим истории"),
        BotCommand(command="status",       description="Статистика базы"),
        BotCommand(command="resetwebhook", description="Переустановить вебхук"),
    ])
    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(morning,       "cron", hour=NOTIFY_HOUR, minute=NOTIFY_MIN)
    sched.add_job(hour_reminder, "cron", minute="*/10")
    sched.add_job(auto_backup,   "interval", days=3)
    sched.add_job(clean_old_markers, "cron", hour=3, minute=0)
    sched.start()
    logging.info("started, webhook -> %s", PUBLIC_URL + WEBHOOK_PATH)

def main():
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    app = web.Application(middlewares=[cors])
    app.router.add_get("/",                      health)
    app.router.add_get("/api/day",               api_day)
    app.router.add_post("/api/day",              api_save_day)
    app.router.add_post("/api/move_lesson",      api_move_lesson)
    app.router.add_get("/api/keys",              api_keys)
    app.router.add_get("/api/students",          api_students)
    app.router.add_post("/api/students",         api_save_student)
    app.router.add_post("/api/students/delete",         api_delete_student)
    app.router.add_post("/api/students/delete-forever",  api_delete_student_forever)
    app.router.add_get("/api/history",           api_history)
    # конструктор заданий
    app.router.add_get("/api/levels",            api_levels)
    app.router.add_post("/api/levels",           api_levels)
    app.router.add_post("/api/levels/delete",    api_delete_level)
    app.router.add_get("/api/groups",            api_groups)
    app.router.add_post("/api/groups",           api_groups)
    app.router.add_post("/api/groups/delete",    api_delete_group)
    app.router.add_get("/api/tasks",             api_tasks)
    app.router.add_post("/api/tasks",            api_tasks)
    app.router.add_post("/api/tasks/delete",     api_delete_task)
    app.router.add_post("/api/tasks/move",       api_move_task)
    app.router.add_get("/api/audio",             api_audio)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda r: web.Response())
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
