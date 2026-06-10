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
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.filters import CommandStart
from aiogram.utils.web_app import safe_parse_webapp_init_data
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======================= CONFIG =======================
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
MINIAPP_URL = "https://openopq.github.io/ljubicasto/"
ALLOWED_IDS = [7653945813]
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
            paid INTEGER DEFAULT 0, groupId TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            chat_id INTEGER PRIMARY KEY, notify INTEGER DEFAULT 1)""")
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
                (id,date,time,name,phone,tg,price,level,about,paid,groupId)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (L.get("id"), date, L.get("time",""), L.get("name",""), L.get("phone",""),
                 L.get("tg",""), L.get("price",""), L.get("level",""), L.get("about",""),
                 1 if L.get("paid") else 0, L.get("groupId")))
        c.commit()

def all_day_keys():
    with closing(db()) as c:
        rows = c.execute("SELECT DISTINCT date FROM lessons").fetchall()
        keys = []
        for r in rows:
            y, m, d = r["date"].split("-")
            keys.append(f"d_{y}_{m}_{d}")
        return keys

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
        "Привет! Я Ljubičasto 🌿\nБуду присылать расписание по утрам. "
        "Открыть календарь — кнопкой ниже или через меню.",
        reply_markup=kb)

# ---------------------- morning notifications ----------------------
async def morning():
    today = datetime.now(tz).strftime("%Y-%m-%d")
    lessons = day_lessons(today)
    if not lessons:
        return
    lines = [f"• {L['time']} — {L['name'] or 'занятие'}"
             + (f" ({L['price']} ₽)" if L.get("price") else "") for L in lessons]
    text = f"Доброе утро! Сегодня занятий: {len(lessons)}\n\n" + "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Узнать подробнее",
                             web_app=WebAppInfo(url=f"{MINIAPP_URL}?d={today}"))
    ]])
    for chat_id in notify_users():
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:
            logging.warning("notify fail %s: %s", chat_id, e)

# ---------------------- startup ----------------------
async def run_bot():
    init_db()
    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(morning, "cron", hour=NOTIFY_HOUR, minute=NOTIFY_MIN)
    sched.start()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Bot started (polling)")
    await dp.start_polling(bot)

async def run_api():
    app = web.Application(middlewares=[cors])
    app.router.add_get("/", health)
    app.router.add_get("/api/day", api_day)
    app.router.add_post("/api/day", api_save_day)
    app.router.add_get("/api/keys", api_keys)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda r: web.Response())
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("API started on port %s", PORT)

async def main_async():
    init_db()
    await asyncio.gather(run_api(), run_bot())

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()

