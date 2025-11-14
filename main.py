import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import json
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# --- Пинг-сервер для Render (чтобы не уснул) ---
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_webserver():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()

threading.Thread(target=run_webserver, daemon=True).start()
print("Fake webserver started!")

# --- Telegram Bot ---
def allowed_user(update: Update):
    with open("config.json") as f:
        cfg = json.load(f)
    return update.effective_user.id in cfg["ALLOWED_USERS"]

def start(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return
    update.message.reply_text("Бот работает ✅")

def main():
    with open("config.json") as f:
        cfg = json.load(f)
    token = cfg["TELEGRAM_TOKEN"]

    updater = Updater(token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()