import os
import json
import logging
import tempfile
from datetime import datetime, time, timedelta
import pytz
from anthropic import Anthropic
from openai import OpenAI
from supabase import create_client, Client
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ── CONFIG ─────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_KEY    = os.environ["OPENAI_API_KEY"]
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
UAE_TZ        = pytz.timezone("Asia/Dubai")

claude   = Anthropic(api_key=ANTHROPIC_KEY)
oai      = OpenAI(api_key=OPENAI_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── CONSTANTS ──────────────────────────────────────────────────────────────────
CAT_LABEL = {
    "cozy":     "🏠 Cozy Home",
    "content":  "🎬 Контент и съёмки",
    "marketing":"📣 Маркетинг и реклама",
    "finance":  "💰 Финансы",
    "life":     "👤 Личное",
    "fit":      "💪 Фитнес и здоровье",
    "edu":      "📚 Обучение",
    "other":    "📌 Другое",
}
CAT_KEYS   = list(CAT_LABEL.keys())
PRIO_EMOJI = {"срочно": "🔴", "важно": "🟡", "обычное": "⚪"}
STATUS_EMOJI = {"open": "📋", "waiting": "⏳", "done": "✅"}

def now_uae():
    return datetime.now(UAE_TZ)

def today_str():
    return now_uae().date().isoformat()

# ── DATABASE: TASKS ────────────────────────────────────────────────────────────
def db_get_open(user_id):
    return supabase.table("tasks").select("*")\
        .eq("user_id", user_id).in_("status", ["open", "waiting"])\
        .order("created_at").execute().data

def db_get_closed_today(user_id):
    return supabase.table("tasks").select("*")\
        .eq("user_id", user_id).eq("status", "done")\
        .gte("closed_at", today_str()).execute().data

def db_get_done(user_id):
    return supabase.table("tasks").select("*")\
        .eq("user_id", user_id).eq("status", "done")\
        .order("closed_at", desc=True).limit(30).execute().data

def db_add_task(user_id, title, category, priority, deadline=None, notes=None, is_recurring=False, recurrence=None):
    res = supabase.table("tasks").insert({
        "user_id": user_id, "title": title, "category": category,
        "priority": priority, "deadline": deadline, "notes": notes,
        "status": "open", "is_recurring": is_recurring, "recurrence": recurrence
    }).execute()
    return res.data[0] if res.data else None

def db_close_task(task_id, user_id):
    res = supabase.table("tasks").select("*").eq("id", task_id).execute()
    if not res.data:
        return
    task = res.data[0]
    supabase.table("tasks").update({
        "status": "done", "closed_at": now_uae().isoformat()
    }).eq("id", task_id).eq("user_id", user_id).execute()
    # Recreate if recurring
    if task.get("is_recurring") and task.get("recurrence"):
        db_add_task(user_id, task["title"], task["category"], task["priority"],
                    nextdeadline(task["recurrence"]), task.get("notes"),
                    True, task["recurrence"])

def db_set_waiting(task_id, user_id):
    supabase.table("tasks").update({
        "status": "waiting", "waiting_since": now_uae().isoformat()
    }).eq("id", task_id).eq("user_id", user_id).execute()

def db_all_users():
    res = supabase.table("tasks").select("user_id").execute()
    return list(set(r["user_id"] for r in res.data))

def nextdeadline(recurrence: str) -> str:
    today = now_uae().date()
    if recurrence == "daily":
        return (today + timedelta(days=1)).isoformat()
    if recurrence.startswith("weekly:"):
        days = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6}
        target = days.get(recurrence.split(":")[1], 0)
        diff = (target - today.weekday()) % 7 or 7
        return (today + timedelta(days=diff)).isoformat()
    return (today + timedelta(days=7)).isoformat()

# ── DATABASE: PROJECTS ─────────────────────────────────────────────────────────
def db_create_project(user_id, title, category, deadline=None):
    res = supabase.table("projects").insert({
        "user_id": user_id, "title": title,
        "category": category, "deadline": deadline, "status": "active"
    }).execute()
    return res.data[0] if res.data else None

def db_add_project_stage(project_id, user_id, title, deadline=None, order_num=0):
    res = supabase.table("project_tasks").insert({
        "project_id": project_id, "user_id": user_id,
        "title": title, "deadline": deadline, "order_num": order_num, "status": "open"
    }).execute()
    return res.data[0] if res.data else None

def db_get_projects(user_id):
    return supabase.table("projects").select("*")\
        .eq("user_id", user_id).eq("status", "active").execute().data

def db_get_project_stages(project_id):
    return supabase.table("project_tasks").select("*")\
        .eq("project_id", project_id).order("order_num").execute().data

def db_close_stage(stage_id, user_id):
    supabase.table("project_tasks").update({
        "status": "done", "closed_at": now_uae().isoformat()
    }).eq("id", stage_id).eq("user_id", user_id).execute()

def db_find_project(user_id, name_part):
    projects = db_get_projects(user_id)
    name_part = name_part.lower()
    for p in projects:
        if name_part in p["title"].lower():
            return p
    return None

# ── DATABASE: NOTES ────────────────────────────────────────────────────────────
def db_add_note(user_id, content, category="other"):
    res = supabase.table("notes").insert({
        "user_id": user_id, "content": content, "category": category
    }).execute()
    return res.data[0] if res.data else None

def db_get_notes(user_id, search=None):
    q = supabase.table("notes").select("*").eq("user_id", user_id).order("created_at", desc=True)
    res = q.execute()
    if search and res.data:
        search = search.lower()
        return [n for n in res.data if search in n["content"].lower()]
    return res.data

# ── DATABASE: HABITS ───────────────────────────────────────────────────────────
def db_get_habits(user_id):
    return supabase.table("habits").select("*").eq("user_id", user_id).execute().data

def db_add_habit(user_id, title, frequency="daily"):
    res = supabase.table("habits").insert({
        "user_id": user_id, "title": title, "frequency": frequency
    }).execute()
    return res.data[0] if res.data else None

def db_log_habit(habit_id, user_id):
    try:
        supabase.table("habit_logs").insert({
            "habit_id": habit_id, "user_id": user_id, "date": today_str()
        }).execute()
        return True
    except:
        return False

def db_get_habit_logs_today(user_id):
    res = supabase.table("habit_logs").select("habit_id")\
        .eq("user_id", user_id).eq("date", today_str()).execute()
    return [r["habit_id"] for r in res.data]

def db_get_habit_logs_week(user_id):
    week_ago = (now_uae().date() - timedelta(days=6)).isoformat()
    return supabase.table("habit_logs").select("*")\
        .eq("user_id", user_id).gte("date", week_ago).execute().data

# ── DATABASE: DAY RATING ───────────────────────────────────────────────────────
def db_save_rating(user_id, rating, note=None):
    try:
        supabase.table("day_ratings").upsert({
            "user_id": user_id, "date": today_str(),
            "rating": rating, "note": note
        }).execute()
    except:
        pass

# ── AI: PARSE ──────────────────────────────────────────────────────────────────
SYSTEM_PARSE = """Ты — AI-планировщик предпринимателя Елены (Al Ain, UAE).

Категории:
  cozy      — Cozy Home: AC чистка, обслуживание, кондиционеры, сервис
  content   — Контент и съёмки: клипы, монтаж, съёмки для брендов, UGC, AI-генерация
  marketing — Маркетинг и реклама: реклама, SMM, посты, блог, Instagram, продвижение
  finance   — Финансы: оплаты, счета, бюджет
  life      — Личное: быт, покупки, семья, путешествия, билеты, личные дела
  fit       — Фитнес и здоровье: тренировки, питание, врачи
  edu       — Обучение: английский, курсы, навыки
  other     — всё остальное

Приоритеты — строго по дедлайну:
  срочно  — дедлайн СЕГОДНЯ (только сегодня, не завтра)
  важно   — дедлайн завтра или в течение недели
  обычное — нет дедлайна, или личные дела без срока (маникюр, покупки и т.д.)

Правила разделения типов:
  ЗАДАЧА (new_tasks) — действие которое нужно выполнить и закрыть
    Примеры: подготовить КП, позвонить клиенту, вернуть облако в список задач
    ВАЖНО: вернуть X, добавить X, поставить X в список = ЗАДАЧА, не заметка
  ЗАМЕТКА (new_notes) — идея, мысль, контакт, референс, НЕ требует действия прямо сейчас
    Примеры: идея для рилс, контакт Марины, референс для съёмки
  ПРОЕКТ (new_projects) — несколько этапов с общей целью
  ПРИВЫЧКА (new_habits) — регулярное действие (каждый день, каждую неделю)

Верни ТОЛЬКО валидный JSON без markdown:
{
  "new_tasks": [{"title":"...","category":"...","priority":"срочно|важно|обычное","deadline":"строка или null","notes":"или null","is_recurring":false,"recurrence":"daily|weekly:mon|null"}],
  "new_projects": [{"title":"...","category":"...","deadline":"или null","stages":["этап1","этап2"]}],
  "new_notes": [{"content":"...","category":"..."}],
  "new_habits": [{"title":"...","frequency":"daily|weekly"}],
  "close_task_ids": [числа],
  "close_stage_ids": [числа],
  "habit_done": ["название привычки"],
  "waiting_ids": [числа],
  "summary": "1-2 предложения"
}"""

def ai_parse(text, existing_tasks, existing_projects, existing_habits):
    tasks_ctx = json.dumps([{"id":t["id"],"title":t["title"]} for t in existing_tasks], ensure_ascii=False)
    proj_ctx  = json.dumps([{"id":p["id"],"title":p["title"]} for p in existing_projects], ensure_ascii=False)
    hab_ctx   = json.dumps([{"id":h["id"],"title":h["title"]} for h in existing_habits], ensure_ascii=False)
    r = claude.messages.create(
        model="claude-opus-4-5", max_tokens=1500,
        system=SYSTEM_PARSE + f"\n\nОткрытые задачи: {tasks_ctx}\nПроекты: {proj_ctx}\nПривычки: {hab_ctx}",
        messages=[{"role":"user","content":text}]
    )
    raw = r.content[0].text.replace("```json","").replace("```","").strip()
    return json.loads(raw)

# ── AI: BRIEFING ───────────────────────────────────────────────────────────────
def ai_morning(open_tasks, closed_today, projects, habits, habit_logs_today):
    urgent = [t for t in open_tasks if t["priority"] == "срочно"]
    proj_summary = []
    for p in projects:
        stages = db_get_project_stages(p["id"])
        done = sum(1 for s in stages if s["status"] == "done")
        proj_summary.append(f"{p['title']}: {done}/{len(stages)} этапов")
    habits_status = []
    for h in habits:
        done = h["id"] in habit_logs_today
        habits_status.append(f"{'✅' if done else '⬜'} {h['title']}")
    prompt = f"""Утро Елены. Напиши:
1. Персональную мотивационную фразу (1-2 предложения) — основана на её задачах сегодня, живая и конкретная, не банальная
2. Топ-3 приоритета на день
3. Что можно отложить

Данные:
Открытых задач: {len(open_tasks)} | Срочных: {len(urgent)} | Закрыто вчера: {len(closed_today)}
Активные проекты: {', '.join(proj_summary) if proj_summary else 'нет'}
Привычки: {', '.join(habits_status) if habits_status else 'не настроены'}

Задачи: {json.dumps([{"title":t["title"],"priority":t["priority"],"deadline":t.get("deadline")} for t in open_tasks[:10]], ensure_ascii=False)}

Формат ответа:
✨ [мотивационная фраза]

🎯 Топ-3 сегодня:
— ...
— ...
— ...

⏭ Можно отложить: ...

Стиль: по-русски, коротко, конкретно."""
    r = claude.messages.create(model="claude-opus-4-5", max_tokens=500,
                               messages=[{"role":"user","content":prompt}])
    return r.content[0].text

def ai_midday(open_tasks, closed_today):
    urgent = [t for t in open_tasks if t["priority"] == "срочно"]
    prompt = f"""Дневное напоминание Елены (13:00). Коротко, по делу.

Открыто: {len(open_tasks)} | Срочных: {len(urgent)} | Закрыто сегодня: {len(closed_today)}
Срочные: {json.dumps([t["title"] for t in urgent], ensure_ascii=False)}

Напиши: что нужно закрыть до вечера, один конкретный совет. По-русски, 3-4 предложения."""
    r = claude.messages.create(model="claude-opus-4-5", max_tokens=300,
                               messages=[{"role":"user","content":prompt}])
    return r.content[0].text

def ai_evening(open_tasks, closed_today, habits, habit_logs_today):
    habits_status = []
    for h in habits:
        done = h["id"] in habit_logs_today
        habits_status.append(f"{'✅' if done else '❌'} {h['title']}")
    prompt = f"""Вечерний итог Елены (21:00).
Закрыто сегодня: {len(closed_today)} задач: {json.dumps([t["title"] for t in closed_today], ensure_ascii=False)}
Осталось открытых: {len(open_tasks)}
Привычки сегодня: {', '.join(habits_status) if habits_status else 'не настроены'}

Напиши:
— Что молодец (конкретно по закрытым)
— Топ-3 на завтра
— Одна ободряющая фраза в конце
По-русски, тепло но без сюсюканья."""
    r = claude.messages.create(model="claude-opus-4-5", max_tokens=400,
                               messages=[{"role":"user","content":prompt}])
    return r.content[0].text

def ai_weekly(open_tasks, closed_week, projects):
    prompt = f"""Еженедельный обзор Елены (воскресенье).
Закрыто за неделю: {len(closed_week)}
Открытых сейчас: {len(open_tasks)}
Задачи старше 7 дней: {json.dumps([t["title"] for t in open_tasks if t.get("created_at","") < (now_uae()-timedelta(days=7)).isoformat()[:10]], ensure_ascii=False)}
Проекты: {json.dumps([p["title"] for p in projects], ensure_ascii=False)}

Напиши еженедельный обзор:
— Итог недели (цифры + оценка)
— Что буксует (задачи которые давно висят)
— Топ-3 фокуса на следующую неделю
— Одна стратегическая мысль
По-русски, структурированно."""
    r = claude.messages.create(model="claude-opus-4-5", max_tokens=500,
                               messages=[{"role":"user","content":prompt}])
    return r.content[0].text

# ── VOICE ──────────────────────────────────────────────────────────────────────
async def transcribe(bot, file_id):
    tg_file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        with open(tmp.name, "rb") as audio:
            result = oai.audio.transcriptions.create(model="whisper-1", file=audio, language="ru")
    return result.text

# ── FORMATTERS ─────────────────────────────────────────────────────────────────
def fmt_tasks(tasks):
    if not tasks:
        return "_Задач нет ✨_"
    by_cat = {}
    for t in tasks:
        by_cat.setdefault(t.get("category","other"), []).append(t)
    lines = []
    for cat, items in by_cat.items():
        lines.append(f"\n{CAT_LABEL.get(cat,'📌 Другое')}")
        for t in items:
            p  = PRIO_EMOJI.get(t.get("priority","обычное"), "⚪")
            st = "⏳ " if t.get("status") == "waiting" else ""
            dl = f" {t['deadline']}" if t.get("deadline") else ""
            rc = " 🔄" if t.get("is_recurring") else ""
            lines.append(f"{p} {st}{t['title']}{dl}{rc}  /c{t['id']}")
    return "\n".join(lines)

def fmt_projects(user_id):
    projects = db_get_projects(user_id)
    if not projects:
        return "_Активных проектов нет_"
    lines = []
    for p in projects:
        stages = db_get_project_stages(p["id"])
        done = sum(1 for s in stages if s["status"] == "done")
        total = len(stages)
        bar = "▓" * done + "░" * (total - done)
        lines.append(f"\n📁 {p['title']} [{done}/{total}] {bar}")
        for s in stages:
            st = "✅" if s["status"] == "done" else "▸"
            dl = f" {s['deadline']}" if s.get("deadline") else ""
            lines.append(f"  {st} {s['title']}{dl}" + ("" if s["status"]=="done" else f"  /cs{s['id']}"))
    return "\n".join(lines)

def fmt_habits(user_id):
    habits = db_get_habits(user_id)
    if not habits:
        return "_Привычки не настроены. Напиши: «Добавь привычку тренировка каждый день»_"
    logs_today = db_get_habit_logs_today(user_id)
    logs_week  = db_get_habit_logs_week(user_id)
    lines = []
    for h in habits:
        done_today = h["id"] in logs_today
        week_dates = [l["date"] for l in logs_week if l["habit_id"] == h["id"]]
        week_days  = []
        for i in range(6, -1, -1):
            d = (now_uae().date() - timedelta(days=i)).isoformat()
            week_days.append("✅" if d in week_dates else "⬜")
        week_str = "".join(week_days)
        status = "✅ сделано" if done_today else f"⬜ /h{h['id']}"
        lines.append(f"*{h['title']}* — {status}\n  Неделя: {week_str}")
    return "\n\n".join(lines)

# ── PROCESS INPUT ──────────────────────────────────────────────────────────────
async def process_input(uid, text, update):
    existing_tasks    = db_get_open(uid)
    existing_projects = db_get_projects(uid)
    existing_habits   = db_get_habits(uid)
    result = ai_parse(text, existing_tasks, existing_projects, existing_habits)

    added_tasks = []
    added_projects = []
    added_notes = []
    added_habits = []
    closed_n = 0
    waiting_n = 0

    # New tasks
    for t in result.get("new_tasks", []):
        task = db_add_task(uid, t["title"], t.get("category","other"),
                           t.get("priority","обычное"), t.get("deadline"),
                           t.get("notes"), t.get("is_recurring", False), t.get("recurrence"))
        if task:
            added_tasks.append(task)

    # New projects
    for p in result.get("new_projects", []):
        project = db_create_project(uid, p["title"], p.get("category","other"), p.get("deadline"))
        if project:
            added_projects.append(project)
            for i, stage_title in enumerate(p.get("stages", [])):
                db_add_project_stage(project["id"], uid, stage_title, order_num=i)

    # New notes
    for n in result.get("new_notes", []):
        note = db_add_note(uid, n["content"], n.get("category","other"))
        if note:
            added_notes.append(note)

    # New habits
    for h in result.get("new_habits", []):
        habit = db_add_habit(uid, h["title"], h.get("frequency","daily"))
        if habit:
            added_habits.append(habit)

    # Close tasks
    for tid in result.get("close_task_ids", []):
        db_close_task(int(tid), uid)
        closed_n += 1

    # Close project stages
    for sid in result.get("close_stage_ids", []):
        db_close_stage(int(sid), uid)
        closed_n += 1

    # Mark waiting
    for tid in result.get("waiting_ids", []):
        db_set_waiting(int(tid), uid)
        waiting_n += 1

    # Habit completions
    for habit_name in result.get("habit_done", []):
        for h in existing_habits:
            if habit_name.lower() in h["title"].lower():
                db_log_habit(h["id"], uid)

    # Build reply
    parts = []
    if added_tasks:
        parts.append(f"✅ Задач добавлено: {len(added_tasks)}\n" +
                     "\n".join(f"{PRIO_EMOJI.get(t['priority'],'⚪')} {t['title']}" for t in added_tasks))
    if added_projects:
        parts.append(f"📁 Проектов создано: {len(added_projects)}\n" +
                     "\n".join(f"• {p['title']}" for p in added_projects))
    if added_notes:
        parts.append(f"📝 Записано заметок: {len(added_notes)}")
    if added_habits:
        parts.append(f"🔄 Привычек добавлено: {len(added_habits)}\n" +
                     "\n".join(f"• {h['title']}" for h in added_habits))
    if closed_n:
        parts.append(f"✅ Закрыто: {closed_n}")
    if waiting_n:
        parts.append(f"⏳ В ожидании: {waiting_n}")
    if result.get("summary"):
        parts.append(f"\n_{result['summary']}_")

    if not parts:
        parts.append("Не распознала задач. Попробуй переформулировать.")

    await update.message.reply_text("\n\n".join(parts), parse_mode="Markdown")

# ── COMMAND HANDLERS ───────────────────────────────────────────────────────────
async def cmd_start(update, _ctx):
    await update.message.reply_text(
        "👋 Привет, Лена!\n\n"
        "Пиши или надиктовывай поток — разберу сама.\n\n"
        "*Команды:*\n"
        "/tasks — открытые задачи\n"
        "/done — закрыто сегодня\n"
        "/projects — активные проекты\n"
        "/notes — заметки\n"
        "/habits — привычки\n"
        "/briefing — брифинг прямо сейчас\n"
        "/report — полный отчёт\n\n"
        "*Закрыть задачу:* /c123\n"
        "*Закрыть этап проекта:* /cs123\n"
        "*Отметить привычку:* /h123\n"
        "*Жду ответа:* /w123\n"
        "*Оценить день:* /rate 4\n\n"
        "🌅 07:00 — утренний брифинг\n"
        "☀️ 13:00 — дневное напоминание\n"
        "🌙 21:00 — вечерний итог\n"
        "📊 вс 19:00 — недельный обзор",
        parse_mode="Markdown"
    )

async def cmd_tasks(update, _ctx):
    tasks = db_get_open(update.effective_user.id)
    await update.message.reply_text(
        f"📋 Открытые задачи — {len(tasks)} шт.\n{fmt_tasks(tasks)}",
        parse_mode="Markdown"
    )

async def cmd_done(update, _ctx):
    tasks = db_get_closed_today(update.effective_user.id)
    if not tasks:
        await update.message.reply_text("Сегодня ещё ничего не закрыто 🌱")
        return
    await update.message.reply_text(
        f"✅ Закрыто сегодня — {len(tasks)} шт.:\n\n" +
        "\n".join(f"✅ {t['title']}" for t in tasks),
        parse_mode="Markdown"
    )

async def cmd_projects(update, _ctx):
    text = fmt_projects(update.effective_user.id)
    await update.message.reply_text(f"📁 Проекты:\n{text}", parse_mode="Markdown")

async def cmd_notes(update, ctx):
    uid   = update.effective_user.id
    args  = ctx.args
    search = " ".join(args) if args else None
    notes = db_get_notes(uid, search)
    if not notes:
        msg = "Заметок не найдено" if search else "Заметок пока нет"
        await update.message.reply_text(f"📝 {msg}")
        return
    by_cat = {}
    for n in notes[:20]:
        by_cat.setdefault(n.get("category","other"), []).append(n)
    lines = [f"📝 Заметки{' по «'+search+'»' if search else ''} — {len(notes)} шт."]
    for cat, items in by_cat.items():
        lines.append(f"\n{CAT_LABEL.get(cat,'📌 Другое')}")
        for n in items:
            date = n["created_at"][:10]
            lines.append(f"• {n['content']} {date}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_habits(update, _ctx):
    text = fmt_habits(update.effective_user.id)
    await update.message.reply_text(f"🔄 Привычки:\n\n{text}", parse_mode="Markdown")

async def cmd_briefing(update, _ctx):
    uid = update.effective_user.id
    await update.message.reply_text("⏳ Анализирую...")
    tasks   = db_get_open(uid)
    closed  = db_get_closed_today(uid)
    projects = db_get_projects(uid)
    habits  = db_get_habits(uid)
    logs    = db_get_habit_logs_today(uid)
    text    = ai_morning(tasks, closed, projects, habits, logs)
    await update.message.reply_text(f"🌅 Брифинг дня\n\n{text}", parse_mode="Markdown")

async def cmd_report(update, _ctx):
    uid = update.effective_user.id
    await update.message.reply_text("⏳ Формирую отчёт...")
    open_tasks   = db_get_open(uid)
    closed_tasks = db_get_done(uid)
    closed_today = db_get_closed_today(uid)
    projects     = db_get_projects(uid)
    proj_lines = []
    for p in projects:
        stages = db_get_project_stages(p["id"])
        done = sum(1 for s in stages if s["status"] == "done")
        proj_lines.append(f"📁 {p['title']}: {done}/{len(stages)} этапов")
    text = (
        f"📊 Полный отчёт\n\n"
        f"Открытых задач: {len(open_tasks)}\n"
        f"Закрыто сегодня: {len(closed_today)}\n"
        f"Всего закрыто: {len(closed_tasks)}\n\n"
    )
    if proj_lines:
        text += "*Проекты:*\n" + "\n".join(proj_lines) + "\n\n"
    text += f"*Открытые задачи:*\n{fmt_tasks(open_tasks)}"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_rate(update, ctx):
    uid = update.effective_user.id
    try:
        rating = int(ctx.args[0]) if ctx.args else 0
        if not 1 <= rating <= 5:
            raise ValueError
        stars = "⭐" * rating + "☆" * (5 - rating)
        db_save_rating(uid, rating)
        await update.message.reply_text(f"День оценён: {stars} ({rating}/5) 📈")
    except:
        await update.message.reply_text("Используй: /rate 4 (от 1 до 5)")

# ── TEXT HANDLER ───────────────────────────────────────────────────────────────
async def handle_text(update, ctx):
    text = update.message.text or ""
    uid  = update.effective_user.id

    # Close task
    if text.startswith("/c") and not text.startswith("/cs"):
        try:
            db_close_task(int(text[2:].strip()), uid)
            await update.message.reply_text("✅ Задача закрыта!")
        except Exception as e:
            await update.message.reply_text(f"Не удалось: {e}")
        return

    # Close project stage
    if text.startswith("/cs"):
        try:
            db_close_stage(int(text[3:].strip()), uid)
            await update.message.reply_text("✅ Этап закрыт!")
        except Exception as e:
            await update.message.reply_text(f"Не удалось: {e}")
        return

    # Mark habit done
    if text.startswith("/h"):
        try:
            habit_id = int(text[2:].strip())
            done = db_log_habit(habit_id, uid)
            await update.message.reply_text("✅ Привычка отмечена!" if done else "Уже отмечена сегодня")
        except Exception as e:
            await update.message.reply_text(f"Не удалось: {e}")
        return

    # Mark waiting
    if text.startswith("/w"):
        try:
            db_set_waiting(int(text[2:].strip()), uid)
            await update.message.reply_text("⏳ Задача переведена в ожидание")
        except Exception as e:
            await update.message.reply_text(f"Не удалось: {e}")
        return

    # Process as AI input
    await update.message.reply_text("🤔 Разбираю...")
    try:
        await process_input(uid, text, update)
    except Exception as e:
        log.error(e)
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_voice(update, ctx):
    await update.message.reply_text("🎤 Расшифровываю...")
    try:
        text = await transcribe(ctx.bot, update.message.voice.file_id)
        await update.message.reply_text(f"📝 {text}", parse_mode="Markdown")
        await update.message.reply_text("🤔 Разбираю...")
        await process_input(update.effective_user.id, text, update)
    except Exception as e:
        log.error(e)
        await update.message.reply_text(f"❌ Ошибка голоса: {e}")

# ── SCHEDULED JOBS ─────────────────────────────────────────────────────────────
async def job_morning(ctx):
    for uid in db_all_users():
        try:
            tasks    = db_get_open(uid)
            closed   = db_get_closed_today(uid)
            projects = db_get_projects(uid)
            habits   = db_get_habits(uid)
            logs     = db_get_habit_logs_today(uid)
            text     = ai_morning(tasks, closed, projects, habits, logs)
            await ctx.bot.send_message(chat_id=uid,
                text=f"🌅 Доброе утро, Лена!\n\n{text}", parse_mode="Markdown")
        except Exception as e:
            log.error(f"Morning job {uid}: {e}")

async def job_midday(ctx):
    for uid in db_all_users():
        try:
            tasks  = db_get_open(uid)
            closed = db_get_closed_today(uid)
            if not tasks:
                continue
            text = ai_midday(tasks, closed)
            await ctx.bot.send_message(chat_id=uid,
                text=f"☀️ Дневное напоминание:\n\n{text}", parse_mode="Markdown")
        except Exception as e:
            log.error(f"Midday job {uid}: {e}")

async def job_evening(ctx):
    for uid in db_all_users():
        try:
            tasks   = db_get_open(uid)
            closed  = db_get_closed_today(uid)
            habits  = db_get_habits(uid)
            logs    = db_get_habit_logs_today(uid)
            text    = ai_evening(tasks, closed, habits, logs)
            msg     = f"🌙 Итог дня:\n\n{text}\n\nОцени день: /rate 1-5"
            await ctx.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Evening job {uid}: {e}")

async def job_weekly(ctx):
    for uid in db_all_users():
        try:
            open_tasks   = db_get_open(uid)
            week_ago     = (now_uae().date() - timedelta(days=7)).isoformat()
            closed_week  = supabase.table("tasks").select("*")\
                .eq("user_id", uid).eq("status","done").gte("closed_at", week_ago).execute().data
            projects     = db_get_projects(uid)
            text         = ai_weekly(open_tasks, closed_week, projects)
            await ctx.bot.send_message(chat_id=uid,
                text=f"📊 Недельный обзор:\n\n{text}", parse_mode="Markdown")
        except Exception as e:
            log.error(f"Weekly job {uid}: {e}")

async def job_deadline_check(ctx):
    """Проверяет дедлайны и напоминает"""
    tomorrow = (now_uae().date() + timedelta(days=1)).isoformat()
    today    = today_str()
    for uid in db_all_users():
        try:
            tasks = db_get_open(uid)
            due_today     = [t for t in tasks if t.get("deadline") == today]
            due_tomorrow  = [t for t in tasks if t.get("deadline") == tomorrow]
            if due_today:
                lines = "\n".join(f"🔴 {t['title']}" for t in due_today)
                await ctx.bot.send_message(chat_id=uid,
                    text=f"⚠️ Дедлайн СЕГОДНЯ:\n{lines}", parse_mode="Markdown")
            if due_tomorrow:
                lines = "\n".join(f"🟡 {t['title']}" for t in due_tomorrow)
                await ctx.bot.send_message(chat_id=uid,
                    text=f"📅 Дедлайн ЗАВТРА:\n{lines}", parse_mode="Markdown")
        except Exception as e:
            log.error(f"Deadline check {uid}: {e}")

async def job_waiting_check(ctx):
    """Напоминает о задачах в ожидании больше 2 дней"""
    two_days_ago = (now_uae() - timedelta(days=2)).isoformat()
    for uid in db_all_users():
        try:
            res = supabase.table("tasks").select("*")\
                .eq("user_id", uid).eq("status","waiting")\
                .lte("waiting_since", two_days_ago).execute()
            if res.data:
                lines = "\n".join(f"⏳ {t['title']}" for t in res.data)
                await ctx.bot.send_message(chat_id=uid,
                    text=f"🔔 Жду ответа — прошло 2+ дня:\n{lines}\n\nПолучила ответ? Закрой /cXXX или продолжи ждать.",
                    parse_mode="Markdown")
        except Exception as e:
            log.error(f"Waiting check {uid}: {e}")

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("tasks",    cmd_tasks))
    app.add_handler(CommandHandler("done",     cmd_done))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("notes",    cmd_notes))
    app.add_handler(CommandHandler("habits",   cmd_habits))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("report",   cmd_report))
    app.add_handler(CommandHandler("rate",     cmd_rate))

    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT,  handle_text))

    jq = app.job_queue
    jq.run_daily(job_morning,        time=time(7,  0,  tzinfo=UAE_TZ))
    jq.run_daily(job_midday,         time=time(13, 0,  tzinfo=UAE_TZ))
    jq.run_daily(job_evening,        time=time(21, 0,  tzinfo=UAE_TZ))
    jq.run_daily(job_deadline_check, time=time(9,  0,  tzinfo=UAE_TZ))
    jq.run_daily(job_deadline_check, time=time(18, 0,  tzinfo=UAE_TZ))
    jq.run_daily(job_waiting_check,  time=time(10, 0,  tzinfo=UAE_TZ))
    # Weekly review — Sunday 19:00
    jq.run_daily(job_weekly,         time=time(19, 0,  tzinfo=UAE_TZ),
                 days=(6,))  # 6 = Sunday

    log.info("🤖 Bot v2 is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
