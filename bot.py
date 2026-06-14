"""
Ljubičasto — бэкенд календаря репетитора.
Один процесс: Telegram-бот (webhook) + HTTP API для Mini App + утренние уведомления.

Деплоится на Bothost из GitHub. Перед деплоем заполни блок CONFIG ниже.
"""

import asyncio
import os
import json
import sqlite3
import logging
from datetime import datetime
from contextlib import closing

import pytz
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, FSInputFile, BotCommand
from aiogram.filters import CommandStart, Command
from aiogram.utils.web_app import safe_parse_webapp_init_data
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======================= CONFIG =======================
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
MINIAPP_URL = "https://openopq.github.io/ljubicasto/?v=4"
ALLOWED_IDS = [7653945813, 6571313515]
NOTIFY_HOUR = 8
NOTIFY_MIN  = 0
TZ          = "Europe/Moscow"
DB_PATH     = os.environ.get("DATA_DIR", "/app/data") + "/ljubicasto.db"
PORT        = int(os.environ.get("PORT", 3000))
# ======================================================

logging.basicConfig(level=logging.INFO)
tz = pytz.timezone(TZ)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

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
            contacts TEXT, note TEXT, trial INTEGER DEFAULT 0, studentId TEXT, color TEXT)""")
        # миграция: добавить недостающие колонки в существующую базу
        for col,decl in [("contacts","TEXT"),("note","TEXT"),("trial","INTEGER DEFAULT 0"),
                         ("studentId","TEXT"),("color","TEXT")]:
            try: c.execute(f"ALTER TABLE lessons ADD COLUMN {col} {decl}")
            except Exception: pass
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            chat_id INTEGER PRIMARY KEY, notify INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS students(
            id TEXT PRIMARY KEY, name TEXT, phone TEXT, contacts TEXT,
            price TEXT, duration TEXT, level TEXT, about TEXT,
            color TEXT, trialUsed INTEGER DEFAULT 0, created INTEGER)""")
        c.commit()

def day_lessons(date):
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM lessons WHERE date=? ORDER BY time", (date,)).fetchall()
        return [dict(r) for r in rows]

def replace_day(date, lessons):
    with closing(db()) as c:
        c.execute("DELETE FROM lessons WHERE date=?", (date,))
        for L in lessons:
            c.execute("""INSERT OR REPLACE INTO lessons
                (id,date,time,name,phone,tg,price,level,about,paid,groupId,contacts,note,trial,studentId,color)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (L.get("id"), date, L.get("time",""), L.get("name",""), L.get("phone",""),
                 L.get("tg",""), L.get("price",""), L.get("level",""), L.get("about",""),
                 1 if L.get("paid") else 0, L.get("groupId"),
                 L.get("contacts",""), L.get("note",""), 1 if L.get("trial") else 0,
                 L.get("studentId"), L.get("color","")))
        c.commit()

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
            (id,name,phone,contacts,price,duration,level,about,color,trialUsed,created)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (s.get("id"), s.get("name",""), s.get("phone",""), s.get("contacts",""),
             s.get("price",""), s.get("duration",""), s.get("level",""), s.get("about",""),
             s.get("color",""), 1 if s.get("trialUsed") else 0, s.get("created") or 0))
        c.commit()

def delete_student(sid):
    with closing(db()) as c:
        c.execute("DELETE FROM students WHERE id=?", (sid,))
        c.commit()

def register_user(chat_id):
    with closing(db()) as c:
        c.execute("INSERT OR IGNORE INTO users(chat_id,notify) VALUES(?,1)", (chat_id,))
        c.commit()

def notify_users():
    with closing(db()) as c:
        return [r["chat_id"] for r in c.execute("SELECT chat_id FROM users WHERE notify=1").fetchall()]

# ---------------------- auth ----------------------
def check_init(request):
    """Проверяет подпись initData из заголовка и возвращает Telegram-id, либо None."""
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
    resp.headers["Access-Control-Allow-Origin"] = "*"
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
    save_student(body)
    return web.json_response({"ok": True})

async def api_delete_student(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    delete_student(body.get("id"))
    return web.json_response({"ok": True})

async def health(request):
    return web.Response(text="Ljubičasto OK")

# ---------------------- bot handlers ----------------------
@dp.message(CommandStart())
async def start(m: Message):
    if ALLOWED_IDS and m.from_user.id not in ALLOWED_IDS:
        await m.answer("Этот календарь только для преподавателя.")
        return
    register_user(m.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть расписание", web_app=WebAppInfo(url=MINIAPP_URL))
    ]])
    await m.answer(
        "Привет, Миляна! Я Љубичица 🌸\nБуду присылать расписание по утрам. "
        "Открыть календарь — кнопкой ниже или через меню.",
        reply_markup=kb)

@dp.message(Command("backup"))
async def backup(m: Message):
    if ALLOWED_IDS and m.from_user.id not in ALLOWED_IDS:
        return
    try:
        await m.answer_document(
            FSInputFile(DB_PATH, filename="ljubicasto.db"),
            caption=f"Бэкап базы — {datetime.now(tz).strftime('%d.%m.%Y %H:%M')}"
        )
    except Exception as e:
        await m.answer(f"Не удалось отправить файл: {e}")

# ---------------------- morning notifications ----------------------
async def morning():
    today = datetime.now(tz).strftime("%Y-%m-%d")
    lessons = day_lessons(today)
    if not lessons:
        return
    lines = [f"• {L['time']} — {L['name'] or 'занятие'}"
             + (f" ({L['price']} ₽)" if L.get("price") else "") for L in lessons]
    text = f"Доброе утро! Сегодня занятий: {len(lessons)}\n\n" + "\n".join(lines)
    sep = "&" if "?" in MINIAPP_URL else "?"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Узнать подробнее",
                             web_app=WebAppInfo(url=f"{MINIAPP_URL}{sep}d={today}"))
    ]])
    for chat_id in notify_users():
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:
            logging.warning("notify fail %s: %s", chat_id, e)

# ---------------------- startup ----------------------
PUBLIC_URL   = "https://bot-1781087941-4553-ruserb.bothost.tech"
WEBHOOK_PATH = "/webhook"

async def on_startup(app):
    init_db()
    await bot.set_webhook(PUBLIC_URL + WEBHOOK_PATH, drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть расписание"),
        BotCommand(command="backup", description="Скачать бэкап базы"),
    ])
    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(morning, "cron", hour=NOTIFY_HOUR, minute=NOTIFY_MIN)
    sched.start()
    logging.info("started, webhook -> %s", PUBLIC_URL + WEBHOOK_PATH)

def main():
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    app = web.Application(middlewares=[cors])
    app.router.add_get("/", health)
    app.router.add_get("/api/day", api_day)
    app.router.add_post("/api/day", api_save_day)
    app.router.add_get("/api/keys", api_keys)
    app.router.add_get("/api/students", api_students)
    app.router.add_post("/api/students", api_save_student)
    app.router.add_post("/api/students/delete", api_delete_student)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda r: web.Response())
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()

