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


# ---------- KuCoin SPOT API КЛИЕНТ ----------

KUCOIN_API_KEY = os.environ.get("KUCOIN_API_KEY")
KUCOIN_API_SECRET = os.environ.get("KUCOIN_API_SECRET")
KUCOIN_API_PASSPHRASE = os.environ.get("KUCOIN_API_PASSPHRASE")
KUCOIN_BASE_URL = "https://api.kucoin.com"


def kucoin_sign(method: str, path: str, params: dict | None = None, body: dict | None = None):
    """
    Подпись запроса KuCoin Spot.
    Метод максимально близок к официальной доке.
    """
    if params:
        query_str = "?" + urlencode(params)
    else:
        query_str = ""

    url_path = path + query_str
    if body:
        body_str = json.dumps(body)
    else:
        body_str = ""

    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method.upper()}{url_path}{body_str}"
    signature = base64.b64encode(
        hmac.new(
            KUCOIN_API_SECRET.encode("utf-8"),
            str_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode()

    passphrase = base64.b64encode(
        hmac.new(
            KUCOIN_API_SECRET.encode("utf-8"),
            KUCOIN_API_PASSPHRASE.encode("utf-8"),
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
    Получаем историю спотовых сделок (fills).
    Если symbol = None -> по всем парам, иначе по конкретной.
    """
    if not (KUCOIN_API_KEY and KUCOIN_API_SECRET and KUCOIN_API_PASSPHRASE):
        raise RuntimeError("KuCoin API ключи не заданы в переменных окружения.")

    path = "/api/v1/fills"
    params = {"pageSize": limit}
    if symbol:
        params["symbol"] = symbol

    headers, url_path = kucoin_sign("GET", path, params=params)
    url = KUCOIN_BASE_URL + url_path

    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"KuCoin API error: HTTP {resp.status_code} {resp.text}")

    data = resp.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"KuCoin API error: {data}")

    return data.get("data", {}).get("items", [])


# ---------- ХЭНДЛЕРЫ КОМАНД ТЕЛЕГРАМ ----------

def cmd_start(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return
    update.message.reply_text("Бот для статистики KuCoin SPOT работает.\n"
                              "Команды:\n"
                              "/stats – общая статистика\n"
                              "/orders – последние сделки\n"
                              "/pair BTC-USDT – статистика по паре")


def format_trade(tr: dict) -> str:
    symbol = tr.get("symbol")
    side = tr.get("side")
    size = tr.get("size")
    price = tr.get("price")
    funds = tr.get("funds")
    time_ms = tr.get("createdAt")
    ts = int(time_ms) / 1000 if time_ms else None
    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "?"
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

    text_lines = ["Последние 10 сделок (SPOT):"]
    for tr in fills:
        text_lines.append(format_trade(tr))

    text = "\n".join(text_lines)
    # чтобы не упереться в лимит, режем, если что
    if len(text) > 4000:
        text = text[:4000] + "\n... (обрезано)"
    update.message.reply_text(text)


def calc_simple_stats(fills: list[dict]):
    total_trades = len(fills)
    if total_trades == 0:
        return "Сделок не найдено."

    symbols = {}
    buy_count = 0
    sell_count = 0

    for tr in fills:
        symbol = tr.get("symbol")
        side = tr.get("side")
        funds = float(tr.get("funds", 0.0))
        if symbol not in symbols:
            symbols[symbol] = 0.0
        # будем считать объем в quote валюте (USDT и т.п.)
        symbols[symbol] += abs(funds)

        if side == "buy":
            buy_count += 1
        elif side == "sell":
            sell_count += 1

    lines = []
    lines.append(f"Всего сделок: {total_trades}")
    lines.append(f"Покупок (BUY): {buy_count}")
    lines.append(f"Продаж (SELL): {sell_count}")
    lines.append("")
    lines.append("Объём по символам (в quote):")
    # сортировка по объёму
    for sym, vol in sorted(symbols.items(), key=lambda x: x[1], reverse=True)[:10]:
        lines.append(f"{sym}: {vol:.2f}")

    return "\n".join(lines)


def cmd_stats(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    update.message.reply_text("Запрашиваю сделки с KuCoin, подожди секунду...")

    try:
        fills = kucoin_get_fills(limit=200)
    except Exception as e:
        update.message.reply_text(f"Ошибка при запросе статистики KuCoin:\n{e}")
        return

    text = calc_simple_stats(fills)
    update.message.reply_text(text)


def cmd_pair(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    if not context.args:
        update.message.reply_text("Использование: /pair BTC-USDT")
        return

    symbol = context.args[0].upper()
    # В KuCoin спот символы формата BTC-USDT
    update.message.reply_text(f"Запрашиваю сделки по паре {symbol}...")

    try:
        fills = kucoin_get_fills(symbol=symbol, limit=200)
    except Exception as e:
        update.message.reply_text(f"Ошибка при запросе KuCoin по паре {symbol}:\n{e}")
        return

    if not fills:
        update.message.reply_text(f"Сделок по паре {symbol} не найдено.")
        return

    text = calc_simple_stats(fills)
    update.message.reply_text(f"Статистика по {symbol}:\n\n{text}")


# ---------- FAKE WEB SERVER ДЛЯ RENDER ----------

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

    # telegram bot v13.14
    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("stats", cmd_stats))
    dp.add_handler(CommandHandler("orders", cmd_orders))
    dp.add_handler(CommandHandler("pair", cmd_pair))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    # Запускаем фейковый веб-сервер для Render
    threading.Thread(target=run_fake_webserver, daemon=True).start()
    main()