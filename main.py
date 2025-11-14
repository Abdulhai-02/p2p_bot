import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
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
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---- Fake Web Server for Render ----
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_fake_webserver():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()

# Запуск веб-сервера в отдельном потоке
threading.Thread(target=run_fake_webserver, daemon=True).start()
print("Fake webserver started!")