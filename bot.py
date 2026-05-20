import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот Легенда работает на Render через Docker!")

def main():
    if not TELEGRAM_TOKEN:
        logging.error("Токен не найден! Установи переменную TELEGRAM_TOKEN")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    port = int(os.environ.get("PORT", 8080))
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_TOKEN}"

    logging.info(f"Запуск вебхука на порту {port}")
    app.run_webhook(listen="0.0.0.0", port=port, webhook_url=webhook_url)

if __name__ == "__main__":
    main()
