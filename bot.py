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
        "Я - твой персональный ассистент с искусственным интеллектом. "
        "Задавай любые вопросы, и я постараюсь помочь!\n\n"
        "🌤️ *Примеры запросов:*\n"
        "• `погода Москва`\n"
        "• `найди новости про ИИ`\n"
        "• `напомни через 10 мин покормить кота`",
        parse_mode="Markdown"
    )

# --- Команда /help ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Доступные команды:*\n\n"
        "/start - Приветствие\n"
        "/help - Это сообщение\n"
        "/status - Статус бота\n\n"
        "И конечно, просто задавай любые вопросы!",
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

# --- Запуск бота ---
def main():
    # Создаем приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))

    # Получаем порт и хост от Render
    port = int(os.environ.get('PORT', 8080))
    webhook_url = os.environ.get('RENDER_EXTERNAL_URL')

    # Проверяем, что webhook_url установлен
    if not webhook_url:
        logger.error("Переменная RENDER_EXTERNAL_URL не найдена!")
        return

    # Формируем полный URL для webhook
    full_webhook_url = f"{webhook_url}/{TELEGRAM_TOKEN}"

    logger.info(f"Запуск webhook на порту {port}")
    logger.info(f"Webhook URL: {full_webhook_url}")

    # Запускаем бота в режиме webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN,
        webhook_url=full_webhook_url
    )

if __name__ == "__main__":
    main()
