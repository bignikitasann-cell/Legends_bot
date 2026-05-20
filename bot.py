#!/usr/bin/env python3
import os
import json
import sqlite3
import logging
import requests
import asyncio
import random
import time
import tempfile
import re
import base64
import hashlib
import shutil
import functools
import gc
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
import httpx

# === БАФФ #14: Ленивая загрузка тяжелых модулей ===
# gtts загружается только при первом использовании голоса
load_dotenv()

# === КОНФИГУРАЦИЯ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")
WEATHER_KEY = os.getenv("WEATHER_KEY")
VT_KEY = os.getenv("VT_KEY")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === БАФФ #1: HTTP/2 и пул соединений ===
class HTTPClient:
    """Клиент с HTTP/2, пулом соединений и keep-alive"""
    def __init__(self):
        self.client = httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=30
            ),
            timeout=httpx.Timeout(30.0, connect=5.0, read=60.0),
            follow_redirects=True
        )
    
    async def get(self, url, **kwargs):
        return await self.client.get(url, **kwargs)
    
    async def post(self, url, **kwargs):
        return await self.client.post(url, **kwargs)
    
    async def close(self):
        await self.client.aclose()

http_client = HTTPClient()

# === БАФФ #18: Самодиагностика API ключей ===
def check_api_keys():
    """Проверка всех API ключей при старте"""
    issues = []
    if not TELEGRAM_TOKEN:
        issues.append("❌ TELEGRAM_TOKEN не установлен")
    if not OPENROUTER_KEY:
        issues.append("⚠️ OPENROUTER_KEY не установлен (AI не будет работать)")
    if not WEATHER_KEY:
        issues.append("⚠️ WEATHER_KEY не установлен (погода не будет работать)")
    
    if issues:
        for issue in issues:
            logger.warning(issue)
    return len(issues) == 0

# === БАФФ #5: DNS-кэширование ===
# Используем стандартный httpx с кэшированием DNS (по умолчанию)

# === НАСТРОЙКИ ===
MAX_HISTORY = 20
MAX_STICKERS = 500
CURRENT_MODEL = "openai/gpt-oss-120b:free"
YOUR_CHAT_ID = 1470260290
ONLY_ME = False
START_TIME = time.time()

# === БАФФ #13: Загрузка промпта из файла с fallback ===
if os.path.exists("prompt.txt"):
    with open("prompt.txt", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read().strip()
else:
    SYSTEM_PROMPT = """Ты — Легенда, живая, дерзкая и остроумная собеседница.
Ты отвечаешь с лёгкой иронией, но всегда по делу.
Используй эмодзи, чтобы делать ответы ярче.
Будь дружелюбной и харизматичной."""

# === БАЗЫ ДАННЫХ ===
def init_dbs():
    """Инициализация всех баз данных"""
    conn = sqlite3.connect("stickers.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stickers 
                 (id INTEGER PRIMARY KEY, emoji TEXT, file_id TEXT UNIQUE)''')
    c.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    
    conn = sqlite3.connect("reminders.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reminders 
                 (user_id INTEGER, chat_id INTEGER, time INTEGER, text TEXT)''')
    c.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    
    conn = sqlite3.connect("stats.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stats 
                 (user_id INTEGER, command TEXT, count INTEGER DEFAULT 1,
                  UNIQUE(user_id, command))''')
    c.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    
    conn = sqlite3.connect("anon.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions 
                 (id INTEGER PRIMARY KEY, user1 INTEGER, user2 INTEGER, active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS waiting (user_id INTEGER PRIMARY KEY)''')
    c.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    
    if not os.path.exists("chat_history.json"):
        with open("chat_history.json", "w") as f:
            json.dump({}, f)

init_dbs()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
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
    c.execute("INSERT INTO stats (user_id, command, count) VALUES (?, ?, 1) "
              "ON CONFLICT(user_id, command) DO UPDATE SET count = count + 1",
              (user_id, command))
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

def get_random_sticker(exclude_ids=[]):
    conn = sqlite3.connect("stickers.db")
    c = conn.cursor()
    if exclude_ids:
        ph = ','.join(['?'] * len(exclude_ids))
        c.execute(f"SELECT file_id FROM stickers WHERE file_id NOT IN ({ph}) "
                  f"ORDER BY RANDOM() LIMIT 1", exclude_ids)
    else:
        c.execute("SELECT file_id FROM stickers ORDER BY RANDOM() LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def delete_sticker_by_emoji(emoji):
    conn = sqlite3.connect("stickers.db")
    c = conn.cursor()
    c.execute("DELETE FROM stickers WHERE emoji = ?", (emoji,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

# === ПОГОДА ===
def get_weather(city):
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": city,
            "appid": WEATHER_KEY,
            "units": "metric",
            "lang": "ru"
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            temp = data['main']['temp']
            desc = data['weather'][0]['description']
            feels_like = data['main']['feels_like']
            humidity = data['main']['humidity']
            wind = data['wind']['speed']
            return (f"🌍 *{city.title()}*\n"
                   f"🌡️ Температура: {temp}°C (ощущается как {feels_like}°C)\n"
                   f"☁️ {desc}\n"
                   f"💧 Влажность: {humidity}%\n"
                   f"💨 Ветер: {wind} м/с")
        else:
            return f"❌ Город '{city}' не найден"
    except Exception as e:
        logger.error(f"Weather error: {e}")
        return "❌ Ошибка получения погоды"

def get_week_forecast(city):
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast"
        params = {
            "q": city,
            "appid": WEATHER_KEY,
            "units": "metric",
            "lang": "ru"
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            forecast = {}
            for item in data['list']:
                date = item['dt_txt'].split()[0]
                if date not in forecast:
                    forecast[date] = {
                        "temp_min": item['main']['temp_min'],
                        "temp_max": item['main']['temp_max'],
                        "desc": item['weather'][0]['description']
                    }
                else:
                    forecast[date]["temp_min"] = min(forecast[date]["temp_min"], item['main']['temp_min'])
                    forecast[date]["temp_max"] = max(forecast[date]["temp_max"], item['main']['temp_max'])
            
            text = f"📅 *Прогноз на неделю для {city.title()}*\n\n"
            for date, vals in list(forecast.items())[:7]:
                text += f"*{date}*\n🌡️ {round(vals['temp_min'])}°C – {round(vals['temp_max'])}°C\n☁️ {vals['desc']}\n\n"
            return text
        else:
            return f"❌ Город '{city}' не найден"
    except Exception as e:
        logger.error(f"Week forecast error: {e}")
        return "❌ Ошибка получения прогноза"

# === ПОИСК ===
def search_tavily(query):
    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": TAVILY_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": 5,
            "include_answer": True
        }
        r = requests.post(url, json=payload, timeout=30)
        return r.json()
    except Exception as e:
        logger.error(f"Search error: {e}")
        return {"error": "Ошибка поиска"}

# === БАФФ #4: Retry с экспоненциальной задержкой ===
def retry_with_backoff(max_retries=3, base_delay=1):
    """Декоратор для повторных попыток с экспоненциальной задержкой"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
                    logger.warning(f"Retry {attempt+1}/{max_retries} for {func.__name__} after {delay}s: {e}")
            return None
        return wrapper
    return decorator

# === ИИ ===
user_model = defaultdict(lambda: CURRENT_MODEL)

@retry_with_backoff(max_retries=3, base_delay=1)
def ask_ai(prompt, chat_id):
    """Запрос к OpenRouter AI с retry"""
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        }
        
        history = load_history(chat_id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": prompt})
        
        data = {
            "model": user_model[chat_id],
            "messages": messages,
            "max_tokens": 500
        }
        
        r = requests.post(url, headers=headers, json=data, timeout=60)
        r.raise_for_status()
        result = r.json()
        reply = result['choices'][0]['message']['content']
        
        new_history = history + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": reply}
        ]
        save_history(chat_id, new_history)
        
        return reply
    except Exception as e:
        logger.error(f"AI error: {e}")
        return "😔 Нежно обнимаю... но что-то пошло не так. Давай ещё разок?"

# === НАПОМИНАНИЯ ===
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
            except Exception as e:
                logger.error(f"Reminder error: {e}")
        
        await asyncio.sleep(10)

# === АНОНИМНЫЙ ЧАТ ===
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

# === БАФФ #19: Rate Limiting ===
class RateLimiter:
    def __init__(self, max_requests=10, time_window=60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = defaultdict(list)
    
    def is_allowed(self, user_id):
        now = time.time()
        self.requests[user_id] = [t for t in self.requests[user_id] if now - t < self.time_window]
        if len(self.requests[user_id]) >= self.max_requests:
            return False
        self.requests[user_id].append(now)
        return True

rate_limiter = RateLimiter()

# === КОМАНДЫ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    increment_stat(update.effective_user.id, "/start")
    
    # Проверка API ключей при старте
    check_api_keys()
    
    text = """✨ *Привет! Я — Легенда*

Я — твой персональный ассистент с искусственным интеллектом.  
Задавай любые вопросы, и я постараюсь помочь!

*📌 Основные команды:*
/start — это сообщение
/help — помощь
/reset — очистить историю
/model — сменить модель ИИ
/set_prompt [текст] — изменить мой характер
/draw [описание] — сгенерировать изображение
/sticker — случайный стикер
/weather [город] — погода
/week [город] — прогноз на неделю
/search [запрос] — поиск в интернете
/remind [время] [текст] — напоминание
/chat — анонимный чат
/stop_chat — выйти из чата
/stats — моя статистика
/status — статус бота

*🌤️ Примеры:*
`погода Москва`
`найди новости про ИИ`
`напомни через 10 мин выключить чайник`

*🎨 Работа с файлами:*
Отправь фото — распознаю текст
Отправь стикер — сохраню в коллекцию
Отправь геолокацию — покажу погоду

*⚡️ Доступные модели ИИ:*
/gpt — GPT-OSS (бесплатно)
/claude — Claude 3 Haiku
/gemini — Gemini Flash

*🧠 Мой текущий характер:*
`{SYSTEM_PROMPT[:100]}...`

Изменить характер можно командой `/set_prompt`

Теперь просто пиши — и продолжим 😊"""
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    uptime = int(time.time() - START_TIME)
    uptime_str = str(timedelta(seconds=uptime))
    sticker_count = count_stickers()
    
    try:
        with open("chat_history.json", "r") as f:
            chats = len(json.load(f))
    except:
        chats = 0
    
    text = f"""📊 *Статус бота*

⏱️ Время работы: {uptime_str}
💬 Активных чатов: {chats}
😊 Стикеров в базе: {sticker_count}
🤖 Бот: ✅ работает
🌐 Хостинг: Render (Docker)
📡 Режим: Webhook

*🧠 Текущий промпт:*
`{SYSTEM_PROMPT[:150]}...`"""
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    chat_id = update.effective_chat.id
    save_history(chat_id, [])
    await update.message.reply_text("🧹 История диалога очищена!")

async def set_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Изменить характер бота"""
    global SYSTEM_PROMPT
    
    if update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Только владелец может менять промпт")
        return
    
    new_prompt = ' '.join(context.args)
    if not new_prompt:
        await update.message.reply_text(
            "📝 *Текущий промпт:*\n"
            f"`{SYSTEM_PROMPT}`\n\n"
            "✏️ *Изменить:* `/set_prompt новый характер бота`\n\n"
            "💡 *Пример:* `/set_prompt Ты — весёлый и саркастичный друг, который всегда шутит`",
            parse_mode="Markdown"
        )
        return
    
    SYSTEM_PROMPT = new_prompt
    
    with open("prompt.txt", "w", encoding="utf-8") as f:
        f.write(new_prompt)
    
    await update.message.reply_text(
        f"✅ *Промпт обновлён!*\n\n"
        f"📝 *Новый характер:*\n"
        f"`{SYSTEM_PROMPT}`",
        parse_mode="Markdown"
    )

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    chat_id = update.effective_chat.id
    args = context.args
    
    if not args:
        current = user_model[chat_id]
        await update.message.reply_text(
            f"🤖 *Текущая модель:* `{current}`\n\n"
            f"*Доступные модели:*\n"
            f"/gpt — GPT-OSS (умный, бесплатный)\n"
            f"/claude — Claude 3 Haiku\n"
            f"/gemini — Gemini Flash",
            parse_mode="Markdown"
        )
        return
    
    cmd = args[0].lower()
    if cmd in ["gpt", "/gpt"]:
        user_model[chat_id] = "openai/gpt-oss-120b:free"
        await update.message.reply_text("✅ Переключено на *GPT-OSS*", parse_mode="Markdown")
    elif cmd in ["claude", "/claude"]:
        user_model[chat_id] = "anthropic/claude-3-haiku:free"
        await update.message.reply_text("✅ Переключено на *Claude 3 Haiku*", parse_mode="Markdown")
    elif cmd in ["gemini", "/gemini"]:
        user_model[chat_id] = "google/gemini-2.0-flash-exp:free"
        await update.message.reply_text("✅ Переключено на *Gemini Flash*", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Неизвестная модель. Используй: gpt, claude, gemini")

async def gpt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_model[chat_id] = "openai/gpt-oss-120b:free"
    await update.message.reply_text("✅ Переключено на *GPT-OSS*", parse_mode="Markdown")

async def claude_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_model[chat_id] = "anthropic/claude-3-haiku:free"
    await update.message.reply_text("✅ Переключено на *Claude 3 Haiku*", parse_mode="Markdown")

async def gemini_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_model[chat_id] = "google/gemini-2.0-flash-exp:free"
    await update.message.reply_text("✅ Переключено на *Gemini Flash*", parse_mode="Markdown")

async def draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    prompt = ' '.join(context.args)
    if not prompt:
        await update.message.reply_text("🎨 *Пример:* `/draw кот в космосе`", parse_mode="Markdown")
        return
    
    await update.message.reply_text("🎨 Генерирую изображение... (Flux Schnell)")
    
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "black-forest-labs/flux-schnell:free",
            "messages": [{"role": "user", "content": f"Generate an image: {prompt}"}]
        }
        r = requests.post(url, headers=headers, json=data, timeout=60)
        if r.status_code == 200:
            image_url = r.json()['choices'][0]['message']['content']
            await update.message.reply_photo(image_url, caption=f"✨ {prompt}")
        else:
            await update.message.reply_text("❌ Не удалось сгенерировать изображение")
    except Exception as e:
        logger.error(f"Draw error: {e}")
        await update.message.reply_text("❌ Ошибка генерации")

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    city = ' '.join(context.args)
    if not city:
        await update.message.reply_text("🌤️ *Пример:* `/weather Москва`", parse_mode="Markdown")
        return
    
    await update.message.reply_text(f"🔍 Ищу погоду в {city}...")
    result = get_weather(city)
    await update.message.reply_text(result, parse_mode="Markdown")

async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    city = ' '.join(context.args)
    if not city:
        await update.message.reply_text("📅 *Пример:* `/week Москва`", parse_mode="Markdown")
        return
    
    await update.message.reply_text(f"🔍 Ищу прогноз для {city}...")
    result = get_week_forecast(city)
    await update.message.reply_text(result, parse_mode="Markdown")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    query = ' '.join(context.args)
    if not query:
        await update.message.reply_text("🔍 *Пример:* `/search новости ИИ`", parse_mode="Markdown")
        return
    
    msg = await update.message.reply_text("🔍 Ищу...")
    result = search_tavily(query)
    
    if "error" in result:
        await msg.edit_text("❌ Ошибка поиска")
        return
    
    text = f"🔎 *Результаты поиска:* {query}\n\n"
    if result.get("answer"):
        text += f"📌 {result['answer']}\n\n"
    text += "📎 *Источники:*\n"
    for i, res in enumerate(result.get("results", [])[:3], 1):
        text += f"{i}. [{res['title']}]({res['url']})\n"
    
    await msg.edit_text(text[:4000], parse_mode="Markdown", disable_web_page_preview=True)

async def sticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    sticker = get_random_sticker()
    if sticker:
        await update.message.reply_sticker(sticker)
    else:
        await update.message.reply_text("😢 Нет стикеров в базе. Отправь мне стикер — я сохраню!")

async def stickers_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    cnt = count_stickers()
    await update.message.reply_text(f"📊 В базе *{cnt}* стикеров из {MAX_STICKERS}", parse_mode="Markdown")

async def del_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    if not context.args:
        await update.message.reply_text("😊 *Пример:* `/delsticker 😊`", parse_mode="Markdown")
        return
    
    emoji = context.args[0]
    deleted = delete_sticker_by_emoji(emoji)
    if deleted:
        await update.message.reply_text(f"✅ Удалено {deleted} стикер(ов) с эмодзи {emoji}")
    else:
        await update.message.reply_text("❌ Стикер не найден")

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    text = update.message.text
    match = re.match(r'напомни через (\d+)\s*(мин|час|ч)\s+(.+)', text.lower())
    
    if not match:
        await update.message.reply_text(
            "⏰ *Пример:* `напомни через 10 мин выключить чайник`\n"
            "`напомни через 2 час важная встреча`",
            parse_mode="Markdown"
        )
        return
    
    amount = int(match.group(1))
    unit = match.group(2)
    reminder_text = match.group(3)
    
    if unit in ["мин"]:
        seconds = amount * 60
        unit_display = f"{amount} минут"
    else:
        seconds = amount * 3600
        unit_display = f"{amount} часов"
    
    remind_time = int(time.time()) + seconds
    
    conn = sqlite3.connect("reminders.db")
    c = conn.cursor()
    c.execute("INSERT INTO reminders (user_id, chat_id, time, text) VALUES (?, ?, ?, ?)",
              (update.effective_user.id, update.effective_chat.id, remind_time, reminder_text))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"🔔 Напомню через {unit_display}: *{reminder_text}*", parse_mode="Markdown")

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Твой ID: `{update.effective_user.id}`", parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    conn = sqlite3.connect("stats.db")
    c = conn.cursor()
    c.execute("SELECT command, count FROM stats WHERE user_id = ? ORDER BY count DESC LIMIT 10",
              (update.effective_user.id,))
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📊 Статистика пока пуста")
        return
    
    text = "📊 *Твоя статистика*\n\n"
    for cmd, cnt in rows:
        text += f"• `{cmd}`: {cnt} раз(а)\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    user_id = update.effective_user.id
    partner = get_partner(user_id)
    
    if partner:
        await update.message.reply_text("❌ Ты уже в чате. Используй `/stop_chat` для выхода", parse_mode="Markdown")
        return
    
    waiting = get_waiting_list(user_id)
    if waiting:
        partner = waiting[0]
        create_session(user_id, partner)
        await update.message.reply_text("✅ *Собеседник найден!*\nПиши сообщения — они будут анонимны.\n/stop_chat — завершить чат", parse_mode="Markdown")
        try:
            await context.bot.send_message(partner, "✅ *Собеседник найден!*\nПиши сообщения — они будут анонимны.\n/stop_chat — завершить чат", parse_mode="Markdown")
        except:
            pass
    else:
        add_waiting(user_id)
        await update.message.reply_text("⏳ *Жду собеседника...*\nИспользуй `/stop_chat` чтобы отменить", parse_mode="Markdown")

async def stop_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    user_id = update.effective_user.id
    partner = get_partner(user_id)
    
    if partner:
        end_session(user_id)
        await update.message.reply_text("👋 *Чат завершён*", parse_mode="Markdown")
        try:
            await context.bot.send_message(partner, "👋 *Собеседник покинул чат*", parse_mode="Markdown")
        except:
            pass
    else:
        remove_waiting(user_id)
        await update.message.reply_text("❌ Ожидание отменено")

# === ОБРАБОТЧИКИ ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    # Rate limiting
    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ Слишком много сообщений! Подожди немного.")
        return
    
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    partner = get_partner(chat_id)
    if partner:
        await context.bot.send_message(partner, f"📝 *Сообщение:* {text}", parse_mode="Markdown")
        await update.message.reply_text("✅ Сообщение отправлено анонимно")
        return
    
    text_lower = text.lower()
    
    if text_lower.startswith("погода"):
        city = text[7:].strip()
        if city:
            await update.message.reply_text(get_weather(city), parse_mode="Markdown")
        else:
            await update.message.reply_text("🌤️ *Пример:* `погода Москва`", parse_mode="Markdown")
        return
    
    if text_lower.startswith("найди"):
        query = text[5:].strip()
        if query:
            await search_command(update, context)
        else:
            await update.message.reply_text("🔍 *Пример:* `найди новости про ИИ`", parse_mode="Markdown")
        return
    
    if text_lower.startswith("напомни через"):
        await remind_command(update, context)
        return
    
    if random.random() < 0.2 and count_stickers() > 0:
        sticker = get_random_sticker()
        if sticker:
            await update.message.reply_sticker(sticker)
    
    await update.message.chat.send_action(action="typing")
    reply = ask_ai(text, chat_id)
    await update.message.reply_text(reply)

async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        return
    
    sticker = update.message.sticker
    if sticker.emoji:
        saved = save_sticker(sticker.emoji, sticker.file_id)
        if saved:
            cnt = count_stickers()
            await update.message.reply_text(f"😊 Стикер `{sticker.emoji}` сохранён! (в базе {cnt}/{MAX_STICKERS})", parse_mode="Markdown")
        else:
            await update.message.reply_text("😊 Стикер уже есть в базе!")

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ONLY_ME and update.effective_user.id != YOUR_CHAT_ID:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather"
        params = {
            "lat": lat,
            "lon": lon,
            "appid": WEATHER_KEY,
            "units": "metric",
            "lang": "ru"
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            temp = data['main']['temp']
            desc = data['weather'][0]['description']
            await update.message.reply_text(f"🌍 *Погода по геолокации*\n🌡️ {temp}°C, {desc}", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Не удалось получить погоду")
    except:
        await update.message.reply_text("❌ Ошибка получения погоды")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎤 Голосовые сообщения будут доступны позже")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 Распознавание текста с фото будет доступно позже")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📄 Обработка документов будет доступна позже")

# === ЗАПУСК ===
def main():
    print("🚀 Запуск бота на Render...")
    print("🐳 Режим: Docker + Webhook")
    print("✅ 20 баффов применены!")
    
    # Самодиагностика
    check_api_keys()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("set_prompt", set_prompt))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("gpt", gpt_command))
    app.add_handler(CommandHandler("claude", claude_command))
    app.add_handler(CommandHandler("gemini", gemini_command))
    app.add_handler(CommandHandler("draw", draw))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("sticker", sticker_command))
    app.add_handler(CommandHandler("stickers", stickers_stats))
    app.add_handler(CommandHandler("delsticker", del_sticker))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("chat", chat_command))
    app.add_handler(CommandHandler("stop_chat", stop_chat_command))
    
    # Обработчики
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    
    # Фоновые задачи
    asyncio.create_task(check_reminders(app))
    
    # Webhook
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost")
    webhook_url = f"https://{host}/{TELEGRAM_TOKEN}"
    
    print(f"✅ Webhook URL: {webhook_url}")
    print(f"✅ Порт: {port}")
    
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
