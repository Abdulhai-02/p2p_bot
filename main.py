import os
import json
import time
import hmac
import hashlib
import base64
import threading
import sqlite3
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone

import requests
from telegram import Update
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    ConversationHandler,
    MessageHandler,
    Filters,
)

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ================== НАСТРОЙКИ ==================

MSK_TZ = timezone(timedelta(hours=3))
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = "token.json"   # Файл, который ты получил через authorize.py
DB_PATH = "gmail_kucoin.db"

# Для ConversationHandler
REPORT_DAY_START, REPORT_DAY_END = range(2)

# ================== КОНФИГ ==================


def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()


def allowed_user(update: Update) -> bool:
    """Проверяем, что пишет только разрешённый пользователь."""
    user_id = update.effective_user.id if update.effective_user else None
    allowed = CFG.get("ALLOWED_USERS", [])
    return user_id in allowed


ALLOWED_CHAT_IDS = CFG.get("ALLOWED_USERS", [])


# ================== БАЗА ДАННЫХ ==================


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            ts_utc REAL,
            ku_type TEXT,
            subject TEXT,
            amount REAL,
            asset TEXT,
            order_id TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def db_save_email(msg_id, ts_utc, ku_type, subject, amount, asset, order_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO emails (id, ts_utc, ku_type, subject, amount, asset, order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (msg_id, ts_utc, ku_type, subject, amount, asset, order_id),
    )
    conn.commit()
    conn.close()


def db_get_all_ids():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM emails")
    rows = cur.fetchall()
    conn.close()
    return {r[0] for r in rows}


def db_get_emails_between(start_ts_utc, end_ts_utc):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM emails
        WHERE ts_utc >= ? AND ts_utc < ?
        ORDER BY ts_utc ASC
        """,
        (start_ts_utc, end_ts_utc),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def db_get_last_n(n=10):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM emails
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (n,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ================== GMAIL ==================


def get_gmail_service():
    if not os.path.exists(GMAIL_TOKEN_FILE):
        raise RuntimeError(
            f"Файл {GMAIL_TOKEN_FILE} не найден. "
            f"Сначала запусти authorize.py локально и положи token.json рядом с main.py."
        )
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    service = build("gmail", "v1", credentials=creds)
    return service


def extract_text_from_payload(payload) -> str:
    """Достаём текст письма из payload (берём text/plain)."""
    data_parts = []

    def _walk(part):
        if part.get("mimeType") == "text/plain":
            body = part.get("body", {})
            b64_data = body.get("data")
            if b64_data:
                text = base64.urlsafe_b64decode(b64_data.encode()).decode(
                    errors="ignore"
                )
                data_parts.append(text)
        for p in part.get("parts", []) or []:
            _walk(p)

    _walk(payload)
    if data_parts:
        return "\n".join(data_parts)
    # fallback: snippet использовать
    return ""


def parse_kucoin_email(subject: str, body: str):
    """
    Очень простой парсер писем KuCoin.
    Возвращает dict: {ku_type, amount, asset, order_id}
    """
    ku_type = "unknown"
    amount = None
    asset = None
    order_id = None

    text = subject + "\n" + body

    # Order ID
    m_id = re.search(r"Order ID:\s*([0-9a-fA-F]+)", text)
    if m_id:
        order_id = m_id.group(1)

    # 1) "The buyer ... has marked the P2P order ... as Payment Completed."
    if "has marked the P2P order" in text and "Payment Completed" in text:
        ku_type = "p2p_payment_completed"

    # 2) "You have completed your sell order of 7.50469 USDT"
    m_sell = re.search(
        r"completed your sell order of\s+([0-9.]+)\s+([A-Z]+)", text, re.IGNORECASE
    )
    if m_sell:
        ku_type = "p2p_sell_completed"
        amount = float(m_sell.group(1))
        asset = m_sell.group(2)

    # 3) "You have received 406.836461 USDT. Please check your Funding Account."
    m_recv = re.search(
        r"You have received\s+([0-9.]+)\s+([A-Z]+)", text, re.IGNORECASE
    )
    if m_recv and "Funding Account" in text:
        ku_type = "p2p_received_funding"
        amount = float(m_recv.group(1))
        asset = m_recv.group(2)

    # 4) "You have received a deposit of 2572.00 USDT."
    m_dep = re.search(
        r"You have received a deposit of\s+([0-9.]+)\s+([A-Z]+)",
        text,
        re.IGNORECASE,
    )
    if m_dep:
        ku_type = "deposit"
        amount = float(m_dep.group(1))
        asset = m_dep.group(2)

    # 5) "Your withdrawal on ... was successful."
    if "Your withdrawal on" in text and "was successful" in text:
        ku_type = "withdrawal"
        m_coin_amt = re.search(
            r"Coin:\s*([A-Z]+).*?Amount:\s*([0-9.]+)", text, re.DOTALL
        )
        if m_coin_amt:
            asset = m_coin_amt.group(1)
            amount = float(m_coin_amt.group(2))

    # 6) "has submitted a 24.860161 USDT P2P buy order to you"
    m_buy = re.search(
        r"has submitted a\s+([0-9.]+)\s+([A-Z]+)\s+P2P buy order to you",
        text,
        re.IGNORECASE,
    )
    if m_buy:
        ku_type = "p2p_buy_submitted"
        amount = float(m_buy.group(1))
        asset = m_buy.group(2)

    return {
        "ku_type": ku_type,
        "amount": amount,
        "asset": asset,
        "order_id": order_id,
    }


def format_notification(subject: str, dt_utc: datetime, parsed: dict) -> str:
    dt_msk = dt_utc.astimezone(MSK_TZ)
    dt_str = dt_msk.strftime("%Y-%m-%d %H:%M:%S")

    ku_type = parsed.get("ku_type")
    amount = parsed.get("amount")
    asset = parsed.get("asset")
    order_id = parsed.get("order_id")

    lines = [f"📩 Новое письмо KuCoin ({dt_str} МСК)", f"Тема: {subject}"]

    type_map = {
        "p2p_payment_completed": "✅ P2P: Buyer отметил 'Payment Completed'",
        "p2p_sell_completed": "✅ P2P: Sell ордер завершён",
        "p2p_received_funding": "💰 Получен перевод на Funding Account",
        "deposit": "💚 Депозит",
        "withdrawal": "🔻 Вывод средств",
        "p2p_buy_submitted": "🟢 Создан P2P buy ордер",
        "unknown": "ℹ️ Неопознанное письмо KuCoin",
    }

    header = type_map.get(ku_type, "ℹ️ Письмо KuCoin")
    lines.append(header)

    if amount is not None and asset:
        lines.append(f"Сумма: {amount} {asset}")
    if order_id:
        lines.append(f"Order ID: {order_id}")

    return "\n".join(lines)


def gmail_worker(bot):
    """
    Фоновый поток: каждые ~15 секунд опрашивает Gmail,
    забирает новые письма KuCoin и шлёт уведомления в Telegram.
    """
    print("[GMAIL] Запускаю Gmail worker...")
    try:
        service = get_gmail_service()
    except Exception as e:
        print(f"[GMAIL] Ошибка инициализации Gmail: {e}")
        return

    seen_ids = db_get_all_ids()
    print(f"[GMAIL] Уже в базе писем: {len(seen_ids)}")

    while True:
        try:
            # Берём свежие письма KuCoin P2P из INBOX
            result = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    labelIds=["INBOX"],
                    q="KuCoin",
                    maxResults=10,
                )
                .execute()
            )
            msgs = result.get("messages", [])

            # Обрабатываем с самых старых к новым
            for m in reversed(msgs):
                msg_id = m["id"]
                if msg_id in seen_ids:
                    continue

                full = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )

                internal_ms = int(full.get("internalDate", "0"))
                dt_utc = datetime.fromtimestamp(
                    internal_ms / 1000.0, tz=timezone.utc
                )

                headers = full.get("payload", {}).get("headers", [])
                subject = ""
                for h in headers:
                    if h.get("name") == "Subject":
                        subject = h.get("value", "")
                        break

                body_text = extract_text_from_payload(full.get("payload", {}))

                parsed = parse_kucoin_email(subject, body_text)
                ku_type = parsed["ku_type"]
                amount = parsed["amount"]
                asset = parsed["asset"]
                order_id = parsed["order_id"]

                # Сохраняем в базу
                db_save_email(
                    msg_id,
                    dt_utc.timestamp(),
                    ku_type,
                    subject,
                    amount,
                    asset,
                    order_id,
                )
                seen_ids.add(msg_id)

                # Формируем и шлём уведомление
                text = format_notification(subject, dt_utc, parsed)
                for chat_id in ALLOWED_CHAT_IDS:
                    try:
                        bot.send_message(chat_id=chat_id, text=text)
                    except Exception as e:
                        print(f"[TG] Ошибка отправки уведомления: {e}")

        except Exception as e:
            print(f"[GMAIL] Ошибка при опросе Gmail: {e}")

        time.sleep(15)


# ================== ОТЧЁТЫ ==================


def aggregate_stats(rows):
    stats = {
        "total_emails": len(rows),
        "deposit_sum": 0.0,
        "withdraw_sum": 0.0,
        "p2p_sell_sum": 0.0,
        "p2p_buy_sum": 0.0,
        "payment_completed_count": 0,
        "received_funding_sum": 0.0,
    }

    for r in rows:
        ku_type = r["ku_type"]
        amount = r["amount"] if r["amount"] is not None else 0.0
        asset = r["asset"]

        # Считаем только USDT (для сумм)
        is_usdt = (asset == "USDT")

        if ku_type == "deposit" and is_usdt:
            stats["deposit_sum"] += amount
        elif ku_type == "withdrawal" and is_usdt:
            stats["withdraw_sum"] += amount
        elif ku_type == "p2p_sell_completed" and is_usdt:
            stats["p2p_sell_sum"] += amount
        elif ku_type == "p2p_buy_submitted" and is_usdt:
            stats["p2p_buy_sum"] += amount
        elif ku_type == "p2p_payment_completed":
            stats["payment_completed_count"] += 1
        elif ku_type == "p2p_received_funding" and is_usdt:
            stats["received_funding_sum"] += amount

    return stats


def build_report_text(start_msk, end_msk, rows, start_balance=None, end_balance=None):
    stats = aggregate_stats(rows)

    start_str = start_msk.strftime("%Y-%m-%d %H:%M")
    end_str = end_msk.strftime("%Y-%m-%d %H:%M")

    lines = [
        f"📊 Отчёт KuCoin P2P",
        f"Период: {start_str} — {end_str} (МСК)",
        "",
        f"Всего писем KuCoin: {stats['total_emails']}",
        f"Депозиты: {stats['deposit_sum']:.2f} USDT",
        f"Выводы: {stats['withdraw_sum']:.2f} USDT",
        f"P2P SELL завершено (объём): {stats['p2p_sell_sum']:.2f} USDT",
        f"P2P BUY заявок (объём): {stats['p2p_buy_sum']:.2f} USDT",
        f"'Payment Completed' (покупатель отметил оплату): {stats['payment_completed_count']}",
        f"Получено на Funding Account: {stats['received_funding_sum']:.2f} USDT",
    ]

    if start_balance is not None and end_balance is not None:
        pnl = end_balance - start_balance
        if start_balance != 0:
            pct = pnl / start_balance * 100
        else:
            pct = 0.0
        lines.append("")
        lines.append(
            f"💼 Баланс в начале: {start_balance:.2f} USDT\n"
            f"💼 Баланс в конце: {end_balance:.2f} USDT\n"
            f"💰 PnL (по балансу): {pnl:.2f} USDT ({pct:+.2f}%)"
        )

    return "\n".join(lines)


# ================== ТЕЛЕГРАМ КОМАНДЫ ==================


def cmd_start(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return
    update.message.reply_text(
        "Бот для статистики KuCoin P2P через Gmail.\n\n"
        "Он в реальном времени читает письма KuCoin и шлёт уведомления.\n\n"
        "Команды:\n"
        "/stats_today – отчёт за сегодня (МСК) с запросом баланса\n"
        "/stats_range YYYY-MM-DD YYYY-MM-DD – отчёт за период без баланса\n"
        "/last_emails – последние 10 писем KuCoin из базы"
    )


def cmd_last_emails(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    rows = db_get_last_n(10)
    if not rows:
        update.message.reply_text("В базе пока нет писем KuCoin.")
        return

    lines = ["Последние 10 писем KuCoin:"]
    for r in rows:
        dt_utc = datetime.fromtimestamp(r["ts_utc"], tz=timezone.utc)
        dt_msk = dt_utc.astimezone(MSK_TZ)
        dt_str = dt_msk.strftime("%Y-%m-%d %H:%M")
        ku_type = r["ku_type"]
        subject = r["subject"]
        amt = r["amount"]
        asset = r["asset"]
        part = f"{dt_str} | {ku_type} | {subject}"
        if amt is not None and asset:
            part += f" | {amt} {asset}"
        lines.append(part)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... (обрезано)"
    update.message.reply_text(text)


# ====== /stats_today как диалог (баланс в начале и в конце) ======


def cmd_stats_today(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    update.message.reply_text(
        "Отчёт за СЕГОДНЯ по МСК.\n\n"
        "Введи баланс на споте в НАЧАЛЕ дня (в USDT):"
    )
    return REPORT_DAY_START


def stats_today_start(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return ConversationHandler.END

    text = update.message.text.strip().replace(",", ".")
    try:
        start_balance = float(text)
    except ValueError:
        update.message.reply_text("Не понял число. Введи ещё раз баланс в НАЧАЛЕ дня (USDT):")
        return REPORT_DAY_START

    context.user_data["start_balance"] = start_balance
    update.message.reply_text("Теперь введи ТЕКУЩИЙ баланс на споте (в USDT):")
    return REPORT_DAY_END


def stats_today_end(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return ConversationHandler.END

    text = update.message.text.strip().replace(",", ".")
    try:
        end_balance = float(text)
    except ValueError:
        update.message.reply_text("Не понял число. Введи ещё раз ТЕКУЩИЙ баланс (USDT):")
        return REPORT_DAY_END

    start_balance = context.user_data.get("start_balance")

    # Период "сегодня" по МСК
    now_msk = datetime.now(MSK_TZ)
    start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    end_msk = start_msk + timedelta(days=1)

    start_utc = start_msk.astimezone(timezone.utc).timestamp()
    end_utc = end_msk.astimezone(timezone.utc).timestamp()

    rows = db_get_emails_between(start_utc, end_utc)
    report = build_report_text(start_msk, end_msk, rows, start_balance, end_balance)

    if len(report) > 4000:
        report = report[:4000] + "\n... (обрезано)"
    update.message.reply_text(report)

    return ConversationHandler.END


def stats_today_cancel(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return ConversationHandler.END
    update.message.reply_text("Ок, отменил отчёт.")
    return ConversationHandler.END


# ====== /stats_range YYYY-MM-DD YYYY-MM-DD ======


def cmd_stats_range(update: Update, context: CallbackContext):
    if not allowed_user(update):
        return

    if len(context.args) != 2:
        update.message.reply_text(
            "Использование:\n"
            "/stats_range 2025-11-10 2025-11-15"
        )
        return

    try:
        start_msk = datetime.strptime(context.args[0], "%Y-%m-%d").replace(
            tzinfo=MSK_TZ
        )
        end_msk = datetime.strptime(context.args[1], "%Y-%m-%d").replace(
            tzinfo=MSK_TZ
        ) + timedelta(days=1)
    except ValueError:
        update.message.reply_text("Неправильный формат даты. Нужно: YYYY-MM-DD YYYY-MM-DD")
        return

    start_utc = start_msk.astimezone(timezone.utc).timestamp()
    end_utc = end_msk.astimezone(timezone.utc).timestamp()

    rows = db_get_emails_between(start_utc, end_utc)
    report = build_report_text(start_msk, end_msk, rows)

    if len(report) > 4000:
        report = report[:4000] + "\n... (обрезано)"
    update.message.reply_text(report)


# ================== FAKE WEB SERVER ДЛЯ RAILWAY ==================


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def run_fake_webserver():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    print(f"[WEB] Fake HTTP server started on port {port}")
    server.serve_forever()


# ================== MAIN ==================


def main():
    init_db()

    token = CFG["TELEGRAM_TOKEN"]
    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher

    # /start, /last_emails, /stats_range
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("last_emails", cmd_last_emails))
    dp.add_handler(CommandHandler("stats_range", cmd_stats_range))

    # Диалог для /stats_today
    conv = ConversationHandler(
        entry_points=[CommandHandler("stats_today", cmd_stats_today)],
        states={
            REPORT_DAY_START: [
                MessageHandler(Filters.text & ~Filters.command, stats_today_start)
            ],
            REPORT_DAY_END: [
                MessageHandler(Filters.text & ~Filters.command, stats_today_end)
            ],
        },
        fallbacks=[CommandHandler("cancel", stats_today_cancel)],
    )
    dp.add_handler(conv)

    # Фейковый веб-сервер для Railway (healthcheck)
    threading.Thread(target=run_fake_webserver, daemon=True).start()

    # Фоновый Gmail worker
    threading.Thread(target=gmail_worker, args=(updater.bot,), daemon=True).start()

    print("[TG] Бот запущен. Слушаю Telegram и Gmail...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()