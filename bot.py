import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Загружаем переменные окружения
load_dotenv()

# Получаем токен
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"✨ Привет, {user.first_name}! Я - **Легенда**.\n\n"
        "Я - твой персональный ассистент.\n\n"
        "🌤️ *Примеры запросов:*\n"
        "• `погода Москва`\n"
        "• `найди новости про ИИ`",
        parse_mode="Markdown"
    )

# --- Команда /help ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Доступные команды:*\n\n"
        "/start - Приветствие\n"
        "/help - Это сообщение\n"
        "/status - Статус бота",
        parse_mode="Markdown"
    )

# --- Команда /status ---
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Бот успешно запущен на сервере Render!\n"
        "🟢 Статус: Активен\n"
        "📡 Тип подключения: Webhook\n"
        "⚙️ Версия: Production Ready"
    )

# --- Главная функция запуска ---
def main():
    # Создаем приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))

    # Получаем порт и хост от Render
    port = int(os.environ.get('PORT', 8080))

    # Запускаем бота в режиме webhook, УКАЗЫВАЯ url_path
    # Это заставит бота слушать адрес: твой-URL/ТОКЕН
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN,  # <--- ЭТО САМОЕ ВАЖНОЕ!
        webhook_url=f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_TOKEN}"
    )

if __name__ == "__main__":
    main()
