import os
import base64
import json
import time
import sqlite3
import re
from datetime import datetime, timedelta
import asyncio
import pytz

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

MSK = pytz.timezone("Europe/Moscow")

# ============================
#  DB
# ============================

def init_db():
    conn = sqlite3.connect("gmail_kucoin.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            type TEXT,
            amount REAL,
            currency TEXT,
            raw TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_event(event_type, amount, currency, raw):
    conn = sqlite3.connect("gmail_kucoin.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events(timestamp, type, amount, currency, raw) VALUES (?, ?, ?, ?, ?)",
        (
            datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
            event_type,
            amount,
            currency,
            raw
        )
    )
    conn.commit()
    conn.close()

# ============================
#  PARSER
# ============================

def extract_amount(text):
    usd = re.search(r"([\d,.]+)\s*USDT", text)
    rub = re.search(r"([\d\s,.]+)\s*(RUB|₽)", text)

    if usd:
        return float(usd.group(1).replace(",", "")), "USDT"
    if rub:
        val = rub.group(1).replace(" ", "").replace(",", "")
        return float(val), "RUB"

    return None, None


def detect_type(text):
    t = text.lower()

    if "payment completed" in t:
        return "PAYMENT_COMPLETED"
    if "release" in t and "crypto" in t:
        return "CRYPTO_RELEASED"
    if "deposit" in t and "received" in t:
        return "DEPOSIT"
    if "withdrawal" in t and "successful" in t:
        return "WITHDRAWAL"
    if "p2p" in t and "order" in t:
        return "P2P_ORDER"

    return "UNKNOWN"


# ============================
#  GMAIL
# ============================

def gmail_service():
    token_b64 = os.getenv("GMAIL_TOKEN_JSON", "")
    if not token_b64:
        raise Exception("❌ ENV GMAIL_TOKEN_JSON отсутствует!")

    token_json = json.loads(base64.b64decode(token_b64).decode())
    creds = Credentials.from_authorized_user_info(token_json)
    return build("gmail", "v1", credentials=creds)


async def process_gmail(bot, chat_id):
    service = gmail_service()
    last_history = None

    while True:
        try:
            msgs = service.users().messages().list(
                userId="me",
                q="from:kucoin"
            ).execute()

            if "messages" not in msgs:
                await asyncio.sleep(5)
                continue

            msg_id = msgs["messages"][0]["id"]
            msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()

            snippet = msg.get("snippet", "")

            event_type = detect_type(snippet)
            amount, currency = extract_amount(snippet)

            # в базу
            save_event(event_type, amount if amount else 0, currency if currency else "", snippet)

            # уведомление
            text = f"📩 <b>{event_type}</b>\n"
            if amount:
                text += f"💰 Сумма: <b>{amount} {currency}</b>\n"
            text += f"\n🧾 {snippet}"

            await bot.send_message(chat_id, text, parse_mode="HTML")

        except Exception as e:
            await bot.send_message(chat_id, f"⚠ Gmail error: {e}")

        await asyncio.sleep(5)


# ============================
#  REPORT /today
# ============================

def get_today_stats():
    conn = sqlite3.connect("gmail_kucoin.db")
    cur = conn.cursor()

    date = datetime.now(MSK).strftime("%Y-%m-%d")  # сегодня

    cur.execute("""
        SELECT type, amount, currency FROM events
        WHERE timestamp LIKE ?
    """, (f"{date}%",))

    rows = cur.fetchall()
    conn.close()

    total_usdt = 0
    total_rub = 0
    p2p_orders = 0
    deposits = 0
    withdrawals = 0
    releases = 0

    for t, amount, cur in rows:
        if t == "PAYMENT_COMPLETED":
            p2p_orders += 1
            if cur == "RUB":
                total_rub += amount
            if cur == "USDT":
                total_usdt += amount

        if t == "DEPOSIT":
            deposits += 1
            if cur == "USDT":
                total_usdt += amount

        if t == "WITHDRAWAL":
            withdrawals += 1
            if cur == "USDT":
                total_usdt -= amount

        if t == "CRYPTO_RELEASED":
            releases += 1
            if cur == "USDT":
                total_usdt -= amount

    return {
        "orders": p2p_orders,
        "deposits": deposits,
        "withdrawals": withdrawals,
        "releases": releases,
        "rub": total_rub,
        "usdt": total_usdt
    }


# ============================
#  TELEGRAM BOT
# ============================

async def main():
    init_db()

    with open("config.json") as f:
        cfg = json.load(f)
    BOT_TOKEN = cfg["TELEGRAM_BOT_TOKEN"]
    CHAT_ID = cfg["TELEGRAM_CHAT_ID"]

    bot = Bot(BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher()

    @dp.message(Command("today"))
    async def today_cmd(msg: types.Message):
        stats = get_today_stats()
        text = f"""
📊 <b>Отчёт за сегодня</b>

📦 P2P ордеров: <b>{stats['orders']}</b>
💸 Депозитов: <b>{stats['deposits']}</b>
🏧 Выводов: <b>{stats['withdrawals']}</b>
🔓 Release: <b>{stats['releases']}</b>

🇷🇺 Оборот RUB: <b>{stats['rub']}</b>
💵 Оборот USDT: <b>{stats['usdt']}</b>

⏱ МСК: {datetime.now(MSK).strftime('%H:%M:%S')}
"""
        await msg.answer(text)

    asyncio.create_task(process_gmail(bot, CHAT_ID))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())