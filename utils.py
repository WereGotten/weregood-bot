# utils.py
import re
import time
import requests
import threading
import secrets
import datetime
from collections import defaultdict
from functools import wraps
from flask import request, jsonify
from config import TELEGRAM_TOKEN, ADMIN_SECRET, DEBUG_MODE

# === RATE LIMITING ===
rate_limits = defaultdict(list)
admin_failures = defaultdict(int)

def check_rate_limit(key: str, limit: int = 30, window_seconds: int = 10) -> bool:
    if key.startswith("click_"):
        limit = 1000
    elif key.startswith("ticket_"):
        limit = 10
        window_seconds = 60
    elif key.startswith("ad_200_"):
        limit = 40
        window_seconds = 86400
    elif key.startswith("ad_limit_"):
        limit = 15
        window_seconds = 86400
    elif key.startswith("status_"):
        limit = 180
        window_seconds = 60
    elif key.startswith("leaderboard_"):
        limit = 90
        window_seconds = 60
    elif key.startswith("lottery_"):
        limit = 90
        window_seconds = 60
    elif key.startswith("recent_players_"):
        limit = 90
        window_seconds = 60
    elif key.startswith("buy_"):
        limit = 30
        window_seconds = 30
    elif key.startswith("vote_"):
        limit = 15
        window_seconds = 60
    elif key.startswith("wallet_"):
        limit = 30
        window_seconds = 60
    elif key.startswith("register_"):
        limit = 30
        window_seconds = 60
    elif key.startswith("promo_"):
        limit = 20
        window_seconds = 60
    elif key.startswith("fortune_bet_"):
        limit = 10
        window_seconds = 30
    now = time.time()
    rate_limits[key] = [t for t in rate_limits[key] if now - t < window_seconds]
    if len(rate_limits[key]) >= limit:
        return False
    rate_limits[key].append(now)
    return True

def check_admin_bruteforce(ip: str) -> bool:
    return admin_failures[ip] < 10

def record_admin_failure(ip: str):
    admin_failures[ip] += 1
    threading.Timer(3600, lambda: admin_failures.pop(ip, None)).start()

def validate_user_id(user_id):
    try:
        user_id = int(user_id)
        return user_id > 0, user_id
    except (TypeError, ValueError):
        return False, None

def validate_ton_address(address: str) -> bool:
    if not address or not isinstance(address, str):
        return False
    if len(address) == 48 and re.match(r'^[A-Za-z0-9_-]{48}$', address):
        return True
    if len(address) == 64 and re.match(r'^[0-9a-fA-F]{64}$', address):
        return True
    if len(address) == 66 and re.match(r'^0:[0-9a-fA-F]{64}$', address):
        return True
    return False

def sanitize_string(text, max_length=100):
    if not isinstance(text, str):
        return ''
    text = re.sub(r'[<>\"\'();]', '', text)
    return text[:max_length]

def escape_html(text: str) -> str:
    if not text:
        return ''
    html_escape_table = {
        "&": "&amp;",
        '"': "&quot;",
        "'": "&apos;",
        ">": "&gt;",
        "<": "&lt;",
    }
    return "".join(html_escape_table.get(c, c) for c in text)

def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            data["reply_markup"] = reply_markup
        verify_ssl = not DEBUG_MODE
        requests.post(url, json=data, timeout=5, verify=verify_ssl)
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")

def check_origin():
    origin = request.headers.get('Origin', '')
    allowed_origins = [
        "https://weregood.ru",
        "https://www.weregood.ru",
        "https://web.telegram.org",
        "https://t.me",
        "http://weregood.ru",
        "http://80.90.185.16:5000",
        "https://80.90.185.16"
    ]
    if DEBUG_MODE:
        return True
    return origin in allowed_origins or origin == ''

def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = request.remote_addr
        if not DEBUG_MODE and not check_admin_bruteforce(client_ip):
            return jsonify({"error": "Too many failed attempts"}), 429
        key = request.args.get('key') or request.headers.get('X-Admin-Key')
        if not key or not secrets.compare_digest(key, ADMIN_SECRET):
            if not DEBUG_MODE:
                record_admin_failure(client_ip)
            return jsonify({"error": "Доступ запрещён"}), 403
        admin_failures.pop(client_ip, None)
        return f(*args, **kwargs)
    return decorated_function

def check_ad_cooldown(user_id: int, ad_type: str, cooldown_minutes: int, daily_limit: int):
    from database import db
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT COUNT(*) FROM ad_watch_history 
            WHERE user_id = ? AND ad_type = ? 
            AND watched_at > datetime('now', '-1 day')
        ''', (user_id, ad_type))
        daily_count = cursor.fetchone()[0]
        if daily_count >= daily_limit:
            return False, f"Дневной лимит ({daily_limit} раз) исчерпан"
        cursor.execute('''
            SELECT watched_at FROM ad_watch_history 
            WHERE user_id = ? AND ad_type = ? 
            ORDER BY watched_at DESC LIMIT 1
        ''', (user_id, ad_type))
        last_watch = cursor.fetchone()
        if last_watch:
            last_time = datetime.datetime.fromisoformat(last_watch[0])
            time_passed = (datetime.datetime.now() - last_time).total_seconds() / 60
            if time_passed < cooldown_minutes:
                remaining = int(cooldown_minutes - time_passed)
                return False, f"Подождите {remaining} минут перед следующим просмотром"
        return True, "OK"

def record_ad_watch(user_id: int, ad_type: str):
    from database import db
    with db.get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO ad_watch_history (user_id, ad_type, watched_at)
            VALUES (?, ?, datetime('now'))
        ''', (user_id, ad_type))

def string_to_hex_payload(text: str) -> str:
    if not text:
        return "00000000"
    return "00000000" + text.encode('utf-8').hex()

def raw_to_user_friendly(raw_address: str) -> str:
    import codecs
    import base64
    try:
        if not raw_address or ":" not in raw_address:
            return raw_address.strip()
        parts = raw_address.split(":")
        workchain = int(parts[0])
        hex_address = parts[1].strip()
        workchain_byte = workchain if workchain >= 0 else 256 + workchain
        tag = 0x11
        address_bytes = bytearray([tag, workchain_byte]) + bytearray(codecs.decode(hex_address, 'hex'))
        POLY = 0x1021
        crc = 0
        for byte in address_bytes:
            crc ^= (byte << 8)
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ POLY
                else:
                    crc <<= 1
                crc &= 0xFFFF
        address_bytes += bytearray([crc >> 8, crc & 0xFF])
        friendly = base64.urlsafe_b64encode(address_bytes).decode('utf-8').rstrip('=')
        return friendly
    except Exception as e:
        print(f"Ошибка конвертации адреса: {e}")
        return raw_address

def check_ton_transaction(sender_wallet, expected_amount, user_id):
    import requests
    import os
    try:
        expected_comment = f"WereGood:{user_id}"
        raw_project_address = "0:69fa7db713b9158c72970e3d577b6b3c2605e0f109fbb0443af97c44fd07be3f"
        url = f"https://toncenter.com/api/v2/getTransactions?address={raw_project_address}&limit=40"
        api_key = os.getenv('TONCENTER_API_KEY')
        if api_key:
            url += f"&api_key={api_key}"
        response = requests.get(url, timeout=12)
        if response.status_code != 200:
            return False, 0, None
        transactions = response.json().get('result', [])
        clean_sender = str(sender_wallet).strip()
        friendly_sender = raw_to_user_friendly(clean_sender).lower() if clean_sender.startswith("0:") else clean_sender.lower()
        for tx_data in transactions:
            in_msg = tx_data.get('in_msg', {})
            if not in_msg:
                continue
            value_nano = int(in_msg.get('value', '0'))
            amount_ton = value_nano / 1e9
            source_address = str(in_msg.get('source', '')).strip().lower()
            comment = in_msg.get('message', '').strip()
            if not comment and in_msg.get('msg_data', {}).get('@type') == 'msg.dataText':
                comment = in_msg.get('msg_data', {}).get('text', '').strip()
            is_comment_match = (expected_comment in comment)
            is_wallet_match = friendly_sender and (friendly_sender in source_address or source_address in friendly_sender)
            if (is_comment_match or is_wallet_match) and (amount_ton >= (expected_amount - 0.02)):
                tx_hash = tx_data.get('transaction_id', {}).get('hash')
                return True, amount_ton, tx_hash
        return False, 0, None
    except Exception as e:
        print(f"Ошибка в check_ton_transaction: {e}")
        return False, 0, None