import os
import json
import time
import hmac
import hashlib
import base64
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode

import requests
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# ---------- ЗАГРУЗКА КОНФИГА ----------

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

CFG = load_config()


def allowed_user(update: Update) -> bool:
    """Проверяем, что пишет только разрешённый пользователь."""
    user_id = update.effective_user.id if update.effective_user else None
    allowed = CFG.get("ALLOWED_USERS", [])
    return user_id in allowed


# ---------- ПРОКСИ ДЛЯ ОБХОДА USA BLOCK ----------

# Франция (стоит и работает стабильно)
proxies = {
    "http": "http://154.16.180.182:3128",
    "https": "http://154.16.180.182:3128",
}


# ---------- KuCoin SPOT API КЛИЕНТ ----------

KUCOIN_API_KEY = os.environ.get("KUCOIN_API_KEY")
KUCOIN_API_SECRET = os.environ.get("KUCOIN_API_SECRET")
KUCOIN_API_PASSPHRASE = os.environ.get("KUCOIN_API_PASSPHRASE")
KUCOIN_BASE_URL = "https://api.kucoin.com"


def kucoin_sign(method: str, path: str, params: dict | None = None, body: dict | None = None):
    """
    Подпись запроса KuCoin Spot.
    """
    if params:
        query_str = "?" + urlencode(params)
    else:
        query_str = ""

    url_path = path + query_str
    body_str = json.dumps(body) if body else ""

    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method.upper()}{url_path}{body_str}"

    signature = base64.b64encode(
        hmac.new(
            KUCOIN_API_SECRET.encode(),
            str_to_sign.encode(),
            hashlib.sha256,
        ).digest()
    ).decode()

    passphrase = base64.b64encode(
        hmac.new(
            KUCOIN_API_SECRET.encode(),
            KUCOIN_API_PASSPHRASE.encode(),
            hashlib.sha256,
        ).digest()
    ).decode()

    headers = {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }
    return headers, url_path


def kucoin_get_fills(symbol: str | None = None, limit: int = 50):
    """
    Получаем историю сделок SPOT.
    """
    if not (KUCOIN_API_KEY and KUCOIN_API_SECRET and KUCOIN_API_PASSPHRASE):
        raise RuntimeError("KuCoin API ключи не заданы в переменных окружения.")

    path = "/api/v1/fills"
    params = {"pageSize": limit}
    if symbol:
        params["symbol"] = symbol

    headers, url_path = kucoin_sign("GET", path, params=params)
    url = KUCOIN_BASE_URL + url_path

    # ВАЖНО: отправляем через европейский прокси
    resp = requests.get(url, headers=headers, timeout=15, proxies=proxies)

    if resp.status_code != 200:
        raise RuntimeError(
            f"KuCoin API error: HTTP {resp.status_code} {resp.text}"
        )

    data = resp.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"KuCoin API error: {data}")

    return data.get("data", {}).get("items", [])


# ---------- ХЭНДЛЕРЫ КОМАНД ----------

def cmd_start(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    update.message.reply_text(
        "Бот для статистики KuCoin SPOT работает.\n"
        "Команды:\n"
        "/stats – общая статистика\n"
        "/orders – последние сделки\n"
        "/pair BTC-USDT – статистика по паре"
    )


def format_trade(tr: dict) -> str:
    symbol = tr.get("symbol")
    side = tr.get("side")
    size = tr.get("size")
    price = tr.get("price")
    funds = tr.get("funds")
    ts = tr.get("createdAt")
    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)) if ts else "?"
    return f"{t_str} | {symbol} | {side.upper()} | size={size} | price={price} | amount={funds}"


def cmd_orders(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    try:
        fills = kucoin_get_fills(limit=10)
    except Exception as e:
        update.message.reply_text(f"Ошибка при запросе ордеров KuCoin:\n{e}")
        return

    if not fills:
        update.message.reply_text("Сделок не найдено.")
        return

    text = "Последние 10 сделок:\n" + "\n".join(format_trade(t) for t in fills)
    update.message.reply_text(text[:4000])


def calc_simple_stats(fills: list):
    if not fills:
        return "Сделок нет."

    total = len(fills)
    buy = sum(1 for f in fills if f.get("side") == "buy")
    sell = sum(1 for f in fills if f.get("side") == "sell")

    symbols = {}
    for tr in fills:
        sym = tr["symbol"]
        funds = float(tr.get("funds", 0))
        symbols[sym] = symbols.get(sym, 0) + abs(funds)

    lines = [
        f"Всего сделок: {total}",
        f"BUY: {buy}",
        f"SELL: {sell}",
        "",
        "Объёмы по парам:",
    ]

    for sym, vol in sorted(symbols.items(), key=lambda x: x[1], reverse=True)[:10]:
        lines.append(f"{sym}: {vol:.2f}")

    return "\n".join(lines)


def cmd_stats(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    update.message.reply_text("Запрашиваю сделки...")

    try:
        fills = kucoin_get_fills(limit=200)
        update.message.reply_text(calc_simple_stats(fills))
    except Exception as e:
        update.message.reply_text(f"Ошибка при запросе статистики KuCoin:\n{e}")


def cmd_pair(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    if not context.args:
        update.message.reply_text("Использование: /pair BTC-USDT")
        return

    symbol = context.args[0].upper()
    update.message.reply_text(f"Запрашиваю сделки по {symbol}...")

    try:
        fills = kucoin_get_fills(symbol=symbol, limit=200)
        update.message.reply_text(f"Статистика по {symbol}:\n\n{calc_simple_stats(fills)}")
    except Exception as e:
        update.message.reply_text(f"Ошибка при запросе KuCoin:\n{e}")


# ---------- FAKE WEB SERVER ----------

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def run_fake_webserver():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()


# ---------- ЗАПУСК БОТА ----------

def main():
    token = CFG["TELEGRAM_TOKEN"]

    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("stats", cmd_stats))
    dp.add_handler(CommandHandler("orders", cmd_orders))
    dp.add_handler(CommandHandler("pair", cmd_pair))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    threading.Thread(target=run_fake_webserver, daemon=True).start()
    main()