import os
import json
import time
import hmac
import hashlib
import base64
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode

import requests
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext


# ===========================================================
#                 WIREGUARD VPN ЗАПУСК
# ===========================================================

def start_vpn():
    """
    Запуск WireGuard, если включено через переменную WG_ENABLED=1
    Конфиг лежит в WG_CONFIG полностью (текст)
    """
    if os.environ.get("WG_ENABLED") == "1":
        print("WG_ENABLED=1 → Запускаю WireGuard...")
        try:
            with open("wg.conf", "w") as f:
                f.write(os.environ["WG_CONFIG"])

            subprocess.Popen(["wg-quick", "up", "wg.conf"])
            print("WireGuard VPN успешно запущен!")
        except Exception as e:
            print("Ошибка запуска WireGuard:", e)
    else:
        print("WireGuard отключён (WG_ENABLED != 1)")


# ===========================================================
#                     ЗАГРУЗКА КОНФИГА
# ===========================================================

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

CFG = load_config()


def allowed_user(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    return user_id in CFG.get("ALLOWED_USERS", [])


# ===========================================================
#                  KuCoin SPOT КЛИЕНТ
# ===========================================================

KUCOIN_API_KEY = os.environ.get("KUCOIN_API_KEY")
KUCOIN_API_SECRET = os.environ.get("KUCOIN_API_SECRET")
KUCOIN_API_PASSPHRASE = os.environ.get("KUCOIN_API_PASSPHRASE")
KUCOIN_BASE_URL = "https://api.kucoin.com"


def kucoin_sign(method: str, path: str, params: dict | None = None, body: dict | None = None):
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


def kucoin_get_fills(symbol=None, limit=50):
    if not (KUCOIN_API_KEY and KUCOIN_API_SECRET and KUCOIN_API_PASSPHRASE):
        raise RuntimeError("KuCoin API ключи не заданы в переменных окружения.")

    path = "/api/v1/fills"
    params = {"pageSize": limit}
    if symbol:
        params["symbol"] = symbol

    headers, url_path = kucoin_sign("GET", path, params=params)
    url = KUCOIN_BASE_URL + url_path

    resp = requests.get(url, headers=headers, timeout=15)

    if resp.status_code != 200:
        raise RuntimeError(f"KuCoin API error: HTTP {resp.status_code} {resp.text}")

    data = resp.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"KuCoin API error: {data}")

    return data.get("data", {}).get("items", [])


# ===========================================================
#                  TELEGRAM КОМАНДЫ
# ===========================================================

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


def format_trade(tr):
    symbol = tr.get("symbol")
    side = tr.get("side")
    size = tr.get("size")
    price = tr.get("price")
    funds = tr.get("funds")
    time_ms = tr.get("createdAt")

    t_str = "?"
    if time_ms:
        t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time_ms / 1000))

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
        update.message.reply_text("Сделок нет.")
        return

    lines = ["Последние 10 сделок:"]
    for tr in fills:
        lines.append(format_trade(tr))

    update.message.reply_text("\n".join(lines))


def calc_stats(fills):
    if not fills:
        return "Сделок нет."

    buy = sum(1 for x in fills if x["side"] == "buy")
    sell = sum(1 for x in fills if x["side"] == "sell")

    volume = {}
    for tr in fills:
        sym = tr["symbol"]
        vol = abs(float(tr.get("funds", 0)))
        volume[sym] = volume.get(sym, 0) + vol

    text = [
        f"Всего сделок: {len(fills)}",
        f"BUY: {buy}",
        f"SELL: {sell}",
        "",
        "Объём по парам:"
    ]

    for sym, vol in sorted(volume.items(), key=lambda x: x[1], reverse=True):
        text.append(f"{sym}: {vol:.2f}")

    return "\n".join(text)


def cmd_stats(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    update.message.reply_text("Запрашиваю сделки...")

    try:
        fills = kucoin_get_fills(limit=200)
    except Exception as e:
        update.message.reply_text(f"Ошибка при запросе статистики KuCoin:\n{e}")
        return

    update.message.reply_text(calc_stats(fills))


def cmd_pair(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    if not context.args:
        update.message.reply_text("Использование: /pair BTC-USDT")
        return

    symbol = context.args[0].upper()

    update.message.reply_text(f"Запрашиваю сделки {symbol}...")

    try:
        fills = kucoin_get_fills(symbol=symbol, limit=200)
    except Exception as e:
        update.message.reply_text(f"Ошибка KuCoin:\n{e}")
        return

    if not fills:
        update.message.reply_text("Сделок нет.")
        return

    update.message.reply_text(f"Статистика по {symbol}:\n\n{calc_stats(fills)}")


# ===========================================================
#              ФЕЙКОВЫЙ WEB SERVER ДЛЯ Railway
# ===========================================================

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def run_fake_webserver():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()


# ===========================================================
#                       MAIN BOT
# ===========================================================

def main():
    updater = Updater(token=CFG["TELEGRAM_TOKEN"], use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("stats", cmd_stats))
    dp.add_handler(CommandHandler("orders", cmd_orders))
    dp.add_handler(CommandHandler("pair", cmd_pair))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    start_vpn()   # ← запуск VPN
    threading.Thread(target=run_fake_webserver, daemon=True).start()
    main()