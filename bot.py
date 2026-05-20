import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Команда /start получена и обрабатывается")
    await update.message.reply_text(
        "✅ Привет! Я - **Легенда**.\n\n"
        "Бот успешно запущен на сервере Render.\n"
        "Вебхук настроен и работает.",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Доступные команды:\n/start - приветствие\n/help - помощь")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🟢 Бот активен и работает через Webhook!")

# --- Запуск ---
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Токен не найден!")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))

    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')

    if not host:
        logger.error("RENDER_EXTERNAL_HOSTNAME не установлен!")
        return

    # КЛЮЧЕВОЙ МОМЕНТ: путь, по которому бот слушает Telegram
    webhook_path = TELEGRAM_TOKEN
    webhook_url = f"https://{host}/{webhook_path}"

    logger.info(f"Запуск на порту: {port}")
    logger.info(f"Путь для вебхука: {webhook_path}")
    logger.info(f"Полный URL вебхука: {webhook_url}")

    # Параметр url_path заставляет бота слушать нужный адрес
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
