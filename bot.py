import sqlite3
import random
import datetime
import threading
import time
import hashlib
import json
from functools import wraps
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO, emit
import requests
from contextlib import contextmanager
from collections import defaultdict
import urllib3

urllib3.disable_warnings()

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = "8723199975:AAEL6n1DEV8pRQnYwZUqB8aJwCd8h7yRkNU"
WEBHOOK_URL = "https://hedy-chylophyllous-laurette.ngrok-free.dev"

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = 'weregood_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")


# ========== ЗАГОЛОВКИ ДЛЯ NGrok И CORS ==========
@app.after_request
def add_ngrok_and_cors_headers(response):
    response.headers['ngrok-skip-browser-warning'] = 'true'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return response


# ========== АДМИН-НАСТРОЙКИ ==========
ADMIN_IDS = [5264622363]
ADMIN_SECRET = "weregood_admin_2026_secure_key_xyz789"

# ========== TON CONNECT 2.0 НАСТРОЙКИ ==========
PROJECT_WALLET_ADDRESS = "UQCa7xhdvDiaKuH6SFLgzLQFH8oRwwS2ElN1s283WnGM4fYB"
TONCENTER_API_KEY = "5ac5fa2e76aa2d18be2033330bd224a743a7a7bbdcfaafc056392d7c887ef76c"

# Глобальные переменные
pending_invoices = {}
online_users = {}
banned_users = {}

# ========== НАСТРОЙКИ CRYPTO PAY ==========
CRYPTO_PAY_TOKEN = "584394:AAwGoJMaqLEEgAL3rXU9SMg4C3nuTzIH65Z"
CRYPTO_PAY_TESTNET = False

# ========== ЕЖЕДНЕВНЫЕ НАГРАДЫ ==========
DAILY_REWARDS = {
    1: {"wg": 15, "lp": 0, "energy_limit": 0, "description": "15 WG"},
    2: {"wg": 50, "lp": 0, "energy_limit": 0, "description": "50 WG"},
    3: {"wg": 0, "lp": 0, "energy_limit": 10, "description": "+10 к лимиту энергии"},
    4: {"wg": 0, "lp": 10, "energy_limit": 0, "description": "10 LP"},
    5: {"wg": 0, "lp": 0, "energy_limit": 15, "description": "+15 к лимиту энергии"},
    6: {"wg": 150, "lp": 0, "energy_limit": 0, "description": "150 WG"},
    7: {"wg": 0, "lp": 20, "energy_limit": 0, "description": "20 LP"}
}


# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self.local = threading.local()

    @contextmanager
    def get_cursor(self):
        if not hasattr(self.local, 'conn'):
            self.local.conn = sqlite3.connect(self.db_file, check_same_thread=False)
            self.local.conn.row_factory = sqlite3.Row
        cursor = self.local.conn.cursor()
        try:
            yield cursor
            self.local.conn.commit()
        except Exception:
            self.local.conn.rollback()
            raise
        finally:
            cursor.close()


db = Database("database.db")

# Создание таблиц
with db.get_cursor() as cursor:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            wg REAL DEFAULT 0,
            lp INTEGER DEFAULT 0,
            energy INTEGER DEFAULT 500,
            last_energy_update REAL,
            tickets TEXT DEFAULT '[]',
            total_clicks INTEGER DEFAULT 0,
            upgrade_counts TEXT DEFAULT '{"1":0,"2":0,"3":0}',
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            ticket_counter INTEGER DEFAULT 0,
            referral_code TEXT DEFAULT '',
            referrer_id INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            dislikes INTEGER DEFAULT 0,
            settings TEXT DEFAULT '{"theme":"dark"}',
            avatar_url TEXT DEFAULT '',
            usdt REAL DEFAULT 0,
            wins INTEGER DEFAULT 0,
            role TEXT DEFAULT 'player',
            stars INTEGER DEFAULT 0,
            max_energy INTEGER DEFAULT 500,
            energy_upgrades INTEGER DEFAULT 0,
            energy_limit_upgrades INTEGER DEFAULT 0,
            unlocked_prefixes TEXT DEFAULT '["player"]',
            tutorial_completed INTEGER DEFAULT 0,
            ton_wallet TEXT DEFAULT ''
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lottery (
            id INTEGER PRIMARY KEY,
            prize_pool REAL DEFAULT 0,
            tickets TEXT DEFAULT '[]',
            winning_numbers TEXT DEFAULT '',
            last_draw TIMESTAMP,
            global_ticket_counter INTEGER DEFAULT 0,
            is_drawn BOOLEAN DEFAULT 0,
            draw_time TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            username TEXT,
            first_name TEXT,
            total_spent_lp INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voter_id INTEGER,
            target_id INTEGER,
            vote_type TEXT,
            last_vote_time TEXT,
            UNIQUE(voter_id, target_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lottery_tickets_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ticket_number INTEGER,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS successful_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            telegram_payment_charge_id TEXT UNIQUE,
            payload TEXT,
            amount INTEGER,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            action TEXT,
            user_id INTEGER,
            username TEXT,
            details TEXT,
            log_type TEXT DEFAULT 'user'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS withdrawal_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount REAL,
            address TEXT,
            network TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            processed_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stats_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            clicks INTEGER DEFAULT 0,
            ad_views INTEGER DEFAULT 0,
            stars_donated INTEGER DEFAULT 0,
            online_peak INTEGER DEFAULT 0,
            tickets_sold INTEGER DEFAULT 0,
            new_users INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_rewards (
            user_id INTEGER PRIMARY KEY,
            current_day INTEGER DEFAULT 0,
            last_claim_date TEXT,
            streak_start_date TEXT,
            recovered_count INTEGER DEFAULT 0
        )
    ''')
    cursor.execute("SELECT * FROM lottery LIMIT 1")
    if not cursor.fetchone():
        cursor.execute("INSERT INTO lottery (prize_pool, tickets, winning_numbers, is_drawn) VALUES (0, '[]', '', 0)")


# ========== ФУНКЦИИ ДЛЯ ЛОГОВ ==========
def add_log(action, user_id, username, old_value=None, new_value=None, currency="", details=""):
    log_message = action
    if old_value is not None and new_value is not None:
        if currency == "wg":
            log_message += f" | WG: {old_value:.2f} → {new_value:.2f}"
        elif currency == "lp":
            log_message += f" | LP: {old_value} → {new_value}"
        elif currency == "usdt":
            log_message += f" | USDT: {old_value:.2f} → {new_value:.2f}"
        elif currency == "energy":
            log_message += f" | Энергия: {old_value} → {new_value}"
        elif currency == "stars":
            log_message += f" | Stars: {old_value} → {new_value}"
        else:
            log_message += f" | {old_value} → {new_value}"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO system_logs (timestamp, action, user_id, username, details, log_type)
            VALUES (?, ?, ?, ?, ?, 'user')
        ''', (timestamp, log_message, user_id, username, details))
    return {"id": 0, "timestamp": timestamp, "action": log_message, "user_id": user_id, "username": username,
            "details": details}


def add_admin_log(action, admin_id, admin_name, target_id=None, target_name=None, details=""):
    if target_id:
        log_msg = f"👑 {action} | Админ: {admin_name} (ID: {admin_id}) | Игрок: {target_name} (ID: {target_id})"
    else:
        log_msg = f"👑 {action} | Админ: {admin_name} (ID: {admin_id})"
    if details:
        log_msg += f" | {details}"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO system_logs (timestamp, action, user_id, username, details, log_type)
            VALUES (?, ?, ?, ?, ?, 'admin')
        ''', (timestamp, log_msg, admin_id, admin_name, details))
    return {"id": 0, "timestamp": timestamp, "action": log_msg, "user_id": admin_id, "username": admin_name,
            "details": details}


def get_logs(log_type='all', limit=500, date=None, action_filter=None, user_id_filter=None):
    with db.get_cursor() as cursor:
        query = "SELECT * FROM system_logs"
        conditions = []
        params = []
        if log_type == 'admin':
            conditions.append("log_type = 'admin'")
        elif log_type == 'user':
            conditions.append("log_type = 'user'")
        if date:
            conditions.append("date(timestamp) = ?")
            params.append(date)
        if action_filter:
            conditions.append("action LIKE ?")
            params.append(f'%{action_filter}%')
        if user_id_filter and user_id_filter.isdigit():
            conditions.append("user_id = ?")
            params.append(int(user_id_filter))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        logs = []
        for row in rows:
            logs.append({
                "id": row['id'],
                "timestamp": row['timestamp'],
                "action": row['action'],
                "user_id": row['user_id'],
                "username": row['username'],
                "details": row['details'],
                "type": row['log_type']
            })
        return logs


# ========== СТАТИСТИКА ==========
def update_stats_history(date, clicks=0, ad_views=0, stars=0, online=0, tickets=0, users=0):
    with db.get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO stats_history (date, clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                clicks = clicks + ?,
                ad_views = ad_views + ?,
                stars_donated = stars_donated + ?,
                online_peak = MAX(online_peak, ?),
                tickets_sold = tickets_sold + ?,
                new_users = new_users + ?
        ''', (date, clicks, ad_views, stars, online, tickets, users,
              clicks, ad_views, stars, online, tickets, users))


def get_stats_history(period='week', metric='clicks'):
    now = datetime.datetime.now()
    data = []
    labels = []
    with db.get_cursor() as cursor:
        if period == 'day':
            for i in range(24):
                labels.append(f"{i}:00")
                date_key = now.strftime("%Y-%m-%d")
                cursor.execute(
                    "SELECT clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users FROM stats_history WHERE date = ?",
                    (date_key,))
                row = cursor.fetchone()
                if row:
                    if metric == 'clicks':
                        data.append(row['clicks'] or 0)
                    elif metric == 'ad_views':
                        data.append(row['ad_views'] or 0)
                    elif metric == 'stars':
                        data.append(row['stars_donated'] or 0)
                    elif metric == 'online':
                        data.append(row['online_peak'] or 0)
                    elif metric == 'tickets':
                        data.append(row['tickets_sold'] or 0)
                    elif metric == 'users':
                        data.append(row['new_users'] or 0)
                    else:
                        data.append(0)
                else:
                    data.append(0)
        elif period == 'week':
            for i in range(6, -1, -1):
                date = (now - datetime.timedelta(days=i)).strftime("%d.%m")
                labels.append(date)
                date_key = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                cursor.execute(
                    "SELECT clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users FROM stats_history WHERE date = ?",
                    (date_key,))
                row = cursor.fetchone()
                if row:
                    if metric == 'clicks':
                        data.append(row['clicks'] or 0)
                    elif metric == 'ad_views':
                        data.append(row['ad_views'] or 0)
                    elif metric == 'stars':
                        data.append(row['stars_donated'] or 0)
                    elif metric == 'online':
                        data.append(row['online_peak'] or 0)
                    elif metric == 'tickets':
                        data.append(row['tickets_sold'] or 0)
                    elif metric == 'users':
                        data.append(row['new_users'] or 0)
                    else:
                        data.append(0)
                else:
                    data.append(0)
        elif period == 'month':
            for i in range(29, -1, -1):
                date = (now - datetime.timedelta(days=i)).strftime("%d.%m")
                labels.append(date)
                date_key = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                cursor.execute(
                    "SELECT clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users FROM stats_history WHERE date = ?",
                    (date_key,))
                row = cursor.fetchone()
                if row:
                    if metric == 'clicks':
                        data.append(row['clicks'] or 0)
                    elif metric == 'ad_views':
                        data.append(row['ad_views'] or 0)
                    elif metric == 'stars':
                        data.append(row['stars_donated'] or 0)
                    elif metric == 'online':
                        data.append(row['online_peak'] or 0)
                    elif metric == 'tickets':
                        data.append(row['tickets_sold'] or 0)
                    elif metric == 'users':
                        data.append(row['new_users'] or 0)
                    else:
                        data.append(0)
                else:
                    data.append(0)
        else:
            for i in range(11, -1, -1):
                month_date = now - datetime.timedelta(days=30 * i)
                labels.append(month_date.strftime("%b %Y"))
                month_start = month_date.strftime("%Y-%m")
                cursor.execute(
                    "SELECT SUM(clicks) as clicks, SUM(ad_views) as ad_views, SUM(stars_donated) as stars_donated, MAX(online_peak) as online_peak, SUM(tickets_sold) as tickets_sold, SUM(new_users) as new_users FROM stats_history WHERE date LIKE ?",
                    (f'{month_start}%',))
                row = cursor.fetchone()
                if row:
                    if metric == 'clicks':
                        data.append(row['clicks'] or 0)
                    elif metric == 'ad_views':
                        data.append(row['ad_views'] or 0)
                    elif metric == 'stars':
                        data.append(row['stars_donated'] or 0)
                    elif metric == 'online':
                        data.append(row['online_peak'] or 0)
                    elif metric == 'tickets':
                        data.append(row['tickets_sold'] or 0)
                    elif metric == 'users':
                        data.append(row['new_users'] or 0)
                    else:
                        data.append(0)
                else:
                    data.append(0)
    return {"labels": labels, "data": data}


# ========== ЗАЯВКИ НА ВЫВОД ==========
def create_withdrawal_request_db(user_id, username, amount, address, network):
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO withdrawal_requests (user_id, username, amount, address, network, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        ''', (user_id, username, amount, address, network, created_at))
        withdrawal_id = cursor.lastrowid
    user = get_user(user_id)
    add_log(f"💸 Создал заявку на вывод {amount} USDT", user_id, username, old_value=user['usdt'],
            new_value=user['usdt'] - amount, currency="usdt")
    return {"id": withdrawal_id, "user_id": user_id, "username": username, "amount": amount, "address": address,
            "network": network, "status": "pending", "created_at": created_at, "processed_at": None}


def get_withdrawal_requests_db():
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM withdrawal_requests ORDER BY id DESC")
        rows = cursor.fetchall()
        withdrawals = []
        for row in rows:
            withdrawals.append(
                {"id": row['id'], "user_id": row['user_id'], "username": row['username'], "amount": row['amount'],
                 "address": row['address'], "network": row['network'], "status": row['status'],
                 "created_at": row['created_at'], "processed_at": row['processed_at']})
        return withdrawals


def process_withdrawal_db(withdrawal_id, status, admin_id, admin_name):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM withdrawal_requests WHERE id = ?", (withdrawal_id,))
        w = cursor.fetchone()
        if not w: return False
        processed_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''UPDATE withdrawal_requests SET status = ?, processed_at = ? WHERE id = ?''',
                       (status, processed_at, withdrawal_id))
        if status == "completed":
            send_telegram_message(w['user_id'],
                                  f"✅ Ваша заявка на вывод {w['amount']} USDT одобрена! Средства отправлены на указанный адрес.")
        elif status == "rejected":
            user = get_user(w['user_id'])
            update_user(w['user_id'], usdt=user['usdt'] + w['amount'])
            send_telegram_message(w['user_id'],
                                  f"❌ Ваша заявка на вывод {w['amount']} USDT отклонена. Средства возвращены на баланс.")
        return True


def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        data = {"chat_id": chat_id, "text": text}
        if reply_markup: data["reply_markup"] = reply_markup
        requests.post(url, json=data, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")


# ========== ОСНОВНЫЕ ФУНКЦИИ ==========
def get_user(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row:
            now = time.time()
            ref_code = hashlib.md5(str(user_id).encode()).hexdigest()[:8]
            founder_id = 5264622363
            role = "founder" if user_id == founder_id else "player"
            unlocked = json.dumps(["player", "founder"]) if role == "founder" else json.dumps(["player"])
            try:
                cursor.execute('''
                    INSERT INTO users (user_id, wg, lp, energy, last_energy_update, tickets, total_clicks,
                    upgrade_counts, ticket_counter, referral_code, referrer_id, likes, dislikes, settings,
                    username, first_name, last_name, avatar_url, usdt, wins, role, stars,
                    max_energy, energy_upgrades, energy_limit_upgrades, unlocked_prefixes, tutorial_completed, ton_wallet)
                    VALUES (?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, 0, 0, 0,
                    '{"theme":"dark"}', ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '')
                ''', (user_id, now, ref_code, "", "", "", "", role, unlocked))
            except:
                cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
                row = cursor.fetchone()
        if not row:
            return {"user_id": user_id, "wg": 0, "lp": 0, "energy": 500, "last_energy_update": time.time(),
                    "tickets": [], "total_clicks": 0, "upgrade_counts": {1: 0, 2: 0, 3: 0}, "username": "",
                    "first_name": "", "last_name": "", "ticket_counter": 0, "referral_code": "", "referrer_id": 0,
                    "likes": 0, "dislikes": 0, "settings": {"theme": "dark"}, "avatar_url": "", "usdt": 0, "wins": 0,
                    "role": "player", "stars": 0, "max_energy": 500, "energy_upgrades": 0, "energy_limit_upgrades": 0,
                    "unlocked_prefixes": ["player"], "ton_wallet": ""}
        upgrade_counts = eval(row['upgrade_counts']) if row['upgrade_counts'] else {1: 0, 2: 0, 3: 0}
        if isinstance(upgrade_counts, dict):
            upgrade_counts = {int(k): v for k, v in upgrade_counts.items()}
        settings = {"theme": "dark"}
        if row['settings']:
            try:
                settings = json.loads(row['settings'])
            except:
                settings = {"theme": "dark"}
        unlocked_prefixes = ["player"]
        if row['unlocked_prefixes']:
            try:
                unlocked_prefixes = json.loads(row['unlocked_prefixes'])
            except:
                unlocked_prefixes = ["player"]
        return {"user_id": row['user_id'], "wg": row['wg'], "lp": row['lp'], "energy": row['energy'],
                "last_energy_update": row['last_energy_update'],
                "tickets": eval(row['tickets']) if row['tickets'] else [], "total_clicks": row['total_clicks'],
                "upgrade_counts": upgrade_counts, "username": row['username'] or '',
                "first_name": row['first_name'] or '', "last_name": row['last_name'] or '',
                "ticket_counter": row['ticket_counter'] or 0, "referral_code": row['referral_code'] or '',
                "referrer_id": row['referrer_id'] or 0, "likes": row['likes'] or 0, "dislikes": row['dislikes'] or 0,
                "settings": settings, "avatar_url": row['avatar_url'] or '',
                "usdt": row['usdt'] if 'usdt' in row.keys() else 0, "wins": row['wins'] if 'wins' in row.keys() else 0,
                "role": row['role'] if 'role' in row.keys() else 'player',
                "stars": row['stars'] if 'stars' in row.keys() else 0,
                "max_energy": row['max_energy'] if 'max_energy' in row.keys() else 500,
                "energy_upgrades": row['energy_upgrades'] if 'energy_upgrades' in row.keys() else 0,
                "energy_limit_upgrades": row['energy_limit_upgrades'] if 'energy_limit_upgrades' in row.keys() else 0,
                "unlocked_prefixes": unlocked_prefixes,
                "tutorial_completed": row['tutorial_completed'] if 'tutorial_completed' in row.keys() else 0,
                "ton_wallet": row['ton_wallet'] if 'ton_wallet' in row.keys() else ''}


def update_user(user_id, **kwargs):
    with db.get_cursor() as cursor:
        for key, value in kwargs.items():
            if value is not None:
                if key in ['upgrade_counts', 'tickets', 'settings', 'unlocked_prefixes']:
                    value = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
                cursor.execute(f"UPDATE users SET {key}=? WHERE user_id=?", (value, user_id))


def calculate_energy(user_data):
    now = time.time()
    last = user_data["last_energy_update"]
    seconds_passed = now - last
    max_energy = user_data.get("max_energy", 500)
    recovery_rate = max_energy / 7200
    recovered = int(seconds_passed * recovery_rate)
    new_energy = min(max_energy, user_data["energy"] + recovered)
    return new_energy, seconds_passed


def update_energy_in_db(user_id, user_data, new_energy):
    now = time.time()
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET energy=?, last_energy_update=? WHERE user_id=?", (new_energy, now, user_id))
    user_data["energy"] = new_energy
    user_data["last_energy_update"] = now
    return new_energy


def spend_energy(user_id, user_data, amount=1):
    current_energy, _ = calculate_energy(user_data)
    if current_energy < amount: return False, current_energy
    new_energy = current_energy - amount
    update_energy_in_db(user_id, user_data, new_energy)
    return True, new_energy


def add_usdt(user_id, amount):
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET usdt = usdt + ? WHERE user_id=?", (amount, user_id))


def add_wins(user_id, amount=1):
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET wins = wins + ? WHERE user_id=?", (amount, user_id))


def add_referral_earning(referrer_id, referred_id, spent_lp):
    earning = spent_lp * 0.05
    with db.get_cursor() as cursor:
        cursor.execute("SELECT lp FROM users WHERE user_id=?", (referrer_id,))
        row = cursor.fetchone()
        if row:
            old_lp = row['lp']
            new_lp = old_lp + earning
            cursor.execute("UPDATE users SET lp = ? WHERE user_id=?", (new_lp, referrer_id))
            cursor.execute(
                "UPDATE referrals SET total_spent_lp = total_spent_lp + ? WHERE referrer_id = ? AND referred_id = ?",
                (spent_lp, referrer_id, referred_id))
            referrer = get_user(referrer_id)
            add_log(f"👥 Получил 5% от трат реферала (+{earning:.2f} LP)", referrer_id, referrer['username'],
                    old_value=old_lp, new_value=new_lp, currency="lp")
            return True
    return False


def create_stars_invoice(chat_id, user_id):
    try:
        title = "✨ Энергетический усилитель"
        description = "Увеличивает максимальную энергию на +50 и даёт +50 LP на баланс!"
        payload = json.dumps({"user_id": user_id, "type": "energy_upgrade"})
        provider_token = ""
        currency = "XTR"
        prices = [{"label": "Энергетический усилитель", "amount": 25}]
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createInvoiceLink"
        data = {"title": title, "description": description, "payload": payload, "provider_token": provider_token,
                "currency": currency, "prices": prices}
        response = requests.post(url, json=data, timeout=10)
        result = response.json()
        if result.get("ok"): return result["result"]
        return None
    except Exception as e:
        print(f"Ошибка в create_stars_invoice: {e}")
        return None


def grant_energy_upgrade(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT energy_upgrades, max_energy, lp FROM users WHERE user_id=?", (user_id,))
        user = cursor.fetchone()
        if not user: return False, "Пользователь не найден", None
        current_upgrades = user['energy_upgrades'] or 0
        if current_upgrades >= 15: return False, "Максимум улучшений! (15/15)", None
        new_upgrades = current_upgrades + 1
        new_max_energy = 500 + (new_upgrades * 50)
        new_lp = (user['lp'] or 0) + 50
        cursor.execute("UPDATE users SET energy_upgrades=?, max_energy=?, lp=? WHERE user_id=?",
                       (new_upgrades, new_max_energy, new_lp, user_id))
        return True, "✨ Улучшение активировано! +50 макс. энергии и +50 LP!", {"energy_upgrades": new_upgrades,
                                                                               "max_energy": new_max_energy,
                                                                               "lp": new_lp}


def handle_successful_payment(chat_id, payment_info):
    try:
        payload = json.loads(payment_info.get('invoice_payload', '{}'))
        user_id = payload.get('user_id')
        payment_charge_id = payment_info.get('telegram_payment_charge_id')
        total_amount = payment_info.get('total_amount', 100)
        stars_amount = total_amount // 100
        if not user_id: return False
        with db.get_cursor() as cursor:
            cursor.execute("SELECT id FROM successful_payments WHERE telegram_payment_charge_id=?",
                           (payment_charge_id,))
            if cursor.fetchone(): return True
            cursor.execute(
                "INSERT INTO successful_payments (user_id, telegram_payment_charge_id, payload, amount) VALUES (?, ?, ?, ?)",
                (user_id, payment_charge_id, payload.get('type', 'energy_upgrade'), stars_amount))
        user = get_user(user_id)
        success, message, data = grant_energy_upgrade(user_id)
        if success:
            send_telegram_message(chat_id, f"✅ {message}\n✨ Макс. энергия: {data['max_energy']}\n💎 LP: {data['lp']}")
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            update_stats_history(today, stars=stars_amount)
        else:
            send_telegram_message(chat_id, f"❌ {message}")
        return success
    except Exception as e:
        print(f"Ошибка в handle_successful_payment: {e}")
        return False


# ========== TON CONNECT 2.0 ФУНКЦИИ ==========
def check_ton_transaction(sender_wallet, expected_amount):
    """Проверка транзакции от указанного кошелька к кошельку проекта"""
    try:
        url = f"https://toncenter.com/api/v2/getTransactions?address={PROJECT_WALLET_ADDRESS}&limit=20"
        if TONCENTER_API_KEY:
            url += f"&api_key={TONCENTER_API_KEY}"

        response = requests.get(url, timeout=15)
        data = response.json()

        if data.get('ok'):
            for tx in data.get('result', []):
                in_msg = tx.get('in_msg', {})
                source = in_msg.get('source', '')
                if source and source.lower() == sender_wallet.lower():
                    amount_nano = int(in_msg.get('value', '0'))
                    amount_ton = amount_nano / 1e9
                    if amount_ton >= expected_amount:
                        return True, amount_ton, tx.get('transaction_id', {}).get('hash')
        return False, 0, None
    except Exception as e:
        print(f"Ошибка проверки TON платежа: {e}")
        return False, 0, None


def get_user_ton_wallet(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT ton_wallet FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row['ton_wallet'] if row else None


# ========== ЛОТЕРЕЯ ==========
lottery_pool = 0
lottery_tickets = []
global_ticket_counter = 0
winning_numbers = []
is_drawn = False
draw_time = None


def load_lottery():
    global lottery_pool, lottery_tickets, global_ticket_counter, winning_numbers, is_drawn, draw_time
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT prize_pool, tickets, global_ticket_counter, winning_numbers, is_drawn, draw_time FROM lottery LIMIT 1")
        row = cursor.fetchone()
        if row:
            lottery_pool = row['prize_pool']
            lottery_tickets = eval(row['tickets']) if row['tickets'] else []
            global_ticket_counter = row['global_ticket_counter']
            winning_numbers = eval(row['winning_numbers']) if row['winning_numbers'] else []
            is_drawn = row['is_drawn'] == 1
            if row['draw_time']:
                try:
                    draw_time = datetime.datetime.fromisoformat(row['draw_time'])
                except:
                    draw_time = None
            else:
                draw_time = None


def save_lottery():
    with db.get_cursor() as cursor:
        cursor.execute(
            "UPDATE lottery SET prize_pool=?, tickets=?, global_ticket_counter=?, winning_numbers=?, is_drawn=?, draw_time=?",
            (lottery_pool, str(lottery_tickets), global_ticket_counter, str(winning_numbers), 1 if is_drawn else 0,
             draw_time))


load_lottery()

UPGRADE_CONFIG = {1: {"base_cost": 1.5, "bonus": 0.01, "name": "Новичок"},
                  2: {"base_cost": 10, "bonus": 0.03, "name": "Профессионал"},
                  3: {"base_cost": 70, "bonus": 0.07, "name": "Мастер"}}


def get_upgrade_cost(upgrade_id, current_count):
    config = UPGRADE_CONFIG[upgrade_id]
    base_cost = config["base_cost"]
    if current_count == 0: return base_cost
    return base_cost * (1.65 ** current_count)


def get_total_earning(upgrade_counts):
    base = 0.01
    total_bonus = 0
    for uid, count in upgrade_counts.items():
        uid_int = int(uid) if isinstance(uid, str) else uid
        if uid_int in UPGRADE_CONFIG:
            total_bonus += UPGRADE_CONFIG[uid_int]["bonus"] * count
    return base + total_bonus


def generate_ticket_numbers():
    return sorted(random.sample(range(1, 81), 12))


def generate_winning_numbers():
    return sorted(random.sample(range(1, 81), 12))


def unlock_prefix(user_id, prefix_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT unlocked_prefixes FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        unlocked = eval(row['unlocked_prefixes']) if row and row['unlocked_prefixes'] else ["player"]
        if prefix_id not in unlocked:
            unlocked.append(prefix_id)
            cursor.execute("UPDATE users SET unlocked_prefixes = ? WHERE user_id=?", (json.dumps(unlocked), user_id))
            return True
    return False


def update_online_count():
    now = time.time()
    to_remove = [uid for uid, last_seen in online_users.items() if now - last_seen > 300]
    for uid in to_remove: del online_users[uid]


def is_banned(user_id):
    if user_id in banned_users:
        ban_info = banned_users[user_id]
        if ban_info["until"] > time.time():
            return True, ban_info
        else:
            del banned_users[user_id]
    return False, None


def ban_user(user_id, days, reason, admin_id):
    until = time.time() + (days * 86400)
    banned_users[user_id] = {"until": until, "reason": reason,
                             "until_date": datetime.datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
                             "banned_by": admin_id}


def unban_user(user_id):
    if user_id in banned_users: del banned_users[user_id]


def buy_ticket(user_id, user_data):
    global lottery_pool, lottery_tickets, global_ticket_counter
    if is_drawn: return False, "Розыгрыш уже прошёл!"
    if user_data["lp"] < 100: return False, "Не хватает LP (нужно 100)"
    bought = len([t for t in lottery_tickets if t.get("user_id") == user_id])
    if bought >= 10: return False, "Уже куплено 10 билетов"
    old_lp = user_data["lp"]
    user_data["lp"] -= 100
    update_user(user_id, lp=user_data["lp"])
    if user_data.get("referrer_id", 0) > 0: add_referral_earning(user_data["referrer_id"], user_id, 100)
    global_ticket_counter += 1
    ticket_num = global_ticket_counter
    if user_data['username'] and user_data['username'] != '':
        display_name = '@' + user_data['username']
    elif user_data['first_name'] and user_data['first_name'] != '':
        display_name = user_data['first_name']
    else:
        display_name = f"Player_{user_id}"
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE lottery SET global_ticket_counter=? WHERE id=1", (global_ticket_counter,))
        cursor.execute(
            "INSERT INTO lottery_tickets_history (user_id, ticket_number, username, created_at) VALUES (?, ?, ?, datetime('now', 'localtime'))",
            (user_id, ticket_num, display_name))
    ticket_numbers = generate_ticket_numbers()
    user_ticket_counter = user_data["ticket_counter"] + 1
    update_user(user_id, ticket_counter=user_ticket_counter)
    ticket_data = {"number": ticket_num, "purchase_number": user_ticket_counter, "numbers": ticket_numbers,
                   "revealed": [False] * 12, "reward_claimed": False, "user_id": user_id}
    lottery_tickets.append(ticket_data)
    lottery_pool = round(lottery_pool + 0.40, 2)
    save_lottery()
    add_log(f"🎫 Купил билет #{ticket_num}", user_id, user_data['username'], old_value=old_lp, new_value=user_data['lp'],
            currency="lp")
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    update_stats_history(today, tickets=1)
    return True, f"Билет #{ticket_num} куплен!"


def perform_draw():
    global winning_numbers, is_drawn, draw_time
    if lottery_tickets:
        winning_numbers = generate_winning_numbers()
        is_drawn = True
        draw_time = datetime.datetime.now()
        save_lottery()
        end_time = draw_time + datetime.timedelta(seconds=1800)
        socketio.emit('draw_completed', {'winning_numbers': winning_numbers,
                                         'message': '🎉 Розыгрыш начался! У вас 30 минут на открытие билетов! ⏰',
                                         'end_time': end_time.isoformat()})
        add_log(f"🎲 Розыгрыш лотереи начался. Выигрышные номера: {winning_numbers}", 0, "System")
        threading.Timer(1800, auto_reveal_and_distribute).start()


def auto_reveal_and_distribute():
    time.sleep(1800)
    if is_drawn:
        for ticket in lottery_tickets:
            if not all(ticket.get("revealed", [])):
                ticket["revealed"] = [True] * 12
        save_lottery()
        add_log(f"⏰ Автоматическое открытие билетов (время вышло)", 0, "System")
        socketio.emit('auto_revealed', {'message': '⏰ 30 минут истекли! Билеты открыты автоматически!'})
        distribute_prizes()
        time.sleep(1800)
        reset_lottery()
        schedule_next_draw()


def schedule_next_draw():
    def wait_and_draw():
        now = datetime.datetime.now()
        next_draw = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= next_draw: next_draw += datetime.timedelta(days=1)
        wait_seconds = (next_draw - now).total_seconds()
        time.sleep(wait_seconds)
        perform_draw()

    threading.Thread(target=wait_and_draw, daemon=True).start()


def distribute_prizes():
    global lottery_pool, lottery_tickets
    if not lottery_tickets: return
    results = []
    for ticket in lottery_tickets:
        if all(ticket.get("revealed", [])):
            matches = sum(1 for i in range(12) if ticket["numbers"][i] in winning_numbers)
            results.append({"user_id": ticket["user_id"], "matches": matches, "ticket": ticket})
    if not results: return
    max_matches = max([r["matches"] for r in results])
    winners = [r for r in results if r["matches"] == max_matches]
    prize_per_winner = round(lottery_pool / len(winners), 2)
    for winner in winners:
        if not winner["ticket"].get("reward_claimed", False):
            winner["ticket"]["reward_claimed"] = True
            old_usdt = get_user(winner["user_id"])['usdt']
            add_usdt(winner["user_id"], prize_per_winner)
            add_wins(winner["user_id"], 1)
            user = get_user(winner["user_id"])
            add_log(f"🏆 ПОБЕДА в лотерее! +{prize_per_winner} USDT (совпадений: {winner['matches']}/12)",
                    winner["user_id"], user['username'], old_value=old_usdt, new_value=user['usdt'], currency="usdt")
            send_telegram_message(winner["user_id"],
                                  f"🎉 ПОБЕДА! +{prize_per_winner} USDT! Совпадений: {winner['matches']}/12")
    save_lottery()
    add_log(f"🎰 Завершение розыгрыша. Призовой фонд {lottery_pool} USDT распределён между {len(winners)} победителями",
            0, "System")
    socketio.emit('prizes_distributed',
                  {'message': f'🏆 Призы распределены! Победители получили по {prize_per_winner} USDT!'})


def reset_lottery():
    global is_drawn, winning_numbers, draw_time, lottery_tickets, lottery_pool, global_ticket_counter
    is_drawn = False
    winning_numbers = []
    draw_time = None
    lottery_tickets = []
    lottery_pool = 0
    global_ticket_counter = 0
    save_lottery()
    add_log(f"🔄 Сброс лотереи для нового розыгрыша", 0, "System")
    socketio.emit('draw_reset', {'message': '🔄 Лотерея сброшена! Новый розыгрыш завтра в 21:00!'})


def schedule_draw():
    while True:
        now = datetime.datetime.now()
        next_draw = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= next_draw: next_draw += datetime.timedelta(days=1)
        wait_seconds = (next_draw - now).total_seconds()
        time.sleep(wait_seconds)
        perform_draw()


threading.Thread(target=schedule_draw, daemon=True).start()


# ========== ЕЖЕДНЕВНЫЕ НАГРАДЫ ==========
def get_daily_status(user_id):
    check_and_reset_streak(user_id)
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM daily_rewards WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row:
            now = datetime.datetime.now().isoformat()
            cursor.execute(
                '''INSERT INTO daily_rewards (user_id, current_day, last_claim_date, streak_start_date, recovered_count) VALUES (?, 1, ?, ?, 0)''',
                (user_id, now, now))
            return {"current_day": 1, "can_claim": True, "next_claim_time": None, "recovered_count": 0,
                    "lost_streak": False}
        last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
        now = datetime.datetime.now()
        time_diff = (now - last_claim).total_seconds()
        current_day = row['current_day']
        can_claim = False
        next_claim_time = None
        lost_streak = False
        if time_diff >= 86400:
            if time_diff < 172800: can_claim = True
        if time_diff > 86400 and time_diff < 172800 and current_day > 1: lost_streak = True
        if not can_claim and time_diff < 86400: next_claim_time = last_claim + datetime.timedelta(seconds=86400)
        recovered_count = row['recovered_count'] or 0
        return {"current_day": current_day, "can_claim": can_claim,
                "next_claim_time": next_claim_time.isoformat() if next_claim_time else None,
                "recovered_count": recovered_count, "lost_streak": lost_streak}


def give_daily_reward(user_id, day):
    reward = DAILY_REWARDS.get(day)
    if not reward: return False
    user = get_user(user_id)
    if reward["wg"] > 0:
        update_user(user_id, wg=user["wg"] + reward["wg"])
        add_log(f"🎁 Ежедневная награда: +{reward['wg']} WG", user_id, user['username'])
    if reward["lp"] > 0:
        update_user(user_id, lp=user["lp"] + reward["lp"])
        add_log(f"🎁 Ежедневная награда: +{reward['lp']} LP", user_id, user['username'])
    if reward["energy_limit"] > 0:
        new_max_energy = user["max_energy"] + reward["energy_limit"]
        update_user(user_id, max_energy=new_max_energy)
        add_log(f"🎁 Ежедневная награда: +{reward['energy_limit']} к макс. энергии", user_id, user['username'])
    return True


def claim_daily_reward(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT current_day, last_claim_date, recovered_count FROM daily_rewards WHERE user_id=?",
                       (user_id,))
        row = cursor.fetchone()
        if not row: return {"success": False, "msg": "Ошибка"}
        last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
        now = datetime.datetime.now()
        time_diff = (now - last_claim).total_seconds()
        current_day = row['current_day']
        if current_day == 1:
            pass
        elif time_diff < 86400:
            return {"success": False, "msg": "Награда ещё не доступна"}
        recovered_count = row['recovered_count'] or 0
        give_daily_reward(user_id, current_day)
        new_day = current_day + 1
        cursor.execute(
            '''UPDATE daily_rewards SET current_day = ?, last_claim_date = ?, recovered_count = ? WHERE user_id = ?''',
            (new_day, now.isoformat(), recovered_count, user_id))
        return {"success": True, "msg": f"Награда за {current_day} день получена!", "new_day": new_day}


def recover_streak_with_stars(user_id):
    user = get_user(user_id)
    if user['stars'] < 20: return {"success": False, "msg": "Недостаточно Stars (нужно 20)"}
    with db.get_cursor() as cursor:
        cursor.execute("SELECT current_day, last_claim_date FROM daily_rewards WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row: return {"success": False, "msg": "Ошибка"}
        now = datetime.datetime.now()
        last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
        time_diff = (now - last_claim).total_seconds()
        current_day = row['current_day']
        if time_diff < 86400 or time_diff >= 172800: return {"success": False,
                                                             "msg": "Сейчас нельзя восстановить серию"}
        update_user(user_id, stars=user['stars'] - 20)
        cursor.execute(
            '''UPDATE daily_rewards SET last_claim_date = ?, recovered_count = recovered_count + 1 WHERE user_id = ?''',
            (now.isoformat(), user_id))
        add_log(f"⭐ Восстановил серию ежедневных наград за 20 Stars (день {current_day})", user_id, user['username'])
        return {"success": True, "msg": f"Серия восстановлена! Вы можете забрать награду за {current_day} день!",
                "current_day": current_day}


def check_and_reset_streak(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT current_day, last_claim_date FROM daily_rewards WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row: return
        last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
        now = datetime.datetime.now()
        time_diff = (now - last_claim).total_seconds()
        current_day = row['current_day']
        if time_diff > 172800 and current_day > 1:
            cursor.execute(
                '''UPDATE daily_rewards SET current_day = 1, streak_start_date = ?, recovered_count = 0 WHERE user_id = ?''',
                (now.isoformat(), user_id))
            add_log(f"🔄 Серия ежедневных наград сброшена (пропущено более 48ч)", user_id, str(user_id))
            return True
    return False


# ========== WEBSOCKET ==========
@socketio.on('reveal_cell')
def handle_reveal_cell(data):
    user_id = data['user_id']
    ticket_number = data['ticket_number']
    cell_index = data['cell_index']
    if not is_drawn:
        emit('reveal_error', {'message': 'Розыгрыш ещё не начался!'})
        return
    for ticket in lottery_tickets:
        if ticket.get("user_id") == user_id and ticket.get("number") == ticket_number:
            if not ticket["revealed"][cell_index]:
                ticket["revealed"][cell_index] = True
                save_lottery()
                number = ticket["numbers"][cell_index]
                is_win = number in winning_numbers
                user = get_user(user_id)
                win_text = "ВЫИГРЫШНАЯ" if is_win else "обычная"
                add_log(f"🔓 Открыл клетку {cell_index + 1} билета #{ticket_number} ({win_text} клетка, число {number})",
                        user_id, user['username'])
                emit('cell_revealed',
                     {'ticket_number': ticket_number, 'cell_index': cell_index, 'number': number, 'is_win': is_win})
                if all(ticket["revealed"]):
                    matches = sum(1 for i in range(12) if ticket["numbers"][i] in winning_numbers)
                    add_log(f"🎫 Полностью открыл билет #{ticket_number} (совпадений: {matches}/12)", user_id,
                            user['username'])
                    emit('ticket_completed', {'ticket_number': ticket_number, 'matches': matches})
            return


@socketio.on('get_draw_status')
def handle_get_draw_status(data):
    emit('draw_status', {'is_drawn': is_drawn, 'winning_numbers': winning_numbers if is_drawn else []})


@socketio.on('get_remaining_time')
def handle_get_remaining_time(data):
    global draw_time, is_drawn
    if is_drawn and draw_time:
        if isinstance(draw_time, str):
            try:
                draw_time = datetime.datetime.fromisoformat(draw_time)
            except:
                draw_time = None
        if draw_time:
            end_time = draw_time + datetime.timedelta(seconds=1800)
            now = datetime.datetime.now()
            remaining = int((end_time - now).total_seconds())
            if remaining < 0: remaining = 0
            emit('remaining_time', {'seconds': remaining})
        else:
            emit('remaining_time', {'seconds': 0})
    else:
        emit('remaining_time', {'seconds': 0})


# ========== API ЭНДПОИНТЫ ==========
@app.route('/')
def game_page():
    return render_template('game.html')


@app.route('/admin')
def admin_panel_page():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return "Доступ запрещён", 403
    return render_template('admin.html')


@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)


@app.route('/terms')
def terms_page():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Условия использования</title></head>
    <body style="background:#0a0a1a; color:white; padding:20px; font-family:system-ui;">
        <h1>Условия использования WereGood</h1>
        <p>Используя наш сервис, вы соглашаетесь с правилами игры.</p>
        <p>Все внутриигровые транзакции финальны.</p>
        <p>Администрация оставляет за собой право блокировать пользователей за нарушение правил.</p>
    </body>
    </html>
    """


@app.route('/privacy')
def privacy_page():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Политика конфиденциальности</title></head>
    <body style="background:#0a0a1a; color:white; padding:20px; font-family:system-ui;">
        <h1>Политика конфиденциальности WereGood</h1>
        <p>Мы собираем только ваш Telegram ID и данные профиля для работы игры.</p>
        <p>Данные не передаются третьим лицам.</p>
        <p>Вы можете удалить свои данные, обратившись к администратору.</p>
    </body>
    </html>
    """


# ========== TON CONNECT 2.0 УЛУЧШЕННЫЕ ФУНКЦИИ ==========
@app.route('/tonconnect-manifest.json', methods=['GET'])
def serve_manifest():
    """Манифест-файл для валидации кошельками"""
    manifest = {
        "url": WEBHOOK_URL,
        "name": "WereGood Game",
        "iconUrl": f"{WEBHOOK_URL}/static/coin.png",
        "termsOfUseUrl": f"{WEBHOOK_URL}/terms",
        "privacyPolicyUrl": f"{WEBHOOK_URL}/privacy"
    }
    return jsonify(manifest)


@app.route('/api/ton/init', methods=['POST'])
def api_ton_init():
    """Инициализация TON Connect для пользователя"""
    data = request.json or {}
    user_id = data.get('user_id')
    return jsonify({
        "success": True,
        "manifestUrl": f"{WEBHOOK_URL}/tonconnect-manifest.json"
    })


@app.route('/api/ton/save_wallet', methods=['POST'])
def api_ton_save_wallet():
    """Сохранение привязанного кошелька"""
    data = request.json or {}
    user_id = data.get('user_id')
    wallet_address = data.get('wallet_address')

    if not user_id or not wallet_address:
        return jsonify({"success": False, "error": "Missing user_id or wallet_address"}), 400

    try:
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET ton_wallet = ? WHERE user_id = ?", (wallet_address, user_id))

        user = get_user(user_id)
        add_log(f"🔗 Подключил TON кошелёк: {wallet_address[:6]}...{wallet_address[-4:]}",
                user_id, user.get('username', 'Unknown'))
        return jsonify({"success": True, "wallet": wallet_address})
    except Exception as e:
        print(f"Ошибка сохранения кошелька: {e}")
        return jsonify({"success": False, "error": "Database error"}), 500


@app.route('/api/ton/get_wallet', methods=['POST'])
def api_ton_get_wallet():
    """Получение привязанного кошелька"""
    data = request.json or {}
    user_id = data.get('user_id')

    with db.get_cursor() as cursor:
        cursor.execute("SELECT ton_wallet FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        wallet = row['ton_wallet'] if row else None

    return jsonify({"success": True, "wallet": wallet})


@app.route('/api/ton/create_payment', methods=['POST'])
def api_ton_create_payment():
    """Создание платежа - возвращает данные для транзакции"""
    data = request.json or {}
    user_id = data.get('user_id')
    amount = data.get('amount', 0.1)

    user_wallet = get_user_ton_wallet(user_id)
    if not user_wallet:
        return jsonify({"success": False, "error": "Кошелёк не подключён", "need_wallet": True})

    return jsonify({
        "success": True,
        "wallet_address": PROJECT_WALLET_ADDRESS,
        "amount": amount,
        "amount_nano": int(amount * 1e9),
        "comment": f"WereGood:{user_id}"
    })


@app.route('/api/ton/check_payment', methods=['POST'])
def api_ton_check_payment():
    """Проверка оплаты"""
    data = request.json or {}
    user_id = data.get('user_id')
    expected_amount = data.get('expected_amount', 0.1)

    if not user_id:
        return jsonify({"success": False, "error": "Missing user_id"}), 400

    user_wallet = get_user_ton_wallet(user_id)
    if not user_wallet:
        return jsonify({"success": False, "error": "Кошелёк не подключён"})

    confirmed, amount, tx_hash = check_ton_transaction(user_wallet, expected_amount)

    if confirmed:
        try:
            user = get_user(user_id)
            old_lp = user.get('lp', 0)
            old_stars = user.get('stars', 0)

            new_lp = old_lp + 50
            new_stars = old_stars + 25

            update_user(user_id, lp=new_lp, stars=new_stars)

            add_log(f"💎 Оплата через TON: +50 LP, +25 Stars ({amount} TON)",
                    user_id, user.get('username', 'Unknown'))

            return jsonify({
                "success": True,
                "confirmed": True,
                "amount": amount,
                "tx_hash": tx_hash,
                "bonus": {"lp": 50, "stars": 25}
            })
        except Exception as e:
            print(f"Ошибка начисления бонуса: {e}")
            return jsonify({"success": False, "error": "Internal reward error"}), 500

    return jsonify({"success": True, "confirmed": False})


@app.route('/api/log_game_entry', methods=['POST'])
def api_log_game_entry():
    data = request.json
    user_id = data['user_id']
    user = get_user(user_id)
    online_users[user_id] = time.time()
    update_online_count()
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    update_stats_history(today, online=len(online_users))
    add_log(f"🟢 Вошёл в игру", user_id, user['username'])
    return jsonify({"success": True})


@app.route('/api/log_game_exit', methods=['POST'])
def api_log_game_exit():
    data = request.json
    user_id = data['user_id']
    user = get_user(user_id)
    if user_id in online_users: del online_users[user_id]
    add_log(f"🔴 Вышел из игры", user_id, user['username'])
    return jsonify({"success": True})


@app.route('/api/online_count', methods=['GET'])
def api_online_count():
    update_online_count()
    return jsonify({"online": len(online_users)})


@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.json
    user_id = data['user_id']
    username = data.get('username', '')
    first_name = data.get('first_name', '')
    last_name = data.get('last_name', '')
    avatar_url = data.get('avatar_url', '')
    referral_code = data.get('referral_code', '')
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("UPDATE users SET username=?, first_name=?, last_name=?, avatar_url=? WHERE user_id=?",
                           (username, first_name, last_name, avatar_url, user_id))
            add_log(f"✏️ Обновил профиль", user_id, username or first_name or str(user_id))
            return jsonify({"success": True})
        else:
            now = time.time()
            ref_code_new = hashlib.md5(str(user_id).encode()).hexdigest()[:8]
            founder_id = 5264622363
            role = "founder" if user_id == founder_id else "player"
            unlocked = json.dumps(["player", "founder"]) if role == "founder" else json.dumps(["player"])
            referrer_id = 0
            if referral_code:
                cursor.execute("SELECT user_id FROM users WHERE referral_code=?", (referral_code,))
                referrer_row = cursor.fetchone()
                if referrer_row:
                    referrer_id = referrer_row['user_id']
                    cursor.execute(
                        '''INSERT INTO referrals (referrer_id, referred_id, username, first_name, total_spent_lp) VALUES (?, ?, ?, ?, 0)''',
                        (referrer_id, user_id, username, first_name))
                    add_log(f"👥 Новый реферал! {username or first_name} зарегистрировался по вашей ссылке", referrer_id,
                            get_user(referrer_id)['username'])
            cursor.execute('''
                INSERT INTO users (user_id, wg, lp, energy, last_energy_update, tickets, total_clicks,
                upgrade_counts, ticket_counter, referral_code, referrer_id, likes, dislikes, settings,
                username, first_name, last_name, avatar_url, usdt, wins, role, stars,
                max_energy, energy_upgrades, energy_limit_upgrades, unlocked_prefixes, tutorial_completed, ton_wallet)
                VALUES (?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, ?, 0, 0,
                '{"theme":"dark"}', ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '')
            ''', (user_id, now, ref_code_new, referrer_id, username, first_name, last_name, avatar_url, role, unlocked))
            add_log(f"✨ Новая регистрация! Добро пожаловать!", user_id, username or first_name or str(user_id))
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            update_stats_history(today, users=1)
    return jsonify({"success": True})


@app.route('/api/click', methods=['POST'])
def api_click():
    data = request.json
    user_id = data['user_id']
    banned, ban_info = is_banned(user_id)
    if banned: return jsonify(
        {"error": f"Вы забанены! Причина: {ban_info['reason']}. До: {ban_info['until_date']}", "banned": True})
    user = get_user(user_id)
    success, new_energy = spend_energy(user_id, user, 1)
    if not success: return jsonify({"error": "Нет энергии", "energy": new_energy, "wg": user["wg"], "lp": user["lp"]})
    earning = get_total_earning(user["upgrade_counts"])
    old_wg = user["wg"]
    new_wg = old_wg + earning
    new_clicks = user["total_clicks"] + 1
    update_user(user_id, wg=new_wg, total_clicks=new_clicks)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    update_stats_history(today, clicks=1)
    if user["total_clicks"] == 0:
        add_log(f"🆕👆 ПЕРВЫЙ КЛИК в игре! +{earning:.4f} WG", user_id, user['username'], old_value=old_wg,
                new_value=new_wg, currency="wg")
    else:
        add_log(f"🖱️ Клик по монете +{earning:.4f} WG", user_id, user['username'], old_value=old_wg, new_value=new_wg,
                currency="wg")
    lp_reward = False
    if random.random() < 0.0025:
        old_lp = user["lp"]
        new_lp = old_lp + 0.5
        update_user(user_id, lp=new_lp)
        lp_reward = True
        add_log(f"🎲 Редкий дроп! +0.5 LP", user_id, user['username'], old_value=old_lp, new_value=new_lp, currency="lp")
        new_lp = new_lp
    else:
        new_lp = user["lp"]
    return jsonify({"energy": new_energy, "wg": new_wg, "lp": new_lp, "total_clicks": new_clicks, "earned": earning,
                    "lp_reward": lp_reward, "earning_per_click": earning})


@app.route('/api/status', methods=['POST'])
def api_status():
    data = request.json
    user_id = data['user_id']
    banned, ban_info = is_banned(user_id)
    if banned: return jsonify({"banned": True, "reason": ban_info['reason'], "until": ban_info['until_date']})
    user = get_user(user_id)
    current_energy, _ = calculate_energy(user)
    earning = get_total_earning(user["upgrade_counts"])
    online_users[user_id] = time.time()
    update_online_count()
    return jsonify({"wg": user["wg"], "lp": user["lp"], "energy": current_energy, "total_clicks": user["total_clicks"],
                    "earning_per_click": earning, "upgrade_counts": user["upgrade_counts"], "likes": user["likes"],
                    "dislikes": user["dislikes"], "username": user["username"], "first_name": user["first_name"],
                    "avatar_url": user["avatar_url"], "settings": user["settings"], "usdt": user["usdt"],
                    "wins": user["wins"], "role": user["role"], "stars": user["stars"],
                    "max_energy": user["max_energy"], "energy_upgrades": user["energy_upgrades"]})


@app.route('/api/buy_upgrade', methods=['POST'])
def api_buy_upgrade():
    data = request.json
    user_id = data['user_id']
    banned, ban_info = is_banned(user_id)
    if banned: return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})
    upgrade_id = data['upgrade_id']
    user = get_user(user_id)
    current_count = user["upgrade_counts"].get(upgrade_id, 0)
    cost = get_upgrade_cost(upgrade_id, current_count)
    if user["wg"] < cost: return jsonify({"success": False, "msg": f"Не хватает WG! Нужно {cost:.2f} WG"})
    old_wg = user["wg"]
    new_wg = old_wg - cost
    new_count = current_count + 1
    user["upgrade_counts"][upgrade_id] = new_count
    update_user(user_id, wg=new_wg, upgrade_counts=user["upgrade_counts"])
    if current_count == 0:
        add_log(f"🆕⭐ ПЕРВАЯ ПОКУПКА улучшения! {UPGRADE_CONFIG[upgrade_id]['name']} за {cost:.2f} WG", user_id,
                user['username'], old_value=old_wg, new_value=new_wg, currency="wg")
    else:
        add_log(f"💰 Купил {UPGRADE_CONFIG[upgrade_id]['name']} #{new_count} за {cost:.2f} WG", user_id,
                user['username'], old_value=old_wg, new_value=new_wg, currency="wg")
    return jsonify(
        {"success": True, "msg": f"{UPGRADE_CONFIG[upgrade_id]['name']} #{new_count} куплено!", "new_count": new_count,
         "next_cost": get_upgrade_cost(upgrade_id, new_count)})


@app.route('/api/watch_ad', methods=['POST'])
def api_watch_ad():
    data = request.json
    user_id = data['user_id']
    banned, ban_info = is_banned(user_id)
    if banned: return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})
    user = get_user(user_id)
    current_energy, _ = calculate_energy(user)
    max_energy = user.get("max_energy", 500)
    old_energy = current_energy
    new_energy = min(max_energy, current_energy + 200)
    update_energy_in_db(user_id, user, new_energy)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    update_stats_history(today, ad_views=1)
    add_log(f"🎬 Просмотрел рекламу (+200 энергии)", user_id, user['username'], old_value=old_energy,
            new_value=new_energy, currency="energy")
    return jsonify({"success": True, "energy": 200})


@app.route('/api/watch_ad_fallback', methods=['POST'])
def api_watch_ad_fallback():
    data = request.json
    user_id = data['user_id']
    user = get_user(user_id)
    current_energy, _ = calculate_energy(user)
    max_energy = user.get("max_energy", 500)
    old_energy = current_energy
    new_energy = min(max_energy, current_energy + 50)
    update_energy_in_db(user_id, user, new_energy)
    add_log(f"🎬 Просмотрел рекламу (резервная, +50 энергии)", user_id, user['username'], old_value=old_energy,
            new_value=new_energy, currency="energy")
    return jsonify({"success": True, "energy": 50})


@app.route('/api/watch_ad_limit', methods=['POST'])
def api_watch_ad_limit():
    data = request.json
    user_id = data['user_id']
    banned, ban_info = is_banned(user_id)
    if banned: return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})
    user = get_user(user_id)
    current_upgrades = user.get('energy_limit_upgrades', 0)
    if current_upgrades >= 300: return jsonify(
        {"success": False, "msg": "Вы достигли максимального лимита улучшений! (300/300)"})
    old_max_energy = user['max_energy']
    new_max_energy = old_max_energy + 1
    new_upgrades = current_upgrades + 1
    update_user(user_id, max_energy=new_max_energy, energy_limit_upgrades=new_upgrades)
    add_log(f"🎬 Просмотрел рекламу (+1 к макс. энергии, теперь {new_max_energy})", user_id, user['username'],
            old_value=old_max_energy, new_value=new_max_energy, currency="energy")
    return jsonify({"success": True, "max_energy": new_max_energy, "upgrades": new_upgrades})


@app.route('/api/buy_ticket', methods=['POST'])
def api_buy_ticket():
    data = request.json
    user_id = data['user_id']
    banned, ban_info = is_banned(user_id)
    if banned: return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})
    user = get_user(user_id)
    success, msg = buy_ticket(user_id, user)
    return jsonify({"success": success, "msg": msg, "lp": user["lp"]})


@app.route('/api/lottery_status', methods=['POST'])
def api_lottery_status():
    data = request.json
    user_id = data['user_id']
    user = get_user(user_id)
    user_tickets = [t for t in lottery_tickets if t.get("user_id") == user_id]
    return jsonify(
        {"prize_pool": lottery_pool, "user_tickets": len(user_tickets), "user_lp": user["lp"], "is_drawn": is_drawn,
         "winning_numbers": winning_numbers if is_drawn else [], "tickets": user_tickets})


@app.route('/api/user_tickets', methods=['POST'])
def api_user_tickets():
    data = request.json
    user_id = data['user_id']
    user = get_user(user_id)
    user_tickets = [t for t in lottery_tickets if t.get("user_id") == user_id]
    add_log(f"🎫👁️ Открыл список своих билетов (всего: {len(user_tickets)})", user_id, user['username'])
    return jsonify({"tickets": user_tickets})


@app.route('/api/recent_players', methods=['GET'])
def api_recent_players():
    with db.get_cursor() as cursor:
        cursor.execute(
            '''SELECT h.user_id, h.username, h.ticket_number, h.created_at, u.avatar_url, u.first_name, u.role FROM lottery_tickets_history h LEFT JOIN users u ON h.user_id = u.user_id ORDER BY h.created_at DESC LIMIT 5''')
        rows = cursor.fetchall()
        players = []
        for row in rows:
            created = datetime.datetime.strptime(row['created_at'], '%Y-%m-%d %H:%M:%S')
            diff = datetime.datetime.now() - created
            seconds = int(diff.total_seconds())
            if seconds < 60:
                time_ago = f"{seconds} сек назад"
            elif seconds < 3600:
                time_ago = f"{seconds // 60} мин {seconds % 60} сек назад"
            elif seconds < 86400:
                time_ago = f"{seconds // 3600} ч {(seconds % 3600) // 60} мин назад"
            else:
                time_ago = f"{seconds // 86400} дн назад"
            if row['username'] and row['username'] != '':
                display_name = '@' + row['username']
            elif row['first_name'] and row['first_name'] != '':
                display_name = row['first_name']
            else:
                display_name = f"Player_{row['user_id']}"
            players.append({"user_id": row['user_id'], "username": display_name, "avatar_url": row['avatar_url'] or '',
                            "time_ago": time_ago, "ticket_number": row['ticket_number'],
                            "role": row['role'] if row['role'] else 'player'})
        return jsonify(players)


@app.route('/api/get_referral_link', methods=['POST'])
def api_get_referral_link():
    data = request.json
    user_id = data['user_id']
    user = get_user(user_id)
    with db.get_cursor() as cursor:
        cursor.execute("SELECT referral_code FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if row and row['referral_code']:
            code = row['referral_code']
        else:
            code = hashlib.md5(str(user_id).encode()).hexdigest()[:8]
            update_user(user_id, referral_code=code)
    BOT_USERNAME = "WereGooodbot"
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    add_log(f"📨 Получил реферальную ссылку", user_id, user['username'])
    return jsonify({"link": link})


@app.route('/api/get_referrals', methods=['POST'])
def api_get_referrals():
    data = request.json
    user_id = data['user_id']
    user = get_user(user_id)
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT r.username, r.first_name, r.created_at, r.total_spent_lp FROM referrals r WHERE r.referrer_id = ?",
            (user_id,))
        rows = cursor.fetchall()
        referrals = []
        total_earned = 0
        for row in rows:
            name = row['username'] or row['first_name'] or "Игрок"
            earned = (row['total_spent_lp'] or 0) * 0.05
            total_earned += earned
            referrals.append({"username": name, "date": row['created_at'], "spent_lp": row['total_spent_lp'] or 0,
                              "earned": round(earned, 2)})
    return jsonify({"referrals": referrals, "total_earned": round(total_earned, 2)})


@app.route('/api/leaderboard', methods=['GET'])
def api_leaderboard():
    limit = int(request.args.get('limit', 50))
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT user_id, total_clicks, username, first_name, avatar_url, role, settings FROM users ORDER BY total_clicks DESC LIMIT ?",
            (limit,))
        rows = cursor.fetchall()
        result = []
        for i, row in enumerate(rows):
            hide_from_top = False
            if row['settings']:
                try:
                    settings = json.loads(row['settings']); hide_from_top = settings.get('hideFromTop', False)
                except:
                    pass
            if hide_from_top:
                display_name = 'Аноним'
                avatar = '👤'
            else:
                if row['username'] and row['username'] != '':
                    display_name = '@' + row['username']
                elif row['first_name'] and row['first_name'] != '':
                    display_name = row['first_name']
                else:
                    display_name = f"Player_{row['user_id']}"
                avatar = row['avatar_url'] or "👤"
            result.append({"rank": i + 1, "user_id": row['user_id'], "username": display_name,
                           "total_clicks": row['total_clicks'], "avatar": avatar,
                           "role": row['role'] if row['role'] else 'player', "hide_from_top": hide_from_top})
        return jsonify(result)


@app.route('/api/get_user_stats', methods=['POST'])
def api_get_user_stats():
    data = request.json
    user_id = data['user_id']
    with db.get_cursor() as cursor:
        cursor.execute(
            '''SELECT wg, lp, total_clicks, likes, dislikes, username, first_name, upgrade_counts, avatar_url, usdt, wins, role, stars, max_energy, energy_upgrades, settings FROM users WHERE user_id=?''',
            (user_id,))
        row = cursor.fetchone()
        if row:
            hide_from_top = False
            if row['settings']:
                try:
                    settings = json.loads(row['settings']); hide_from_top = settings.get('hideFromTop', False)
                except:
                    pass
            if hide_from_top:
                display_name = 'Аноним'
            else:
                if row['username'] and row['username'] != '':
                    display_name = '@' + row['username']
                elif row['first_name'] and row['first_name'] != '':
                    display_name = row['first_name']
                else:
                    display_name = f"Player_{user_id}"
            return jsonify(
                {"wg": row['wg'], "lp": row['lp'], "total_clicks": row['total_clicks'], "likes": row['likes'] or 0,
                 "dislikes": row['dislikes'] or 0, "username": display_name, "avatar_url": row['avatar_url'] or "👤",
                 "usdt": row['usdt'] if 'usdt' in row.keys() else 0, "wins": row['wins'] if 'wins' in row.keys() else 0,
                 "role": row['role'] if 'role' in row.keys() else 'player',
                 "stars": row['stars'] if 'stars' in row.keys() else 0,
                 "max_energy": row['max_energy'] if 'max_energy' in row.keys() else 500,
                 "energy_upgrades": row['energy_upgrades'] if 'energy_upgrades' in row.keys() else 0,
                 "hide_from_top": hide_from_top})
    return jsonify({"error": "Пользователь не найден"})


@app.route('/api/vote', methods=['POST'])
def api_vote():
    data = request.json
    voter_id = data['voter_id']
    target_id = data['target_id']
    vote_type = data['vote_type']
    voter = get_user(voter_id)
    target = get_user(target_id)
    with db.get_cursor() as cursor:
        cursor.execute('SELECT last_vote_time FROM votes WHERE voter_id=? AND target_id=?', (voter_id, target_id))
        row = cursor.fetchone()
        if row and row['last_vote_time']:
            last_vote = datetime.datetime.fromisoformat(row['last_vote_time'])
            time_passed = datetime.datetime.now() - last_vote
            if time_passed.total_seconds() < 86400:
                hours_left = 24 - (time_passed.total_seconds() / 3600)
                return jsonify({"success": False, "msg": f"Через {int(hours_left)}ч {int((hours_left % 1) * 60)}мин"})
        cursor.execute(
            '''INSERT INTO votes (voter_id, target_id, vote_type, last_vote_time) VALUES (?, ?, ?, ?) ON CONFLICT(voter_id, target_id) DO UPDATE SET vote_type=?, last_vote_time=?''',
            (voter_id, target_id, vote_type, datetime.datetime.now().isoformat(), vote_type,
             datetime.datetime.now().isoformat()))
        if vote_type == 'like':
            cursor.execute("UPDATE users SET likes = likes + 1 WHERE user_id=?", (target_id,))
            add_log(f"👍 Поставил лайк игроку {target['username']}", voter_id, voter['username'])
        else:
            cursor.execute("UPDATE users SET dislikes = dislikes + 1 WHERE user_id=?", (target_id,))
            add_log(f"👎 Поставил дизлайк игроку {target['username']}", voter_id, voter['username'])
    return jsonify({"success": True, "msg": "Голос учтён!"})


@app.route('/api/update_settings', methods=['POST'])
def api_update_settings():
    data = request.json
    user_id = data['user_id']
    setting = data['setting']
    value = data['value']
    user = get_user(user_id)
    with db.get_cursor() as cursor:
        cursor.execute("SELECT settings FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        settings = {"theme": "dark"}
        if row and row['settings']:
            try:
                settings = json.loads(row['settings'])
            except:
                settings = {"theme": "dark"}
        old_value = settings.get(setting)
        settings[setting] = value
        cursor.execute("UPDATE users SET settings=? WHERE user_id=?", (json.dumps(settings), user_id))
    setting_names = {'theme': 'тему', 'notifications': 'уведомления', 'sounds': 'звуки', 'vibration': 'вибрацию',
                     'hideFromTop': 'скрытие из топа'}
    setting_name = setting_names.get(setting, setting)
    status = "включил" if value else "выключил"
    if setting == 'theme': status = "переключил на светлую тему" if value == 'light' else "переключил на тёмную тему"
    add_log(f"⚙️ Изменил настройки: {status} {setting_name}", user_id, user['username'], old_value=old_value,
            new_value=value)
    return jsonify({"success": True})


@app.route('/api/get_settings', methods=['POST'])
def api_get_settings():
    data = request.json
    user_id = data['user_id']
    with db.get_cursor() as cursor:
        cursor.execute("SELECT settings FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        default_settings = {"notifications": True, "theme": "dark", "sounds": True, "vibration": True,
                            "hideFromTop": False}
        if row and row['settings']:
            try:
                settings = json.loads(row['settings'])
            except:
                settings = default_settings
        else:
            settings = default_settings
        for key, default_value in default_settings.items():
            if key not in settings: settings[key] = default_value
    return jsonify(settings)


@app.route('/api/lottery_all_tickets', methods=['GET'])
def api_lottery_all_tickets():
    with db.get_cursor() as cursor:
        cursor.execute("SELECT tickets FROM lottery LIMIT 1")
        row = cursor.fetchone()
        if row and row['tickets']:
            tickets = eval(row['tickets']) if row['tickets'] else []
            result = []
            for ticket in tickets:
                user_id = ticket.get('user_id')
                cursor.execute("SELECT username, first_name, role FROM users WHERE user_id=?", (user_id,))
                user_row = cursor.fetchone()
                username = user_row['username'] if user_row and user_row['username'] else (
                    user_row['first_name'] if user_row else 'Игрок')
                ticket['username'] = username
                ticket['role'] = user_row['role'] if user_row else 'player'
                result.append(ticket)
            return jsonify({"tickets": result})
    return jsonify({"tickets": []})


@app.route('/api/create_stars_invoice', methods=['POST'])
def api_create_stars_invoice():
    data = request.json
    user_id = data['user_id']
    chat_id = data.get('chat_id', user_id)
    user = get_user(user_id)
    if user['energy_upgrades'] >= 15: return jsonify({"success": False, "msg": "Максимум улучшений! (15/15)"})
    invoice_link = create_stars_invoice(chat_id, user_id)
    if invoice_link: return jsonify({"success": True, "invoice_link": invoice_link})
    return jsonify({"success": False, "msg": "Ошибка создания счёта"})


@app.route('/api/get_stars_balance', methods=['POST'])
def api_get_stars_balance():
    data = request.json
    user_id = data['user_id']
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getStarBalance"
        response = requests.get(url, timeout=10)
        result = response.json()
        if result.get("ok"):
            balance = result.get("result", {}).get("balance", 0)
            with db.get_cursor() as cursor:
                cursor.execute("UPDATE users SET stars = ? WHERE user_id=?", (balance, user_id))
            return jsonify({"success": True, "balance": balance})
    except Exception as e:
        print(f"Ошибка получения баланса звезд: {e}")
    user = get_user(user_id)
    return jsonify({"success": True, "balance": user['stars']})


@app.route('/api/get_available_prefixes', methods=['POST'])
def api_get_available_prefixes():
    data = request.json
    user_id = data['user_id']
    user = get_user(user_id)
    unlocked = user.get('unlocked_prefixes', ['player'])
    all_prefixes = {
        "player": {"name": "Игрок", "icon": "🎮", "desc": "Выдаётся абсолютно всем игрокам", "color": "player"},
        "pioneer": {"name": "Первооткрыватель", "icon": "⭐", "desc": "За регистрацию в первый день",
                    "color": "pioneer"},
        "founder": {"name": "Основатель", "icon": "👑", "desc": "Основатель проекта", "color": "founder"}}
    prefixes = [{"id": pid, **all_prefixes[pid]} for pid in unlocked if pid in all_prefixes]
    return jsonify({"success": True, "prefixes": prefixes, "current": user['role']})


@app.route('/api/change_prefix', methods=['POST'])
def api_change_prefix():
    data = request.json
    user_id = data['user_id']
    new_prefix = data['prefix']
    user = get_user(user_id)
    unlocked = user.get('unlocked_prefixes', ['player'])
    if new_prefix not in unlocked: return jsonify({"success": False, "msg": "Префикс не разблокирован"})
    old_role = user['role']
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET role = ? WHERE user_id=?", (new_prefix, user_id))
    add_log(f"👑 Сменил префикс с {old_role} на {new_prefix}", user_id, user['username'])
    if new_prefix == "pioneer" and "pioneer" not in unlocked:
        add_log(f"🏷️✨ Получил уникальный префикс ПЕРВООТКРЫВАТЕЛЬ!", user_id, user['username'])
    elif new_prefix == "founder" and "founder" not in unlocked:
        add_log(f"🏷️✨ Получил уникальный префикс ОСНОВАТЕЛЬ!", user_id, user['username'])
    return jsonify({"success": True, "msg": f"Префикс изменён на {new_prefix}"})


@app.route('/api/create_withdrawal', methods=['POST'])
def api_create_withdrawal():
    data = request.json
    user_id = data['user_id']
    amount = data['amount']
    address = data['address']
    network = data['network']
    user = get_user(user_id)
    if amount < 2: return jsonify({"success": False, "msg": "Минимальная сумма вывода: 2 USDT"})
    if user['usdt'] < amount: return jsonify({"success": False, "msg": "Недостаточно USDT на балансе"})
    update_user(user_id, usdt=user['usdt'] - amount)
    create_withdrawal_request_db(user_id, user['username'], amount, address, network)
    return jsonify({"success": True, "msg": "Заявка на вывод создана! Ожидайте обработки администратором."})


@app.route('/api/get_withdrawals', methods=['POST'])
def api_get_withdrawals():
    data = request.json
    user_id = data['user_id']
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM withdrawal_requests WHERE user_id = ? ORDER BY id DESC", (user_id,))
        rows = cursor.fetchall()
        withdrawals = []
        for row in rows:
            withdrawals.append(
                {"id": row['id'], "user_id": row['user_id'], "username": row['username'], "amount": row['amount'],
                 "address": row['address'], "network": row['network'], "status": row['status'],
                 "created_at": row['created_at'], "processed_at": row['processed_at']})
    return jsonify({"success": True, "withdrawals": withdrawals})


@app.route('/api/daily_status', methods=['POST'])
def api_daily_status():
    data = request.json
    user_id = data['user_id']
    status = get_daily_status(user_id)
    return jsonify(status)


@app.route('/api/claim_daily', methods=['POST'])
def api_claim_daily():
    data = request.json
    user_id = data['user_id']
    result = claim_daily_reward(user_id)
    return jsonify(result)


@app.route('/api/recover_daily', methods=['POST'])
def api_recover_daily():
    data = request.json
    user_id = data['user_id']
    result = recover_streak_with_stars(user_id)
    return jsonify(result)


@app.route('/api/get_tutorial_status', methods=['POST'])
def api_get_tutorial_status():
    data = request.json
    user_id = data['user_id']
    with db.get_cursor() as cursor:
        cursor.execute("SELECT tutorial_completed FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        completed = row['tutorial_completed'] if row else 0
    return jsonify({"completed": completed == 1})


@app.route('/api/complete_tutorial', methods=['POST'])
def api_complete_tutorial():
    data = request.json
    user_id = data['user_id']
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET tutorial_completed = 1 WHERE user_id=?", (user_id,))
    add_log(f"🎓 Завершил обучение", user_id, str(user_id))
    return jsonify({"success": True})


# ========== АДМИН-ЭНДПОИНТЫ ==========
@app.route('/api/admin/stats', methods=['GET'])
def api_admin_stats():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    update_online_count()
    with db.get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) as total FROM users")
        total_users = cursor.fetchone()['total']
        cursor.execute(
            "SELECT SUM(wg) as total_wg, SUM(lp) as total_lp, SUM(usdt) as total_usdt, SUM(wins) as total_wins, SUM(total_clicks) as total_clicks FROM users")
        stats = cursor.fetchone()
        cursor.execute(
            "SELECT SUM(json_extract(upgrade_counts, '$.1')) as upgrade_1, SUM(json_extract(upgrade_counts, '$.2')) as upgrade_2, SUM(json_extract(upgrade_counts, '$.3')) as upgrade_3 FROM users")
        upgrade_stats = cursor.fetchone()
        cursor.execute("SELECT SUM(stars) as total_stars, SUM(energy_upgrades) as total_energy_upgrades FROM users")
        star_stats = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) as total_tickets FROM lottery_tickets_history")
        ticket_history = cursor.fetchone()
        cursor.execute("SELECT SUM(amount) as total_donated FROM successful_payments")
        donated = cursor.fetchone()
        total_current_tickets = len(lottery_tickets)
        players_in_lottery = len(set([t.get('user_id') for t in lottery_tickets if t.get('user_id')]))
        with db.get_cursor() as cursor2:
            cursor2.execute("SELECT COUNT(*) as count FROM system_logs WHERE action LIKE '%рекламу%'")
            ad_views = cursor2.fetchone()['count']
        return jsonify({"success": True, "total_users": total_users, "total_wg": round(stats['total_wg'] or 0, 2),
                        "total_lp": int(stats['total_lp'] or 0), "total_usdt": round(stats['total_usdt'] or 0, 2),
                        "total_wins": int(stats['total_wins'] or 0), "total_clicks": int(stats['total_clicks'] or 0),
                        "upgrade_1": int(upgrade_stats['upgrade_1'] or 0),
                        "upgrade_2": int(upgrade_stats['upgrade_2'] or 0),
                        "upgrade_3": int(upgrade_stats['upgrade_3'] or 0),
                        "total_stars": int(star_stats['total_stars'] or 0),
                        "total_energy_upgrades": int(star_stats['total_energy_upgrades'] or 0),
                        "total_tickets_history": int(ticket_history['total_tickets'] or 0),
                        "total_current_tickets": total_current_tickets, "players_in_lottery": players_in_lottery,
                        "ad_views": ad_views, "total_donated_stars": int(donated['total_donated'] or 0),
                        "lottery_pool": lottery_pool, "is_drawn": is_drawn, "online": len(online_users)})


@app.route('/api/admin/lottery_participants', methods=['GET'])
def api_admin_lottery_participants():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    participants = []
    for ticket in lottery_tickets:
        participants.append({"user_id": ticket.get('user_id'), "ticket_number": ticket.get('number'),
                             "purchase_number": ticket.get('purchase_number'),
                             "revealed_count": sum(ticket.get('revealed', [])), "numbers": ticket.get('numbers', [])})
    return jsonify({"success": True, "participants": participants, "count": len(participants)})


@app.route('/api/admin/logs', methods=['GET'])
def api_admin_logs():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    log_type = request.args.get('type', 'all')
    limit = int(request.args.get('limit', 500))
    date = request.args.get('date', '')
    action_filter = request.args.get('action', '')
    user_id_filter = request.args.get('user_id', '')
    logs = get_logs(log_type, limit, date, action_filter, user_id_filter)
    return jsonify({"success": True, "logs": logs, "total": len(logs)})


@app.route('/api/admin/withdrawals', methods=['GET'])
def api_admin_withdrawals():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    withdrawals = get_withdrawal_requests_db()
    return jsonify({"success": True, "withdrawals": withdrawals})


@app.route('/api/admin/search_users', methods=['POST'])
def api_admin_search_users():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    data = request.get_json()
    if not data: return jsonify({"success": False, "error": "No JSON"}), 400
    query = data.get('query', '')
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT user_id, username, first_name, role, wg, lp, usdt, wins, total_clicks, stars, max_energy, energy_upgrades, unlocked_prefixes FROM users WHERE user_id LIKE ? OR username LIKE ? OR first_name LIKE ? LIMIT 50",
            (f'%{query}%', f'%{query}%', f'%{query}%'))
        rows = cursor.fetchall()
        users = []
        for row in rows:
            banned, _ = is_banned(row['user_id'])
            users.append(
                {"user_id": row['user_id'], "username": row['username'] or row['first_name'] or str(row['user_id']),
                 "role": row['role'] or 'player', "wg": round(row['wg'] or 0, 2), "lp": int(row['lp'] or 0),
                 "usdt": round(row['usdt'] or 0, 2), "wins": int(row['wins'] or 0),
                 "total_clicks": int(row['total_clicks'] or 0), "stars": int(row['stars'] or 0),
                 "max_energy": int(row['max_energy'] or 500), "energy_upgrades": int(row['energy_upgrades'] or 0),
                 "unlocked_prefixes": eval(row['unlocked_prefixes']) if row['unlocked_prefixes'] else ["player"],
                 "is_banned": banned})
    return jsonify({"success": True, "users": users})


@app.route('/api/admin/get_user', methods=['POST'])
def api_admin_get_user():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    data = request.get_json()
    if not data: return jsonify({"success": False, "error": "No JSON"}), 400
    user_id = data.get('user_id')
    if not user_id: return jsonify({"success": False, "error": "user_id required"}), 400
    user = get_user(user_id)
    banned, ban_info = is_banned(user_id)
    user['is_banned'] = banned
    if banned: user['ban_info'] = ban_info
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT r.username, r.first_name, r.created_at, r.total_spent_lp FROM referrals r WHERE r.referrer_id = ?",
            (user_id,))
        referrals = cursor.fetchall()
        user['referrals'] = [{"username": r['username'] or r['first_name'] or 'Игрок', "date": r['created_at'],
                              "spent_lp": r['total_spent_lp'] or 0,
                              "earned": round((r['total_spent_lp'] or 0) * 0.05, 2)} for r in referrals]
    user['personal_logs'] = get_logs('all', 50, None, None, str(user_id))
    for log in user['personal_logs']: log.pop('type', None)
    return jsonify({"success": True, "user": user})


@app.route('/api/admin/update_user', methods=['POST'])
def api_admin_update_user():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    data = request.get_json()
    if not data: return jsonify({"success": False, "error": "No JSON"}), 400
    user_id = data.get('user_id')
    if not user_id: return jsonify({"success": False, "error": "user_id required"}), 400
    action_type = data.get('action_type')
    amount = data.get('amount', 0)
    user = get_user(user_id)
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"
    if action_type == 'add_wg':
        old_value = user['wg'];
        new_value = old_value + amount;
        update_user(user_id, wg=new_value)
        add_admin_log(f"💰 Добавил {amount} WG", admin_id, admin_name, user_id, user['username'],
                      f"Баланс WG: {old_value:.2f} → {new_value:.2f}")
    elif action_type == 'remove_wg':
        old_value = user['wg'];
        new_value = max(0, old_value - amount);
        update_user(user_id, wg=new_value)
        add_admin_log(f"📉 Отнял {amount} WG", admin_id, admin_name, user_id, user['username'],
                      f"Баланс WG: {old_value:.2f} → {new_value:.2f}")
    elif action_type == 'add_lp':
        old_value = user['lp'];
        new_value = old_value + amount;
        update_user(user_id, lp=new_value)
        add_admin_log(f"🎯 Добавил {amount} LP", admin_id, admin_name, user_id, user['username'],
                      f"Баланс LP: {old_value} → {new_value}")
    elif action_type == 'remove_lp':
        old_value = user['lp'];
        new_value = max(0, old_value - amount);
        update_user(user_id, lp=new_value)
        add_admin_log(f"📉 Отнял {amount} LP", admin_id, admin_name, user_id, user['username'],
                      f"Баланс LP: {old_value} → {new_value}")
    elif action_type == 'add_usdt':
        old_value = user['usdt'];
        new_value = old_value + amount;
        update_user(user_id, usdt=new_value)
        add_admin_log(f"💰 Добавил {amount} USDT", admin_id, admin_name, user_id, user['username'],
                      f"Баланс USDT: {old_value:.2f} → {new_value:.2f}")
    elif action_type == 'remove_usdt':
        old_value = user['usdt'];
        new_value = max(0, old_value - amount);
        update_user(user_id, usdt=new_value)
        add_admin_log(f"📉 Отнял {amount} USDT", admin_id, admin_name, user_id, user['username'],
                      f"Баланс USDT: {old_value:.2f} → {new_value:.2f}")
    elif action_type == 'add_stars':
        old_value = user['stars'];
        new_value = old_value + amount;
        update_user(user_id, stars=new_value)
        add_admin_log(f"⭐ Добавил {amount} Stars", admin_id, admin_name, user_id, user['username'],
                      f"Баланс Stars: {old_value} → {new_value}")
    elif action_type == 'remove_stars':
        old_value = user['stars'];
        new_value = max(0, old_value - amount);
        update_user(user_id, stars=new_value)
        add_admin_log(f"⭐ Отнял {amount} Stars", admin_id, admin_name, user_id, user['username'],
                      f"Баланс Stars: {old_value} → {new_value}")
    elif action_type == 'add_energy':
        old_value = user['energy'];
        new_value = min(user['max_energy'], old_value + amount);
        update_energy_in_db(user_id, user, new_value)
        add_admin_log(f"⚡ Добавил {amount} энергии", admin_id, admin_name, user_id, user['username'],
                      f"Энергия: {old_value} → {new_value}")
    elif action_type == 'remove_energy':
        old_value = user['energy'];
        new_value = max(0, old_value - amount);
        update_energy_in_db(user_id, user, new_value)
        add_admin_log(f"⚡ Отнял {amount} энергии", admin_id, admin_name, user_id, user['username'],
                      f"Энергия: {old_value} → {new_value}")
    elif action_type == 'add_max_energy':
        old_value = user['max_energy'];
        new_value = old_value + amount;
        update_user(user_id, max_energy=new_value)
        add_admin_log(f"⚡ Увеличил макс. энергию на {amount}", admin_id, admin_name, user_id, user['username'],
                      f"Макс. энергия: {old_value} → {new_value}")
    elif action_type == 'remove_max_energy':
        old_value = user['max_energy'];
        new_value = max(100, old_value - amount);
        update_user(user_id, max_energy=new_value)
        add_admin_log(f"⚡ Уменьшил макс. энергию на {amount}", admin_id, admin_name, user_id, user['username'],
                      f"Макс. энергия: {old_value} → {new_value}")
    elif action_type == 'add_clicks':
        old_value = user['total_clicks'];
        new_value = old_value + amount;
        update_user(user_id, total_clicks=new_value)
        add_admin_log(f"👆 Добавил {amount} кликов", admin_id, admin_name, user_id, user['username'],
                      f"Клики: {old_value} → {new_value}")
    elif action_type == 'remove_clicks':
        old_value = user['total_clicks'];
        new_value = max(0, old_value - amount);
        update_user(user_id, total_clicks=new_value)
        add_admin_log(f"📉 Отнял {amount} кликов", admin_id, admin_name, user_id, user['username'],
                      f"Клики: {old_value} → {new_value}")
    elif action_type == 'unlock_prefix':
        prefix_id = data.get('prefix_id')
        if prefix_id:
            unlock_prefix(user_id, prefix_id)
            add_admin_log(f"👑 Разблокировал префикс {prefix_id}", admin_id, admin_name, user_id, user['username'])
    elif action_type == 'set_role':
        new_role = data.get('role')
        if new_role:
            old_role = user['role'];
            update_user(user_id, role=new_role)
            add_admin_log(f"👑 Изменил роль с {old_role} на {new_role}", admin_id, admin_name, user_id, user['username'])
    elif action_type == 'reset_energy':
        old_value = user['energy'];
        new_value = user['max_energy'];
        update_energy_in_db(user_id, user, new_value)
        add_admin_log(f"⚡ Сбросил энергию до максимума", admin_id, admin_name, user_id, user['username'],
                      f"Энергия: {old_value} → {new_value}")
    elif action_type == 'ban':
        days = data.get('days', 7);
        reason = data.get('reason', 'Нарушение правил')
        ban_user(user_id, days, reason, admin_id)
        add_admin_log(f"🔨 ЗАБАНИЛ игрока на {days} дней. Причина: {reason}", admin_id, admin_name, user_id,
                      user['username'])
    elif action_type == 'unban':
        unban_user(user_id)
        add_admin_log(f"🔓 РАЗБАНИЛ игрока", admin_id, admin_name, user_id, user['username'])
    elif action_type == 'process_withdrawal':
        withdrawal_id = data.get('withdrawal_id');
        status = data.get('status')
        if withdrawal_id and status in ['completed', 'rejected']:
            process_withdrawal_db(withdrawal_id, status, admin_id, admin_name)
            add_admin_log(f"💸 Обработал заявку на вывод #{withdrawal_id} - {status}", admin_id, admin_name)
    return jsonify({"success": True, "msg": "Обновлено"})


@app.route('/api/admin/clear_wallets', methods=['POST'])
def api_clear_wallets():
    key = request.args.get('key')
    if key != ADMIN_SECRET:
        return jsonify({"error": "Доступ запрещён"}), 403

    try:
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET ton_wallet = ''")
            count = cursor.rowcount

        add_admin_log(f"🗑️ Очистил все TON кошельки игроков (удалено {count} записей)",
                      request.args.get('user_id', 'Admin'), "Admin")

        return jsonify({"success": True, "count": count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/admin/clear_user_wallet', methods=['POST'])
def api_clear_user_wallet():
    key = request.args.get('key')
    if key != ADMIN_SECRET:
        return jsonify({"error": "Доступ запрещён"}), 403

    data = request.json
    user_id = data.get('user_id')

    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400

    try:
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET ton_wallet = '' WHERE user_id = ?", (user_id,))

        add_admin_log(f"🗑️ Отвязал TON кошелёк у игрока",
                      request.args.get('user_id', 'Admin'), "Admin", user_id)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/admin/lottery_action', methods=['POST'])
def api_admin_lottery_action():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    data = request.get_json()
    if not data: return jsonify({"success": False, "error": "No JSON"}), 400
    action = data['action']
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"
    if action == 'force_draw':
        perform_draw()
        add_admin_log(f"🎲 Принудительный розыгрыш лотереи", admin_id, admin_name)
        return jsonify({"success": True, "msg": "Розыгрыш запущен"})
    elif action == 'reset_lottery':
        reset_lottery()
        add_admin_log(f"🔄 Сброс лотереи", admin_id, admin_name)
        return jsonify({"success": True, "msg": "Лотерея сброшена"})
    elif action == 'set_pool':
        global lottery_pool
        old_pool = lottery_pool
        lottery_pool = data.get('amount', 0)
        save_lottery()
        add_admin_log(f"💰 Изменил призовой фонд с {old_pool} на {lottery_pool} USDT", admin_id, admin_name)
        return jsonify({"success": True, "msg": "Фонд изменён"})
    return jsonify({"success": False, "msg": "Неизвестное действие"})


@app.route('/api/admin/chart_data', methods=['GET'])
def api_admin_chart_data():
    key = request.args.get('key')
    if key != ADMIN_SECRET: return jsonify({"error": "Доступ запрещён"}), 403
    period = request.args.get('period', 'week')
    metric = request.args.get('metric', 'clicks')
    result = get_stats_history(period, metric)
    return jsonify(
        {"success": True, "labels": result["labels"], "data": result["data"], "metric": metric, "period": period})


# ========== TELEGRAM БОТ ==========
def handle_telegram_updates():
    last_update_id = 0
    session = requests.Session()
    session.verify = False
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            response = session.get(url, params=params, timeout=35)
            updates = response.json()
            if updates.get("ok"):
                for update in updates.get("result", []):
                    last_update_id = update["update_id"]
                    if "message" in update and "text" in update["message"]:
                        text = update["message"]["text"]
                        chat_id = update["message"]["chat"]["id"]
                        username = update["message"]["chat"].get("username", "")
                        first_name = update["message"]["chat"].get("first_name", "")
                        last_name = update["message"]["chat"].get("last_name", "")
                        if text.startswith("/start"):
                            parts = text.split()
                            ref_code = parts[1] if len(parts) > 1 else None
                            with db.get_cursor() as cursor:
                                cursor.execute("SELECT * FROM users WHERE user_id=?", (chat_id,))
                                existing = cursor.fetchone()
                                if not existing:
                                    now = time.time()
                                    ref_code_new = hashlib.md5(str(chat_id).encode()).hexdigest()[:8]
                                    founder_id = 5264622363
                                    role = "founder" if chat_id == founder_id else "player"
                                    unlocked = json.dumps(["player", "founder"]) if role == "founder" else json.dumps(
                                        ["player"])
                                    referrer_id = 0
                                    if ref_code:
                                        cursor.execute("SELECT user_id, username FROM users WHERE referral_code=?",
                                                       (ref_code,))
                                        referrer_row = cursor.fetchone()
                                        if referrer_row:
                                            referrer_id = referrer_row['user_id']
                                            cursor.execute(
                                                '''INSERT INTO referrals (referrer_id, referred_id, username, first_name, total_spent_lp) VALUES (?, ?, ?, ?, 0)''',
                                                (referrer_id, chat_id, username, first_name))
                                            add_log(
                                                f"👥 Новый реферал! {first_name or username} зарегистрировался по вашей ссылке",
                                                referrer_id, referrer_row['username'] or str(referrer_id))
                                            send_telegram_message(referrer_id,
                                                                  f"🎉 Новый реферал! {first_name or username} присоединился по вашей ссылке!")
                                    cursor.execute('''
                                        INSERT INTO users (user_id, wg, lp, energy, last_energy_update, tickets, total_clicks,
                                        upgrade_counts, ticket_counter, referral_code, referrer_id, likes, dislikes, settings,
                                        username, first_name, last_name, avatar_url, usdt, wins, role, stars,
                                        max_energy, energy_upgrades, energy_limit_upgrades, unlocked_prefixes, tutorial_completed, ton_wallet)
                                        VALUES (?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, ?, 0, 0,
                                        '{"theme":"dark"}', ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '')
                                    ''', (chat_id, time.time(), ref_code_new, referrer_id, username, first_name,
                                          last_name, "", role, unlocked))
                                    project_start = datetime.datetime(2026, 5, 1)
                                    if datetime.datetime.now() - project_start < datetime.timedelta(days=1):
                                        unlock_prefix(chat_id, 'pioneer')
                                        add_log(f"🏷️✨ Получил уникальный префикс ПЕРВООТКРЫВАТЕЛЬ!", chat_id,
                                                username or first_name)
                            keyboard = {
                                "inline_keyboard": [[{"text": "💰 Открыть игру", "web_app": {"url": WEBHOOK_URL}}]]}
                            send_telegram_message(chat_id,
                                                  "✨ Добро пожаловать в WereGood!\n\n💰 Кликай по монете, улучшай заработок и участвуй в вызовах!\n\n⬇️ Нажми на кнопку ниже, чтобы начать!",
                                                  keyboard)
                        elif text.startswith("/admin"):
                            if chat_id in ADMIN_IDS:
                                admin_url = f"{WEBHOOK_URL}/admin?key={ADMIN_SECRET}&user_id={chat_id}"
                                keyboard = {"inline_keyboard": [
                                    [{"text": "👑 Открыть админ-панель", "web_app": {"url": admin_url}}]]}
                                send_telegram_message(chat_id,
                                                      "👑 Админ-панель WereGood\n\n• 📊 Статистика\n• 💰 Выдача валюты\n• 🎲 Управление лотереей\n• 👑 Управление префиксами\n• 💸 Заявки на вывод\n\n⬇️ Нажми на кнопку",
                                                      keyboard)
                            else:
                                send_telegram_message(chat_id, "⛔ У вас нет доступа к админ-панели")
                    elif "pre_checkout_query" in update:
                        query = update["pre_checkout_query"]
                        answer_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerPreCheckoutQuery"
                        requests.post(answer_url, json={"pre_checkout_query_id": query["id"], "ok": True}, timeout=5)
                    elif "message" in update and "successful_payment" in update["message"]:
                        handle_successful_payment(update["message"]["chat"]["id"],
                                                  update["message"]["successful_payment"])
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка в polling: {e}")
            time.sleep(5)


# ========== ЗАПУСК ==========
if __name__ == '__main__':
    threading.Thread(target=handle_telegram_updates, daemon=True).start()
    print("✅ Бот запущен!")
    print("✅ Игра: http://127.0.0.1:5000")
    print("✅ Админ-панель: http://127.0.0.1:5000/admin?key=weregood_admin_2026_secure_key_xyz789")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)