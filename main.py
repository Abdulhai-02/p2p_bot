import os
import json
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


# --- Проверка allowed user ---
def allowed_user(update: Update):
    with open("config.json") as f:
        cfg = json.load(f)
    return update.effective_user.id in cfg["ALLOWED_USERS"]


# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_user(update):
        return
    await update.message.reply_text("Бот запущен и работает!")


# --- Основной бот ---
async def run_bot():
    with open("config.json") as f:
        cfg = json.load(f)

    token = cfg["TELEGRAM_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))

    await app.initialize()
    await app.start()
    print("Bot started!")
    await app.updater.start_polling()
    await app.updater.idle()


# --- Fake Render Ping Server ---
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_webserver():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()


Thread(target=start_webserver, daemon=True).start()

asyncio.run(run_bot())