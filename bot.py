import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот работает!")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
    webhook_url = f"https://{host}/{TELEGRAM_TOKEN}"

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN,  # <--- ЭТО ГЛАВНОЕ
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
