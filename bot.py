import os
import json
import logging
import tempfile
from datetime import datetime, time

import pytz
from anthropic import Anthropic
from openai import OpenAI
from supabase import create_client, Client
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

BOT_TOKEN        = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
OPENAI_KEY       = os.environ["OPENAI_API_KEY"]
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_KEY"]
UAE_TZ           = pytz.timezone("Asia/Dubai")

claude   = Anthropic(api_key=ANTHROPIC_KEY)
oai      = OpenAI(api_key=OPENAI_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

CAT_LABEL  = {"cozy": "🏠 Cozy Home", "ai": "🎬 AI-контент", "life": "👤 Личное", "fit": "💪 Фитнес", "other": "📌 Другое"}
PRIO_EMOJI = {"срочно": "🔴", "важно": "🟡", "обычное": "⚪"}

def db_get_open(user_id):
    return supabase.table("tasks").select("*").eq("user_id", user_id).eq("status", "open").order("created_at").execute().data

def db_get_done(user_id):
    return supabase.table("tasks").select("*").eq("user_id", user_id).eq("status", "done").order("closed_at", desc=True).limit(20).execute().data

def db_get_closed_today(user_id):
    today = datetime.now(UAE_TZ).date().isoformat()
    return supabase.table("tasks").select("*").eq("user_id", user_id).eq("status", "done").gte("closed_at", today).execute().data

def db_add_task(user_id, title, category, priority, deadline=None, notes=None):
    res = supabase.table("tasks").insert({"user_id": user_id, "title": title, "category": category, "priority": priority, "deadline": deadline, "notes": notes, "status": "open"}).execute()
    return res.data[0] if res.data else None

def db_close_task(task_id, user_id):
    supabase.table("tasks").update({"status": "done", "closed_at": datetime.now(UAE_TZ).isoformat()}).eq("id", task_id).eq("user_id", user_id).execute()

def db_all_users():
    res = supabase.table("tasks").select("user_id").execute()
    return list(set(r["user_id"] for r in res.data))

SYSTEM_PARSE = """Ты — AI-планировщик предпринимателя Елены (Al Ain, UAE). Категории: cozy (Cozy Home), ai (AI-контент), life (личное), fit (фитнес), other. Приоритеты: срочно (сегодня/завтра), важно (эта неделя), обычное. Из потока сознания извлеки задачи и закрытия. Верни ТОЛЬКО JSON без markdown: {"new_tasks":[{"title":"...","category":"cozy|ai|life|fit|other","priority":"срочно|важно|обычное","deadline":"строка или null","notes":"строка или null"}],"close_ids":[],"summary":"1-2 предложения"}"""

def ai_parse(text, existing):
    ctx = json.dumps([{"id": t["id"], "title": t["title"]} for t in existing], ensure_ascii=False)
    r = claude.messages.create(model="claude-opus-4-5", max_tokens=1000, system=SYSTEM_PARSE + f"\nОткрытые задачи:\n{ctx}", messages=[{"role": "user", "content": text}])
    raw = r.content[0].text.replace("```json","").replace("```","").strip()
    return json.loads(raw)

def ai_briefing(open_tasks, closed_today):
    urgent = [t for t in open_tasks if t["priority"] == "срочно"]
    tasks_json = json.dumps([{"title": t["title"], "cat": t["category"], "priority": t["priority"], "deadline": t.get("deadline")} for t in open_tasks[:15]], ensure_ascii=False)
    prompt = f"Утренний брифинг Елены. Чётко, без воды.\nОткрыто: {len(open_tasks)} | Срочных: {len(urgent)} | Закрыто сегодня: {closed_today}\nЗадачи: {tasks_json}\nНапиши: топ-3 приоритета, что отложить, один совет. По-русски, коротко."
    r = claude.messages.create(model="claude-opus-4-5", max_tokens=500, messages=[{"role": "user", "content": prompt}])
    return r.content[0].text

async def transcribe(bot, file_id):
    tg_file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        with open(tmp.name, "rb") as audio:
            result = oai.audio.transcriptions.create(model="whisper-1", file=audio, language="ru")
    return result.text

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
            p = PRIO_EMOJI.get(t.get("priority","обычное"),"⚪")
            dl = f"  _{t['deadline']}_" if t.get("deadline") else ""
            lines.append(f"{p} {t['title']}{dl}  /c{t['id']}")
    return "\n".join(lines)

async def cmd_start(update, _ctx):
    await update.message.reply_text("👋 Привет, Лена!\n\nПиши или надиктовывай поток — разберу и добавлю задачи.\n\n/tasks — открытые задачи\n/done — закрытые\n/briefing — анализ дня\n\nЗакрыть задачу: нажми /cXXX рядом с ней.")

async def cmd_tasks(update, _ctx):
    tasks = db_get_open(update.effective_user.id)
    await update.message.reply_text(f"📋 *Открытые задачи* — {len(tasks)} шт.\n{fmt_tasks(tasks)}", parse_mode="Markdown")

async def cmd_done(update, _ctx):
    tasks = db_get_done(update.effective_user.id)
    if not tasks:
        await update.message.reply_text("Ещё ничего не закрыто 🌱")
        return
    await update.message.reply_text("✅ *Закрытые:*\n\n" + "\n".join(f"✅ {t['title']}" for t in tasks[:15]), parse_mode="Markdown")

async def cmd_briefing(update, _ctx):
    uid = update.effective_user.id
    await update.message.reply_text("⏳ Анализирую день...")
    tasks = db_get_open(uid)
    closed = db_get_closed_today(uid)
    text = ai_briefing(tasks, len(closed))
    await update.message.reply_text(f"🌅 *Брифинг дня*\n\n{text}", parse_mode="Markdown")

async def process_text(uid, text, update):
    existing = db_get_open(uid)
    result = ai_parse(text, existing)
    added = []
    for t in result.get("new_tasks", []):
        task = db_add_task(uid, t["title"], t.get("category","other"), t.get("priority","обычное"), t.get("deadline"), t.get("notes"))
        if task:
            added.append(task)
    closed_n = 0
    for tid in result.get("close_ids", []):
        db_close_task(int(tid), uid)
        closed_n += 1
    reply = f"✅ Добавлено: *{len(added)}*"
    if closed_n:
        reply += f"  |  Закрыто: *{closed_n}*"
    if result.get("summary"):
        reply += f"\n\n_{result['summary']}_"
    if added:
        reply += "\n\n*Новые задачи:*\n" + "\n".join(f"{PRIO_EMOJI.get(t['priority'],'⚪')} {t['title']}" for t in added)
    await update.message.reply_text(reply, parse_mode="Markdown")

async def handle_text(update, ctx):
    text = update.message.text or ""
    if text.startswith("/c"):
        try:
            task_id = int(text.replace("/c","").strip())
            db_close_task(task_id, update.effective_user.id)
            await update.message.reply_text("✅ Задача закрыта!")
        except Exception as e:
            await update.message.reply_text(f"Не удалось: {e}")
        return
    await update.message.reply_text("🤔 Разбираю...")
    try:
        await process_text(update.effective_user.id, text, update)
    except Exception as e:
        log.error(e)
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_voice(update, ctx):
    await update.message.reply_text("🎤 Расшифровываю...")
    try:
        text = await transcribe(ctx.bot, update.message.voice.file_id)
        await update.message.reply_text(f"📝 _{text}_", parse_mode="Markdown")
        await update.message.reply_text("🤔 Разбираю...")
        await process_text(update.effective_user.id, text, update)
    except Exception as e:
        log.error(e)
        await update.message.reply_text(f"❌ Ошибка голоса: {e}")

async def morning_job(ctx):
    for uid in db_all_users():
        try:
            tasks = db_get_open(uid)
            closed = db_get_closed_today(uid)
            text = ai_briefing(tasks, len(closed))
            await ctx.bot.send_message(chat_id=uid, text=f"🌅 *Доброе утро! Брифинг дня:*\n\n{text}", parse_mode="Markdown")
        except Exception as e:
            log.error(f"Briefing failed for {uid}: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.job_queue.run_daily(morning_job, time=time(hour=7, minute=0, tzinfo=UAE_TZ))
    log.info("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
