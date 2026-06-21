"""
Ljubičasto — бэкенд календаря репетитора.
Один процесс: Telegram-бот (webhook) + HTTP API для Mini App + уведомления.

Деплоится на Bothost из GitHub. Перед деплоем заполни блок CONFIG ниже.
"""

import os
import sqlite3
import secrets as _secrets
import logging
import base64
import io
from datetime import datetime, timedelta
from contextlib import closing

import pytz
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import (Message, InlineKeyboardMarkup,
                           InlineKeyboardButton, WebAppInfo, FSInputFile, BotCommand,
                           CallbackQuery, BufferedInputFile, MenuButtonWebApp)
from aiogram.filters import CommandStart, Command
from aiogram.utils.web_app import safe_parse_webapp_init_data
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======================= CONFIG =======================
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
MINIAPP_URL = "https://openopq.github.io/ljubicasto/?v=22"
STUDENT_URL = "https://openopq.github.io/ljubicasto/student.html?v=21"
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
        try: c.execute("ALTER TABLE students ADD COLUMN city TEXT")
        except Exception: pass
        try: c.execute("ALTER TABLE students ADD COLUMN timezone TEXT")
        except Exception: pass
        try: c.execute("ALTER TABLE students ADD COLUMN notes TEXT")
        except Exception: pass
        try: c.execute("ALTER TABLE students ADD COLUMN greeting_name TEXT")
        except Exception: pass
        try: c.execute("ALTER TABLE students ADD COLUMN hidden_from_stats INTEGER DEFAULT 0")
        except Exception: pass
        # ---- одноразовые токены реферальной ссылки ----
        c.execute("""CREATE TABLE IF NOT EXISTS connect_tokens(
            token TEXT PRIMARY KEY, student_id TEXT, created INTEGER,
            used INTEGER DEFAULT 0, used_at INTEGER)""")
        # ---- конструктор заданий ----
        c.execute("""CREATE TABLE IF NOT EXISTS task_levels(
            id TEXT PRIMARY KEY, name TEXT, sort INTEGER DEFAULT 0, created INTEGER)""")
        try: c.execute("ALTER TABLE task_levels ADD COLUMN is_cards INTEGER DEFAULT 0")
        except Exception: pass
        c.execute("""CREATE TABLE IF NOT EXISTS task_groups(
            id TEXT PRIMARY KEY, name TEXT, level_id TEXT, sort INTEGER DEFAULT 0, created INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, type TEXT, word TEXT, translation TEXT,
            choices TEXT, correct INTEGER DEFAULT 0,
            audio_file_id TEXT, group_id TEXT, level_id TEXT, created INTEGER)""")
        try: c.execute("ALTER TABLE tasks ADD COLUMN sort INTEGER DEFAULT 0")
        except Exception: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN word_comment TEXT")
        except Exception: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN choice_comments TEXT")
        except Exception: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN photo TEXT")
        except Exception: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN photo_choice TEXT")
        except Exception: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN photo_input TEXT")
        except Exception: pass
        # ---- карточки (особый уровень с фото) ----
        c.execute("""CREATE TABLE IF NOT EXISTS task_cards(
            id TEXT PRIMARY KEY, group_id TEXT, title TEXT,
            photo_file_id TEXT, audio_file_id TEXT, sort INTEGER DEFAULT 0, created INTEGER)""")
        # ---- ученики ----
        c.execute("""CREATE TABLE IF NOT EXISTS student_users(
            student_id TEXT PRIMARY KEY,
            chat_id INTEGER UNIQUE,
            timezone TEXT DEFAULT 'Europe/Moscow',
            connected_at INTEGER,
            notif_homework INTEGER DEFAULT 1,
            notif_hour INTEGER DEFAULT 1,
            notif_day INTEGER DEFAULT 0)""")
        try: c.execute("ALTER TABLE student_users ADD COLUMN notif_cards INTEGER DEFAULT 1")
        except Exception: pass
        try: c.execute("ALTER TABLE student_users ADD COLUMN notif_posts INTEGER DEFAULT 1")
        except Exception: pass
        try: c.execute("ALTER TABLE student_users ADD COLUMN onboarding_done INTEGER DEFAULT 0")
        except Exception: pass
        # ---- просмотр карточек учеником ----
        c.execute("""CREATE TABLE IF NOT EXISTS card_views(
            id TEXT PRIMARY KEY,
            student_id TEXT,
            group_id TEXT,
            viewed_at INTEGER)""")
        # ---- домашние задания ----
        c.execute("""CREATE TABLE IF NOT EXISTS assigned_tasks(
            id TEXT PRIMARY KEY,
            student_id TEXT,
            task_id TEXT,
            group_id TEXT,
            assigned_at INTEGER,
            status TEXT DEFAULT 'pending')""")
        c.execute("""CREATE TABLE IF NOT EXISTS task_progress(
            id TEXT PRIMARY KEY,
            student_id TEXT,
            task_id TEXT,
            assigned_id TEXT,
            attempt INTEGER DEFAULT 1,
            is_correct INTEGER DEFAULT 0,
            input_value TEXT,
            answered_at INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS homework_results(
            id TEXT PRIMARY KEY,
            student_id TEXT,
            group_id TEXT,
            completed_at INTEGER,
            dismissed INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS broadcast_messages(
            id TEXT PRIMARY KEY,
            student_id TEXT,
            text TEXT,
            photo_file_id TEXT,
            sent_at INTEGER,
            expires_at INTEGER)""")
        try: c.execute("ALTER TABLE broadcast_messages ADD COLUMN expires_at INTEGER")
        except Exception: pass
        c.commit()

# ---------------------- DB helpers ----------------------
def day_lessons(date):
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM lessons WHERE date=? ORDER BY time", (date,)).fetchall()
        return [dict(r) for r in rows]

def get_student_next_lesson(student_id, student_tz_name=None):
    """Ближайший будущий урок ученика + его номер по счёту (без пробных), как в календаре.
    Если передан student_tz_name — время урока пересчитывается под часовой пояс ученика
    (дата/время в БД всегда хранятся в московском времени)."""
    now = datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")
    with closing(db()) as c:
        # все уроки этого ученика, отсортированные по дате+времени
        rows = c.execute(
            "SELECT * FROM lessons WHERE studentId=? ORDER BY date, time", (student_id,)
        ).fetchall()
    all_lessons = [dict(r) for r in rows]
    not_trial = [L for L in all_lessons if not L.get("trial")]
    # ищем первый урок (среди непробных) который ещё не наступил
    next_lesson = None
    next_idx = None
    next_lesson_dt = None
    for idx, L in enumerate(not_trial):
        lesson_dt_str = f"{L['date']} {L['time']}"
        try:
            lesson_dt = tz.localize(datetime.strptime(lesson_dt_str, "%Y-%m-%d %H:%M"))
        except Exception:
            continue
        if lesson_dt >= now:
            next_lesson = L
            next_idx = idx
            next_lesson_dt = lesson_dt
            break
    if not next_lesson:
        return None

    result_date, result_time = next_lesson["date"], next_lesson["time"]
    if student_tz_name:
        try:
            student_tz = pytz.timezone(student_tz_name)
            local_dt = next_lesson_dt.astimezone(student_tz)
            result_date = local_dt.strftime("%Y-%m-%d")
            result_time = local_dt.strftime("%H:%M")
        except Exception:
            pass  # если пояс некорректный — отдаём московское время как есть

    return {
        "date": result_date,
        "time": result_time,
        "lesson_number": next_idx + 1,  # позиция в списке непробных, 1-based
        "price": next_lesson.get("price", ""),
        "level": next_lesson.get("level", ""),
    }

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
             trialUsed,created,archived,notes,city,timezone,greeting_name)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s.get("id"), s.get("name",""), s.get("phone",""),
             s.get("contacts",""), s.get("price",""), s.get("duration",""),
             s.get("level",""), s.get("about",""), s.get("color",""),
             1 if s.get("trialUsed") else 0, s.get("created") or 0,
             1 if s.get("archived") else 0,
             s.get("notes",""), s.get("city",""), s.get("timezone",""),
             s.get("greeting_name","")))
        c.commit()

def delete_student(sid):
    with closing(db()) as c:
        c.execute("UPDATE students SET archived=1 WHERE id=?", (sid,))
        c.commit()

def delete_student_forever(sid):
    """Полное удаление ученика и всех связанных с ним данных (Telegram-подключение, прогресс, токены)."""
    with closing(db()) as c:
        c.execute("DELETE FROM students WHERE id=?", (sid,))
        c.execute("DELETE FROM student_users WHERE student_id=?", (sid,))
        c.execute("DELETE FROM card_views WHERE student_id=?", (sid,))
        c.execute("DELETE FROM assigned_tasks WHERE student_id=?", (sid,))
        c.execute("DELETE FROM task_progress WHERE student_id=?", (sid,))
        c.execute("DELETE FROM homework_results WHERE student_id=?", (sid,))
        c.execute("DELETE FROM broadcast_messages WHERE student_id=?", (sid,))
        c.execute("DELETE FROM connect_tokens WHERE student_id=?", (sid,))
        c.commit()

def hide_student_from_stats(sid):
    """Скрывает ученика из счётчиков статистики (активных/всего), не трогая данные —
    уроки, доход и сама карточка ученика остаются целыми."""
    with closing(db()) as c:
        c.execute("UPDATE students SET hidden_from_stats=1 WHERE id=?", (sid,))
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
        tcount = c.execute("SELECT COUNT(*) FROM tasks WHERE group_id=?", (gid,)).fetchone()[0]
        ccount = c.execute("SELECT COUNT(*) FROM task_cards WHERE group_id=?", (gid,)).fetchone()[0]
        count = tcount + ccount
        if count > 0 and not force:
            return {"ok": False, "count": count}
        c.execute("DELETE FROM tasks WHERE group_id=?", (gid,))
        c.execute("DELETE FROM task_cards WHERE group_id=?", (gid,))
        c.execute("DELETE FROM task_groups WHERE id=?", (gid,))
        c.commit()
        return {"ok": True}

def list_tasks(group_id=None, level_id=None):
    with closing(db()) as c:
        if group_id:
            rows=c.execute("SELECT * FROM tasks WHERE group_id=? ORDER BY sort",(group_id,)).fetchall()
        elif level_id:
            rows=c.execute("SELECT * FROM tasks WHERE level_id=? ORDER BY sort",(level_id,)).fetchall()
        else:
            rows=c.execute("SELECT * FROM tasks ORDER BY sort").fetchall()
        return [dict(r) for r in rows]

def save_task(t):
    with closing(db()) as c:
        c.execute("""INSERT OR REPLACE INTO tasks
            (id,type,word,translation,choices,correct,audio_file_id,group_id,level_id,created,
             sort,word_comment,choice_comments,photo,photo_choice,photo_input)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t["id"], t.get("type","choice"), t.get("word",""), t.get("translation",""),
             t.get("choices",""), t.get("correct",0), t.get("audio_file_id",""),
             t.get("group_id",""), t.get("level_id",""), t.get("created",0),
             t.get("sort",0), t.get("word_comment",""), t.get("choice_comments","[]"),
             t.get("photo",""), t.get("photo_choice",""), t.get("photo_input","")))
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

def get_dev_mode():
    return get_setting("dev_mode", "0") == "1"

def set_dev_mode(on: bool):
    set_setting("dev_mode", "1" if on else "0")

def db_stats():
    with closing(db()) as c:
        students = c.execute("SELECT COUNT(*) FROM students WHERE archived=0").fetchone()[0]
        lessons  = c.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        markers  = c.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'reminded_%'").fetchone()[0]
    size_kb = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0
    return students, lessons, markers, size_kb

def clean_expired_broadcasts():
    """Удаляем из базы рассылки, у которых истёк срок жизни (>7 дней после expires_at — на всякий случай)."""
    cutoff = int(datetime.now().timestamp() * 1000) - 7 * 86400 * 1000
    with closing(db()) as c:
        c.execute("""DELETE FROM broadcast_messages
            WHERE expires_at IS NOT NULL AND expires_at < ?""", (cutoff,))
        c.commit()

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

def check_init_student(request):
    """Проверка для ученического Mini App — без ALLOWED_IDS."""
    init = request.headers.get("X-Init-Data", "")
    if not init:
        return None
    try:
        data = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=init)
        return data.user.id if data.user else None
    except Exception:
        return None

# ---- ученики helpers ----
def get_student_by_chat(chat_id):
    with closing(db()) as c:
        row = c.execute("""
            SELECT su.*, s.greeting_name as greeting_name, s.timezone as preset_timezone
            FROM student_users su
            LEFT JOIN students s ON s.id = su.student_id
            WHERE su.chat_id=?""", (chat_id,)).fetchone()
        return dict(row) if row else None

def get_student_user(student_id):
    with closing(db()) as c:
        row = c.execute("SELECT * FROM student_users WHERE student_id=?", (student_id,)).fetchone()
        return dict(row) if row else None

def create_connect_token(student_id):
    """Создаёт новый одноразовый токен для подключения ученика.
    Старые неиспользованные токены этого ученика автоматически становятся недействительными."""
    token = _secrets.token_urlsafe(12)  # ~16 случайных URL-safe символов
    with closing(db()) as c:
        # инвалидируем все предыдущие неиспользованные токены этого ученика
        c.execute("DELETE FROM connect_tokens WHERE student_id=? AND used=0", (student_id,))
        c.execute("INSERT INTO connect_tokens(token,student_id,created,used) VALUES(?,?,?,0)",
                  (token, student_id, int(datetime.now().timestamp()*1000)))
        c.commit()
    return token

def resolve_connect_token(token):
    """Возвращает student_id если токен валиден и не использован, иначе None."""
    with closing(db()) as c:
        row = c.execute("SELECT * FROM connect_tokens WHERE token=?", (token,)).fetchone()
        if not row or row["used"]:
            return None
        return row["student_id"]

def mark_token_used(token):
    with closing(db()) as c:
        c.execute("UPDATE connect_tokens SET used=1, used_at=? WHERE token=?",
                  (int(datetime.now().timestamp()*1000), token))
        c.commit()

def connect_student(student_id, chat_id, timezone="Europe/Moscow"):
    with closing(db()) as c:
        c.execute("""INSERT OR REPLACE INTO student_users
            (student_id, chat_id, timezone, connected_at, notif_homework, notif_hour, notif_day, notif_cards, notif_posts)
            VALUES(?,?,?,?,1,1,0,1,1)""",
            (student_id, chat_id, timezone, int(datetime.now().timestamp()*1000)))
        c.commit()

def update_student_tz(student_id, timezone):
    with closing(db()) as c:
        c.execute("UPDATE student_users SET timezone=? WHERE student_id=?", (timezone, student_id))
        c.commit()

def update_student_notif(student_id, homework=None, hour=None, day=None, cards=None, posts=None):
    with closing(db()) as c:
        if homework is not None:
            c.execute("UPDATE student_users SET notif_homework=? WHERE student_id=?", (1 if homework else 0, student_id))
        if hour is not None:
            c.execute("UPDATE student_users SET notif_hour=? WHERE student_id=?", (1 if hour else 0, student_id))
        if day is not None:
            c.execute("UPDATE student_users SET notif_day=? WHERE student_id=?", (1 if day else 0, student_id))
        if cards is not None:
            c.execute("UPDATE student_users SET notif_cards=? WHERE student_id=?", (1 if cards else 0, student_id))
        if posts is not None:
            c.execute("UPDATE student_users SET notif_posts=? WHERE student_id=?", (1 if posts else 0, student_id))
        c.commit()

# ---- домашние задания helpers ----
import uuid as _uuid
def new_id(): return _uuid.uuid4().hex

def assign_homework(student_id, task_ids, group_id):
    with closing(db()) as c:
        now = int(datetime.now().timestamp()*1000)
        for tid in task_ids:
            c.execute("""INSERT INTO assigned_tasks(id,student_id,task_id,group_id,assigned_at,status)
                VALUES(?,?,?,?,?,'pending')""",
                (new_id(), student_id, tid, group_id, now))
        c.commit()

def get_student_homework(student_id):
    with closing(db()) as c:
        rows = c.execute("""
            SELECT at.*, t.word, t.translation, t.type, t.choices, t.correct, t.audio_file_id,
                   t.word_comment, t.choice_comments, t.photo, t.photo_choice, t.photo_input,
                   tg.name as group_name, tg.id as group_id
            FROM assigned_tasks at
            JOIN tasks t ON t.id=at.task_id
            JOIN task_groups tg ON tg.id=at.group_id
            WHERE at.student_id=?
            ORDER BY at.assigned_at, at.id""", (student_id,)).fetchall()
        return [dict(r) for r in rows]

def save_progress(student_id, task_id, assigned_id, is_correct, input_value=""):
    with closing(db()) as c:
        # считаем попытку
        attempt = c.execute("""SELECT COUNT(*) FROM task_progress
            WHERE student_id=? AND task_id=? AND assigned_id=?""",
            (student_id, task_id, assigned_id)).fetchone()[0] + 1
        c.execute("""INSERT INTO task_progress(id,student_id,task_id,assigned_id,attempt,is_correct,input_value,answered_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (new_id(), student_id, task_id, assigned_id, attempt,
             1 if is_correct else 0, input_value, int(datetime.now().timestamp()*1000)))
        # если правильно — помечаем assigned_task как done
        if is_correct:
            c.execute("UPDATE assigned_tasks SET status='done' WHERE id=?", (assigned_id,))
        c.commit()

def check_group_complete(student_id, group_id):
    """Проверяет завершена ли вся группа заданий."""
    with closing(db()) as c:
        total = c.execute("SELECT COUNT(*) FROM assigned_tasks WHERE student_id=? AND group_id=?",
                          (student_id, group_id)).fetchone()[0]
        done = c.execute("""SELECT COUNT(*) FROM assigned_tasks
            WHERE student_id=? AND group_id=? AND status='done'""",
            (student_id, group_id)).fetchone()[0]
        return total > 0 and done == total

def save_homework_result(student_id, group_id):
    with closing(db()) as c:
        c.execute("""INSERT OR IGNORE INTO homework_results(id,student_id,group_id,completed_at,dismissed)
            VALUES(?,?,?,?,0)""",
            (new_id(), student_id, group_id, int(datetime.now().timestamp()*1000)))
        c.commit()

def get_homework_results(dismissed=False):
    with closing(db()) as c:
        rows = c.execute("""
            SELECT hr.*, s.name as student_name, tg.name as group_name
            FROM homework_results hr
            LEFT JOIN students s ON s.id=hr.student_id
            LEFT JOIN task_groups tg ON tg.id=hr.group_id
            WHERE hr.dismissed=?
            ORDER BY hr.completed_at DESC""", (1 if dismissed else 0,)).fetchall()
        return [dict(r) for r in rows]

def dismiss_result(result_id):
    with closing(db()) as c:
        c.execute("UPDATE homework_results SET dismissed=1 WHERE id=?", (result_id,))
        c.commit()

def get_task_stats(student_id, group_id):
    """Статистика прохождения группы заданий."""
    with closing(db()) as c:
        rows = c.execute("""
            SELECT t.word, t.translation, t.type,
                   COUNT(tp.id) as attempts,
                   SUM(tp.is_correct) as correct_count,
                   GROUP_CONCAT(CASE WHEN tp.is_correct=0 THEN tp.input_value END, '|') as wrong_inputs
            FROM assigned_tasks at
            JOIN tasks t ON t.id=at.task_id
            LEFT JOIN task_progress tp ON tp.assigned_id=at.id
            WHERE at.student_id=? AND at.group_id=?
            GROUP BY at.id, t.word""", (student_id, group_id)).fetchall()
        return [dict(r) for r in rows]

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

async def api_hide_student_from_stats(request):
    """Скрывает ученика из счётчиков статистики (без удаления данных)."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    hide_student_from_stats(body.get("id"))
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

async def api_history_set(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    set_history_mode(bool(body.get("on", False)))
    return web.json_response({"ok": True, "history": get_history_mode()})

async def api_dev_mode(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    return web.json_response({"dev_mode": get_dev_mode()})

async def api_dev_mode_set(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    set_dev_mode(bool(body.get("on", False)))
    return web.json_response({"ok": True, "dev_mode": get_dev_mode()})

async def api_notify(request):
    """Возвращает персональные настройки уведомлений."""
    uid = check_init(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    hour = get_setting(f"notify_hour_{uid}", "1") == "1"
    morning = get_setting(f"notify_morning_{uid}", "1") == "1"
    evening = get_setting(f"notify_evening_{uid}", "0") == "1"
    return web.json_response({"hour_reminder": hour, "morning": morning, "evening": evening})

async def api_notify_set(request):
    """Сохраняет персональные настройки уведомлений."""
    uid = check_init(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    set_setting(f"notify_hour_{uid}", "1" if body.get("hour_reminder", True) else "0")
    set_setting(f"notify_morning_{uid}", "1" if body.get("morning", True) else "0")
    set_setting(f"notify_evening_{uid}", "1" if body.get("evening", False) else "0")
    return web.json_response({"ok": True})

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
        body = await request.json()
        with closing(db()) as c:
            is_new = c.execute("SELECT 1 FROM task_groups WHERE id=?", (body.get("id"),)).fetchone() is None
        save_group(body)
        # уведомляем подключённых учеников о новой группе карточек (не о заданиях — те назначаются персонально)
        if is_new:
            with closing(db()) as c:
                lvl = c.execute("SELECT is_cards FROM task_levels WHERE id=?", (body.get("level_id"),)).fetchone()
            if lvl and lvl["is_cards"]:
                with closing(db()) as c:
                    students = c.execute(
                        "SELECT chat_id FROM student_users WHERE notif_cards=1 AND chat_id IS NOT NULL"
                    ).fetchall()
                for s in students:
                    try:
                        await bot.send_message(s["chat_id"], f"🖼 Новые карточки: «{body.get('name','')}»")
                    except Exception as e:
                        logging.warning("notify cards fail: %s", e)
        return web.json_response({"ok":True})
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
        body = await request.json()
        # если фото пришло как base64 — конвертируем в Telegram file_id, чтобы не раздувать базу
        photo_val = body.get("photo", "")
        if photo_val and photo_val.startswith("data:"):
            try:
                raw = photo_val.split(",", 1)[-1]
                photo_bytes = base64.b64decode(raw)
                msg = await bot.send_photo(
                    DEV_ID, BufferedInputFile(photo_bytes, filename="task.jpg"),
                    caption="📌 Фото для задания сохранено"
                )
                if msg.photo:
                    body["photo"] = msg.photo[-1].file_id
            except Exception as e:
                logging.warning("task photo upload fail: %s", e)
        save_task(body)
        return web.json_response({"ok":True})
    group_id=request.query.get("group_id"); level_id=request.query.get("level_id")
    tasks = list_tasks(group_id, level_id)
    # отдаём прямую ссылку на фото вместо file_id
    for t in tasks:
        if t.get("photo"):
            try:
                f = await bot.get_file(t["photo"])
                t["photo"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
            except Exception:
                t["photo"] = ""
    return web.json_response({"tasks": tasks})

async def api_delete_task(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    body=await request.json(); delete_task(body["id"])
    return web.json_response({"ok":True})

async def api_move_task(request):
    if check_init(request) is None: return web.json_response({"error":"auth"},status=403)
    body=await request.json(); move_task_group(body["task_id"], body["group_id"])
    return web.json_response({"ok":True})

# ---- API домашних заданий ----
async def api_homework_assign(request):
    """Назначить домашнее задание ученику. Только для Миляны."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    student_id = body.get("student_id")
    task_ids = body.get("task_ids", [])
    group_id = body.get("group_id")
    if not student_id or not task_ids:
        return web.json_response({"error": "missing fields"}, status=400)
    assign_homework(student_id, task_ids, group_id)
    # уведомление ученику
    su = get_student_user(student_id)
    if su and su.get("chat_id") and su.get("notif_homework", 1):
        try:
            await bot.send_message(su["chat_id"],
                "📚 Вам пришло домашнее задание!")
        except Exception as e:
            logging.warning("notify student fail: %s", e)
    return web.json_response({"ok": True})

async def api_student_next_lesson(request):
    """Ближайший урок ученика — для шапки ученического Mini App.
    Время урока пересчитывается под часовой пояс, который выбрал ученик."""
    uid = check_init_student(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    su = get_student_by_chat(uid)
    if not su:
        return web.json_response({"error": "not connected"}, status=403)
    student_tz_name = su.get("timezone") or "Europe/Moscow"
    lesson = get_student_next_lesson(su["student_id"], student_tz_name)
    return web.json_response({"lesson": lesson, "timezone": student_tz_name})

async def api_homework_student(request):
    """Получить домашние задания для ученика (ученический Mini App)."""
    uid = check_init_student(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    su = get_student_by_chat(uid)
    if not su:
        return web.json_response({"error": "not connected"}, status=403)
    hw = get_student_homework(su["student_id"])
    for t in hw:
        if t.get("photo"):
            try:
                f = await bot.get_file(t["photo"])
                t["photo"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
            except Exception:
                t["photo"] = ""
    return web.json_response({"homework": hw, "student": su})

async def api_homework_progress(request):
    """Записать прогресс ученика."""
    uid = check_init_student(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    su = get_student_by_chat(uid)
    if not su:
        return web.json_response({"error": "not connected"}, status=403)
    body = await request.json()
    save_progress(
        su["student_id"],
        body.get("task_id"),
        body.get("assigned_id"),
        body.get("is_correct", False),
        body.get("input_value", "")
    )
    # проверяем завершена ли группа
    group_id = body.get("group_id")
    if group_id and check_group_complete(su["student_id"], group_id):
        save_homework_result(su["student_id"], group_id)
        # уведомляем Милян
        for chat_id in notify_users():
            if get_setting(f"notify_hw_results_{chat_id}", "0") == "1":
                stu = next((s for s in list_students() if s["id"] == su["student_id"]), None)
                stu_name = stu["name"] if stu else "Ученик"
                with closing(db()) as c:
                    grp = c.execute("SELECT name FROM task_groups WHERE id=?", (group_id,)).fetchone()
                grp_name = grp["name"] if grp else "задание"
                try:
                    await bot.send_message(chat_id,
                        f"✅ {stu_name} выполнил домашнее задание «{grp_name}»!")
                except Exception as e:
                    logging.warning("notify teacher fail: %s", e)
    return web.json_response({"ok": True})

async def api_homework_results(request):
    """Результаты выполненных домашних заданий (для колокольчика)."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    results = get_homework_results(dismissed=False)
    return web.json_response({"results": results})

async def api_homework_stats(request):
    """Статистика прохождения конкретной домашки."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    student_id = request.query.get("student_id")
    group_id = request.query.get("group_id")
    stats = get_task_stats(student_id, group_id)
    return web.json_response({"stats": stats})

async def api_homework_dismiss(request):
    """Отметить уведомление о домашке как прочитанное."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    dismiss_result(body.get("id"))
    return web.json_response({"ok": True})

async def api_student_onboarding_done(request):
    """Отметить что ученик прошёл обучающий тур — больше не показываем."""
    uid = check_init_student(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    su = get_student_by_chat(uid)
    if not su:
        return web.json_response({"error": "not connected"}, status=403)
    with closing(db()) as c:
        c.execute("UPDATE student_users SET onboarding_done=1 WHERE student_id=?", (su["student_id"],))
        c.commit()
    return web.json_response({"ok": True})

async def api_gen_connect_link(request):
    """Генерирует новую одноразовую ссылку подключения для ученика (вызывается Миляной из карточки)."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    student_id = body.get("student_id")
    if not student_id:
        return web.json_response({"error": "no student_id"}, status=400)
    with closing(db()) as c:
        stu = c.execute("SELECT id FROM students WHERE id=?", (student_id,)).fetchone()
    if not stu:
        return web.json_response({"error": "student not found"}, status=404)
    token = create_connect_token(student_id)
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={token}"
    return web.json_response({"link": link, "token": token})

async def api_student_connect(request):
    """Проверка подключения ученика и обновление настроек."""
    uid = check_init_student(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    su = get_student_by_chat(uid)
    if not su:
        return web.json_response({"connected": False})
    return web.json_response({"connected": True, "student": su})

async def api_student_notif(request):
    """Получить/обновить настройки уведомлений ученика.
    Доступ либо самому ученику (по его chat_id), либо Миляне (по ALLOWED_IDS + явный student_id)."""
    student_id_override = request.query.get("student_id")
    su = None
    if student_id_override and check_init(request) is not None:
        # запрос от Миляны/DEV — student_id передан явно
        su = get_student_user(student_id_override)
        target_student_id = student_id_override
    else:
        uid = check_init_student(request)
        if uid is None:
            return web.json_response({"error": "auth"}, status=403)
        su = get_student_by_chat(uid)
        target_student_id = su["student_id"] if su else None
    if not su:
        return web.json_response({"error": "not connected"}, status=403)
    if request.method == "POST":
        body = await request.json()
        body_student_id = body.get("student_id") or target_student_id
        update_student_notif(
            body_student_id,
            homework=body.get("homework"),
            hour=body.get("hour"),
            day=body.get("day"),
            cards=body.get("cards"),
            posts=body.get("posts"),
        )
        if "timezone" in body:
            update_student_tz(body_student_id, body["timezone"])
        return web.json_response({"ok": True})
    return web.json_response({
        "notif_homework": su.get("notif_homework", 1),
        "notif_hour": su.get("notif_hour", 1),
        "notif_day": su.get("notif_day", 0),
        "notif_cards": su.get("notif_cards", 1),
        "notif_posts": su.get("notif_posts", 1),
        "timezone": su.get("timezone", "Europe/Moscow")
    })

async def api_student_connected_list(request):
    """Список подключённых учеников (для индикатора ✓)."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    with closing(db()) as c:
        rows = c.execute("SELECT student_id FROM student_users").fetchall()
    return web.json_response({"connected": [r["student_id"] for r in rows]})

async def api_broadcast(request):
    """Рассылка сообщения (с опциональным фото) выбранным ученикам."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    text = body.get("text", "").strip()
    student_ids = body.get("student_ids", [])
    is_html = body.get("html", False)
    photo_b64 = body.get("photo")  # data:image/...;base64,XXXX или просто base64
    ttl_days = body.get("ttl_days", 7)
    expires_at = None if ttl_days is None else int(datetime.now().timestamp() * 1000) + ttl_days * 86400 * 1000
    if not (text or photo_b64) or not student_ids:
        return web.json_response({"error": "missing fields"}, status=400)
    sent, failed = 0, 0
    for sid in student_ids:
        su = get_student_user(sid)
        if not su or not su.get("chat_id"):
            failed += 1
            continue
        try:
            photo_file_id = None
            caption = f"🌸 {text}" if text else None
            wants_chat_msg = su.get("notif_posts", 1) == 1
            if photo_b64:
                raw = photo_b64.split(",", 1)[-1]
                photo_bytes = base64.b64decode(raw)
                if wants_chat_msg:
                    msg = await bot.send_photo(
                        su["chat_id"], BufferedInputFile(photo_bytes, filename="broadcast.jpg"),
                        caption=caption, parse_mode="HTML" if is_html else None
                    )
                    if msg.photo:
                        photo_file_id = msg.photo[-1].file_id
                else:
                    # не шлём в чат, но всё равно нужен file_id для отображения в приложении —
                    # отправляем себе (DEV) тихо, чтобы получить file_id без уведомления ученика
                    msg = await bot.send_photo(
                        DEV_ID, BufferedInputFile(photo_bytes, filename="broadcast.jpg"),
                        caption="📌 фото поста (тихая рассылка)"
                    )
                    if msg.photo:
                        photo_file_id = msg.photo[-1].file_id
            else:
                if wants_chat_msg:
                    await bot.send_message(su["chat_id"], caption, parse_mode="HTML" if is_html else None)
            sent += 1
            # сохраняем в историю сообщений ученика
            with closing(db()) as c:
                c.execute("""INSERT INTO broadcast_messages(id,student_id,text,photo_file_id,sent_at,expires_at)
                    VALUES(?,?,?,?,?,?)""",
                    (new_id(), sid, text, photo_file_id, int(datetime.now().timestamp()*1000), expires_at))
                c.commit()
        except Exception as e:
            logging.warning("broadcast fail %s: %s", sid, e)
            failed += 1
    return web.json_response({"ok": True, "sent": sent, "failed": failed})

async def api_student_messages(request):
    """Получить сообщения от Миляны для ученика (для карусели). Истёкшие не показываются."""
    uid = check_init_student(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    su = get_student_by_chat(uid)
    if not su:
        return web.json_response({"error": "not connected"}, status=403)
    now = int(datetime.now().timestamp() * 1000)
    with closing(db()) as c:
        rows = c.execute("""SELECT * FROM broadcast_messages
            WHERE student_id=? AND (expires_at IS NULL OR expires_at>?)
            ORDER BY sent_at DESC LIMIT 20""",
            (su["student_id"], now)).fetchall()
    messages = []
    for r in rows:
        d = dict(r)
        if d.get("photo_file_id"):
            try:
                f = await bot.get_file(d["photo_file_id"])
                d["photo_url"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
            except Exception:
                d["photo_url"] = None
        messages.append(d)
    return web.json_response({"messages": messages})

async def api_tasks_reorder(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    with closing(db()) as c:
        for idx, tid in enumerate(body.get("ids", [])):
            c.execute("UPDATE tasks SET sort=? WHERE id=?", (idx, tid))
        c.commit()
    return web.json_response({"ok": True})

async def api_cards(request):
    """CRUD для карточек (раздел «Карточки»)."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    if request.method == "POST":
        body = await request.json()
        with closing(db()) as c:
            c.execute("""INSERT OR REPLACE INTO task_cards
                (id,group_id,title,photo_file_id,audio_file_id,sort,created)
                VALUES(?,?,?,?,?,?,?)""",
                (body.get("id"), body.get("group_id"), body.get("title",""),
                 body.get("photo_file_id") or body.get("photo",""),
                 body.get("audio_file_id",""), body.get("sort",0), body.get("created") or 0))
            c.commit()
        return web.json_response({"ok": True})
    group_id = request.query.get("group_id")
    with closing(db()) as c:
        if group_id:
            rows = c.execute("SELECT * FROM task_cards WHERE group_id=? ORDER BY sort", (group_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM task_cards ORDER BY sort").fetchall()
    cards = [dict(r) for r in rows]
    # отдаём фронту фото как прямую ссылку (фронт хранит её как "photo")
    for c_ in cards:
        c_["photo"] = ""
        if c_.get("photo_file_id"):
            try:
                f = await bot.get_file(c_["photo_file_id"])
                c_["photo"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
            except Exception:
                pass
    return web.json_response({"cards": cards})

async def api_cards_delete(request):
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    with closing(db()) as c:
        c.execute("DELETE FROM task_cards WHERE id=?", (body.get("id"),))
        c.commit()
    return web.json_response({"ok": True})

async def api_student_cards(request):
    """Список групп карточек для ученика — с фото, аудио и статусом просмотра."""
    uid = check_init_student(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    su = get_student_by_chat(uid)
    if not su:
        return web.json_response({"error": "not connected"}, status=403)
    student_id = su["student_id"]
    with closing(db()) as c:
        cards_level = c.execute("SELECT id FROM task_levels WHERE is_cards=1 LIMIT 1").fetchone()
        if not cards_level:
            return web.json_response({"groups": []})
        groups_rows = c.execute(
            "SELECT * FROM task_groups WHERE level_id=? ORDER BY sort", (cards_level["id"],)
        ).fetchall()
        viewed_rows = c.execute(
            "SELECT group_id FROM card_views WHERE student_id=?", (student_id,)
        ).fetchall()
        viewed_ids = {r["group_id"] for r in viewed_rows}
    result = []
    for g in groups_rows:
        with closing(db()) as c:
            card_rows = c.execute(
                "SELECT * FROM task_cards WHERE group_id=? ORDER BY sort", (g["id"],)
            ).fetchall()
        cards = []
        for cr in card_rows:
            cd = dict(cr)
            cd["photo"] = ""
            if cd.get("photo_file_id"):
                try:
                    f = await bot.get_file(cd["photo_file_id"])
                    cd["photo"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
                except Exception:
                    pass
            cards.append(cd)
        if not cards:
            continue
        result.append({
            "group_id": g["id"],
            "group_name": g["name"],
            "viewed": g["id"] in viewed_ids,
            "cover_photo": cards[0]["photo"] if cards else "",
            "cards": cards,
        })
    return web.json_response({"groups": result})

async def api_card_view(request):
    """Отметить группу карточек как просмотренную учеником."""
    uid = check_init_student(request)
    if uid is None:
        return web.json_response({"error": "auth"}, status=403)
    su = get_student_by_chat(uid)
    if not su:
        return web.json_response({"error": "not connected"}, status=403)
    body = await request.json()
    group_id = body.get("group_id")
    if not group_id:
        return web.json_response({"error": "no group_id"}, status=400)
    with closing(db()) as c:
        existing = c.execute(
            "SELECT id FROM card_views WHERE student_id=? AND group_id=?",
            (su["student_id"], group_id)
        ).fetchone()
        if not existing:
            c.execute(
                "INSERT INTO card_views(id,student_id,group_id,viewed_at) VALUES(?,?,?,?)",
                (new_id(), su["student_id"], group_id, int(datetime.now().timestamp()*1000))
            )
            c.commit()
    return web.json_response({"ok": True})

async def api_upload_photo(request):
    """Принимает base64 фото, загружает в Telegram (себе, без показа в чате никому кроме DEV),
    возвращает photo_file_id для сохранения в карточке."""
    if check_init(request) is None:
        return web.json_response({"error": "auth"}, status=403)
    body = await request.json()
    photo_b64 = body.get("photo")
    if not photo_b64:
        return web.json_response({"error": "no photo"}, status=400)
    try:
        raw = photo_b64.split(",", 1)[-1]
        photo_bytes = base64.b64decode(raw)
        msg = await bot.send_photo(
            DEV_ID, BufferedInputFile(photo_bytes, filename="card.jpg"),
            caption="📌 Фото для карточки сохранено"
        )
        photo_file_id = msg.photo[-1].file_id if msg.photo else None
        return web.json_response({"ok": True, "photo_file_id": photo_file_id})
    except Exception as e:
        logging.warning("upload_photo fail: %s", e)
        return web.json_response({"error": str(e)}, status=500)

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
    args = m.text.split(maxsplit=1)[1] if len(m.text.split()) > 1 else ""
    # реферальная ссылка ученика: /start TOKEN — одноразовый токен, сгенерированный в карточке ученика
    if args:
        student_id = resolve_connect_token(args)
        if not student_id:
            await m.answer("Ссылка недействительна или уже использована. Попросите Миляну выслать новую ссылку.")
            return
        with closing(db()) as c:
            stu = c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if not stu:
            await m.answer("Ссылка недействительна.")
            return
        existing = get_student_user(student_id)
        connect_student(student_id, m.from_user.id)
        mark_token_used(args)
        # персональная кнопка меню "Задания" вместо общей "Расписание" — только для этого ученика
        try:
            await bot.set_chat_menu_button(
                chat_id=m.from_user.id,
                menu_button=MenuButtonWebApp(text="Задания", web_app=WebAppInfo(url=STUDENT_URL))
            )
        except Exception as e:
            logging.warning("set_chat_menu_button fail for student %s: %s", student_id, e)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📖 Открыть задания",
                                 web_app=WebAppInfo(url=STUDENT_URL))
        ]])
        greeting_name = (stu["greeting_name"] or "").strip() if stu["greeting_name"] else ""
        hello = f"Здравствуйте, {greeting_name}! 🌸" if greeting_name else "Привет! 🌸"
        if not existing:
            await m.answer(
                f"{hello}\nВы подключились к урокам сербского языка.\n\n"
                f"Открывайте задания по кнопке ниже.",
                reply_markup=kb)
        else:
            await m.answer(
                f"{hello}\nОткрывайте задания по кнопке ниже.",
                reply_markup=kb)
        return

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

async def notify_students_hour():
    """Уведомление ученикам за час до урока."""
    now = datetime.now(tz)
    target = now + timedelta(hours=1)
    target_time = target.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")
    lessons = day_lessons(date_str)
    for L in lessons:
        if L.get("time") != target_time or not L.get("studentId"):
            continue
        su = get_student_user(L["studentId"])
        if not su or not su.get("chat_id") or not su.get("notif_hour", 1):
            continue
        try:
            # пересчёт времени для часового пояса ученика
            student_tz = pytz.timezone(su.get("timezone", "Europe/Moscow"))
            lesson_dt = tz.localize(datetime.strptime(f"{date_str} {L['time']}", "%Y-%m-%d %H:%M"))
            student_time = lesson_dt.astimezone(student_tz).strftime("%H:%M")
            await bot.send_message(su["chat_id"],
                f"⏰ Через час занятие по сербскому языку — в {student_time}!")
        except Exception as e:
            logging.warning("student hour notify fail: %s", e)

async def notify_students_day():
    """Утреннее уведомление ученикам о занятии сегодня."""
    today = datetime.now(tz).strftime("%Y-%m-%d")
    lessons = day_lessons(today)
    notified = set()
    for L in lessons:
        sid = L.get("studentId")
        if not sid or sid in notified:
            continue
        su = get_student_user(sid)
        if not su or not su.get("chat_id") or not su.get("notif_day", 0):
            continue
        try:
            student_tz = pytz.timezone(su.get("timezone", "Europe/Moscow"))
            lesson_dt = tz.localize(datetime.strptime(f"{today} {L['time']}", "%Y-%m-%d %H:%M"))
            student_time = lesson_dt.astimezone(student_tz).strftime("%H:%M")
            await bot.send_message(su["chat_id"],
                f"🌸 Сегодня занятие по сербскому языку в {student_time}!")
            notified.add(sid)
        except Exception as e:
            logging.warning("student day notify fail: %s", e)
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
                if get_setting(f"notify_hour_{chat_id}", "1") != "1":
                    continue
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
        if get_setting(f"notify_morning_{chat_id}", "1") != "1":
            continue
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:
            logging.warning("notify fail %s: %s", chat_id, e)

async def evening():
    """Уведомление в 20:00 о расписании на завтра."""
    tomorrow = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
    lessons = day_lessons(tomorrow)
    if not lessons:
        return
    lines = [f"• {L['time']} — {L['name'] or 'занятие'}"
             + (f" ({L['price']} ₽)" if L.get("price") else "")
             for L in lessons]
    text = f"Завтра занятий: {len(lessons)}\n\n" + "\n".join(lines)
    sep  = "&" if "?" in MINIAPP_URL else "?"
    kb   = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть расписание",
                             web_app=WebAppInfo(url=f"{MINIAPP_URL}{sep}d={tomorrow}"))
    ]])
    for chat_id in notify_users():
        if get_setting(f"notify_evening_{chat_id}", "0") != "1":
            continue
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:
            logging.warning("evening notify fail %s: %s", chat_id, e)

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

TASKS_URL = "https://openopq.github.io/ljubicasto/tasks.html?v=21"

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
    sched.add_job(evening,       "cron", hour=20, minute=0)
    sched.add_job(hour_reminder, "cron", minute="*/10")
    sched.add_job(notify_students_hour, "cron", minute="*/10")
    sched.add_job(notify_students_day,  "cron", hour=8, minute=5)
    sched.add_job(auto_backup,   "cron", day_of_week="mon,wed,fri", hour=3, minute=0)
    sched.add_job(clean_old_markers, "cron", hour=3, minute=0)
    sched.add_job(clean_expired_broadcasts, "cron", hour=3, minute=15)
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
    app.router.add_post("/api/students/hide-from-stats",  api_hide_student_from_stats)
    app.router.add_get("/api/history",           api_history)
    app.router.add_post("/api/history/set",      api_history_set)
    app.router.add_get("/api/dev_mode",          api_dev_mode)
    app.router.add_post("/api/dev_mode/set",     api_dev_mode_set)
    app.router.add_get("/api/notify",            api_notify)
    app.router.add_post("/api/notify/set",       api_notify_set)
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
    # домашние задания
    app.router.add_post("/api/homework/assign",  api_homework_assign)
    app.router.add_get("/api/homework/student",  api_homework_student)
    app.router.add_get("/api/student/next_lesson", api_student_next_lesson)
    app.router.add_post("/api/homework/progress",api_homework_progress)
    app.router.add_get("/api/homework/results",  api_homework_results)
    app.router.add_get("/api/homework/stats",    api_homework_stats)
    app.router.add_post("/api/homework/dismiss", api_homework_dismiss)
    # ученик
    app.router.add_get("/api/student/connect",   api_student_connect)
    app.router.add_post("/api/gen_connect_link",  api_gen_connect_link)
    app.router.add_post("/api/student/onboarding_done", api_student_onboarding_done)
    app.router.add_get("/api/student/notif",     api_student_notif)
    app.router.add_post("/api/student/notif",    api_student_notif)
    app.router.add_get("/api/students/connected",api_student_connected_list)
    app.router.add_post("/api/broadcast",        api_broadcast)
    app.router.add_get("/api/student/messages",  api_student_messages)
    # задания
    app.router.add_post("/api/tasks/reorder",    api_tasks_reorder)
    app.router.add_get("/api/cards",              api_cards)
    app.router.add_post("/api/cards",             api_cards)
    app.router.add_post("/api/cards/delete",      api_cards_delete)
    app.router.add_get("/api/student/cards",      api_student_cards)
    app.router.add_post("/api/cards/view",        api_card_view)
    app.router.add_post("/api/upload_photo",      api_upload_photo)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda r: web.Response())
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
