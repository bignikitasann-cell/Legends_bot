import os
import json
import sqlite3
import logging
import requests
import asyncio
import random
import time
import re
import threading
from datetime import timedelta
from collections import defaultdict
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from gtts import gTTS
import tempfile

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")
WEATHER_KEY = os.getenv("WEATHER_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
MAX_HISTORY = 20
MAX_STICKERS = 500
CURRENT_MODEL = "openai/gpt-oss-120b:free"
YOUR_CHAT_ID = 1470260290
ONLY_ME = False
START_TIME = time.time()

SYSTEM_PROMPT = """Ты — Легенда, живая, дерзкая и остроумная собеседница.
Ты отвечаешь с лёгкой иронией, но всегда по делу.
Используй эмодзи, чтобы делать ответы ярче.
Будь дружелюбной и харизматичной."""

# === БАЗЫ ДАННЫХ ===
def init_dbs():
    conn = sqlite3.connect("stickers.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stickers (id INTEGER PRIMARY KEY, emoji TEXT, file_id TEXT UNIQUE)''')
    c.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    
    conn = sqlite3.connect("reminders.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (user_id INTEGER, chat_id INTEGER, time INTEGER, text TEXT)''')
    c.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    
    conn = sqlite3.connect("stats.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stats (user_id INTEGER, command TEXT, count INTEGER DEFAULT 1, UNIQUE(user_id, command))''')
    c.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    
    conn = sqlite3.connect("anon.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user1 INTEGER, user2 INTEGER, active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS waiting (user_id INTEGER PRIMARY KEY)''')
    c.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    
    if not os.path.exists("chat_history.json"):
        with open("chat_history.json", "w") as f:
            json.dump({}, f)

init_dbs()

def save_history(chat_id, messages):
    try:
        with open("chat_history.json", "r") as f:
            data = json.load(f)
    except:
        data = {}
    data[str(chat_id)] = messages[-MAX_HISTORY:]
    with open("chat_history.json", "w") as f:
        json.dump(data, f)

def load_history(chat_id):
    try:
        with open("chat_history.json", "r") as f:
            data = json.load(f)
            return data.get(str(chat_id), [])
    except:
        return []

def increment_stat(user_id, command):
    conn = sqlite3.connect("stats.db")
    c = conn.cursor()
    c.execute("INSERT INTO stats (user_id, command, count) VALUES (?, ?, 1) ON CONFLICT(user_id, command) DO UPDATE SET count = count + 1", (user_id, command))
    conn.commit()
    conn.close()

def save_sticker(emoji, file_id):
    if count_stickers() >= MAX_STICKERS:
        return False
    conn = sqlite3.connect("stickers.db")
    c = conn.cursor()
    try:
        c.execute("INSERT INTO stickers (emoji, file_id) VALUES (?, ?)", (emoji, file_id))
        conn.commit()
        return True
    except:
        return False
    finally:
        conn.close()

def count_stickers():
    conn = sqlite3.connect("stickers.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM stickers")
    cnt = c.fetchone()[0]
    conn.close()
    return cnt

def get_random_sticker():
    conn = sqlite3.connect("stickers.db")
    c = conn.cursor()
    c.execute("SELECT file_id FROM stickers ORDER BY RANDOM() LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_weather(city):
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather"
        params = {"q": city, "appid": WEATHER_KEY, "units": "metric", "lang": "ru"}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return f"🌍 *{city.title()}*\n🌡️ {data['main']['temp']}°C\n☁️ {data['weather'][0]['description']}"
        return f"❌ Город '{city}' не найден"
    except:
        return "❌ Ошибка погоды"

def search_tavily(query):
    try:
        url = "https://api.tavily.com/search"
        payload = {"api_key": TAVILY_KEY, "query": query, "search_depth": "basic", "max_results": 3, "include_answer": True}
        r = requests.post(url, json=payload, timeout=30)
        return r.json()
    except:
        return {"error": "Ошибка поиска"}

user_model = defaultdict(lambda: CURRENT_MODEL)

def ask_ai(prompt, chat_id):
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
        history = load_history(chat_id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": prompt})
        
        data = {"model": user_model[chat_id], "messages": messages, "max_tokens": 500}
        r = requests.post(url, headers=headers, json=data, timeout=60)
        r.raise_for_status()
        reply = r.json()['choices'][0]['message']['content']
        
        new_history = history + [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}]
        save_history(chat_id, new_history)
        return reply
    except Exception as e:
        logger.error(f"AI error: {e}")
        return "😔 Нежно обнимаю... но что-то пошло не так. Давай ещё разок?"

async def check_reminders(app):
    while True:
        now = int(time.time())
        conn = sqlite3.connect("reminders.db")
        c = conn.cursor()
        c.execute("SELECT user_id, chat_id, text FROM reminders WHERE time <= ?", (now,))
        rows = c.fetchall()
        c.execute("DELETE FROM reminders WHERE time <= ?", (now,))
        conn.commit()
        conn.close()
        for user_id, chat_id, text in rows:
            try:
                await app.bot.send_message(chat_id, f"🔔 *Напоминание:* {text}", parse_mode="Markdown")
            except:
                pass
        await asyncio.sleep(10)

def add_waiting(user_id):
    conn = sqlite3.connect("anon.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO waiting (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_waiting(user_id):
    conn = sqlite3.connect("anon.db")
    c = conn.cursor()
    c.execute("DELETE FROM waiting WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_waiting_list(exclude_id):
    conn = sqlite3.connect("anon.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM waiting WHERE user_id != ?", (exclude_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def create_session(user1, user2):
    conn = sqlite3.connect("anon.db")
    c = conn.cursor()
    c.execute("INSERT INTO sessions (user1, user2, active) VALUES (?, ?, 1)", (user1, user2))
    conn.commit()
    conn.close()
    remove_waiting(user1)
    remove_waiting(user2)

def get_partner(user_id):
    conn = sqlite3.connect("anon.db")
    c = conn.cursor()
    c.execute("SELECT user1, user2 FROM sessions WHERE active=1 AND (user1=? OR user2=?)", (user_id, user_id))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0] if row[1] == user_id else row[1]
    return None

def end_session(user_id):
    conn = sqlite3.connect("anon.db")
    c = conn.cursor()
    c.execute("UPDATE sessions SET active=0 WHERE (user1=? OR user2=?) AND active=1", (user_id, user_id))
    conn.commit()
    conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    increment_stat(update.effective_user.id, "/start")
    
    text = """✨ *Привет! Я — Легенда*

*📌 Команды:*
/start — это сообщение
/help — помощь
/reset — очистить историю
/model — сменить модель ИИ
/draw [описание] — сгенерировать изображение
/weather [город] — погода
/search [запрос] — поиск
/sticker — случайный стикер
/chat — анонимный чат
/stop_chat — выйти из чата
/stats — статистика
/status — статус бота

*🌤️ Примеры:*
`погода Москва`
`найди новости`

Теперь просто пиши — и продолжим 😊"""
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = int(time.time() - START_TIME)
    uptime_str = str(timedelta(seconds=uptime))
    sticker_count = count_stickers()
    await update.message.reply_text(f"📊 *Статус*\n⏱️ Время работы: {uptime_str}\n😊 Стикеров: {sticker_count}\n🤖 Бот: ✅ работает", parse_mode="Markdown")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_history(chat_id, [])
    await update.message.reply_text("🧹 История очищена!")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text(f"🤖 Текущая модель: {user_model[chat_id]}\n/gpt /claude /gemini", parse_mode="Markdown")
        return
    cmd = args[0].lower()
    if cmd in ["gpt", "/gpt"]:
        user_model[chat_id] = "openai/gpt-oss-120b:free"
        await update.message.reply_text("✅ Переключено на GPT")
    elif cmd in ["claude", "/claude"]:
        user_model[chat_id] = "anthropic/claude-3-haiku:free"
        await update.message.reply_text("✅ Переключено на Claude")
    elif cmd in ["gemini", "/gemini"]:
        user_model[chat_id] = "google/gemini-2.0-flash-exp:free"
        await update.message.reply_text("✅ Переключено на Gemini")

async def draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = ' '.join(context.args)
    if not prompt:
        await update.message.reply_text("🎨 *Пример:* `/draw кот в космосе`", parse_mode="Markdown")
        return
    await update.message.reply_text("🎨 Генерирую изображение...")
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
        data = {"model": "black-forest-labs/flux-schnell:free", "messages": [{"role": "user", "content": f"Generate an image: {prompt}"}]}
        r = requests.post(url, headers=headers, json=data, timeout=60)
        if r.status_code == 200:
            image_url = r.json()['choices'][0]['message']['content']
            await update.message.reply_photo(image_url, caption=f"✨ {prompt}")
        else:
            await update.message.reply_text("❌ Ошибка генерации")
    except:
        await update.message.reply_text("❌ Ошибка")

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = ' '.join(context.args)
    if not city:
        await update.message.reply_text("🌤️ *Пример:* `/weather Москва`", parse_mode="Markdown")
        return
    await update.message.reply_text(get_weather(city), parse_mode="Markdown")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = ' '.join(context.args)
    if not query:
        await update.message.reply_text("🔍 *Пример:* `/search новости`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("🔍 Ищу...")
    result = search_tavily(query)
    if "error" in result:
        await msg.edit_text("❌ Ошибка поиска")
        return
    text = f"🔎 *{query}*\n\n"
    if result.get("answer"):
        text += f"📌 {result['answer']}\n\n"
    for i, res in enumerate(result.get("results", [])[:3], 1):
        text += f"{i}. [{res['title']}]({res['url']})\n"
    await msg.edit_text(text[:4000], parse_mode="Markdown", disable_web_page_preview=True)

async def sticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sticker = get_random_sticker()
    if sticker:
        await update.message.reply_sticker(sticker)
    else:
        await update.message.reply_text("😢 Нет стикеров. Отправь мне стикер — сохраню!")

async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    partner = get_partner(user_id)
    if partner:
        await update.message.reply_text("❌ Ты уже в чате. /stop_chat")
        return
    waiting = get_waiting_list(user_id)
    if waiting:
        partner = waiting[0]
        create_session(user_id, partner)
        await update.message.reply_text("✅ Собеседник найден!")
        await context.bot.send_message(partner, "✅ Собеседник найден!")
    else:
        add_waiting(user_id)
        await update.message.reply_text("⏳ Жду собеседника...")

async def stop_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    partner = get_partner(user_id)
    if partner:
        end_session(user_id)
        await update.message.reply_text("👋 Чат завершён")
        await context.bot.send_message(partner, "👋 Собеседник покинул чат")
    else:
        remove_waiting(user_id)
        await update.message.reply_text("Ожидание отменено")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("stats.db")
    c = conn.cursor()
    c.execute("SELECT command, count FROM stats WHERE user_id = ? ORDER BY count DESC LIMIT 10", (update.effective_user.id,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📊 Статистика пуста")
        return
    text = "📊 *Твоя статистика*\n"
    for cmd, cnt in rows:
        text += f"• {cmd}: {cnt}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sticker = update.message.sticker
    if sticker.emoji:
        saved = save_sticker(sticker.emoji, sticker.file_id)
        if saved:
            await update.message.reply_text(f"😊 Стикер {sticker.emoji} сохранён!")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    partner = get_partner(chat_id)
    if partner:
        await context.bot.send_message(partner, f"📝 {text}")
        await update.message.reply_text("✅ Отправлено")
        return
    
    if text.lower().startswith("погода"):
        city = text[7:].strip()
        if city:
            await update.message.reply_text(get_weather(city), parse_mode="Markdown")
        return
    
    if text.lower().startswith("найди"):
        query = text[5:].strip()
        if query:
            await search_command(update, context)
        return
    
    if random.random() < 0.2 and count_stickers() > 0:
        sticker = get_random_sticker()
        if sticker:
            await update.message.reply_sticker(sticker)
    
    await update.message.chat.send_action(action="typing")
    reply = ask_ai(text, chat_id)
    await update.message.reply_text(reply)

# === ЗАПУСК ===
def main():
    print("🚀 Запуск бота на Render...")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("gpt", model_command))
    app.add_handler(CommandHandler("claude", model_command))
    app.add_handler(CommandHandler("gemini", model_command))
    app.add_handler(CommandHandler("draw", draw))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("sticker", sticker_command))
    app.add_handler(CommandHandler("chat", chat_command))
    app.add_handler(CommandHandler("stop_chat", stop_chat_command))
    app.add_handler(CommandHandler("stats", stats_command))
    
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Запускаем напоминания в отдельном потоке
    def run_reminders():
        asyncio.run(check_reminders(app))
    threading.Thread(target=run_reminders, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost")
    webhook_url = f"https://{host}/{TELEGRAM_TOKEN}"
    
    print(f"✅ Webhook URL: {webhook_url}")
    
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
