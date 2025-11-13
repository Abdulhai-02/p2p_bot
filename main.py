import json
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Проверка доступа
def allowed_user(update: Update):
    with open("config.json") as f:
        cfg = json.load(f)
    return update.effective_user.id in cfg["ALLOWED_USERS"]

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_user(update):
        return
    await update.message.reply_text("Бот работает. Отвечаю только тебе.")

async def main():
    with open("config.json") as f:
        cfg = json.load(f)

    token = cfg["TELEGRAM_TOKEN"]

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))

    await app.run_polling()

import asyncio
asyncio.run(main())