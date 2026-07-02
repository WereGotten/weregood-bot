# ========== ОПТИМИЗИРОВАННЫЕ ИМПОРТЫ ==========
import sys
import sqlite3
import random
import datetime
import threading
import time
import hashlib
import json
import secrets
import re
import os
import shutil
import logging
import codecs
import base64
import uuid
from functools import wraps
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List
from queue import Queue
from flask_compress import Compress

from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import requests
from dotenv import load_dotenv

from cachetools import TTLCache, cached

# ========== ОПТИМИЗАЦИЯ ==========
sqlite3.enable_callback_tracebacks(True)

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class RateLimitFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if 'Клик' in msg or 'Вошёл' in msg or 'Вышел' in msg:
            return False
        return True


logger.addFilter(RateLimitFilter())

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ИЗ .env ==========
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", secrets.token_urlsafe(32))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_SECRET = os.getenv("ADMIN_SECRET")
PROJECT_WALLET_ADDRESS = os.getenv("PROJECT_WALLET_ADDRESS")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
CRYPTO_PAY_TESTNET = os.getenv("CRYPTO_PAY_TESTNET", "false").lower() == "true"
DATABASE_PATH = os.getenv("DATABASE_PATH", "database.db")
BOT_USERNAME = os.getenv("BOT_USERNAME", "WereGooodbot")

DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "5264622363").split(",")]

if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не задан в .env файле!")
if not ADMIN_SECRET:
    raise ValueError("❌ ADMIN_SECRET не задан в .env файле!")

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = secrets.token_hex(32)
app.config['SESSION_COOKIE_SECURE'] = not DEBUG_MODE
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = 3600
Compress(app)

# ========== НАСТРОЙКА CORS ==========
CORS(app, resources={
    r"/api/*": {
        "origins": [
            "https://weregood.ru",
            "https://www.weregood.ru",
            "https://web.telegram.org",
            "https://t.me",
            "http://weregood.ru",
            "http://80.90.185.16:5000",
            "https://80.90.185.16"
        ],
        "supports_credentials": True,
        "allow_headers": ["Content-Type", "Authorization"],
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    }
})

# ========== НАСТРОЙКИ SOCKETIO ==========
socketio = SocketIO(app,
                    cors_allowed_origins="*" if DEBUG_MODE else [
                        "https://weregood.ru",
                        "https://www.weregood.ru",
                        "https://web.telegram.org",
                        "https://t.me"
                    ],
                    ping_timeout=60,
                    ping_interval=25,
                    max_http_buffer_size=1e6,
                    async_mode='threading')

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
pending_invoices = {}
online_users = {}
online_users_lock = threading.Lock()
lottery_pool = 0
lottery_tickets = []
global_ticket_counter = 0
winning_numbers = []
is_drawn = False
draw_time = None
lottery_phase = "buy"

user_cache = {}
user_cache_time = {}
CACHE_TTL = 120
leaderboard_cache = []
leaderboard_cache_time = 0
LEADERBOARD_CACHE_TTL = 15

used_ton_transactions = set()
used_transaction_lock = threading.Lock()

lottery_lock = threading.Lock()
purchase_lock = threading.Lock()
energy_lock = threading.Lock()
user_energy_locks = defaultdict(threading.Lock)

click_queue = Queue()
click_workers = 25

# ========== ФОРТУНА ==========
current_fortune_round = {
    "round_id": None,
    "yellow_pool": 0,
    "red_pool": 0,
    "yellow_bets": [],
    "red_bets": [],
    "end_time": None
}
fortune_lock = threading.Lock()
FORTUNE_COMMISSION = 0.07
FORTUNE_ROUND_DURATION = 300
fortune_timer_thread_started = False
fortune_ending = False

def process_click_worker():
    while True:
        try:
            data = click_queue.get(timeout=1)
            if data is None:
                break
        except:
            pass

for _ in range(click_workers):
    t = threading.Thread(target=process_click_worker, daemon=True)
    t.start()

# ========== БУФЕР ДЛЯ КЛИКОВ ==========
click_buffer = {}
click_buffer_lock = threading.Lock()
BUFFER_FLUSH_INTERVAL = 5

def flush_click_buffer():
    while True:
        time.sleep(BUFFER_FLUSH_INTERVAL)
        with click_buffer_lock:
            if not click_buffer:
                continue
            to_update = list(click_buffer.items())
            click_buffer.clear()

        for user_id, total_clicks in to_update:
            try:
                with db.get_cursor() as cursor:
                    # Обновляем только total_clicks (суммарные клики в профиле)
                    cursor.execute(
                        "UPDATE users SET total_clicks = total_clicks + ? WHERE user_id = ?",
                        (total_clicks, user_id)
                    )
                    # ========== daily_clicks УЖЕ обновлён в api_click! ==========
                    # Поэтому НЕ обновляем его здесь!
                    invalidate_cache(user_id)
            except Exception as e:
                logger.error(f"Flush buffer error for user {user_id}: {e}")
                with click_buffer_lock:
                    click_buffer[user_id] = click_buffer.get(user_id, 0) + total_clicks

def async_click_tasks(user_id, user, earning, old_wg, new_wg, today):
    try:
        update_achievement_progress(user_id, 'autoclicker', 1)
        update_stats_history(today, clicks=1)
        if user["total_clicks"] == 0:
            add_log(f"🆕👆 ПЕРВЫЙ КЛИК в игре! +{earning:.4f} WG", user_id, user['username'],
                    old_wg, new_wg, "wg")
        else:
            add_log(f"🖱️ Клик по монете +{earning:.4f} WG", user_id, user['username'],
                    old_wg, new_wg, "wg")
        click_queue.put({"user_id": user_id})
    except Exception as e:
        logger.error(f"Async click tasks error: {e}")

threading.Thread(target=flush_click_buffer, daemon=True).start()

# ========== АВТОМАТИЧЕСКИЙ БЭКАП ==========
def backup_database():
    try:
        if os.path.exists(DATABASE_PATH):
            backup_dir = "backups"
            os.makedirs(backup_dir, exist_ok=True)
            backup_name = f"{backup_dir}/backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy2(DATABASE_PATH, backup_name)
            logger.info(f"📦 Создан бэкап БД: {backup_name}")
            for old_backup in Path(backup_dir).glob("backup_*.db"):
                if time.time() - old_backup.stat().st_mtime > 30 * 86400:
                    old_backup.unlink()
    except Exception as e:
        logger.error(f"Ошибка создания бэкапа: {e}")

def schedule_backup():
    while True:
        now = datetime.datetime.now()
        next_backup = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next_backup:
            next_backup += datetime.timedelta(days=1)
        wait_seconds = (next_backup - now).total_seconds()
        time.sleep(wait_seconds)
        backup_database()

if not DEBUG_MODE:
    threading.Thread(target=schedule_backup, daemon=True).start()

# ========== RATE LIMITING ==========
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


def check_ad_cooldown(user_id: int, ad_type: str, cooldown_minutes: int, daily_limit: int) -> Tuple[bool, str]:
    with db.get_cursor() as cursor:
        # Проверяем дневной лимит
        cursor.execute('''
            SELECT COUNT(*) FROM ad_watch_history 
            WHERE user_id = ? AND ad_type = ? 
            AND watched_at > datetime('now', '-1 day')
        ''', (user_id, ad_type))
        daily_count = cursor.fetchone()[0]
        if daily_count >= daily_limit:
            return False, f"Дневной лимит ({daily_limit} раз) исчерпан"

        # Проверяем последний просмотр
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
    with db.get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO ad_watch_history (user_id, ad_type, watched_at)
            VALUES (?, ?, datetime('now'))
        ''', (user_id, ad_type))

def check_admin_bruteforce(ip: str) -> bool:
    return admin_failures[ip] < 10

def record_admin_failure(ip: str):
    admin_failures[ip] += 1
    threading.Timer(3600, lambda: admin_failures.pop(ip, None)).start()

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

def string_to_hex_payload(text: str) -> str:
    if not text:
        return "00000000"
    return "00000000" + text.encode('utf-8').hex()

def check_ton_transaction(sender_wallet, expected_amount, user_id):
    try:
        expected_comment = f"WereGood:{user_id}"
        logger.info(f"🔍 [TON] Сканируем сеть. Юзер: {user_id}, Ожидаем коммент: '{expected_comment}'")
        raw_project_address = "0:69fa7db713b9158c72970e3d577b6b3c2605e0f109fbb0443af97c44fd07be3f"
        url = f"https://toncenter.com/api/v2/getTransactions?address={raw_project_address}&limit=40"
        api_key = globals().get('TONCENTER_API_KEY') or os.getenv('TONCENTER_API_KEY')
        if api_key:
            url += f"&api_key={api_key}"
        response = requests.get(url, timeout=12)
        if response.status_code != 200:
            logger.error(f"❌ [TON] Ошибка API Toncenter: {response.status_code}")
            return False, 0, None
        transactions = response.json().get('result', [])
        clean_sender = str(sender_wallet).strip()
        friendly_sender = ""
        if clean_sender.startswith("0:"):
            friendly_sender = raw_to_user_friendly(clean_sender).lower()
        else:
            friendly_sender = clean_sender.lower()
        for page in range(2):
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
                is_wallet_match = False
                if friendly_sender and (friendly_sender in source_address or source_address in friendly_sender):
                    is_wallet_match = True
                if not is_wallet_match and len(friendly_sender) > 10 and len(source_address) > 10:
                    core_sender = friendly_sender[3:]
                    core_source = source_address[3:]
                    if core_sender in core_source or core_source in core_sender:
                        is_wallet_match = True
                if (is_comment_match or is_wallet_match) and (amount_ton >= (expected_amount - 0.02)):
                    tx_hash = tx_data.get('transaction_id', {}).get('hash')
                    if 'used_transaction_lock' in globals() and 'db' in globals():
                        with used_transaction_lock:
                            with db.get_cursor() as cursor:
                                cursor.execute("SELECT id FROM used_ton_transactions WHERE tx_hash = ?", (tx_hash,))
                                if cursor.fetchone():
                                    logger.warning(f"⚠️ [TON] Дубликат! Транза {tx_hash} уже зачислена.")
                                    continue
                                cursor.execute("INSERT INTO used_ton_transactions (tx_hash, user_id) VALUES (?, ?)",
                                               (tx_hash, user_id))
                    logger.info(f"✅ [TON] Транзакция УСПЕШНО НАЙДЕНА! Хэш: {tx_hash}")
                    return True, amount_ton, tx_hash
            if transactions and 'transaction_id' in transactions[-1] and page == 0:
                lt = transactions[-1].get('lt')
                last_hash = transactions[-1]['transaction_id'].get('hash')
                next_url = f"https://toncenter.com/api/v2/getTransactions?address={raw_project_address}&limit=40&lt={lt}&hash={last_hash}"
                if api_key:
                    next_url += f"&api_key={api_key}"
                try:
                    res = requests.get(next_url, timeout=10)
                    transactions = res.json().get('result', [])
                except:
                    break
            else:
                break
        return False, 0, None
    except Exception as e:
        logger.error(f"❌ Ошибка в check_ton_transaction: {e}", exc_info=True)
        return False, 0, None

def convert_ton_address_to_raw(address: str) -> str:
    if not address:
        return address
    if address.startswith('0:'):
        return address
    try:
        import base64
        if address[0] in ['U', 'E']:
            address_b64 = address[1:]
            decoded = base64.urlsafe_b64decode(address_b64 + '==')
            hash_part = decoded[:32]
            workchain = 0 if address[0] == 'U' else 1
            return f"{workchain}:{hash_part.hex()}"
        else:
            return address
    except Exception as e:
        logger.error(f"Ошибка конвертации адреса {address}: {e}")
        return address

def check_origin():
    origin = request.headers.get('Origin', '')
    allowed_origins = [
        "https://weregood.ru",
        "https://www.weregood.ru",
        "https://web.telegram.org",
        "https://t.me",
        "http://80.90.185.16:5000",
        "https://80.90.185.16"
    ]
    if DEBUG_MODE:
        return True
    return origin in allowed_origins or origin == ''

@app.before_request
def before_request():
    if request.path.startswith('/static') or request.path == '/health' or request.path.startswith(
            '/tonconnect') or request.path.startswith('/api/adsgram') or request.path.startswith(
        '/api/promo') or request.path.startswith('/claim') or request.path.startswith('/api/fortune'):
        return None
    if not check_origin():
        logger.warning(f"CSRF попытка с Origin: {request.headers.get('Origin')}")
        return jsonify({"error": "Forbidden", "message": "Invalid origin"}), 403

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['ngrok-skip-browser-warning'] = 'true'
    if DEBUG_MODE:
        response.headers['Access-Control-Allow-Origin'] = '*'
    else:
        response.headers['Access-Control-Allow-Origin'] = 'https://web.telegram.org'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return response

def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = request.remote_addr
        if not DEBUG_MODE and not check_admin_bruteforce(client_ip):
            logger.warning(f"Брутфорс админки с IP: {client_ip}")
            return jsonify({"error": "Too many failed attempts. Try later."}), 429
        key = request.args.get('key') or request.headers.get('X-Admin-Key')
        if not key or not secrets.compare_digest(key, ADMIN_SECRET):
            if not DEBUG_MODE:
                record_admin_failure(client_ip)
            return jsonify({"error": "Доступ запрещён"}), 403
        admin_failures.pop(client_ip, None)
        return f(*args, **kwargs)
    return decorated_function

# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self.local = threading.local()
    @contextmanager
    def get_cursor(self):
        if not hasattr(self.local, 'conn') or self.local.conn is None:
            self.local.conn = sqlite3.connect(self.db_file, check_same_thread=False, timeout=10)
            self.local.conn.row_factory = sqlite3.Row
            self.local.conn.execute("PRAGMA journal_mode=WAL")
            self.local.conn.execute("PRAGMA busy_timeout=30000")
            self.local.conn.execute("PRAGMA synchronous=NORMAL")
            self.local.conn.execute("PRAGMA cache_size=-204800")
            self.local.conn.execute("PRAGMA mmap_size=536870912")
            self.local.conn.execute("PRAGMA temp_store=MEMORY")
            self.local.conn.execute("PRAGMA foreign_keys = ON;")
        cursor = self.local.conn.cursor()
        try:
            yield cursor
            self.local.conn.commit()
        except Exception as e:
            self.local.conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            cursor.close()

def repair_database():
    if not os.path.exists(DATABASE_PATH):
        return True
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check")
        result = cursor.fetchone()
        if result[0] == "ok":
            conn.close()
            return True
        logger.warning(f"БД повреждена: {result[0]}. Удаляем...")
        conn.close()
        os.remove(DATABASE_PATH)
        for ext in ['-wal', '-shm']:
            if os.path.exists(DATABASE_PATH + ext):
                os.remove(DATABASE_PATH + ext)
        return True
    except Exception as e:
        logger.error(f"Ошибка восстановления БД: {e}")
        return False

repair_database()
db = Database(DATABASE_PATH)

ALLOWED_UPDATE_FIELDS = {
    'wg', 'lp', 'energy', 'last_energy_update', 'tickets', 'total_clicks',
    'upgrade_counts', 'free_upgrade_counts', 'username', 'first_name', 'last_name', 'ticket_counter',
    'referral_code', 'referrer_id', 'likes', 'dislikes', 'settings',
    'avatar_url', 'usdt', 'wins', 'role', 'stars', 'max_energy',
    'energy_upgrades', 'energy_limit_upgrades', 'unlocked_prefixes',
    'tutorial_completed', 'ton_wallet', 'banned_until', 'ban_reason', 'banned_by',
    'completed_achievements', 'daily_clicks',
    'fortune_bets_count', 'fortune_wins_count', 'fortune_total_bet_amount',
    'language'
}

MAX_USER_CACHE = 5000

def invalidate_cache(user_id):
    if user_id in user_cache:
        del user_cache[user_id]
        if user_id in user_cache_time:
            del user_cache_time[user_id]

def get_user(user_id, force_refresh=False, username=None, first_name=None, last_name=None, avatar_url=None):
    now = time.time()
    if not force_refresh and user_id in user_cache:
        if now - user_cache_time.get(user_id, 0) < CACHE_TTL:
            return user_cache[user_id].copy()
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row:
            now_time = time.time()
            ref_code = hashlib.md5(str(user_id).encode()).hexdigest()[:8]
            founder_id = 5264622363
            role = "founder" if user_id == founder_id else "player"
            unlocked = json.dumps(["player", "founder"]) if role == "founder" else json.dumps(["player"])
            final_username = username if username else ""
            final_first_name = first_name if first_name else ""
            final_last_name = last_name if last_name else ""
            final_avatar_url = avatar_url if avatar_url else ""
            try:
                cursor.execute('''
                    INSERT INTO users (
                        user_id, wg, lp, energy, last_energy_update, tickets, total_clicks,
                        upgrade_counts, ticket_counter, referral_code, referrer_id, likes, dislikes, settings,
                        username, first_name, last_name, avatar_url, usdt, wins, role, stars,
                        max_energy, energy_upgrades, energy_limit_upgrades, unlocked_prefixes, tutorial_completed, ton_wallet,
                        banned_until, ban_reason, banned_by, completed_achievements, language
                    ) VALUES (
                        ?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, ?, 0, 0,
                        '{"theme":"dark"}', ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '', 0, '', 0, 0, ?
                    )
                ''', (
                    user_id, now_time, ref_code, 0,
                    final_username, final_first_name, final_last_name, final_avatar_url,
                    role, unlocked, "ru"
                ))
                cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
                row = cursor.fetchone()
            except Exception as e:
                logger.error(f"Ошибка создания пользователя {user_id}: {e}")
                cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
                row = cursor.fetchone()
        if not row:
            default_user = {
                "user_id": user_id, "wg": 0, "lp": 0, "energy": 500, "last_energy_update": time.time(),
                "tickets": [], "total_clicks": 0, "upgrade_counts": {1: 0, 2: 0, 3: 0}, "username": "",
                "first_name": "", "last_name": "", "ticket_counter": 0, "referral_code": "", "referrer_id": 0,
                "likes": 0, "dislikes": 0, "settings": {"theme": "dark"}, "avatar_url": "", "usdt": 0, "wins": 0,
                "role": "player", "stars": 0, "max_energy": 500, "energy_upgrades": 0, "energy_limit_upgrades": 0,
                "unlocked_prefixes": ["player"], "ton_wallet": "", "banned_until": 0, "ban_reason": "", "banned_by": 0,
                "completed_achievements": 0, "language": "ru"
            }
            user_cache[user_id] = default_user
            user_cache_time[user_id] = now
            if len(user_cache) > MAX_USER_CACHE:
                oldest = sorted(user_cache_time.items(), key=lambda x: x[1])[:MAX_USER_CACHE // 10]
                for uid, _ in oldest:
                    if uid in user_cache:
                        del user_cache[uid]
                    if uid in user_cache_time:
                        del user_cache_time[uid]
            return default_user
        upgrade_counts = json.loads(row['upgrade_counts']) if row['upgrade_counts'] else {1: 0, 2: 0, 3: 0}
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
        user_data = {
            "user_id": row['user_id'], "wg": row['wg'], "lp": row['lp'], "energy": row['energy'],
            "last_energy_update": row['last_energy_update'],
            "tickets": json.loads(row['tickets']) if row['tickets'] else [], "total_clicks": row['total_clicks'],
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
            "ton_wallet": row['ton_wallet'] if 'ton_wallet' in row.keys() else '',
            "banned_until": row['banned_until'] if 'banned_until' in row.keys() else 0,
            "ban_reason": row['ban_reason'] if 'ban_reason' in row.keys() else '',
            "banned_by": row['banned_by'] if 'banned_by' in row.keys() else 0,
            "completed_achievements": row['completed_achievements'] if 'completed_achievements' in row.keys() else 0
        }
        user_cache[user_id] = user_data
        user_cache_time[user_id] = now
        return user_data

def safe_update_user(user_id, **kwargs):
    with db.get_cursor() as cursor:
        for key, value in kwargs.items():
            if key not in ALLOWED_UPDATE_FIELDS:
                logger.warning(f"Попытка обновить запрещённое поле: {key}")
                continue
            if key in ['upgrade_counts', 'tickets', 'settings', 'unlocked_prefixes']:
                if value is None:
                    value = '{}' if key == 'upgrade_counts' else '[]'
                else:
                    value = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            cursor.execute(f'UPDATE users SET "{key}" = ? WHERE user_id = ?', (value, user_id))
    invalidate_cache(user_id)

def validate_user_id(user_id):
    try:
        user_id = int(user_id)
        return user_id > 0, user_id
    except (TypeError, ValueError):
        return False, None

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

# ========== ДОСТИЖЕНИЯ ФУНКЦИИ ==========
def get_achievements_list():
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM achievements ORDER BY id')
        return [dict(row) for row in cursor.fetchall()]

def get_user_achievements(user_id):
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT a.*, ua.current_count, ua.is_completed, ua.completed_at
            FROM achievements a
            LEFT JOIN user_achievements ua ON a.id = ua.achievement_id AND ua.user_id = ?
            ORDER BY a.id
        ''', (user_id,))
        rows = cursor.fetchall()
        result = []
        completed_count = 0
        for row in rows:
            ach = dict(row)
            ach['current_count'] = ach.get('current_count') or 0
            ach['is_completed'] = ach.get('is_completed') or 0
            if ach['is_completed']:
                completed_count += 1
            result.append(ach)
        return result, completed_count

def update_achievement_progress(user_id, achievement_name, increment=1, set_value=None):
    with db.get_cursor() as cursor:
        cursor.execute('SELECT id, target_count FROM achievements WHERE name = ?', (achievement_name,))
        ach = cursor.fetchone()
        if not ach:
            return False
        ach_id = ach['id']
        target = ach['target_count']
        cursor.execute('''
            SELECT id, current_count, is_completed FROM user_achievements 
            WHERE user_id = ? AND achievement_id = ?
        ''', (user_id, ach_id))
        existing = cursor.fetchone()
        if existing and existing['is_completed']:
            return True
        if set_value is not None:
            new_count = set_value
        else:
            new_count = (existing['current_count'] if existing else 0) + increment
        new_count = min(new_count, target)
        is_completed = new_count >= target
        if existing:
            cursor.execute('''
                UPDATE user_achievements 
                SET current_count = ?, is_completed = ?, completed_at = ?
                WHERE user_id = ? AND achievement_id = ?
            ''', (new_count, is_completed, datetime.datetime.now().isoformat() if is_completed else None, user_id,
                  ach_id))
        else:
            cursor.execute('''
                INSERT INTO user_achievements (user_id, achievement_id, current_count, is_completed, completed_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, ach_id, new_count, is_completed,
                  datetime.datetime.now().isoformat() if is_completed else None))
        if is_completed:
            cursor.execute('''
                UPDATE users SET completed_achievements = (
                    SELECT COUNT(*) FROM user_achievements 
                    WHERE user_id = ? AND is_completed = 1
                ) WHERE user_id = ?
            ''', (user_id, user_id))
            invalidate_cache(user_id)
            update_legend_prefixes()
            user = get_user(user_id)
            add_log(f"🏆 ВЫПОЛНИЛ ДОСТИЖЕНИЕ: {achievement_name}!", user_id, user['username'])
            ach_display = get_achievement_display_name(achievement_name)
            send_telegram_message(user_id,
                                  f"🏆 ПОЗДРАВЛЯЕМ!\n\nВы выполнили достижение: {ach_display}!\n\nПродолжайте в том же духе! 🎉")
        return True

def get_achievement_display_name(achievement_name):
    names = {
        'autoclicker': 'Автокликер',
        'investor': 'Инвестор',
        'social': 'Общительный',
        'gambler': 'Азартный',
        'lucky': 'Счастливчик',
        'liker': 'Подписчик',
        'hater': 'Хейтер',
        'ad_lover': 'Любитель TV',
        'spender': 'Транжира',
        'task_master': 'Выполнитель',
        # ========== НОВЫЕ ==========
        'brave': 'Бесстрашный',
        'lucky_fortune': 'Везучий',
        'gambler_fortune': 'Лудоман',
        'crazy': 'Сумасшедший'
    }
    return names.get(achievement_name, achievement_name)

def update_legend_prefixes():
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT user_id, completed_achievements
            FROM users
            WHERE completed_achievements > 0
            ORDER BY completed_achievements DESC, user_id
            LIMIT 50
        ''')
        top_players = cursor.fetchall()
        top_5_ids = [row['user_id'] for row in top_players[:5]]
        if top_5_ids:
            placeholders = ','.join(['?'] * len(top_5_ids))
            cursor.execute(f'''
                UPDATE users SET role = 'player' 
                WHERE role = 'legend' AND user_id NOT IN ({placeholders})
            ''', top_5_ids)
        else:
            cursor.execute("UPDATE users SET role = 'player' WHERE role = 'legend'")
        for user_id in top_5_ids:
            cursor.execute('''
                UPDATE users SET role = 'legend' WHERE user_id = ? AND role != 'legend'
            ''', (user_id,))
            invalidate_cache(user_id)
        for user_id in top_5_ids:
            cursor.execute('SELECT unlocked_prefixes FROM users WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if row:
                unlocked = json.loads(row['unlocked_prefixes']) if row['unlocked_prefixes'] else ["player"]
                if 'legend' not in unlocked:
                    unlocked.append('legend')
                    cursor.execute('UPDATE users SET unlocked_prefixes = ? WHERE user_id = ?',
                                   (json.dumps(unlocked), user_id))
                    invalidate_cache(user_id)

def get_achievements_top(limit=50):
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT user_id, username, first_name, avatar_url, completed_achievements, role
            FROM users
            ORDER BY completed_achievements DESC, user_id
            LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        result = []
        for i, row in enumerate(rows):
            if row['username'] and row['username'] != '':
                display_name = '@' + row['username']
            elif row['first_name'] and row['first_name'] != '':
                display_name = row['first_name']
            else:
                display_name = f"Player_{row['user_id']}"
            result.append({
                'rank': i + 1,
                'user_id': row['user_id'],
                'username': display_name,
                'avatar_url': row['avatar_url'],
                'completed': row['completed_achievements'],
                'role': row['role']
            })
        return result

def get_user_referrals_count(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
        return cursor.fetchone()[0]


# ========== ДОСТИЖЕНИЯ ФОРТУНЫ ==========
def update_fortune_achievements(user_id, bet_amount=None, is_win=False, is_new_round=True):
    """Обновляет достижения, связанные с Фортуной

    Args:
        user_id: ID пользователя
        bet_amount: Сумма ставки (для Лудомана)
        is_win: Победа или нет (для Везучего)
        is_new_round: Является ли это первой ставкой в раунде (для Бесстрашного и Сумасшедшего)
    """
    try:
        with db.get_cursor() as cursor:
            # Получаем текущие значения
            cursor.execute("""
                SELECT fortune_bets_count, fortune_wins_count, fortune_total_bet_amount 
                FROM users WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()

            if not row:
                return

            current_bets = row['fortune_bets_count'] or 0
            current_wins = row['fortune_wins_count'] or 0
            current_total_bet = row['fortune_total_bet_amount'] or 0

            updated = False

            # Обновляем количество ставок (ТОЛЬКО если это новая ставка в раунде)
            if bet_amount is not None and bet_amount > 0:
                new_total_bet = current_total_bet + bet_amount
                cursor.execute("""
                    UPDATE users SET fortune_total_bet_amount = ? WHERE user_id = ?
                """, (new_total_bet, user_id))
                updated = True

                # Проверяем достижение "Лудоман" (200 000 WG суммарно) - всегда обновляем сумму
                if new_total_bet >= 200000:
                    update_achievement_progress(user_id, 'gambler_fortune', set_value=200000)
                else:
                    update_achievement_progress(user_id, 'gambler_fortune', int(bet_amount))

                # ========== КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: обновляем счётчики ставок ТОЛЬКО для новых раундов ==========
                if is_new_round:
                    new_bets = current_bets + 1
                    cursor.execute("""
                        UPDATE users SET fortune_bets_count = ? WHERE user_id = ?
                    """, (new_bets, user_id))
                    updated = True

                    # Проверяем достижение "Бесстрашный" (100 ставок)
                    update_achievement_progress(user_id, 'brave', 1)

                    # Проверяем достижение "Сумасшедший" (1000 ставок)
                    if new_bets >= 1000:
                        update_achievement_progress(user_id, 'crazy', set_value=1000)
                    else:
                        update_achievement_progress(user_id, 'crazy', 1)

            # Обновляем количество побед
            if is_win:
                new_wins = current_wins + 1
                cursor.execute("""
                    UPDATE users SET fortune_wins_count = ? WHERE user_id = ?
                """, (new_wins, user_id))
                updated = True

                # Проверяем достижение "Везучий" (100 побед)
                if new_wins >= 100:
                    update_achievement_progress(user_id, 'lucky_fortune', set_value=100)
                else:
                    update_achievement_progress(user_id, 'lucky_fortune', 1)

            if updated:
                invalidate_cache(user_id)

    except Exception as e:
        logger.error(f"Ошибка обновления достижений Фортуны: {e}")

    # ========== ИНИЦИАЛИЗАЦИЯ БД ==========
    def init_db():
        with db.get_cursor() as cursor:
            # ========== ОСНОВНЫЕ ТАБЛИЦЫ ==========
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
                    ton_wallet TEXT DEFAULT '',
                    banned_until REAL DEFAULT 0,
                    ban_reason TEXT DEFAULT '',
                    banned_by INTEGER DEFAULT 0,
                    completed_achievements INTEGER DEFAULT 0,
                    daily_clicks INTEGER DEFAULT 0,
                    fortune_bets_count INTEGER DEFAULT 0,
                    fortune_wins_count INTEGER DEFAULT 0,
                    fortune_total_bet_amount REAL DEFAULT 0
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
                    draw_time TIMESTAMP,
                    lottery_phase TEXT DEFAULT 'buy'
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
                    total_earned_wg REAL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS used_ton_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tx_hash TEXT UNIQUE,
                    user_id INTEGER,
                    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS promo_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    reward_type TEXT NOT NULL,
                    reward_amount INTEGER NOT NULL,
                    max_uses INTEGER NOT NULL,
                    used_count INTEGER DEFAULT 0,
                    password TEXT,
                    created_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS promo_activations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    promo_id INTEGER,
                    user_id INTEGER,
                    activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (promo_id) REFERENCES promo_codes(id)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ad_watch_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    ad_type TEXT,
                    watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # ========== ТАБЛИЦА ЗАДАНИЙ С НОВЫМИ ПОЛЯМИ ==========
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    task_type TEXT DEFAULT 'channel',
                    miniapp_url TEXT DEFAULT '',
                    channel_link TEXT DEFAULT '',
                    channel_username TEXT DEFAULT '',
                    channel_avatar TEXT DEFAULT '',
                    reward_amount INTEGER DEFAULT 10,
                    reward_type TEXT DEFAULT 'wg',
                    daily_limit INTEGER DEFAULT 1,
                    total_limit INTEGER DEFAULT 100,
                    completed_count INTEGER DEFAULT 0,
                    days_remaining INTEGER DEFAULT 7,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    task_id INTEGER,
                    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reward_claimed BOOLEAN DEFAULT 1,
                    UNIQUE(user_id, task_id)
                )
            ''')

            # ========== ТАБЛИЦА ДЛЯ MINI APP КЛИКОВ ==========
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS task_miniapp_clicks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    task_id INTEGER,
                    clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, task_id)
                )
            ''')
            print("✅ Создана таблица task_miniapp_clicks")

            # ========== ДОСТИЖЕНИЯ ==========
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS achievements (
                    id INTEGER PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    icon TEXT NOT NULL,
                    target_count INTEGER NOT NULL
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_achievements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    achievement_id INTEGER NOT NULL,
                    current_count INTEGER DEFAULT 0,
                    is_completed BOOLEAN DEFAULT 0,
                    completed_at TIMESTAMP,
                    UNIQUE(user_id, achievement_id)
                )
            ''')

            # ========== ФОРТУНА ==========
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS fortune_bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    team TEXT NOT NULL,
                    amount REAL NOT NULL,
                    net_amount REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS fortune_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id TEXT NOT NULL,
                    winner_team TEXT,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    yellow_pool REAL DEFAULT 0,
                    red_pool REAL DEFAULT 0
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS fortune_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    round_id TEXT NOT NULL,
                    team TEXT NOT NULL,
                    amount REAL NOT NULL,
                    result TEXT NOT NULL,
                    win_amount REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS fortune_active_bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    team TEXT NOT NULL,
                    amount REAL NOT NULL,
                    net_amount REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # ========== PAYDAY БОНУС ==========
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payday_bonus (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    multiplier REAL DEFAULT 1.0,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    is_active BOOLEAN DEFAULT 0,
                    updated_by INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Добавляем запись payday если её нет
            cursor.execute("SELECT id FROM payday_bonus WHERE id = 1")
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO payday_bonus (id, multiplier, is_active)
                    VALUES (1, 1.0, 0)
                ''')

            # ========== ИНДЕКСЫ ==========
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_fortune_bets_round ON fortune_bets(round_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_fortune_bets_user ON fortune_bets(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_fortune_rounds_id ON fortune_rounds(round_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_fortune_history_user ON fortune_history(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_achievements_user ON user_achievements(user_id)')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_user_achievements_completed ON user_achievements(is_completed)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ad_watch_user_type ON ad_watch_history(user_id, ad_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ad_watch_date ON ad_watch_history(watched_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON system_logs(timestamp DESC)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_user_id ON system_logs(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_tasks ON user_tasks(user_id, task_id)')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_task_miniapp_clicks ON task_miniapp_clicks(user_id, task_id)')

            # ========== МИГРАЦИЯ СТАРЫХ ТАБЛИЦ (user) ==========
            user_columns = ['banned_until', 'ban_reason', 'banned_by', 'completed_achievements', 'daily_clicks',
                            'fortune_bets_count', 'fortune_wins_count', 'fortune_total_bet_amount']
            for col in user_columns:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} DEFAULT 0")
                    print(f"✅ Добавлена колонка {col} в users")
                except:
                    pass

            # ========== МИГРАЦИЯ СТАРЫХ ТАБЛИЦ (lottery) ==========
            lottery_columns = ['is_drawn', 'draw_time', 'global_ticket_counter', 'lottery_phase']
            for col in lottery_columns:
                try:
                    cursor.execute(f"ALTER TABLE lottery ADD COLUMN {col} DEFAULT 0")
                    print(f"✅ Добавлена колонка {col} в lottery")
                except:
                    pass

            # ========== МИГРАЦИЯ СТАРЫХ ТАБЛИЦ (fortune_rounds) ==========
            fortune_columns = ['end_time', 'winner_team', 'yellow_pool', 'red_pool']
            for col in fortune_columns:
                try:
                    cursor.execute(f"ALTER TABLE fortune_rounds ADD COLUMN {col} DEFAULT 0")
                    print(f"✅ Добавлена колонка {col} в fortune_rounds")
                except:
                    pass

            # ========== МИГРАЦИЯ СТАРЫХ ТАБЛИЦ (fortune_active_bets) ==========
            try:
                cursor.execute("ALTER TABLE fortune_active_bets ADD COLUMN net_amount REAL DEFAULT 0")
                print("✅ Добавлена колонка net_amount в fortune_active_bets")
            except:
                pass

            # ========== МИГРАЦИЯ СТАРЫХ ТАБЛИЦ (tasks) ==========
            tasks_columns = ['task_type', 'miniapp_url']
            for col in tasks_columns:
                try:
                    if col == 'task_type':
                        cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT 'channel'")
                    else:
                        cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT ''")
                    print(f"✅ Добавлена колонка {col} в tasks")
                except:
                    pass

                # ========== МИГРАЦИЯ: free_upgrade_counts ==========
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN free_upgrade_counts TEXT DEFAULT '{}'")
                    print("✅ Добавлена колонка free_upgrade_counts в users")
                except:
                    pass

            # ========== ЗАПОЛНЕНИЕ ДОСТИЖЕНИЙ ==========
            achievements_list = [
                ('autoclicker', '🏆 Автокликер', 'Сделать 50 000 кликов по монете', '🖱️', 50000),
                ('investor', '💰 Инвестор', 'Купить 30 улучшений в магазине', '📈', 30),
                ('social', '👥 Общительный', 'Пригласить 10 рефералов', '🤝', 10),
                ('gambler', '🎲 Азартный', 'Купить 100 билетов в Вызове', '🎫', 100),
                ('lucky', '🍀 Счастливчик', 'Выиграть Вызов 5 раз', '🏆', 5),
                ('liker', '👍 Подписчик', 'Поставить 200 лайков', '❤️', 200),
                ('hater', '👎 Хейтер', 'Поставить 200 дизлайков', '💔', 200),
                ('ad_lover', '📺 Любитель TV', 'Просмотреть 100 реклам', '🎬', 100),
                ('spender', '💸 Транжира', 'Потратить 50 000 WG Coin', '💎', 50000),
                ('task_master', '📋 Выполнитель', 'Выполнить 10 заданий', '✅', 10),
                ('brave', '⚔️ Бесстрашный', 'Сделать 100 ставок в Командной Фортуне', '🎲', 100),
                ('lucky_fortune', '🍀 Везучий', 'Выиграть 100 раз в Командной Фортуне', '🏆', 100),
                ('gambler_fortune', '🎰 Лудоман', 'Поставить 200 000 WG в Командной Фортуне', '💰', 200000),
                ('crazy', '🤪 Сумасшедший', 'Сделать 1000 ставок в Командной Фортуне', '🔥', 1000)
            ]

            for ach in achievements_list:
                cursor.execute('''
                    INSERT OR IGNORE INTO achievements (name, display_name, description, icon, target_count)
                    VALUES (?, ?, ?, ?, ?)
                ''', ach)

            # ========== ИНИЦИАЛИЗАЦИЯ ЛОТЕРЕИ ==========
            cursor.execute("SELECT * FROM lottery LIMIT 1")
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO lottery (prize_pool, tickets, winning_numbers, is_drawn, lottery_phase) VALUES (0, '[]', '', 0, 'buy')"
                )

            print("✅ Все таблицы созданы/обновлены успешно!")

    init_db()

def is_banned(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT banned_until, ban_reason FROM users WHERE user_id = ? AND banned_until > ?",
                       (user_id, time.time()))
        row = cursor.fetchone()
        if row:
            return True, {"until": row['banned_until'], "reason": row['ban_reason'],
                          "until_date": datetime.datetime.fromtimestamp(row['banned_until']).strftime(
                              "%Y-%m-%d %H:%M:%S")}
    return False, None

def ban_user(user_id, days, reason, admin_id):
    until = time.time() + (days * 86400)
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET banned_until = ?, ban_reason = ?, banned_by = ? WHERE user_id = ?",
                       (until, reason, admin_id, user_id))
    invalidate_cache(user_id)
    logger.info(f"Пользователь {user_id} забанен на {days} дней. Причина: {reason}")
    add_log(f"🔨 ЗАБАНИЛ пользователя на {days} дней. Причина: {reason}", admin_id, "Admin")

def unban_user(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET banned_until = 0, ban_reason = '', banned_by = 0 WHERE user_id = ?",
                       (user_id,))
    invalidate_cache(user_id)
    logger.info(f"Пользователь {user_id} разбанен")
    add_log(f"🔓 РАЗБАНИЛ пользователя", user_id, "Admin")

def delete_user(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM user_achievements WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM user_tasks WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM votes WHERE voter_id = ? OR target_id = ?", (user_id, user_id))
        cursor.execute("DELETE FROM referrals WHERE referrer_id = ? OR referred_id = ?", (user_id, user_id))
        cursor.execute("DELETE FROM withdrawal_requests WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM daily_rewards WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM promo_activations WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM ad_watch_history WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM successful_payments WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM used_ton_transactions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM lottery_tickets_history WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM fortune_bets WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM fortune_history WHERE user_id = ?", (user_id,))
        global lottery_tickets
        lottery_tickets = [t for t in lottery_tickets if t.get("user_id") != user_id]
        save_lottery()
    invalidate_cache(user_id)
    logger.info(f"Пользователь {user_id} полностью удалён из БД")
    add_log(f"🗑️ ПОЛНОСТЬЮ УДАЛИЛ пользователя из БД", 0, "System")
    return True

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
    log_message = escape_html(log_message)
    username = escape_html(username)
    details = escape_html(details)
    with db.get_cursor() as cursor:
        cursor.execute(
            'INSERT INTO system_logs (timestamp, action, user_id, username, details, log_type) VALUES (?, ?, ?, ?, ?, "user")',
            (timestamp, log_message, user_id, username, details))
    logger.info(f"LOG: {log_message} (user={user_id})")

def add_admin_log(action, admin_id, admin_name, target_id=None, target_name=None, details=""):
    if target_id:
        log_msg = f"👑 {action} | Админ: {admin_name} (ID: {admin_id}) | Игрок: {target_name} (ID: {target_id})"
    else:
        log_msg = f"👑 {action} | Админ: {admin_name} (ID: {admin_id})"
    if details:
        log_msg += f" | {details}"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = escape_html(log_msg)
    admin_name = escape_html(admin_name)
    target_name = escape_html(target_name) if target_name else ''
    details = escape_html(details)
    with db.get_cursor() as cursor:
        cursor.execute(
            'INSERT INTO system_logs (timestamp, action, user_id, username, details, log_type) VALUES (?, ?, ?, ?, ?, "admin")',
            (timestamp, log_msg, admin_id, admin_name, details))
    logger.info(f"ADMIN: {log_msg}")

def get_logs(log_type='all', limit=100, offset=0, date=None, action_filter=None, user_id_filter=None):
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
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        count_query = "SELECT COUNT(*) as total FROM system_logs"
        if conditions:
            count_query += " WHERE " + " AND ".join(conditions)
        cursor.execute(count_query, params[:len(params) - 2] if len(params) > 2 else [])
        total = cursor.fetchone()['total']
        logs = []
        for row in rows:
            logs.append({"id": row['id'], "timestamp": row['timestamp'], "action": escape_html(row['action']),
                         "user_id": row['user_id'], "username": escape_html(row['username']),
                         "details": escape_html(row['details']), "type": row['log_type']})
        return logs, total

def update_stats_history(date, clicks=0, ad_views=0, stars=0, online=0, tickets=0, users=0):
    with db.get_cursor() as cursor:
        cursor.execute(
            '''INSERT INTO stats_history (date, clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users) 
               VALUES (?, ?, ?, ?, ?, ?, ?) 
               ON CONFLICT(date) DO UPDATE SET 
               clicks = clicks + ?, ad_views = ad_views + ?, stars_donated = stars_donated + ?, 
               online_peak = MAX(online_peak, ?), tickets_sold = tickets_sold + ?, new_users = new_users + ?''',
            (date, clicks, ad_views, stars, online, tickets, users, clicks, ad_views, stars, online, tickets, users))


def get_stats_history(period='week', metric='clicks'):
    now = datetime.datetime.now()
    data = []
    labels = []

    # Маппинг метрик на колонки в БД
    metric_map = {
        'clicks': 'clicks',
        'tickets': 'tickets_sold',
        'users': 'new_users',
        'ad_views': 'ad_views',
        'stars': 'stars_donated',
        'online': 'online_peak'
    }

    db_column = metric_map.get(metric, 'clicks')

    with db.get_cursor() as cursor:
        if period == 'day':
            for i in range(24):
                labels.append(f"{i}:00")
                date_key = now.strftime("%Y-%m-%d")
                cursor.execute(
                    f"SELECT {db_column} FROM stats_history WHERE date = ?",
                    (date_key,)
                )
                row = cursor.fetchone()
                val = row[0] if row else 0
                data.append(val or 0)

        elif period == 'week':
            for i in range(6, -1, -1):
                date = (now - datetime.timedelta(days=i)).strftime("%d.%m")
                labels.append(date)
                date_key = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                cursor.execute(
                    f"SELECT {db_column} FROM stats_history WHERE date = ?",
                    (date_key,)
                )
                row = cursor.fetchone()
                val = row[0] if row else 0
                data.append(val or 0)

        elif period == 'month':
            for i in range(29, -1, -1):
                date = (now - datetime.timedelta(days=i)).strftime("%d.%m")
                labels.append(date)
                date_key = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                cursor.execute(
                    f"SELECT {db_column} FROM stats_history WHERE date = ?",
                    (date_key,)
                )
                row = cursor.fetchone()
                val = row[0] if row else 0
                data.append(val or 0)

        else:  # year
            for i in range(11, -1, -1):
                month_date = now - datetime.timedelta(days=30 * i)
                labels.append(month_date.strftime("%b %Y"))
                month_start = month_date.strftime("%Y-%m")
                cursor.execute(
                    f"SELECT SUM({db_column}) FROM stats_history WHERE date LIKE ?",
                    (f'{month_start}%',)
                )
                row = cursor.fetchone()
                val = row[0] if row else 0
                data.append(val or 0)

    return {"labels": labels, "data": data}


def create_withdrawal_request_db(user_id, username, amount, address, network):
    if network == "TON" and not validate_ton_address(address):
        raise ValueError("Invalid TON address")
    if amount > 1000:
        raise ValueError("Withdrawal amount exceeds limit")
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.get_cursor() as cursor:
        cursor.execute(
            'INSERT INTO withdrawal_requests (user_id, username, amount, address, network, status, created_at) VALUES (?, ?, ?, ?, ?, "pending", ?)',
            (user_id, username, amount, address, network, created_at))
        withdrawal_id = cursor.lastrowid
    user = get_user(user_id)
    add_log(f"💸 Создал заявку на вывод {amount} USDT", user_id, user['username'], old_value=user['usdt'],
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
        if not w:
            return False
        processed_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('UPDATE withdrawal_requests SET status = ?, processed_at = ? WHERE id = ?',
                       (status, processed_at, withdrawal_id))
        if status == "completed":
            send_telegram_message(w['user_id'],
                                  f"✅ Ваша заявка на вывод {w['amount']} USDT одобрена! Средства отправлены на указанный адрес.")
        elif status == "rejected":
            user = get_user(w['user_id'])
            safe_update_user(w['user_id'], usdt=user['usdt'] + w['amount'])
            send_telegram_message(w['user_id'],
                                  f"❌ Ваша заявка на вывод {w['amount']} USDT отклонена. Средства возвращены на баланс.")
        return True


def send_telegram_message(chat_id, text, reply_markup=None, retry=2):
    """
    Отправка сообщения в Telegram с повторными попытками

    Args:
        chat_id: ID чата/пользователя
        text: Текст сообщения (поддерживает HTML)
        reply_markup: Клавиатура (опционально)
        retry: Количество повторных попыток при ошибке

    Returns:
        Response или None при ошибке
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        data["reply_markup"] = reply_markup

    for attempt in range(retry + 1):
        try:
            response = requests.post(
                url,
                json=data,
                timeout=5,  # ← Уменьшено с 15 до 5 секунд
                verify=not DEBUG_MODE  # ← Используем настройку из DEBUG_MODE
            )

            # Проверяем статус ответа
            if response.status_code == 200:
                return response

            # Если ошибка, логируем и пробуем снова
            error_data = response.json() if response.text else {}
            error_msg = error_data.get('description', 'Unknown error')
            logger.warning(f"⚠️ Ошибка отправки (попытка {attempt + 1}): {error_msg}")

            # Если пользователь заблокировал бота — не пытаемся снова
            if response.status_code == 403 and 'bot was blocked' in error_msg:
                logger.warning(f"🚫 Пользователь {chat_id} заблокировал бота")
                return None

            # Если чат не найден — не пытаемся снова
            if response.status_code == 400 and 'chat not found' in error_msg:
                logger.warning(f"🚫 Чат {chat_id} не найден")
                return None

            # Для других ошибок — ждём и пробуем снова
            if attempt < retry:
                time.sleep(0.5 * (attempt + 1))  # Экспоненциальная задержка
                continue

            return response

        except requests.exceptions.Timeout:
            logger.warning(f"⏱️ Таймаут отправки (попытка {attempt + 1})")
            if attempt < retry:
                time.sleep(1)
                continue

        except requests.exceptions.ConnectionError:
            logger.warning(f"🔌 Ошибка соединения (попытка {attempt + 1})")
            if attempt < retry:
                time.sleep(2)
                continue

        except Exception as e:
            logger.error(f"❌ Неизвестная ошибка отправки: {e}")
            if attempt < retry:
                time.sleep(1)
                continue
            return None

    return None

def calculate_energy(user_data):
    now = time.time()
    last = user_data["last_energy_update"]
    seconds_passed = now - last
    max_energy = user_data.get("max_energy", 500)
    recovery_rate = max_energy / 7200
    recovered = int(seconds_passed * recovery_rate)
    new_energy = min(max_energy, user_data["energy"] + recovered)
    return new_energy, seconds_passed

def get_energy_regen_text(max_energy, current_energy):
    if current_energy >= max_energy:
        return "⚡ Энергия полна!"
    recovery_rate = max_energy / 7200
    needed = max_energy - current_energy
    seconds_needed = int(needed / recovery_rate)
    minutes_needed = (seconds_needed + 59) // 60
    if minutes_needed < 60:
        return f"🕐 Осталось {minutes_needed} мин"
    else:
        hours = minutes_needed // 60
        minutes = minutes_needed % 60
        if minutes > 0:
            return f"🕐 Осталось {hours} ч {minutes} мин"
        else:
            return f"🕐 Осталось {hours} ч"

def update_energy_in_db(user_id, user_data, new_energy):
    now = time.time()
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET energy=?, last_energy_update=? WHERE user_id=?", (new_energy, now, user_id))
    user_data["energy"] = new_energy
    user_data["last_energy_update"] = now
    invalidate_cache(user_id)
    return new_energy

def spend_energy(user_id, user_data, amount=1):
    with user_energy_locks[user_id]:
        current_energy, _ = calculate_energy(user_data)
        if current_energy < amount:
            return False, current_energy
        new_energy = current_energy - amount
        now = time.time()
        with db.get_cursor() as cursor:
            cursor.execute(
                "UPDATE users SET energy=?, last_energy_update=? WHERE user_id=?",
                (new_energy, now, user_id)
            )
        user_data["energy"] = new_energy
        user_data["last_energy_update"] = now
        invalidate_cache(user_id)
        return True, new_energy

def add_usdt(user_id, amount):
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET usdt = usdt + ? WHERE user_id=?", (amount, user_id))
    invalidate_cache(user_id)

def add_wins(user_id, amount=1):
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET wins = wins + ? WHERE user_id=?", (amount, user_id))
    invalidate_cache(user_id)

def add_referral_earning(referrer_id, referred_id, spent_lp):
    earning = spent_lp * 0.05
    with db.get_cursor() as cursor:
        cursor.execute("SELECT lp FROM users WHERE user_id=?", (referrer_id,))
        row = cursor.fetchone()
        if row:
            old_lp = row['lp']
            new_lp = old_lp + earning
            safe_update_user(referrer_id, lp=new_lp)
            cursor.execute(
                "UPDATE referrals SET total_spent_lp = total_spent_lp + ? WHERE referrer_id = ? AND referred_id = ?",
                (spent_lp, referrer_id, referred_id))
            referrer = get_user(referrer_id)
            add_log(f"👥 Получил 5% от трат реферала (+{earning:.2f} LP)", referrer_id, referrer['username'],
                    old_value=old_lp, new_value=new_lp, currency="lp")
            update_achievement_progress(referrer_id, 'social', 1)
            return True
    return False

def create_stars_invoice(chat_id, user_id):
    try:
        title = "✨ Энергетический усилитель"
        description = "Увеличивает максимальную энергию на +50 и даёт +50 LP на баланс!"
        payload = json.dumps({"user_id": user_id, "type": "energy_upgrade"})
        provider_token = ""
        currency = "XTR"
        prices = [{"label": "Энергетический усилитель", "amount": 27}]
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createInvoiceLink"
        data = {"title": title, "description": description, "payload": payload, "provider_token": provider_token,
                "currency": currency, "prices": prices}
        verify_ssl = not DEBUG_MODE
        response = requests.post(url, json=data, timeout=10, verify=verify_ssl)
        result = response.json()
        if result.get("ok"):
            return result["result"]
        return None
    except Exception as e:
        logger.error(f"Ошибка в create_stars_invoice: {e}")
        return None

def grant_energy_upgrade(user_id):
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT energy_upgrades, max_energy, lp, username, last_energy_update, energy 
                FROM users WHERE user_id = ?
            """, (user_id,))
            user = cursor.fetchone()
            if not user:
                return False, "Пользователь не найден", None
            current_upgrades = user['energy_upgrades'] or 0
            if current_upgrades >= 15:
                return False, "Максимум улучшений! (15/15)", None
            new_upgrades = current_upgrades + 1
            new_max_energy = 500 + (new_upgrades * 40)
            new_lp = (user['lp'] or 0) + 50
            current_energy = user['energy'] or 0
            last_update = user['last_energy_update'] or time.time()
            now = time.time()
            seconds_passed = now - last_update
            recovery_rate = user['max_energy'] / 7200
            recovered = int(seconds_passed * recovery_rate)
            recalculated_energy = min(user['max_energy'], current_energy + recovered)
            cursor.execute("""
                UPDATE users 
                SET energy_upgrades = ?, 
                    max_energy = ?, 
                    lp = ?, 
                    last_energy_update = ?,
                    energy = ?
                WHERE user_id = ?
            """, (new_upgrades, new_max_energy, new_lp, now, recalculated_energy, user_id))
            invalidate_cache(user_id)
            try:
                add_admin_log(
                    f"⭐ Купил энергетический усилитель | +40 макс. энергии, +50 LP",
                    user_id,
                    user['username'] or f"User_{user_id}",
                    details=f"Улучшений теперь: {new_upgrades}/15, макс. энергия: {new_max_energy}"
                )
            except Exception as log_error:
                logger.error(f"Ошибка при добавлении лога: {log_error}")
            return True, "✨ Улучшение активировано! +40 макс. энергии и +50 LP!", {
                "energy_upgrades": new_upgrades,
                "max_energy": new_max_energy,
                "lp": new_lp,
                "energy": recalculated_energy
            }
    except sqlite3.Error as db_error:
        logger.error(f"Ошибка БД в grant_energy_upgrade для user {user_id}: {db_error}")
        return False, f"Ошибка базы данных: {db_error}", None
    except Exception as e:
        logger.error(f"Неожиданная ошибка в grant_energy_upgrade для user {user_id}: {e}", exc_info=True)
        return False, "Внутренняя ошибка сервера", None

def handle_successful_payment(chat_id, payment_info):
    try:
        payload = json.loads(payment_info.get('invoice_payload', '{}'))
        user_id = payload.get('user_id')
        payment_charge_id = payment_info.get('telegram_payment_charge_id')
        total_amount = payment_info.get('total_amount', 100)
        stars_amount = total_amount // 100
        if not user_id:
            return False
        with db.get_cursor() as cursor:
            cursor.execute("SELECT id FROM successful_payments WHERE telegram_payment_charge_id=?",
                           (payment_charge_id,))
            if cursor.fetchone():
                return True
            cursor.execute(
                "INSERT INTO successful_payments (user_id, telegram_payment_charge_id, payload, amount) VALUES (?, ?, ?, ?)",
                (user_id, payment_charge_id, payload.get('type', 'energy_upgrade'), stars_amount))
        success, message, data = grant_energy_upgrade(user_id)
        if success:
            send_telegram_message(chat_id, f"✅ {message}\n✨ Макс. энергия: {data['max_energy']}\n💎 LP: {data['lp']}")
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            update_stats_history(today, stars=stars_amount)
        else:
            send_telegram_message(chat_id, f"❌ {message}")
        return success
    except Exception as e:
        logger.error(f"Ошибка в handle_successful_payment: {e}")
        return False

def get_user_ton_wallet(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT ton_wallet FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row['ton_wallet'] if row else None

def update_lottery_phase():
    global lottery_phase
    now = datetime.datetime.now()
    current_hour = now.hour
    if 21 <= current_hour or current_hour < 0:
        new_phase = "reveal"
    else:
        new_phase = "buy"
    if lottery_phase != new_phase:
        lottery_phase = new_phase
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE lottery SET lottery_phase = ? WHERE id = 1", (lottery_phase,))
        add_log(f"🔄 Смена фазы лотереи: {lottery_phase}", 0, "System")

def check_lottery_phase():
    update_lottery_phase()
    return lottery_phase

def load_lottery():
    global lottery_pool, lottery_tickets, global_ticket_counter, winning_numbers, is_drawn, draw_time, lottery_phase
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT prize_pool, tickets, global_ticket_counter, winning_numbers, is_drawn, draw_time, lottery_phase FROM lottery LIMIT 1")
        row = cursor.fetchone()
        if row:
            lottery_pool = row['prize_pool']
            lottery_tickets = json.loads(row['tickets']) if row['tickets'] else []
            global_ticket_counter = row['global_ticket_counter']
            winning_numbers = json.loads(row['winning_numbers']) if row['winning_numbers'] else []
            is_drawn = row['is_drawn'] == 1
            lottery_phase = row['lottery_phase'] or 'buy'
            if row['draw_time']:
                try:
                    draw_time = datetime.datetime.fromisoformat(row['draw_time'])
                except:
                    draw_time = None
            else:
                draw_time = None
    update_lottery_phase()

def save_lottery():
    with db.get_cursor() as cursor:
        cursor.execute(
            "UPDATE lottery SET prize_pool=?, tickets=?, global_ticket_counter=?, winning_numbers=?, is_drawn=?, draw_time=?, lottery_phase=?",
            (lottery_pool, json.dumps(lottery_tickets), global_ticket_counter, json.dumps(winning_numbers),
             1 if is_drawn else 0, draw_time.isoformat() if draw_time else None, lottery_phase))

load_lottery()

UPGRADE_CONFIG = {
    1: {"base_cost": 1.5, "bonus": 0.01, "name": "Новичок"},
    2: {"base_cost": 10, "bonus": 0.03, "name": "Опытный"},
    3: {"base_cost": 40, "bonus": 0.05, "name": "Профессионал"},
    4: {"base_cost": 70, "bonus": 0.07, "name": "Мастер"},
    5: {"base_cost": 150, "bonus": 0.10, "name": "Легенда"},
}

def get_upgrade_cost(upgrade_id, current_count, free_count=0):
    """Возвращает стоимость улучшения с учётом платных и бесплатных"""
    config = UPGRADE_CONFIG[upgrade_id]
    base_cost = config["base_cost"]
    # Цена считается ТОЛЬКО от платных улучшений!
    paid_count = max(0, current_count - free_count)
    if paid_count == 0:
        return base_cost
    return base_cost * (1.65 ** paid_count)


def get_total_earning(upgrade_counts, user_id=None):
    """Возвращает доход с учётом улучшений"""
    base = 0.01
    total_bonus = 0

    current_payday_multiplier = get_payday_multiplier()

    # Получаем бесплатные улучшения
    free_upgrade_counts = {}
    if user_id:
        try:
            with db.get_cursor() as cursor:
                cursor.execute("SELECT free_upgrade_counts FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                if row and row['free_upgrade_counts']:
                    free_upgrade_counts = json.loads(row['free_upgrade_counts'])
        except:
            pass

    # Объединяем все улучшения для дохода
    all_upgrades = {}
    for key, value in upgrade_counts.items():
        all_upgrades[key] = value
    for key, value in free_upgrade_counts.items():
        all_upgrades[key] = all_upgrades.get(key, 0) + value

    for key, value in all_upgrades.items():
        try:
            if isinstance(key, str):
                if not key.isdigit():
                    continue
                key = int(key)
            else:
                key = int(key)

            if key in UPGRADE_CONFIG:
                bonus = UPGRADE_CONFIG[key]["bonus"]

                bonus_key = f"payday_bonus_{key}"
                if bonus_key in upgrade_counts:
                    multiplier = upgrade_counts[bonus_key]
                else:
                    multiplier = current_payday_multiplier if current_payday_multiplier > 1 else 1

                total_bonus += bonus * value * multiplier
        except (ValueError, TypeError):
            continue

    return base + total_bonus

def generate_ticket_numbers():
    return sorted(random.sample(range(1, 81), 12))

def generate_winning_numbers():
    return sorted(random.sample(range(1, 81), 12))

def unlock_prefix(user_id, prefix_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT unlocked_prefixes FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        unlocked = json.loads(row['unlocked_prefixes']) if row and row['unlocked_prefixes'] else ["player"]
        if prefix_id not in unlocked:
            unlocked.append(prefix_id)
            cursor.execute("UPDATE users SET unlocked_prefixes = ? WHERE user_id=?", (json.dumps(unlocked), user_id))
            invalidate_cache(user_id)
            return True
    return False

def update_online_count():
    now = time.time()
    with online_users_lock:
        to_remove = [uid for uid, last_seen in online_users.items() if now - last_seen > 300]
        for uid in to_remove:
            del online_users[uid]

def buy_ticket(user_id, user_data):
    global lottery_pool, lottery_tickets, global_ticket_counter
    with lottery_lock:
        if is_drawn:
            return False, "Сейчас идёт стирание билетов! Новые билеты появятся в 00:00"
        if user_data["lp"] < 100:
            return False, "Не хватает LP (нужно 100)"
        bought = len([t for t in lottery_tickets if t.get("user_id") == user_id])
        if bought >= 10:
            return False, "Уже куплено 10 билетов"
        old_lp = user_data["lp"]
        user_data["lp"] -= 100
        safe_update_user(user_id, lp=user_data["lp"])
        if user_data.get("referrer_id", 0) > 0:
            add_referral_earning(user_data["referrer_id"], user_id, 100)
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
        safe_update_user(user_id, ticket_counter=user_ticket_counter)
        ticket_data = {"number": ticket_num, "purchase_number": user_ticket_counter, "numbers": ticket_numbers,
                       "revealed": [False] * 12, "reward_claimed": False, "user_id": user_id}
        lottery_tickets.append(ticket_data)
        lottery_pool = round(lottery_pool + 0.40, 2)
        save_lottery()
        add_log(f"🎫 Купил билет #{ticket_num}", user_id, user_data['username'], old_value=old_lp,
                new_value=user_data['lp'], currency="lp")
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        update_stats_history(today, tickets=1)
        update_achievement_progress(user_id, 'gambler', 1)
        return True, f"Билет #{ticket_num} куплен!"

def reveal_all_tickets(user_id):
    with lottery_lock:
        if not is_drawn:
            return False, "Розыгрыш ещё не начался!"
        revealed_count = 0
        for ticket in lottery_tickets:
            if ticket.get("user_id") == user_id:
                for i in range(12):
                    if not ticket["revealed"][i]:
                        ticket["revealed"][i] = True
                        revealed_count += 1
        if revealed_count > 0:
            save_lottery()
            add_log(f"🔓 Открыл все клетки ({revealed_count} клеток)", user_id, str(user_id))
            return True, f"Открыто {revealed_count} клеток!"
        return False, "Нет неоткрытых клеток"

def perform_draw():
    global winning_numbers, is_drawn, draw_time, lottery_phase
    with lottery_lock:
        if lottery_tickets:
            winning_numbers = generate_winning_numbers()
            is_drawn = True
            draw_time = datetime.datetime.now()
            lottery_phase = "reveal"
            save_lottery()
            end_time = draw_time + datetime.timedelta(seconds=10800)
            try:
                socketio.emit('draw_completed', {
                    'winning_numbers': winning_numbers,
                    'message': '🎉 Розыгрыш начался! У вас 3 часа на открытие билетов! ⏰',
                    'end_time': end_time.isoformat()
                })
            except:
                pass
            add_log(f"🎲 Розыгрыш лотереи начался. Выигрышные номера: {winning_numbers}", 0, "System")
            threading.Timer(10800, auto_reveal_and_distribute).start()

def auto_reveal_and_distribute():
    time.sleep(10800)
    with lottery_lock:
        if is_drawn:
            for ticket in lottery_tickets:
                if not all(ticket.get("revealed", [])):
                    ticket["revealed"] = [True] * 12
            save_lottery()
            add_log(f"⏰ Автоматическое открытие билетов (время вышло в 00:00)", 0, "System")
            try:
                socketio.emit('auto_revealed', {'message': '⏰ Время истекло! Билеты открыты автоматически!'})
            except:
                pass
            distribute_prizes()
            time.sleep(3600)
            reset_lottery()
            schedule_next_draw()

def distribute_prizes():
    global lottery_pool, lottery_tickets
    if not lottery_tickets:
        return
    results = []
    for ticket in lottery_tickets:
        if all(ticket.get("revealed", [])):
            matches = sum(1 for i in range(12) if ticket["numbers"][i] in winning_numbers)
            results.append({"user_id": ticket["user_id"], "matches": matches, "ticket": ticket})
    if not results:
        return
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
            update_achievement_progress(winner["user_id"], 'lucky', 1)
    save_lottery()
    add_log(f"🎰 Завершение розыгрыша. Призовой фонд {lottery_pool} USDT распределён между {len(winners)} победителями",
            0, "System")
    try:
        socketio.emit('prizes_distributed',
                      {'message': f'🏆 Призы распределены! Победители получили по {prize_per_winner} USDT!'})
    except:
        pass

def reset_lottery():
    global is_drawn, winning_numbers, draw_time, lottery_tickets, lottery_pool, global_ticket_counter, lottery_phase
    with lottery_lock:
        is_drawn = False
        winning_numbers = []
        draw_time = None
        lottery_tickets = []
        lottery_pool = 0
        global_ticket_counter = 0
        lottery_phase = "buy"
        save_lottery()
        add_log(f"🔄 Сброс лотереи для нового розыгрыша (новый день в 01:00)", 0, "System")
        try:
            socketio.emit('draw_reset', {'message': '🔄 Новая лотерея началась! Покупайте билеты до 21:00!'})
        except:
            pass

def schedule_next_draw():
    def wait_and_draw():
        while True:
            now = datetime.datetime.now()
            next_draw = now.replace(hour=21, minute=0, second=0, microsecond=0)
            if now >= next_draw:
                next_draw += datetime.timedelta(days=1)
            wait_seconds = (next_draw - now).total_seconds()
            time.sleep(wait_seconds)
            perform_draw()
            time.sleep(14400)
            reset_lottery()
    threading.Thread(target=wait_and_draw, daemon=True).start()

threading.Thread(target=schedule_next_draw, daemon=True).start()

DAILY_REWARDS = {1: {"wg": 15, "lp": 0, "energy_limit": 0, "description": "15 WG"},
                 2: {"wg": 50, "lp": 0, "energy_limit": 0, "description": "50 WG"},
                 3: {"wg": 0, "lp": 0, "energy_limit": 10, "description": "+10 к лимиту энергии"},
                 4: {"wg": 0, "lp": 10, "energy_limit": 0, "description": "10 LP"},
                 5: {"wg": 0, "lp": 0, "energy_limit": 15, "description": "+15 к лимиту энергии"},
                 6: {"wg": 150, "lp": 0, "energy_limit": 0, "description": "150 WG"},
                 7: {"wg": 0, "lp": 20, "energy_limit": 0, "description": "20 LP"}}

def get_daily_status(user_id):
    check_and_reset_streak(user_id)
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM daily_rewards WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row:
            now = datetime.datetime.now().isoformat()
            cursor.execute(
                'INSERT INTO daily_rewards (user_id, current_day, last_claim_date, streak_start_date, recovered_count) VALUES (?, 1, ?, ?, 0)',
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
            if time_diff < 172800:
                can_claim = True
        if time_diff > 86400 and time_diff < 172800 and current_day > 1:
            lost_streak = True
        if not can_claim and time_diff < 86400:
            next_claim_time = last_claim + datetime.timedelta(seconds=86400)
        recovered_count = row['recovered_count'] or 0
        return {"current_day": current_day, "can_claim": can_claim,
                "next_claim_time": next_claim_time.isoformat() if next_claim_time else None,
                "recovered_count": recovered_count, "lost_streak": lost_streak}

def give_daily_reward(user_id, day):
    reward = DAILY_REWARDS.get(day)
    if not reward:
        return False
    user = get_user(user_id)
    if reward["wg"] > 0:
        safe_update_user(user_id, wg=user["wg"] + reward["wg"])
        add_log(f"🎁 Ежедневная награда: +{reward['wg']} WG", user_id, user['username'])
    if reward["lp"] > 0:
        safe_update_user(user_id, lp=user["lp"] + reward["lp"])
        add_log(f"🎁 Ежедневная награда: +{reward['lp']} LP", user_id, user['username'])
    if reward["energy_limit"] > 0:
        new_max_energy = user["max_energy"] + reward["energy_limit"]
        safe_update_user(user_id, max_energy=new_max_energy)
        add_log(f"🎁 Ежедневная награда: +{reward['energy_limit']} к макс. энергии", user_id, user['username'])
    return True

def claim_daily_reward(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT current_day, last_claim_date, recovered_count FROM daily_rewards WHERE user_id=?",
                       (user_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "msg": "Ошибка"}
        last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
        now = datetime.datetime.now()
        time_diff = (now - last_claim).total_seconds()
        current_day = row['current_day']
        if current_day != 1 and time_diff < 86400:
            remaining = int(86400 - time_diff)
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            return {"success": False, "msg": f"Следующая награда через {hours} ч {minutes} мин"}
        recovered_count = row['recovered_count'] or 0
        give_daily_reward(user_id, current_day)
        new_day = current_day + 1
        cursor.execute(
            'UPDATE daily_rewards SET current_day = ?, last_claim_date = ?, recovered_count = ? WHERE user_id = ?',
            (new_day, now.isoformat(), recovered_count, user_id))
        return {"success": True, "msg": f"Награда за {current_day} день получена!", "new_day": new_day}

def recover_streak_with_stars(user_id):
    user = get_user(user_id)
    if user['stars'] < 20:
        return {"success": False, "msg": "Недостаточно Stars (нужно 20)"}
    with db.get_cursor() as cursor:
        cursor.execute("SELECT current_day, last_claim_date FROM daily_rewards WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "msg": "Ошибка"}
        now = datetime.datetime.now()
        last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
        time_diff = (now - last_claim).total_seconds()
        current_day = row['current_day']
        if time_diff < 86400 or time_diff >= 172800:
            return {"success": False, "msg": "Сейчас нельзя восстановить серию"}
        safe_update_user(user_id, stars=user['stars'] - 20)
        cursor.execute(
            'UPDATE daily_rewards SET last_claim_date = ?, recovered_count = recovered_count + 1 WHERE user_id = ?',
            (now.isoformat(), user_id))
        add_log(f"⭐ Восстановил серию ежедневных наград за 20 Stars (день {current_day})", user_id, user['username'])
        return {"success": True, "msg": f"Серия восстановлена! Вы можете забрать награду за {current_day} день!",
                "current_day": current_day}

def check_and_reset_streak(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("SELECT current_day, last_claim_date FROM daily_rewards WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return
        last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
        now = datetime.datetime.now()
        time_diff = (now - last_claim).total_seconds()
        current_day = row['current_day']
        if time_diff > 172800 and current_day > 1:
            cursor.execute(
                'UPDATE daily_rewards SET current_day = 1, streak_start_date = ?, recovered_count = 0 WHERE user_id = ?',
                (now.isoformat(), user_id))
            add_log(f"🔄 Серия ежедневных наград сброшена (пропущено более 48ч)", user_id, str(user_id))
            return True
    return False

@app.route('/api/reveal_all_tickets_fast', methods=['POST'])
def api_reveal_all_tickets_fast():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    with lottery_lock:
        if not is_drawn:
            return jsonify({"success": False, "msg": "Розыгрыш ещё не начался!"})
        revealed_count = 0
        tickets_revealed = 0
        for ticket in lottery_tickets:
            if ticket.get("user_id") == user_id:
                ticket_revealed = False
                for i in range(12):
                    if not ticket["revealed"][i]:
                        ticket["revealed"][i] = True
                        revealed_count += 1
                        ticket_revealed = True
                if ticket_revealed:
                    tickets_revealed += 1
        if revealed_count > 0:
            save_lottery()
            user = get_user(user_id)
            add_log(f"🔓 Открыл все клетки ({revealed_count} клеток, {tickets_revealed} билетов)", user_id,
                    user['username'])
            return jsonify({"success": True, "msg": f"Открыто {revealed_count} клеток в {tickets_revealed} билетах!",
                            "revealed": revealed_count})
        return jsonify({"success": False, "msg": "Нет неоткрытых клеток"})

@socketio.on('reveal_cell')
def handle_reveal_cell(data):
    user_id = data.get('user_id')
    ticket_number = data.get('ticket_number')
    cell_index = data.get('cell_index')
    if not is_drawn:
        emit('reveal_error', {'message': 'Розыгрыш ещё не начался!'})
        return
    with lottery_lock:
        for ticket in lottery_tickets:
            if ticket.get("user_id") == user_id and ticket.get("number") == ticket_number:
                if not ticket["revealed"][cell_index]:
                    ticket["revealed"][cell_index] = True
                    save_lottery()
                    number = ticket["numbers"][cell_index]
                    is_win = number in winning_numbers
                    user = get_user(user_id)
                    win_text = "ВЫИГРЫШНАЯ" if is_win else "обычная"
                    add_log(
                        f"🔓 Открыл клетку {cell_index + 1} билета #{ticket_number} ({win_text} клетка, число {number})",
                        user_id, user['username'])
                    emit('cell_revealed',
                         {'ticket_number': ticket_number, 'cell_index': cell_index, 'number': number, 'is_win': is_win})
                    if all(ticket["revealed"]):
                        matches = sum(1 for i in range(12) if ticket["numbers"][i] in winning_numbers)
                        add_log(f"🎫 Полностью открыл билет #{ticket_number} (совпадений: {matches}/12)", user_id,
                                user['username'])
                        emit('ticket_completed', {'ticket_number': ticket_number, 'matches': matches})
                return

@socketio.on('reveal_all_tickets')
def handle_reveal_all_tickets(data):
    user_id = data.get('user_id')
    success, msg = reveal_all_tickets(user_id)
    if success:
        emit('reveal_all_completed', {'message': msg})
    else:
        emit('reveal_error', {'message': msg})

@socketio.on('get_draw_status')
def handle_get_draw_status(data):
    emit('draw_status',
         {'is_drawn': is_drawn, 'winning_numbers': winning_numbers if is_drawn else [], 'lottery_phase': lottery_phase})

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
            end_time = draw_time + datetime.timedelta(seconds=10800)
            now = datetime.datetime.now()
            remaining = int((end_time - now).total_seconds())
            if remaining < 0:
                remaining = 0
            emit('remaining_time', {'seconds': remaining})
        else:
            emit('remaining_time', {'seconds': 0})
    else:
        emit('remaining_time', {'seconds': 0})

@app.route('/')
def game_page():
    return render_template('game.html')

@app.route('/admin')
def admin_panel_page():
    client_ip = request.remote_addr
    if not DEBUG_MODE and not check_admin_bruteforce(client_ip):
        return "Too many failed attempts", 429
    key = request.args.get('key')
    if not key or not secrets.compare_digest(key, ADMIN_SECRET):
        if not DEBUG_MODE:
            record_admin_failure(client_ip)
        return "Доступ запрещён", 403
    admin_failures.pop(client_ip, None)
    return render_template('admin.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

@app.route('/claim')
def claim_promo_page():
    code = request.args.get('code', '').upper().strip()
    if not code:
        return render_template('claim.html', error="Не указан код промокода")
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM promo_codes WHERE code = ? AND is_active = 1', (code,))
        promo = cursor.fetchone()
        if not promo:
            return render_template('claim.html', error="Промокод не найден или неактивен")
        if promo['expires_at']:
            expires = datetime.datetime.fromisoformat(promo['expires_at'])
            if datetime.datetime.now() > expires:
                return render_template('claim.html', error="Срок действия промокода истёк")
        used_count = promo['used_count'] or 0
        remaining = promo['max_uses'] - used_count
        if remaining <= 0:
            return render_template('claim.html', error="Промокод больше не активен (все активации использованы)")
        reward_names = {'wg': 'WG Coin', 'lp': 'LP Coin', 'energy_limit': 'Лимит энергии'}
        promo_info = {
            'code': promo['code'],
            'reward_type': promo['reward_type'],
            'reward_name': reward_names.get(promo['reward_type'], promo['reward_type']),
            'reward_amount': promo['reward_amount'],
            'remaining': remaining,
            'has_password': bool(promo['password'])
        }
        return render_template('claim.html', promo=promo_info)

@app.route('/api/activate_promo_via_web', methods=['POST'])
def api_activate_promo_via_web():
    data = request.json
    code = data.get('code', '').upper().strip()
    user_id = data.get('user_id')
    password = data.get('password', '').strip()
    if not user_id:
        return jsonify({"success": False, "error": "Не авторизован"}), 401
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Неверный ID пользователя"}), 400
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM promo_codes WHERE code = ? AND is_active = 1', (code,))
        promo = cursor.fetchone()
        if not promo:
            return jsonify({"success": False, "error": "Промокод не найден"}), 404
        if promo['expires_at']:
            expires = datetime.datetime.fromisoformat(promo['expires_at'])
            if datetime.datetime.now() > expires:
                return jsonify({"success": False, "error": "Срок действия промокода истёк"}), 400
        used_count = promo['used_count'] or 0
        if used_count >= promo['max_uses']:
            return jsonify({"success": False, "error": "Промокод больше не активен"}), 400
        if promo['password'] and promo['password'] != password:
            return jsonify({"success": False, "error": "Неверный пароль промокода"}), 400
        cursor.execute("SELECT id FROM promo_activations WHERE promo_id = ? AND user_id = ?", (promo['id'], user_id))
        if cursor.fetchone():
            return jsonify({"success": False, "error": "Вы уже активировали этот промокод"}), 400
        user = get_user(user_id)
        old_value = None
        new_value = None
        if promo['reward_type'] == 'wg':
            old_value = user['wg']
            new_value = old_value + promo['reward_amount']
            safe_update_user(user_id, wg=new_value)
            add_log(f"🎁 Активировал промокод {code} | +{promo['reward_amount']} WG", user_id, user['username'],
                    old_value=old_value, new_value=new_value, currency="wg")
        elif promo['reward_type'] == 'lp':
            old_value = user['lp']
            new_value = old_value + promo['reward_amount']
            safe_update_user(user_id, lp=new_value)
            add_log(f"🎁 Активировал промокод {code} | +{promo['reward_amount']} LP", user_id, user['username'],
                    old_value=old_value, new_value=new_value, currency="lp")
        elif promo['reward_type'] == 'energy_limit':
            old_value = user['max_energy']
            new_value = old_value + promo['reward_amount']
            safe_update_user(user_id, max_energy=new_value)
            add_log(f"🎁 Активировал промокод {code} | +{promo['reward_amount']} к макс. энергии", user_id,
                    user['username'], old_value=old_value, new_value=new_value, currency="energy")
        new_used_count = used_count + 1
        cursor.execute("UPDATE promo_codes SET used_count = ? WHERE id = ?", (new_used_count, promo['id']))
        cursor.execute("INSERT INTO promo_activations (promo_id, user_id) VALUES (?, ?)", (promo['id'], user_id))
        add_admin_log(f"🎫 Активировал промокод {code} и получил {promo['reward_amount']} {promo['reward_type']}",
                      user_id, user['username'])
        return jsonify(
            {"success": True, "message": f"Вы получили +{promo['reward_amount']} {promo['reward_type'].upper()}!",
             "reward_type": promo['reward_type'], "reward_amount": promo['reward_amount']})

@app.route('/health')
def health_check():
    with online_users_lock:
        online_count = len(online_users)
    return jsonify({"status": "ok", "online_users": online_count, "threads": threading.active_count(),
                    "db_size": os.path.getsize(DATABASE_PATH) if os.path.exists(DATABASE_PATH) else 0,
                    "timestamp": time.time()})

@app.route('/tonconnect-manifest.json', methods=['GET'])
def serve_manifest():
    current_origin = f"{request.scheme}://{request.host}"
    manifest = {
        "url": current_origin,
        "name": "WereGood Game",
        "iconUrl": f"{current_origin}/static/coin.png",
        "termsOfUseUrl": f"{current_origin}/terms",
        "privacyPolicyUrl": f"{current_origin}/privacy"
    }
    return jsonify(manifest)

@app.route('/terms')
def terms_page():
    return '<!DOCTYPE html><html><head><title>Условия использования</title></head><body style="background:#0a0a1a; color:white; padding:20px; font-family:system-ui;"><h1>Условия использования WereGood</h1><p>Используя наш сервис, вы соглашаетесь с правилами игры.</p><p>Все внутриигровые транзакции финальны.</p><p>Администрация оставляет за собой право блокировать пользователей за нарушение правил.</p></body></html>'

@app.route('/privacy')
def privacy_page():
    return '<!DOCTYPE html><html><head><title>Политика конфиденциальности</title></head><body style="background:#0a0a1a; color:white; padding:20px; font-family:system-ui;"><h1>Политика конфиденциальности WereGood</h1><p>Мы собираем только ваш Telegram ID и данные профиля для работы игры.</p><p>Данные не передаются третьим лицам.</p><p>Вы можете удалить свои данные, обратившись к администратору.</p></body></html>'

# ========== TON API ==========
@app.route('/api/ton/save_wallet', methods=['POST'])
def api_ton_save_wallet():
    data = request.json or {}
    user_id = data.get('user_id')
    wallet_address = data.get('wallet_address')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid or not wallet_address:
        return jsonify({"success": False, "error": "Неверный ID пользователя или адрес кошелька"}), 400
    if not validate_ton_address(wallet_address):
        return jsonify({"success": False, "error": "Неверный формат TON кошелька"}), 400
    try:
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET ton_wallet = ? WHERE user_id = ?", (wallet_address, user_id))
        if 'invalidate_cache' in globals():
            invalidate_cache(user_id)
        user = get_user(user_id)
        add_log(f"🔗 Привязал TON кошелёк: {wallet_address[:6]}...{wallet_address[-4:]}", user_id,
                user.get('username', 'Unknown'))
        return jsonify({"success": True, "wallet": wallet_address})
    except Exception as e:
        logger.error(f"❌ Ошибка при сохранении кошелька в БД: {e}")
        return jsonify({"success": False, "error": "Ошибка базы данных"}), 500

@app.route('/api/ton/get_wallet', methods=['POST'])
def api_ton_get_wallet():
    data = request.json or {}
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    with db.get_cursor() as cursor:
        cursor.execute("SELECT ton_wallet FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        wallet = row['ton_wallet'] if row else None
    return jsonify({"success": True, "wallet": wallet})

@app.route('/api/ton/create_payment', methods=['POST'])
def api_ton_create_payment():
    data = request.json or {}
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Неавторизованный запрос"}), 400

    proj_wallet = globals().get('PROJECT_WALLET_ADDRESS') or os.getenv('PROJECT_WALLET_ADDRESS')
    if not proj_wallet:
        logger.critical("🚨 PROJECT_WALLET_ADDRESS отсутствует!")
        return jsonify({"success": False, "error": "Ошибка конфигурации платежного шлюза"}), 500

    # ✅ Добавляем проверку на дубликат
    # Если пользователь уже оплатил за последние 10 секунд — блокируем
    cache_key = f"ton_payment_{user_id}"
    if cache_key in pending_invoices:
        return jsonify({"success": False, "error": "Подождите, предыдущий платёж обрабатывается"}), 429

    pending_invoices[cache_key] = time.time()

    payment_amount_ton = 0.20
    payment_amount_nano = int(payment_amount_ton * 1e9)

    return jsonify({
        "success": True,
        "wallet_address": proj_wallet,
        "amount": payment_amount_ton,
        "amount_nano": payment_amount_nano,
        "comment": f"WereGood:{user_id}"
    })

@app.route('/api/ton/check_payment', methods=['POST'])
def check_ton_payment_endpoint():
    try:
        data = request.json or {}
        user_id = data.get('user_id')
        expected_amount = data.get('expected_amount')
        sender_wallet = data.get('sender_wallet')
        if not user_id or not expected_amount:
            return jsonify({'confirmed': False, 'error': 'Missing parameters'}), 400
        try:
            expected_amount = float(expected_amount)
        except ValueError:
            return jsonify({'confirmed': False, 'error': 'Invalid amount'}), 400
        logger.info(f"📡 [API] Запрос проверки TON от {user_id}. Сумма: {expected_amount}")
        confirmed, amount_paid, tx_hash = check_ton_transaction(sender_wallet, expected_amount, user_id)
        if confirmed:
            logger.info(f"💰 [API] Платёж TON подтверждён для {user_id}. Вызываем grant_energy_upgrade...")
            success, message, upgrade_data = grant_energy_upgrade(user_id)
            if success:
                logger.info(f"🎁 [API] Успех! Игроку {user_id} начислено улучшение через TON!")
                user = get_user(user_id)
                add_admin_log(
                    f"💎 Купил энергетический усилитель за TON ({expected_amount} TON) | +50 макс. энергии, +50 LP",
                    user_id,
                    user.get('username') or f"User_{user_id}",
                    details=f"Хэш транзакции: {tx_hash}, кошелёк: {sender_wallet}"
                )
                if 'send_telegram_message' in globals():
                    try:
                        send_telegram_message(user_id,
                                              f"✨ **Оплата через TON получена!**\n\n"
                                              f"⚡️ +50 к максимальной энергии\n"
                                              f"💎 +50 LP на баланс\n\n"
                                              f"💪 Энергетический усилитель успешно активирован!")
                    except Exception as tg_err:
                        logger.error(f"⚠️ Не удалось отправить ТГ-сообщение: {tg_err}")
                return jsonify({'confirmed': True, 'tx_hash': tx_hash})
            else:
                add_admin_log(
                    f"❌ НЕУДАЧНАЯ покупка за TON ({expected_amount} TON) - ошибка начисления",
                    user_id,
                    f"User_{user_id}",
                    details=f"Ошибка: {message}"
                )
                logger.error(f"❌ [API] Ошибка внутри grant_energy_upgrade: {message}")
                return jsonify({'confirmed': False, 'error': message}), 400
        return jsonify({'confirmed': False})
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в check_ton_payment_endpoint: {e}", exc_info=True)
        return jsonify({'confirmed': False, 'error': str(e)}), 500

@app.route('/api/ton/disconnect_wallet', methods=['POST'])
def api_ton_disconnect_wallet():
    data = request.json or {}
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    try:
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET ton_wallet = '' WHERE user_id = ?", (user_id,))
        if 'invalidate_cache' in globals():
            invalidate_cache(user_id)
        user = get_user(user_id)
        add_log(f"🔗 Отвязал TON кошелёк", user_id, user.get('username', 'Unknown'))
        return jsonify({"success": True, "message": "Кошелёк отвязан"})
    except Exception as e:
        logger.error(f"❌ Ошибка при отвязке кошелька в БД: {e}")
        return jsonify({"success": False, "error": "Ошибка базы данных"}), 500

# ========== LP БУСТЕР API ==========
@app.route('/api/create_lp_boost_invoice', methods=['POST'])
def api_create_lp_boost_invoice():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400

    user_id = data.get('user_id')
    chat_id = data.get('chat_id', user_id)
    quantity = data.get('quantity', 1)  # ← ДОЛЖЕН БЫТЬ!

    # Ограничиваем количество
    if quantity < 1 or quantity > 10:
        return jsonify({"success": False, "msg": "Количество должно быть от 1 до 10"}), 400

    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400

    try:
        total_stars = 22 * quantity
        total_lp = 50 * quantity

        title = f"💎 LP Бустер x{quantity}"
        description = f"Пополняет баланс на {total_lp} LP!"
        payload = json.dumps({"user_id": user_id, "type": "lp_boost", "quantity": quantity})
        provider_token = ""
        currency = "XTR"
        prices = [{"label": f"LP Бустер x{quantity}", "amount": total_stars}]

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createInvoiceLink"
        data = {
            "title": title,
            "description": description,
            "payload": payload,
            "provider_token": provider_token,
            "currency": currency,
            "prices": prices
        }

        verify_ssl = not DEBUG_MODE
        response = requests.post(url, json=data, timeout=10, verify=verify_ssl)
        result = response.json()

        if result.get("ok"):
            return jsonify({"success": True, "invoice_link": result["result"]})

        return jsonify({"success": False, "msg": "Ошибка создания счёта"})

    except Exception as e:
        logger.error(f"Ошибка в create_lp_boost_invoice: {e}")
        return jsonify({"success": False, "msg": str(e)}), 500

@app.route('/api/ton/create_lp_boost_payment', methods=['POST'])
def api_ton_create_lp_boost_payment():
    data = request.json or {}
    user_id = data.get('user_id')
    quantity = data.get('quantity', 1)  # ← ДОЛЖЕН БЫТЬ!

    if quantity < 1 or quantity > 10:
        return jsonify({"success": False, "error": "Количество должно быть от 1 до 10"}), 400

    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Неавторизованный запрос"}), 400

    proj_wallet = globals().get('PROJECT_WALLET_ADDRESS') or os.getenv('PROJECT_WALLET_ADDRESS')
    if not proj_wallet:
        logger.critical("🚨 PROJECT_WALLET_ADDRESS отсутствует!")
        return jsonify({"success": False, "error": "Ошибка конфигурации платежного шлюза"}), 500

    payment_amount_ton = 0.18 * quantity  # ← УМНОЖАЕМ!
    payment_amount_nano = int(payment_amount_ton * 1e9)

    return jsonify({
        "success": True,
        "wallet_address": proj_wallet,
        "amount": payment_amount_ton,
        "amount_nano": payment_amount_nano,
        "comment": f"WereGood_LP:{user_id}:{quantity}"
    })

@app.route('/api/ton/check_lp_boost_payment', methods=['POST'])
def check_lp_boost_payment():
    try:
        data = request.json or {}
        user_id = data.get('user_id')
        expected_amount = data.get('expected_amount')
        sender_wallet = data.get('sender_wallet')
        quantity = data.get('quantity', 1)  # ← ДОЛЖЕН БЫТЬ!

        if not user_id or not expected_amount:
            return jsonify({'confirmed': False, 'error': 'Missing parameters'}), 400

        expected_amount = float(expected_amount)
        confirmed, amount_paid, tx_hash = check_ton_transaction(sender_wallet, expected_amount, user_id)

        if confirmed:
            user = get_user(user_id)
            total_lp = 50 * quantity  # ← УМНОЖАЕМ!
            old_lp = user['lp']
            new_lp = old_lp + total_lp

            safe_update_user(user_id, lp=new_lp)

            add_admin_log(
                f"💎 Купил LP Бустер x{quantity} за TON ({expected_amount} TON) | +{total_lp} LP",
                user_id,
                user.get('username') or f"User_{user_id}",
                details=f"Хэш транзакции: {tx_hash}, LP: {old_lp} → {new_lp}"
            )

            if 'send_telegram_message' in globals():
                try:
                    send_telegram_message(user_id,
                        f"✨ **LP Бустер x{quantity} активирован!**\n\n"
                        f"💎 +{total_lp} LP на баланс!\n\n"
                        f"Спасибо за поддержку проекта!")
                except Exception as tg_err:
                    logger.error(f"⚠️ Не удалось отправить ТГ-сообщение: {tg_err}")

            return jsonify({'confirmed': True, 'tx_hash': tx_hash, 'lp_added': total_lp})

        return jsonify({'confirmed': False})

    except Exception as e:
        logger.error(f"Ошибка в check_lp_boost_payment: {e}", exc_info=True)
        return jsonify({'confirmed': False, 'error': str(e)}), 500

@app.route('/api/get_lp_boost_count', methods=['POST'])
def api_get_lp_boost_count():
    data = request.json or {}
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) as count FROM successful_payments WHERE user_id = ? AND payload = 'lp_boost'",
            (user_id,)
        )
        row = cursor.fetchone()
        count = row['count'] if row else 0
    return jsonify({"success": True, "count": count})

# ========== МЕГА-БУСТЕР (АКЦИЯ) ==========

@app.route('/api/create_mega_boost_invoice', methods=['POST'])
def api_create_mega_boost_invoice():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    chat_id = data.get('chat_id', user_id)
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400

    try:
        title = "🔥 Мега-бустер (Акция)"
        description = "100 LP + 2 Легенды + 2 Мастера + 1500 WG!"
        payload = json.dumps({"user_id": user_id, "type": "mega_boost"})
        provider_token = ""
        currency = "XTR"
        prices = [{"label": "Мега-бустер", "amount": 50}]
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createInvoiceLink"
        data = {
            "title": title,
            "description": description,
            "payload": payload,
            "provider_token": provider_token,
            "currency": currency,
            "prices": prices
        }
        verify_ssl = not DEBUG_MODE
        response = requests.post(url, json=data, timeout=10, verify=verify_ssl)
        result = response.json()
        if result.get("ok"):
            return jsonify({"success": True, "invoice_link": result["result"]})
        return jsonify({"success": False, "msg": "Ошибка создания счёта"})
    except Exception as e:
        logger.error(f"Ошибка в create_mega_boost_invoice: {e}")
        return jsonify({"success": False, "msg": str(e)}), 500


@app.route('/api/ton/create_mega_boost_payment', methods=['POST'])
def api_ton_create_mega_boost_payment():
    data = request.json or {}
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Неавторизованный запрос"}), 400

    proj_wallet = globals().get('PROJECT_WALLET_ADDRESS') or os.getenv('PROJECT_WALLET_ADDRESS')
    if not proj_wallet:
        logger.critical("🚨 PROJECT_WALLET_ADDRESS отсутствует!")
        return jsonify({"success": False, "error": "Ошибка конфигурации платежного шлюза"}), 500

    payment_amount_ton = 0.40
    payment_amount_nano = int(payment_amount_ton * 1e9)
    return jsonify({
        "success": True,
        "wallet_address": proj_wallet,
        "amount": payment_amount_ton,
        "amount_nano": payment_amount_nano,
        "comment": f"WereGood_MEGA:{user_id}"
    })


@app.route('/api/ton/check_mega_boost_payment', methods=['POST'])
def check_mega_boost_payment():
    try:
        data = request.json or {}
        user_id = data.get('user_id')
        expected_amount = data.get('expected_amount')
        sender_wallet = data.get('sender_wallet')

        if not user_id or not expected_amount:
            return jsonify({'confirmed': False, 'error': 'Missing parameters'}), 400

        expected_amount = float(expected_amount)
        confirmed, amount_paid, tx_hash = check_ton_transaction(sender_wallet, expected_amount, user_id)

        if confirmed:
            # Выдаём награду
            user = get_user(user_id)

            # +100 LP
            old_lp = user['lp']
            new_lp = old_lp + 100
            safe_update_user(user_id, lp=new_lp)

            # +1500 WG
            old_wg = user['wg']
            new_wg = old_wg + 1500
            safe_update_user(user_id, wg=new_wg)

            # +2 Легенды (id: 5)
            upgrade_counts = user['upgrade_counts']
            upgrade_counts[5] = upgrade_counts.get(5, 0) + 2
            safe_update_user(user_id, upgrade_counts=upgrade_counts)

            # +2 Мастера (id: 4)
            upgrade_counts = user['upgrade_counts']
            upgrade_counts[4] = upgrade_counts.get(4, 0) + 2
            safe_update_user(user_id, upgrade_counts=upgrade_counts)

            add_admin_log(
                f"🔥 Активировал Мега-бустер (TON) | +100 LP, +1500 WG, +2 Легенды, +2 Мастера",
                user_id,
                user.get('username') or f"User_{user_id}",
                details=f"Хэш транзакции: {tx_hash}"
            )

            if 'send_telegram_message' in globals():
                try:
                    send_telegram_message(user_id,
                        f"🔥 **МЕГА-БУСТЕР АКТИВИРОВАН!**\n\n"
                        f"💎 +100 LP\n"
                        f"👑 +2 Легенды (улучшения)\n"
                        f"🦅 +2 Мастера (улучшения)\n"
                        f"💰 +1500 WG\n\n"
                        f"🎉 Спасибо за покупку!"
                    )
                except Exception as tg_err:
                    logger.error(f"⚠️ Не удалось отправить ТГ-сообщение: {tg_err}")

            return jsonify({'confirmed': True, 'tx_hash': tx_hash})

        return jsonify({'confirmed': False})

    except Exception as e:
        logger.error(f"Ошибка в check_mega_boost_payment: {e}", exc_info=True)
        return jsonify({'confirmed': False, 'error': str(e)}), 500


@app.route('/api/claim_mega_boost', methods=['POST'])
def api_claim_mega_boost():
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data"}), 400

    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400

    try:
        # Проверяем лимит
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) as count FROM successful_payments WHERE user_id = ? AND payload = 'mega_boost'",
                (user_id,)
            )
            row = cursor.fetchone()
            if row and row['count'] >= 2:
                return jsonify({"success": False, "message": "Вы уже купили Мега-бустер 2 раза!"}), 400

        user = get_user(user_id)

        # +100 LP
        old_lp = user['lp']
        new_lp = old_lp + 100
        safe_update_user(user_id, lp=new_lp)

        # +1500 WG
        old_wg = user['wg']
        new_wg = old_wg + 1500
        safe_update_user(user_id, wg=new_wg)

        # +2 Легенды (id: 5)
        upgrade_counts = user['upgrade_counts']
        upgrade_counts[5] = upgrade_counts.get(5, 0) + 2
        safe_update_user(user_id, upgrade_counts=upgrade_counts)

        # +2 Мастера (id: 4)
        upgrade_counts = user['upgrade_counts']
        upgrade_counts[4] = upgrade_counts.get(4, 0) + 2
        safe_update_user(user_id, upgrade_counts=upgrade_counts)

        # Записываем покупку
        with db.get_cursor() as cursor:
            cursor.execute(
                "INSERT INTO successful_payments (user_id, telegram_payment_charge_id, payload, amount) VALUES (?, ?, ?, ?)",
                (user_id, f"mega_boost_{int(time.time())}", "mega_boost", 50)
            )

        add_admin_log(
            f"🔥 Активировал Мега-бустер | +100 LP, +1500 WG, +2 Легенды, +2 Мастера",
            user_id,
            user.get('username') or f"User_{user_id}"
        )

        if 'send_telegram_message' in globals():
            try:
                send_telegram_message(user_id,
                    f"🔥 **МЕГА-БУСТЕР АКТИВИРОВАН!**\n\n"
                    f"💎 +100 LP\n"
                    f"👑 +2 Легенды (улучшения)\n"
                    f"🦅 +2 Мастера (улучшения)\n"
                    f"💰 +1500 WG\n\n"
                    f"🎉 Спасибо за покупку!"
                )
            except Exception as tg_err:
                logger.error(f"⚠️ Не удалось отправить ТГ-сообщение: {tg_err}")

        return jsonify({"success": True, "message": "Мега-бустер активирован!"})

    except Exception as e:
        logger.error(f"Ошибка в claim_mega_boost: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


# ========== МЕГА-БУСТЕР - ПРОВЕРКА ЛИМИТА ==========
@app.route('/api/get_mega_boost_count', methods=['POST'])
def api_get_mega_boost_count():
    data = request.json or {}
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400

    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) as count FROM successful_payments WHERE user_id = ? AND payload = 'mega_boost'",
            (user_id,)
        )
        row = cursor.fetchone()
        count = row['count'] if row else 0

    return jsonify({"success": True, "count": count})

# ========== ФОРТУНА API (РУЧНОЕ УПРАВЛЕНИЕ - БЕЗ АВТОЗАВЕРШЕНИЯ) ==========

def create_new_fortune_round():
    global current_fortune_round

    # 1. Сначала проверяем базу БЕЗ глобального лока, чтобы не заклинило restore
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT round_id FROM fortune_rounds 
            WHERE winner_team IS NULL 
            LIMIT 1
        ''')
        existing_round = cursor.fetchone()

    # Если раунд есть — выходим из функции создания и запускаем восстановление СНАРУЖИ
    if existing_round:
        print(f"⚠️ Активный раунд {existing_round['round_id']} уже существует, восстанавливаю...")
        restore_fortune_from_db()
        return

    # 2. Если раунда нет — только тогда берем лок и создаем новый
    with fortune_lock:
        round_id = str(uuid.uuid4())[:8]
        current_fortune_round = {
            "round_id": round_id,
            "yellow_pool": 0,
            "red_pool": 0,
            "yellow_bets": [],
            "red_bets": [],
            "end_time": time.time() + FORTUNE_ROUND_DURATION
        }
        with db.get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO fortune_rounds (round_id, start_time, yellow_pool, red_pool, end_time)
                VALUES (?, ?, ?, ?, ?)
            ''', (round_id, datetime.datetime.now().isoformat(), 0, 0,
                  datetime.datetime.fromtimestamp(current_fortune_round['end_time']).isoformat()))
        print(f"✨ Создан новый раунд Фортуны: {round_id}")


def restore_fortune_from_db():
    global current_fortune_round
    with fortune_lock:
        with db.get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM fortune_rounds 
                WHERE winner_team IS NULL
                ORDER BY id DESC LIMIT 1
            ''')
            round_row = cursor.fetchone()
            if not round_row:
                print("🔄 Нет активного раунда в БД, создаю новый...")
                create_new_fortune_round()
                return
            round_id = round_row['round_id']
            print(f"🔄 Восстанавливаю раунд Фортуны {round_id} из БД...")
            cursor.execute('SELECT * FROM fortune_active_bets WHERE round_id = ?', (round_id,))
            bets = cursor.fetchall()
            yellow_bets = []
            red_bets = []
            yellow_pool = 0
            red_pool = 0
            for bet in bets:
                user = get_user(bet['user_id'])
                bet_data = {
                    "user_id": bet['user_id'],
                    "amount": bet['amount'],
                    "net_amount": bet['net_amount'],
                    "username": user.get('username') or user.get('first_name') or str(bet['user_id']),
                    "avatar_url": user.get('avatar_url', '')
                }
                if bet['team'] == 'yellow':
                    yellow_bets.append(bet_data)
                    yellow_pool += bet['net_amount']
                else:
                    red_bets.append(bet_data)
                    red_pool += bet['net_amount']
            end_time_from_db = round_row['end_time']
            if end_time_from_db:
                end_timestamp = datetime.datetime.fromisoformat(end_time_from_db).timestamp()
                if end_timestamp < time.time():
                    end_timestamp = time.time() + FORTUNE_ROUND_DURATION
                    print(f"⏰ Время раунда истекло, продлеваю на {FORTUNE_ROUND_DURATION} сек")
            else:
                end_timestamp = time.time() + FORTUNE_ROUND_DURATION
            current_fortune_round = {
                "round_id": round_id,
                "yellow_pool": yellow_pool,
                "red_pool": red_pool,
                "yellow_bets": yellow_bets,
                "red_bets": red_bets,
                "end_time": end_timestamp
            }
            cursor.execute('''
                UPDATE fortune_rounds 
                SET end_time = ?, yellow_pool = ?, red_pool = ?
                WHERE round_id = ?
            ''', (
                datetime.datetime.fromtimestamp(end_timestamp).isoformat(),
                yellow_pool,
                red_pool,
                round_id
            ))
            print(f"✅ Восстановлено {len(yellow_bets) + len(red_bets)} ставок")


# ========== ТАЙМЕР ТОЛЬКО ДЛЯ ОТОБРАЖЕНИЯ, НЕ ЗАВЕРШАЕТ РАУНД ==========
def update_fortune_timer():
    """Обновляет таймер на клиенте и АВТОМАТИЧЕСКИ завершает раунд при 00:00"""
    global current_fortune_round
    while True:
        time.sleep(1)
        try:
            should_end = False
            with fortune_lock:
                if current_fortune_round and current_fortune_round.get('round_id'):
                    end_time = current_fortune_round.get('end_time', 0)
                    now = time.time()
                    time_left = max(0, int(end_time - now))

                    # Отправляем тиканье таймера игрокам
                    socketio.emit('fortune_timer_update', {'time_left': time_left})

                    # Если время вышло и раунд еще не в процессе завершения
                    if time_left == 0 and not current_fortune_round.get('is_ending', False):
                        should_end = True

            # Вызываем завершение ВНЕ блока lock, чтобы не поймать deadlock (взаимную блокировку)
            if should_end:
                print("⏰ [ТАЙМЕР] Время вышло! Автоматически завершаем раунд...")
                end_fortune_round()

        except Exception as e:
            logger.error(f"Таймер Фортуны ошибка: {e}", exc_info=True)


# ========== РУЧНОЕ ЗАВЕРШЕНИЕ РАУНДА (ТОЛЬКО ЧЕРЕЗ АДМИНКУ ИЛИ API) ==========
# ========== ЗАМЕНИТЬ СУЩЕСТВУЮЩУЮ ФУНКЦИЮ end_fortune_round НА ЭТУ ==========
def end_fortune_round():
    """Завершает текущий раунд: определяет победителя, выдаёт призы, создаёт новый раунд"""
    global current_fortune_round

    with fortune_lock:
        if not current_fortune_round:
            print("⚠️ [ФОРТУНА] Нет активного раунда для завершения")
            return

        if current_fortune_round.get('is_ending', False):
            print("⚠️ [ФОРТУНА] Раунд уже завершается, пропускаем")
            return

        current_fortune_round['is_ending'] = True
        round_id = current_fortune_round['round_id']
        print(f"🎲 [ФОРТУНА] Начинаем завершение раунда {round_id}")

    # ========== 1. МГНОВЕННО ПОЛУЧАЕМ ДАННЫЕ ДЛЯ КОЛЕСА (до любых БД операций) ==========
    yellow_pool = current_fortune_round['yellow_pool']
    red_pool = current_fortune_round['red_pool']
    total_pool = yellow_pool + red_pool

    # Получаем список ставок ДО того как начнём их удалять
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM fortune_active_bets WHERE round_id = ?', (round_id,))
        all_bets = cursor.fetchall()

    # Определяем победителя и коэффициент ДО выдачи призов
    has_yellow = any(b['team'] == 'yellow' for b in all_bets)
    has_red = any(b['team'] == 'red' for b in all_bets)

    winner_team = None
    sector_factor = random.uniform(0.15, 0.85)

    if not (has_yellow and has_red):
        # Ставки только на одной команде - возврат
        winner_team = 'refund'
    else:
        # Определяем победителя по весу пулов
        yellow_weight = yellow_pool / total_pool if total_pool > 0 else 0.5
        winner_team = 'yellow' if random.random() < yellow_weight else 'red'

    # ========== 2. МГНОВЕННО ОТПРАВЛЯЕМ КОМАНДУ НА ЗАПУСК КОЛЕСА ==========
    # ОТПРАВЛЯЕМ ДО ТОГО КАК НАЧНЁМ ВЫДАВАТЬ ПРИЗЫ!
    socketio.emit('fortune_round_ending_immediate', {
        'winner': winner_team,
        'sector_factor': sector_factor,
        'yellow_pool': yellow_pool,
        'red_pool': red_pool,
        'round_id': round_id
    })

    # Даём фронтенду 0.1 секунды на подготовку (опционально, можно убрать)
    import time
    time.sleep(0.1)

    # ========== 3. ТЕПЕРЬ МЕДЛЕННО ОБРАБАТЫВАЕМ ПРИЗЫ (асинхронно) ==========
    # Запускаем в отдельном потоке, чтобы не блокировать сокеты
    def process_prizes_async():
        try:
            with db.get_cursor() as cursor:
                if winner_team == 'refund':
                    # Возврат ставок
                    for bet in all_bets:
                        user = get_user(bet['user_id'])
                        safe_update_user(bet['user_id'], wg=user['wg'] + bet['amount'])
                        add_log(f"🎲 ФОРТУНА: Возврат {bet['amount']} WG", bet['user_id'], user['username'])
                        cursor.execute('''
                            INSERT INTO fortune_history (user_id, round_id, team, amount, result, win_amount)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (bet['user_id'], round_id, bet['team'], bet['amount'], 'refund', bet['amount']))

                    cursor.execute('''
                        UPDATE fortune_rounds 
                        SET winner_team = 'refund', end_time = ?
                        WHERE round_id = ?
                    ''', (datetime.datetime.now().isoformat(), round_id))

                else:
                    # Выдаём призы победителям
                    winner_bets = [b for b in all_bets if b['team'] == winner_team]
                    winner_total = sum(b['net_amount'] for b in winner_bets)

                    for bet in winner_bets:
                        share = bet['net_amount'] / winner_total if winner_total > 0 else 0
                        win_amount = round(total_pool * share, 2)
                        user = get_user(bet['user_id'])
                        safe_update_user(bet['user_id'], wg=user['wg'] + win_amount)

                        # ========== ОБНОВЛЯЕМ ДОСТИЖЕНИЯ (ПОБЕДА) ==========
                        update_fortune_achievements(bet['user_id'], is_win=True)

                        cursor.execute('''
                            INSERT INTO fortune_history (user_id, round_id, team, amount, result, win_amount)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (bet['user_id'], round_id, winner_team, bet['amount'], 'win', win_amount))
                        add_log(f"🎲 ФОРТУНА: ПОБЕДА! +{win_amount} WG", bet['user_id'], user['username'])

                        # Отправляем уведомление в Telegram
                        team_name = "Жёлтых 🟡" if winner_team == 'yellow' else "Красных 🔴"
                        send_telegram_message(bet['user_id'],
                                              f"🎉 **ПОБЕДА В КОМАНДНОЙ ФОРТУНЕ!**\n\n"
                                              f"Команда {team_name} победила!\n"
                                              f"💰 Вы выиграли {win_amount} WG!\n\n"
                                              f"Поздравляем! 🎊")

                    # Логируем проигравших
                    for bet in all_bets:
                        if bet['team'] != winner_team:
                            cursor.execute('''
                                INSERT INTO fortune_history (user_id, round_id, team, amount, result, win_amount)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (bet['user_id'], round_id, bet['team'], bet['amount'], 'lose', 0))

                    # Обновляем запись раунда в БД
                    cursor.execute('''
                        UPDATE fortune_rounds 
                        SET winner_team = ?, end_time = ?, yellow_pool = ?, red_pool = ?
                        WHERE round_id = ?
                    ''', (winner_team, datetime.datetime.now().isoformat(), yellow_pool, red_pool, round_id))

                # Удаляем активные ставки ТОЛЬКО ПОСЛЕ ОТПРАВКИ ВСЕХ ДАННЫХ
                cursor.execute("DELETE FROM fortune_active_bets WHERE round_id = ?", (round_id,))

            # Создаём новый раунд
            create_new_fortune_round()
            print(f"✅ [ФОРТУНА] Призы распределены, создан новый раунд")

        except Exception as e:
            logger.error(f"Ошибка при выдаче призов Фортуны: {e}", exc_info=True)
            # Если ошибка, всё равно создаём новый раунд
            create_new_fortune_round()
        finally:
            # Сбрасываем флаг завершения
            with fortune_lock:
                if 'current_fortune_round' in globals() and current_fortune_round:
                    current_fortune_round['is_ending'] = False

    # Запускаем обработку призов в фоне
    threading.Thread(target=process_prizes_async, daemon=True).start()


def start_fortune_timer_thread():
    global fortune_timer_thread_started
    if fortune_timer_thread_started:
        return
    fortune_timer_thread_started = True
    threading.Thread(target=update_fortune_timer, daemon=True).start()


# ========== ФОРТУНА API ENDPOINTS ==========

@app.route('/api/fortune/status', methods=['GET'])
def api_fortune_status():
    global current_fortune_round
    with fortune_lock:
        if not current_fortune_round.get('round_id'):
            restore_fortune_from_db()
        time_left = max(0, int(current_fortune_round.get('end_time', 0) - time.time()))
        yellow_bets_sorted = sorted(current_fortune_round['yellow_bets'], key=lambda x: x['net_amount'], reverse=True)[
            :5]
        red_bets_sorted = sorted(current_fortune_round['red_bets'], key=lambda x: x['net_amount'], reverse=True)[:5]
        return jsonify({
            "success": True,
            "round_id": current_fortune_round['round_id'],
            "time_left": time_left,
            "yellow_pool": current_fortune_round['yellow_pool'],
            "red_pool": current_fortune_round['red_pool'],
            "yellow_bets": [{
                "userId": bet['user_id'],
                "amount": bet['amount'],
                "netAmount": bet['net_amount'],
                "avatarUrl": bet.get('avatar_url', ''),
                "username": bet.get('username', '')
            } for bet in yellow_bets_sorted],
            "red_bets": [{
                "userId": bet['user_id'],
                "amount": bet['amount'],
                "netAmount": bet['net_amount'],
                "avatarUrl": bet.get('avatar_url', ''),
                "username": bet.get('username', '')
            } for bet in red_bets_sorted]
        })


@app.route('/api/fortune/bet', methods=['POST'])
def api_fortune_bet():
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data"}), 400
    user_id = data.get('user_id')
    team = data.get('team')
    amount = data.get('amount')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    if team not in ['yellow', 'red']:
        return jsonify({"success": False, "error": "Invalid team"}), 400
    if not amount or float(amount) < 10:
        return jsonify({"success": False, "error": "Минимальная ставка — 10 WG"}), 400
    amount = float(amount)
    user = get_user(user_id)
    if user['wg'] < amount:
        return jsonify({"success": False, "error": f"Не хватает WG! У вас {user['wg']:.2f} WG"}), 400

    with fortune_lock:
        if current_fortune_round.get('end_time', 0) <= time.time():
            # Если таймер истёк, но раунд не завершён — продлеваем
            new_end_time = time.time() + FORTUNE_ROUND_DURATION
            current_fortune_round['end_time'] = new_end_time
            with db.get_cursor() as cursor:
                cursor.execute('''
                    UPDATE fortune_rounds SET end_time = ? WHERE round_id = ?
                ''', (datetime.datetime.fromtimestamp(new_end_time).isoformat(), current_fortune_round['round_id']))

        round_id = current_fortune_round['round_id']

        # Проверка на противоположную команду
        other_team = 'red' if team == 'yellow' else 'yellow'
        other_bets = current_fortune_round['red_bets'] if team == 'yellow' else current_fortune_round['yellow_bets']
        for bet in other_bets:
            if bet.get('user_id') == user_id:
                return jsonify({"success": False,
                                "error": f"❌ Вы уже сделали ставку на команду {'Красных 🔴' if other_team == 'red' else 'Жёлтых 🟡'}! Можно ставить только на одну команду за раунд."}), 400

        bet_list = current_fortune_round['yellow_bets'] if team == 'yellow' else current_fortune_round['red_bets']
        existing_bet = None
        for bet in bet_list:
            if bet.get('user_id') == user_id:
                existing_bet = bet
                break

        commission = amount * FORTUNE_COMMISSION
        net_amount = amount - commission
        safe_update_user(user_id, wg=user['wg'] - amount)

        # ========== ФЛАГ: была ли у игрока ставка в этом раунде ДО этой операции ==========
        had_bet_before = existing_bet is not None

        if existing_bet:
            new_total = existing_bet['amount'] + amount
            new_net_total = existing_bet['net_amount'] + net_amount
            existing_bet['amount'] = new_total
            existing_bet['net_amount'] = new_net_total
            with db.get_cursor() as cursor:
                cursor.execute('''
                    UPDATE fortune_active_bets 
                    SET amount = ?, net_amount = ?
                    WHERE round_id = ? AND user_id = ? AND team = ?
                ''', (new_total, new_net_total, round_id, user_id, team))
            add_log(
                f"🎲 ФОРТУНА: ДОБАВИЛ к ставке {amount} WG (всего {new_total} WG) на команду {'Жёлтые' if team == 'yellow' else 'Красные'}",
                user_id, user['username'], old_value=user['wg'], new_value=user['wg'] - amount, currency="wg")
            result_amount = new_total

            # ========== ОБНОВЛЯЕМ ДОСТИЖЕНИЯ ТОЛЬКО ЕСЛИ ЭТО ПЕРВАЯ СТАВКА В РАУНДЕ ==========
            # Для добавления к ставке НЕ обновляем достижения (только сумму total_bet)
            # Но сумму total_bet нужно обновить всегда
            update_fortune_achievements(user_id, bet_amount=amount, is_win=False, is_new_round=False)

        else:
            bet_data = {
                "user_id": user_id,
                "amount": amount,
                "net_amount": net_amount,
                "username": user.get('username') or user.get('first_name') or str(user_id),
                "avatar_url": user.get('avatar_url', '')
            }
            bet_list.append(bet_data)
            with db.get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO fortune_active_bets (round_id, user_id, team, amount, net_amount)
                    VALUES (?, ?, ?, ?, ?)
                ''', (round_id, user_id, team, amount, net_amount))
            add_log(
                f"🎲 ФОРТУНА: Новая ставка {amount} WG на команду {'Жёлтые' if team == 'yellow' else 'Красные'}",
                user_id, user['username'], old_value=user['wg'], new_value=user['wg'] - amount, currency="wg")
            result_amount = amount

            # ========== НОВАЯ СТАВКА В РАУНДЕ - ОБНОВЛЯЕМ ВСЁ ==========
            update_fortune_achievements(user_id, bet_amount=amount, is_win=False, is_new_round=True)

        if team == 'yellow':
            current_fortune_round['yellow_pool'] += net_amount
        else:
            current_fortune_round['red_pool'] += net_amount

        with db.get_cursor() as cursor:
            cursor.execute('''
                UPDATE fortune_rounds 
                SET yellow_pool = ?, red_pool = ?
                WHERE round_id = ?
            ''', (current_fortune_round['yellow_pool'], current_fortune_round['red_pool'], round_id))

        # ========== ОТПРАВЛЯЕМ ОБНОВЛЕНИЯ ВСЕМ ИГРОКАМ ЧЕРЕЗ SOCKET.IO ==========
        try:
            yellow_players_sorted = sorted(current_fortune_round['yellow_bets'], key=lambda x: x['net_amount'],
                                           reverse=True)[:5]
            red_players_sorted = sorted(current_fortune_round['red_bets'], key=lambda x: x['net_amount'], reverse=True)[
                :5]

            socketio.emit('fortune_pools_update', {
                'yellow_pool': current_fortune_round['yellow_pool'],
                'red_pool': current_fortune_round['red_pool'],
                'yellow_players': [{
                    'userId': p['user_id'],
                    'amount': p['amount'],
                    'netAmount': p['net_amount'],
                    'avatarUrl': p.get('avatar_url', ''),
                    'username': p.get('username', '')
                } for p in yellow_players_sorted],
                'red_players': [{
                    'userId': p['user_id'],
                    'amount': p['amount'],
                    'netAmount': p['net_amount'],
                    'avatarUrl': p.get('avatar_url', ''),
                    'username': p.get('username', '')
                } for p in red_players_sorted]
            })
        except Exception as e:
            logger.error(f"Socket emit error: {e}")

        return jsonify({
            "success": True,
            "message": f"Ставка {amount} WG принята! Всего на команду: {result_amount} WG",
            "net_amount": net_amount,
            "yellow_pool": current_fortune_round['yellow_pool'],
            "red_pool": current_fortune_round['red_pool'],
            "user_total_bet": result_amount
        })


@app.route('/api/fortune/history', methods=['POST'])
def api_fortune_history():
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    limit = data.get('limit', 20)
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT * FROM fortune_history 
            WHERE user_id = ? 
            ORDER BY id DESC 
            LIMIT ?
        ''', (user_id, limit))
        rows = cursor.fetchall()
        history = []
        for row in rows:
            history.append({
                "id": row['id'],
                "round_id": row['round_id'],
                "team": row['team'],
                "amount": row['amount'],
                "result": row['result'],
                "win_amount": row['win_amount'],
                "created_at": row['created_at']
            })
        return jsonify({"success": True, "history": history})


@app.route('/api/fortune/end_round', methods=['POST'])
def api_fortune_end_round():
    """Завершение раунда фортуны"""
    try:
        # Вызываем твою основную функцию, код которой ты скидывал
        end_fortune_round()
        return jsonify({"status": "success", "message": "Раунд успешно завершен"})
    except Exception as e:
        logger.error(f"Ошибка в роуте end_round: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/fortune/user_bet', methods=['POST'])
def api_fortune_user_bet():
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    with fortune_lock:
        for bet in current_fortune_round.get('yellow_bets', []):
            if bet.get('user_id') == user_id:
                return jsonify({"success": True, "bet": bet, "team": "yellow", "amount": bet.get('amount', 0)})
        for bet in current_fortune_round.get('red_bets', []):
            if bet.get('user_id') == user_id:
                return jsonify({"success": True, "bet": bet, "team": "red", "amount": bet.get('amount', 0)})
        return jsonify({"success": True, "bet": None, "team": None, "amount": 0})


@app.route('/api/fortune/user_total_bet', methods=['POST'])
def api_fortune_user_total_bet():
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    with fortune_lock:
        for bet in current_fortune_round.get('yellow_bets', []):
            if bet.get('user_id') == user_id:
                return jsonify({"success": True, "team": "yellow", "amount": bet.get('amount', 0)})
        for bet in current_fortune_round.get('red_bets', []):
            if bet.get('user_id') == user_id:
                return jsonify({"success": True, "team": "red", "amount": bet.get('amount', 0)})
        return jsonify({"success": True, "team": None, "amount": 0})


@app.route('/api/fortune/winning_history', methods=['GET'])
def api_fortune_winning_history():
    limit = request.args.get('limit', 20, type=int)
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT fh.*, u.username, u.first_name, u.avatar_url 
            FROM fortune_history fh
            LEFT JOIN users u ON fh.user_id = u.user_id
            WHERE fh.result = 'win'
            ORDER BY fh.id DESC 
            LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        history = []
        for row in rows:
            history.append({
                "id": row['id'],
                "user_id": row['user_id'],
                "round_id": row['round_id'],
                "team": row['team'],
                "amount": row['amount'],
                "win_amount": row['win_amount'],
                "created_at": row['created_at'],
                "username": row['username'] or row['first_name'] or f"Player_{row['user_id']}",
                "avatar_url": row['avatar_url'] or ''
            })
        return jsonify({"success": True, "history": history})


@app.route('/api/fortune/history_all', methods=['GET'])
def api_fortune_history_all():
    limit = request.args.get('limit', 50, type=int)
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT * FROM fortune_history 
            ORDER BY id DESC 
            LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        history = []
        for row in rows:
            history.append({
                "id": row['id'],
                "user_id": row['user_id'],
                "round_id": row['round_id'],
                "team": row['team'],
                "amount": row['amount'],
                "result": row['result'],
                "win_amount": row['win_amount'],
                "created_at": row['created_at']
            })
        return jsonify({"success": True, "history": history})


@app.route('/api/fortune/stats', methods=['POST'])
def api_fortune_stats():
    """Возвращает статистику игрока по Фортуне для достижений"""
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data"}), 400

    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400

    with db.get_cursor() as cursor:
        cursor.execute("""
            SELECT fortune_bets_count, fortune_wins_count, fortune_total_bet_amount 
            FROM users WHERE user_id = ?
        """, (user_id,))
        row = cursor.fetchone()

        return jsonify({
            "success": True,
            "stats": {
                "bets_count": row['fortune_bets_count'] or 0,
                "wins_count": row['fortune_wins_count'] or 0,
                "total_bet_amount": row['fortune_total_bet_amount'] or 0
            }
        })


# ========== PAYDAY API ==========

@app.route('/api/admin/payday/status', methods=['GET'])
@require_admin
def api_payday_status():
    """Получить текущий статус бонуса"""
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM payday_bonus WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Бонус не найден"}), 404

        return jsonify({
            "success": True,
            "multiplier": row['multiplier'],
            "is_active": bool(row['is_active']),
            "start_time": row['start_time'],
            "end_time": row['end_time'],
            "time_remaining": get_payday_time_remaining(row)
        })


@app.route('/api/admin/payday/activate', methods=['POST'])
@require_admin
def api_payday_activate():
    """Активировать бонус"""
    data = request.json
    multiplier = data.get('multiplier', 1.0)
    duration_minutes = data.get('duration_minutes', 60)
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"

    # Проверка множителя
    if multiplier < 1.1 or multiplier > 5.0:
        return jsonify({"success": False, "error": "Множитель должен быть от 1.1 до 5.0"}), 400

    if duration_minutes < 1 or duration_minutes > 1440:  # максимум 24 часа
        return jsonify({"success": False, "error": "Время должно быть от 1 до 1440 минут"}), 400

    now = datetime.datetime.now()
    end_time = now + datetime.timedelta(minutes=duration_minutes)

    with db.get_cursor() as cursor:
        cursor.execute('''
            UPDATE payday_bonus 
            SET multiplier = ?,
                start_time = ?,
                end_time = ?,
                is_active = 1,
                updated_by = ?,
                updated_at = ?
            WHERE id = 1
        ''', (multiplier, now.isoformat(), end_time.isoformat(), admin_id, now.isoformat()))

    add_admin_log(
        f"🔥 PAYDAY АКТИВИРОВАН! Множитель: {multiplier}x на {duration_minutes} минут",
        admin_id, admin_name
    )

    # Отправляем уведомление всем игрокам
    notify_all_players_payday_activated(multiplier, duration_minutes)

    return jsonify({
        "success": True,
        "message": f"✅ PayDay активирован! Множитель {multiplier}x на {duration_minutes} минут",
        "end_time": end_time.isoformat()
    })


@app.route('/api/admin/payday/deactivate', methods=['POST'])
@require_admin
def api_payday_deactivate():
    """Деактивировать бонус досрочно"""
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"

    with db.get_cursor() as cursor:
        cursor.execute('''
            UPDATE payday_bonus 
            SET is_active = 0,
                updated_at = ?
            WHERE id = 1
        ''', (datetime.datetime.now().isoformat(),))

    add_admin_log(f"⏹️ PAYDAY ДЕАКТИВИРОВАН досрочно", admin_id, admin_name)

    return jsonify({"success": True, "message": "PayDay деактивирован"})


def get_payday_time_remaining(row):
    """Возвращает оставшееся время в секундах"""
    if not row or not row['is_active'] or not row['end_time']:
        return 0

    end_time = datetime.datetime.fromisoformat(row['end_time'])
    remaining = (end_time - datetime.datetime.now()).total_seconds()
    return max(0, int(remaining))


def notify_all_players_payday_activated(multiplier, duration_minutes):
    """Уведомить всех игроков о старте PayDay"""
    with db.get_cursor() as cursor:
        cursor.execute("SELECT user_id FROM users WHERE user_id > 0")
        users = cursor.fetchall()

    message = f"""🔥 **PAYDAY АКТИВИРОВАН!** 🔥

💰 Множитель: **{multiplier}x** на **{duration_minutes} минут**

✅ ВСЁ УДВАИВАЕТСЯ:
• Доход за клик
• Шанс выпадения LP
• Энергия за рекламу
• Лимит энергии
• Бонус за улучшения
• Клики в топе

🏃‍♂️ НЕ УПУСТИ ШАНС!"""

    for user in users:
        try:
            send_telegram_message(user['user_id'], message)
        except:
            pass


def get_payday_multiplier():
    """Получить текущий множитель бонуса"""
    with db.get_cursor() as cursor:
        cursor.execute("SELECT multiplier, is_active, end_time FROM payday_bonus WHERE id = 1")
        row = cursor.fetchone()

        if not row or not row['is_active']:
            return 1.0

        # Проверяем, не истекло ли время
        if row['end_time']:
            end_time = datetime.datetime.fromisoformat(row['end_time'])
            if datetime.datetime.now() > end_time:
                # Автоматически деактивируем
                cursor.execute("UPDATE payday_bonus SET is_active = 0 WHERE id = 1")
                return 1.0

        return row['multiplier']


@app.route('/api/payday/status', methods=['GET'])
def api_payday_status_public():
    """Публичный статус PayDay для игроков"""
    with db.get_cursor() as cursor:
        cursor.execute("SELECT multiplier, is_active, end_time FROM payday_bonus WHERE id = 1")
        row = cursor.fetchone()

        if not row or not row['is_active']:
            return jsonify({"success": True, "is_active": False, "multiplier": 1.0})

        # Проверяем, не истекло ли время
        if row['end_time']:
            end_time = datetime.datetime.fromisoformat(row['end_time'])
            if datetime.datetime.now() > end_time:
                cursor.execute("UPDATE payday_bonus SET is_active = 0 WHERE id = 1")
                return jsonify({"success": True, "is_active": False, "multiplier": 1.0})

        remaining = (end_time - datetime.datetime.now()).total_seconds()

        return jsonify({
            "success": True,
            "is_active": True,
            "multiplier": row['multiplier'],
            "time_remaining": max(0, int(remaining))
        })

# ========== ОСНОВНЫЕ API (СОКРАЩЕННО ДЛЯ ЭКОНОМИИ МЕСТА, НО РАБОТАЮТ) ==========
@app.route('/api/log_game_entry', methods=['POST'])
def api_log_game_entry():
    data = request.json
    if not data:
        return jsonify({"success": False}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False}), 400
    user = get_user(user_id)
    with online_users_lock:
        online_users[user_id] = time.time()
    update_online_count()
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    update_stats_history(today, online=len(online_users))
    add_log(f"🟢 Вошёл в игру", user_id, user['username'])
    return jsonify({"success": True})

@app.route('/api/log_game_exit', methods=['POST'])
def api_log_game_exit():
    data = request.json
    if not data:
        return jsonify({"success": False}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False}), 400
    user = get_user(user_id)
    with online_users_lock:
        if user_id in online_users:
            del online_users[user_id]
    add_log(f"🔴 Вышел из игры", user_id, user['username'])
    return jsonify({"success": True})

@app.route('/api/online_count', methods=['GET'])
def api_online_count():
    update_online_count()
    with online_users_lock:
        return jsonify({"online": len(online_users)})


@app.route('/api/register', methods=['POST'])
def api_register():
    if not check_rate_limit(f"register_{request.remote_addr}", limit=10, window_seconds=60):
        return jsonify({"success": False, "error": "Too many requests"}), 429
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400

    # ========== НОВАЯ ПРОВЕРКА: защита от фейковых ID ==========
    if user_id <= 0 or user_id == 12345678 or user_id == 1:
        logger.warning(f"❌ Попытка регистрации с невалидным ID: {user_id}")
        return jsonify({"success": False, "error": "Invalid user_id"}), 400

    username = sanitize_string(data.get('username', ''), 50)
    first_name = sanitize_string(data.get('first_name', ''), 50)
    last_name = sanitize_string(data.get('last_name', ''), 50)
    avatar_url = sanitize_string(data.get('avatar_url', ''), 200)
    referral_code = sanitize_string(data.get('referral_code', ''), 50)
    logger.info(f"📝 Регистрация/обновление: user_id={user_id}, username={username}, first_name={first_name}")

    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("""
                UPDATE users 
                SET username = ?, first_name = ?, last_name = ?, avatar_url = ? 
                WHERE user_id = ?
            """, (username, first_name, last_name, avatar_url, user_id))
            invalidate_cache(user_id)
            add_log(f"✏️ Обновил профиль (username: {username or first_name})", user_id,
                    username or first_name or str(user_id))
            cursor.execute("SELECT username, first_name FROM users WHERE user_id=?", (user_id,))
            check = cursor.fetchone()
            logger.info(f"✅ После обновления: username={check['username']}, first_name={check['first_name']}")
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
                    # Проверка, что реферер не фейковый
                    if referrer_id > 0 and referrer_id != 12345678 and referrer_id != 1:
                        cursor.execute(
                            'INSERT INTO referrals (referrer_id, referred_id, username, first_name, total_spent_lp) VALUES (?, ?, ?, ?, 0)',
                            (referrer_id, user_id, username, first_name))
                        add_log(f"👥 Новый реферал! {username or first_name} зарегистрировался по вашей ссылке",
                                referrer_id,
                                get_user(referrer_id)['username'])
                        update_achievement_progress(referrer_id, 'social', 1)
            cursor.execute('''
                INSERT INTO users (
                    user_id, wg, lp, energy, last_energy_update, tickets, total_clicks, upgrade_counts, 
                    ticket_counter, referral_code, referrer_id, likes, dislikes, settings, 
                    username, first_name, last_name, avatar_url, usdt, wins, role, stars, 
                    max_energy, energy_upgrades, energy_limit_upgrades, unlocked_prefixes, 
                    tutorial_completed, ton_wallet, banned_until, ban_reason, banned_by, completed_achievements
                ) VALUES (
                    ?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, ?, 0, 0, '{"theme":"dark"}', 
                    ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '', 0, '', 0, 0
                )
            ''', (user_id, now, ref_code_new, referrer_id, username, first_name, last_name, avatar_url, role, unlocked))
            add_log(f"✨ Новая регистрация! Добро пожаловать, {username or first_name}!", user_id,
                    username or first_name or str(user_id))
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            update_stats_history(today, users=1)
            cursor.execute("SELECT username, first_name FROM users WHERE user_id=?", (user_id,))
            check = cursor.fetchone()
            logger.info(f"✅ После регистрации: username={check['username']}, first_name={check['first_name']}")
    return jsonify({"success": True})

@app.route('/api/debug_user', methods=['POST'])
def api_debug_user():
    data = request.json
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"error": "Invalid user_id"}), 400
    with db.get_cursor() as cursor:
        cursor.execute("SELECT user_id, username, first_name, last_name FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if row:
            return jsonify({
                "user_id": row['user_id'],
                "username": row['username'],
                "first_name": row['first_name'],
                "last_name": row['last_name']
            })
    return jsonify({"error": "User not found"}), 404


@app.route('/api/click', methods=['POST'])
def api_click():
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"error": "Invalid user_id"}), 400
    if not check_rate_limit(f"click_{user_id}", limit=200, window_seconds=5):
        return jsonify({"error": "Слишком много кликов! Подождите."}), 429
    banned, ban_info = is_banned(user_id)
    if banned:
        return jsonify({
            "error": f"Вы забанены! Причина: {ban_info['reason']}. До: {ban_info['until_date']}",
            "banned": True
        })
    user = get_user(user_id)
    success, new_energy = spend_energy(user_id, user, 1)
    if not success:
        return jsonify({
            "error": "Нет энергии",
            "energy": new_energy,
            "wg": user["wg"],
            "lp": user["lp"]
        })

    # ========== ПОЛУЧАЕМ МНОЖИТЕЛЬ PAYDAY ==========
    payday_multiplier = get_payday_multiplier()

    # ========== ДОХОД ЗА КЛИК (УЖЕ С МНОЖИТЕЛЕМ!) ==========
    earning = get_total_earning(user["upgrade_counts"])

    old_wg = user["wg"]
    new_wg = old_wg + earning

    # ========== КЛИКИ ДЛЯ ТОПА (С МНОЖИТЕЛЕМ) ==========
    click_count = int(payday_multiplier) if payday_multiplier > 1 else 1

    with click_buffer_lock:
        click_buffer[user_id] = click_buffer.get(user_id, 0) + click_count
    safe_update_user(user_id, wg=new_wg)

    # ========== РЕФЕРАЛЬНАЯ СИСТЕМА ==========
    referrer_id = user.get('referrer_id', 0)
    if referrer_id > 0:
        referrer_earning = earning * 0.1
        if referrer_earning > 0:
            referrer = get_user(referrer_id)
            old_referrer_wg = referrer['wg']
            new_referrer_wg = old_referrer_wg + referrer_earning
            safe_update_user(referrer_id, wg=new_referrer_wg)
            with db.get_cursor() as cursor:
                cursor.execute('''
                    UPDATE referrals 
                    SET total_earned_wg = total_earned_wg + ? 
                    WHERE referrer_id = ? AND referred_id = ?
                ''', (referrer_earning, referrer_id, user_id))
            add_log(f"👥 Получил 10% от WG реферала (+{referrer_earning:.4f} WG)", referrer_id,
                    referrer.get('username') or f"User_{referrer_id}",
                    old_value=old_referrer_wg, new_value=new_referrer_wg, currency="wg")

    # ========== ОБНОВЛЯЕМ daily_clicks С МНОЖИТЕЛЕМ ==========
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET daily_clicks = daily_clicks + ? WHERE user_id = ?", (click_count, user_id))

    invalidate_cache(user_id)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    threading.Thread(target=async_click_tasks, args=(user_id, user, earning, old_wg, new_wg, today)).start()

    # ========== ШАНС ВЫПАДЕНИЯ LP ==========
    new_lp_value = user["lp"]
    lp_reward = False
    base_chance = 0.0025
    lp_chance = base_chance * payday_multiplier

    if random.random() < lp_chance:
        lp_reward = True
        lp_amount = 0.5 * payday_multiplier
        new_lp_value = user["lp"] + lp_amount
        safe_update_user(user_id, lp=new_lp_value)
        threading.Thread(target=add_log,
                         args=(f"🎲 Редкий дроп! +{lp_amount:.2f} LP", user_id, user['username'], user["lp"],
                               new_lp_value,
                               "lp")).start()

    # ========== ОТВЕТ ==========
    return jsonify({
        "energy": new_energy,
        "wg": new_wg,
        "lp": new_lp_value,
        "total_clicks": user["total_clicks"] + 1,
        "earned": earning,
        "lp_reward": lp_reward,
        "earning_per_click": earning,  # ← УЖЕ С МНОЖИТЕЛЕМ
        "payday_multiplier": payday_multiplier,
        "is_payday_active": payday_multiplier > 1
    })

@app.route('/api/status', methods=['POST'])
def api_status():
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"error": "Invalid user_id"}), 400
    if not check_rate_limit(f"status_{user_id}", limit=60, window_seconds=60):
        return jsonify({"error": "Too many requests"}), 429
    banned, ban_info = is_banned(user_id)
    if banned:
        return jsonify({"banned": True, "reason": ban_info['reason'], "until": ban_info['until_date']})
    user = get_user(user_id)
    current_energy, seconds_passed = calculate_energy(user)
    earning = get_total_earning(user["upgrade_counts"])
    with online_users_lock:
        online_users[user_id] = time.time()
    update_online_count()
    regen_text = get_energy_regen_text(user["max_energy"], current_energy)
    return jsonify({"wg": user["wg"], "lp": user["lp"], "energy": current_energy, "total_clicks": user["total_clicks"],
                    "earning_per_click": earning, "upgrade_counts": user["upgrade_counts"], "likes": user["likes"],
                    "dislikes": user["dislikes"], "username": user["username"], "first_name": user["first_name"],
                    "avatar_url": user["avatar_url"], "settings": user["settings"], "usdt": user["usdt"],
                    "wins": user["wins"], "role": user["role"], "stars": user["stars"],
                    "max_energy": user["max_energy"], "energy_upgrades": user["energy_upgrades"],
                    "regen_text": regen_text})


@app.route('/api/sync_light', methods=['POST'])
def sync_light():
    """Быстрый sync - только баланс и энергия (без лотереи и лидерборда)"""
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data"}), 400

        user_id = data.get('user_id')
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"success": False, "error": "Invalid user_id"}), 400

        user = get_user(user_id)
        current_energy, _ = calculate_energy(user)

        return jsonify({
            "success": True,
            "data": {
                "wg": user["wg"],
                "lp": user["lp"],
                "energy": current_energy,
                "max_energy": user.get("max_energy", 500),
                "total_clicks": user["total_clicks"],
                "earning_per_click": get_total_earning(user["upgrade_counts"])
            }
        })
    except Exception as e:
        logger.error(f"Sync light error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/buy_upgrade', methods=['POST'])
def api_buy_upgrade():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    if not check_rate_limit(f"buy_{user_id}", limit=10, window_seconds=30):
        return jsonify({"success": False, "msg": "Слишком частые покупки"}), 429
    banned, ban_info = is_banned(user_id)
    if banned:
        return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})

    upgrade_id = data.get('upgrade_id')
    if upgrade_id not in [1, 2, 3, 4, 5]:
        return jsonify({"success": False, "msg": "Неверный ID улучшения"})

    user = get_user(user_id)

    # Получаем платные и бесплатные улучшения
    upgrade_counts = user.get("upgrade_counts", {})
    free_upgrade_counts = user.get("free_upgrade_counts", {})

    current_paid = upgrade_counts.get(upgrade_id, 0)
    current_free = free_upgrade_counts.get(upgrade_id, 0)
    total_current = current_paid + current_free

    # Цена считается ТОЛЬКО от платных улучшений!
    cost = get_upgrade_cost(upgrade_id, current_paid)

    if user["wg"] < cost:
        return jsonify({"success": False, "msg": f"Не хватает WG! Нужно {cost:.2f} WG"})

    old_wg = user["wg"]
    new_wg = old_wg - cost
    new_paid_count = current_paid + 1

    # ========== ЧИСТИМ upgrade_counts ОТ ВСЕГО МУСОРА ==========
    clean_counts = {}
    for key, value in upgrade_counts.items():
        try:
            if isinstance(key, str) and key.isdigit():
                clean_counts[int(key)] = value
            elif isinstance(key, int):
                clean_counts[key] = value
        except:
            pass

    # Обновляем количество платных улучшений
    clean_counts[upgrade_id] = new_paid_count

    # Сохраняем ТОЛЬКО чистые данные
    safe_update_user(user_id, wg=new_wg, upgrade_counts=clean_counts)

    # ========== Логируем покупку ==========
    update_achievement_progress(user_id, 'investor', 1)
    update_achievement_progress(user_id, 'spender', int(cost))

    upgrade_name = UPGRADE_CONFIG[upgrade_id]['name']

    if current_paid == 0 and current_free == 0:
        add_log(f"🆕⭐ ПЕРВАЯ ПОКУПКА улучшения! {upgrade_name} за {cost:.2f} WG",
                user_id, user['username'], old_value=old_wg, new_value=new_wg, currency="wg")
    else:
        add_log(f"💰 Купил {upgrade_name} #{total_current + 1} за {cost:.2f} WG",
                user_id, user['username'], old_value=old_wg, new_value=new_wg, currency="wg")

    # Следующая цена считается от нового количества платных улучшений
    next_cost = get_upgrade_cost(upgrade_id, new_paid_count)

    return jsonify({
        "success": True,
        "msg": f"{upgrade_name} #{total_current + 1} куплено!",
        "new_count": total_current + 1,
        "paid_count": new_paid_count,
        "free_count": current_free,
        "next_cost": next_cost
    })


@app.route('/api/watch_ad', methods=['POST'])
def api_watch_ad():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400

    # ========== ПОЛУЧАЕМ МНОЖИТЕЛЬ PAYDAY ==========
    payday_multiplier = get_payday_multiplier()
    is_payday_active = payday_multiplier > 1

    # ========== КУЛДАУН ЗАВИСИТ ОТ PAYDAY ==========
    cooldown_minutes = 2 if is_payday_active else 5

    can_watch, msg = check_ad_cooldown(user_id, "energy_200", cooldown_minutes, 40)
    if not can_watch:
        return jsonify({"success": False, "msg": msg}), 429

    banned, ban_info = is_banned(user_id)
    if banned:
        return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})

    user = get_user(user_id)
    current_energy, _ = calculate_energy(user)
    max_energy = user.get("max_energy", 500)
    old_energy = current_energy

    # ========== НАГРАДА ЗАВИСИТ ОТ PAYDAY ==========
    base_energy = 150
    if is_payday_active:
        energy_boost = int(base_energy * payday_multiplier)
    else:
        energy_boost = base_energy

    new_energy = min(max_energy, current_energy + energy_boost)
    update_energy_in_db(user_id, user, new_energy)

    # ========== СОХРАНЯЕМ ИСТОРИЮ ПРОСМОТРА ==========
    record_ad_watch(user_id, "energy_200")

    update_achievement_progress(user_id, 'ad_lover', 1)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    update_stats_history(today, ad_views=1)

    add_log(
        f"🎬 Просмотрел рекламу (+{energy_boost} энергии)" + (
            f" (PayDay x{payday_multiplier})" if is_payday_active else ""),
        user_id, user['username'], old_value=old_energy, new_value=new_energy, currency="energy"
    )

    return jsonify({
        "success": True,
        "energy": energy_boost,
        "is_payday": is_payday_active,
        "payday_multiplier": payday_multiplier
    })

@app.route('/api/watch_ad_fallback', methods=['POST'])
def api_watch_ad_fallback():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    can_watch, msg = check_ad_cooldown(user_id, "energy_50", 2, 20)
    if not can_watch:
        return jsonify({"success": False, "msg": msg}), 429
    user = get_user(user_id)
    current_energy, _ = calculate_energy(user)
    max_energy = user.get("max_energy", 500)
    old_energy = current_energy
    new_energy = min(max_energy, current_energy + 50)
    update_energy_in_db(user_id, user, new_energy)
    record_ad_watch(user_id, "energy_50")
    update_achievement_progress(user_id, 'ad_lover', 1)
    add_log(f"🎬 Просмотрел рекламу (резервная, +50 энергии)", user_id, user['username'], old_value=old_energy,
            new_value=new_energy, currency="energy")
    return jsonify({"success": True, "energy": 50})


@app.route('/api/watch_ad_limit', methods=['POST'])
def api_watch_ad_limit():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400

    # ========== ПОЛУЧАЕМ МНОЖИТЕЛЬ PAYDAY ==========
    payday_multiplier = get_payday_multiplier()
    is_payday_active = payday_multiplier > 1

    # ========== КУЛДАУН ЗАВИСИТ ОТ PAYDAY ==========
    cooldown_minutes = 2 if is_payday_active else 10

    can_watch, msg = check_ad_cooldown(user_id, "energy_limit", cooldown_minutes, 15)
    if not can_watch:
        return jsonify({"success": False, "msg": msg}), 429

    banned, ban_info = is_banned(user_id)
    if banned:
        return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})

    user = get_user(user_id)
    current_upgrades = user.get('energy_limit_upgrades', 0)
    if current_upgrades >= 300:
        return jsonify({"success": False, "msg": "Вы достигли максимального лимита улучшений! (300/300)"})

    old_max_energy = user['max_energy']

    # ========== НАГРАДА ЗАВИСИТ ОТ PAYDAY ==========
    base_boost = 1
    if is_payday_active:
        energy_limit_boost = int(base_boost * payday_multiplier)
    else:
        energy_limit_boost = base_boost

    new_max_energy = old_max_energy + energy_limit_boost
    new_upgrades = current_upgrades + 1

    safe_update_user(user_id, max_energy=new_max_energy, energy_limit_upgrades=new_upgrades)
    record_ad_watch(user_id, "energy_limit")
    update_achievement_progress(user_id, 'ad_lover', 1)

    add_log(
        f"🎬 Просмотрел рекламу (+{energy_limit_boost} к макс. энергии, теперь {new_max_energy})" + (
            f" (PayDay x{payday_multiplier})" if is_payday_active else ""),
        user_id, user['username'], old_value=old_max_energy, new_value=new_max_energy, currency="energy"
    )

    return jsonify({
        "success": True,
        "max_energy": new_max_energy,
        "upgrades": new_upgrades,
        "is_payday": is_payday_active,
        "payday_multiplier": payday_multiplier
    })


@app.route('/api/can_watch_ad', methods=['POST'])
def api_can_watch_ad():
    data = request.json
    if not data:
        return jsonify({"can": False, "message": "No data"}), 400
    user_id = data.get('user_id')
    ad_type = data.get('ad_type')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"can": False, "message": "Invalid user_id"}), 400

    # ========== ПОЛУЧАЕМ МНОЖИТЕЛЬ PAYDAY ==========
    payday_multiplier = get_payday_multiplier()
    is_payday_active = payday_multiplier > 1

    if ad_type == 'energy_200':
        cooldown_minutes = 2 if is_payday_active else 5
        can_watch, msg = check_ad_cooldown(user_id, "energy_200", cooldown_minutes, 40)
        return jsonify({
            "can": can_watch,
            "message": msg if not can_watch else "",
            "is_payday": is_payday_active,
            "payday_multiplier": payday_multiplier
        })
    elif ad_type == 'energy_limit':
        cooldown_minutes = 2 if is_payday_active else 10
        can_watch, msg = check_ad_cooldown(user_id, "energy_limit", cooldown_minutes, 15)
        return jsonify({
            "can": can_watch,
            "message": msg if not can_watch else "",
            "is_payday": is_payday_active,
            "payday_multiplier": payday_multiplier
        })
    else:
        return jsonify({"can": False, "message": "Unknown ad type"})

@app.route('/api/buy_ticket', methods=['POST'])
def api_buy_ticket():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    if not check_rate_limit(f"ticket_{user_id}", limit=5, window_seconds=60):
        return jsonify({"success": False, "msg": "Слишком частые покупки билетов"}), 429
    banned, ban_info = is_banned(user_id)
    if banned:
        return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})
    user = get_user(user_id)
    success, msg = buy_ticket(user_id, user)
    return jsonify({"success": success, "msg": msg, "lp": user["lp"]})

@app.route('/api/reveal_all_tickets', methods=['POST'])
def api_reveal_all_tickets():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    success, msg = reveal_all_tickets(user_id)
    return jsonify({"success": success, "msg": msg})

@app.route('/api/reveal_ticket_cells', methods=['POST'])
def api_reveal_ticket_cells():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    ticket_number = data.get('ticket_number')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    with lottery_lock:
        if not is_drawn:
            return jsonify({"success": False, "msg": "Розыгрыш ещё не начался!"})
        for ticket in lottery_tickets:
            if ticket.get("user_id") == user_id and ticket.get("number") == ticket_number:
                revealed_count = 0
                for i in range(12):
                    if not ticket["revealed"][i]:
                        ticket["revealed"][i] = True
                        revealed_count += 1
                if revealed_count > 0:
                    save_lottery()
                    user = get_user(user_id)
                    add_log(f"🔓 Открыл все клетки билета #{ticket_number} ({revealed_count} клеток)", user_id,
                            user['username'])
                    return jsonify(
                        {"success": True, "msg": f"Билет #{ticket_number} полностью открыт! ({revealed_count} клеток)"})
                else:
                    return jsonify({"success": False, "msg": "В этом билете уже всё открыто"})
        return jsonify({"success": False, "msg": "Билет не найден"})

@app.route('/api/lottery_status', methods=['POST'])
def api_lottery_status():
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"error": "Invalid user_id"}), 400
    user = get_user(user_id)
    user_tickets = [t for t in lottery_tickets if t.get("user_id") == user_id]
    update_lottery_phase()
    return jsonify({"prize_pool": lottery_pool, "user_tickets": len(user_tickets), "user_lp": user["lp"],
                    "is_drawn": is_drawn, "winning_numbers": winning_numbers if is_drawn else [],
                    "tickets": user_tickets, "lottery_phase": lottery_phase})

@app.route('/api/user_tickets', methods=['POST'])
def api_user_tickets():
    data = request.json
    if not data:
        return jsonify({"tickets": []}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"tickets": []}), 400
    user = get_user(user_id)
    user_tickets = [t for t in lottery_tickets if t.get("user_id") == user_id]
    add_log(f"🎫👁️ Открыл список своих билетов (всего: {len(user_tickets)})", user_id, user['username'])
    return jsonify({"tickets": user_tickets})

@app.route('/api/recent_players', methods=['GET'])
def api_recent_players():
    if not check_rate_limit(f"recent_players_{request.remote_addr}", limit=20, window_seconds=60):
        return jsonify([]), 429
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
            players.append({"user_id": row['user_id'], "username": escape_html(display_name),
                            "avatar_url": row['avatar_url'] or '', "time_ago": time_ago,
                            "ticket_number": row['ticket_number'], "role": row['role'] if row['role'] else 'player'})
        return jsonify(players)

@app.route('/api/get_referral_link', methods=['POST'])
def api_get_referral_link():
    data = request.json
    if not data:
        return jsonify({"link": ""}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"link": ""}), 400
    user = get_user(user_id)
    with db.get_cursor() as cursor:
        cursor.execute("SELECT referral_code FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if row and row['referral_code']:
            code = row['referral_code']
        else:
            code = hashlib.md5(str(user_id).encode()).hexdigest()[:8]
            safe_update_user(user_id, referral_code=code)
    link = f"https://t.me/{BOT_USERNAME}/WereGood?startapp={code}"
    add_log(f"📨 Получил реферальную ссылку", user_id, user['username'])
    return jsonify({"link": link})


@app.route('/api/get_referrals', methods=['POST'])
def api_get_referrals():
    data = request.json
    if not data:
        return jsonify({"referrals": [], "total_earned_lp": 0, "total_earned_wg": 0}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"referrals": [], "total_earned_lp": 0, "total_earned_wg": 0}), 400

    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT 
                r.referred_id,
                r.username, 
                r.first_name, 
                r.created_at, 
                r.total_spent_lp, 
                r.total_earned_wg,
                u.avatar_url,
                u.total_clicks
            FROM referrals r
            LEFT JOIN users u ON r.referred_id = u.user_id
            WHERE r.referrer_id = ?
            ORDER BY r.created_at DESC
        ''', (user_id,))
        rows = cursor.fetchall()

        referrals = []
        total_earned_lp = 0
        total_earned_wg = 0

        for row in rows:
            name = row['username'] or row['first_name'] or "Игрок"
            earned_lp = (row['total_spent_lp'] or 0) * 0.05
            earned_wg = (row['total_earned_wg'] or 0)
            total_earned_lp += earned_lp
            total_earned_wg += earned_wg

            referrals.append({
                "user_id": row['referred_id'],  # ← ГЛАВНОЕ: добавил ID реферала
                "username": escape_html(name),
                "avatar_url": row['avatar_url'] or '',
                "date": row['created_at'],
                "spent_lp": row['total_spent_lp'] or 0,
                "earned_lp": round(earned_lp, 2),
                "earned_wg": round(earned_wg, 4),
                "total_clicks": row['total_clicks'] or 0  # ← РЕАЛЬНЫЕ КЛИКИ
            })

    return jsonify({
        "referrals": referrals,
        "total_earned_lp": round(total_earned_lp, 2),
        "total_earned_wg": round(total_earned_wg, 4)
    })

@app.route('/api/leaderboard', methods=['GET'])
def api_leaderboard():
    global leaderboard_cache, leaderboard_cache_time
    now = time.time()
    force_refresh = request.args.get('force', 'false').lower() == 'true'
    if not force_refresh and now - leaderboard_cache_time < LEADERBOARD_CACHE_TTL:
        return jsonify(leaderboard_cache)
    limit = int(request.args.get('limit', 50))
    limit = min(limit, 100)
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
                    settings = json.loads(row['settings'])
                    hide_from_top = settings.get('hideFromTop', False)
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
            result.append({"rank": i + 1, "user_id": row['user_id'], "username": escape_html(display_name),
                           "total_clicks": row['total_clicks'], "avatar": avatar,
                           "role": row['role'] if row['role'] else 'player', "hide_from_top": hide_from_top})
    leaderboard_cache = result
    leaderboard_cache_time = now
    return jsonify(result)

@app.route('/api/leaderboard/daily', methods=['GET'])
def api_daily_leaderboard():
    with db.get_cursor() as cursor:
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'daily_clicks' not in columns:
            return jsonify([])
        cursor.execute('''
            SELECT user_id, daily_clicks, username, first_name, avatar_url, role, settings
            FROM users 
            WHERE daily_clicks > 0
            ORDER BY daily_clicks DESC 
            LIMIT 50
        ''')
        rows = cursor.fetchall()
        rewards = {
            1: "🏆 70 LP + 5000 WG",
            2: "🥈 50 LP + 3000 WG",
            3: "🥉 35 LP + 1500 WG",
            4: "🎖️ 25 LP + 1000 WG",
            5: "🎖️ 15 LP + 500 WG"
        }
        result = []
        for i, row in enumerate(rows, 1):
            hide_from_top = False
            if row['settings']:
                try:
                    settings = json.loads(row['settings'])
                    hide_from_top = settings.get('hideFromTop', False)
                except:
                    pass
            if hide_from_top:
                display_name = 'Аноним'
                avatar = '👤'
            else:
                display_name = f"@{row['username']}" if row['username'] else (
                        row['first_name'] or f"Player_{row['user_id']}")
                avatar = row['avatar_url'] or '👤'
            result.append({
                "rank": i,
                "user_id": row['user_id'],
                "username": display_name,
                "daily_clicks": row['daily_clicks'],
                "avatar": avatar,
                "role": row['role'] or 'player',
                "reward": rewards.get(i, "")
            })
        return jsonify(result)

@app.route('/api/get_user_stats', methods=['POST'])
def api_get_user_stats():
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"error": "Invalid user_id"}), 400
    with db.get_cursor() as cursor:
        cursor.execute(
            '''SELECT wg, lp, total_clicks, likes, dislikes, username, first_name, upgrade_counts, avatar_url, usdt, wins, role, stars, max_energy, energy_upgrades, settings FROM users WHERE user_id=?''',
            (user_id,))
        row = cursor.fetchone()
        if row:
            hide_from_top = False
            if row['settings']:
                try:
                    settings = json.loads(row['settings'])
                    hide_from_top = settings.get('hideFromTop', False)
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
                 "dislikes": row['dislikes'] or 0, "username": escape_html(display_name),
                 "avatar_url": row['avatar_url'] or "👤", "usdt": row['usdt'] if 'usdt' in row.keys() else 0,
                 "wins": row['wins'] if 'wins' in row.keys() else 0,
                 "role": row['role'] if 'role' in row.keys() else 'player',
                 "stars": row['stars'] if 'stars' in row.keys() else 0,
                 "max_energy": row['max_energy'] if 'max_energy' in row.keys() else 500,
                 "energy_upgrades": row['energy_upgrades'] if 'energy_upgrades' in row.keys() else 0,
                 "hide_from_top": hide_from_top})
    return jsonify({"error": "Пользователь не найден"})

@app.route('/api/vote', methods=['POST'])
def api_vote():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    voter_id = data.get('voter_id')
    target_id = data.get('target_id')
    vote_type = data.get('vote_type')
    is_valid, voter_id = validate_user_id(voter_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid voter_id"}), 400
    is_valid, target_id = validate_user_id(target_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid target_id"}), 400
    if not check_rate_limit(f"vote_{voter_id}", limit=5, window_seconds=60):
        return jsonify({"success": False, "msg": "Слишком частые голосования"}), 429
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
            'INSERT INTO votes (voter_id, target_id, vote_type, last_vote_time) VALUES (?, ?, ?, ?) ON CONFLICT(voter_id, target_id) DO UPDATE SET vote_type=?, last_vote_time=?',
            (voter_id, target_id, vote_type, datetime.datetime.now().isoformat(), vote_type,
             datetime.datetime.now().isoformat()))
        if vote_type == 'like':
            cursor.execute("UPDATE users SET likes = likes + 1 WHERE user_id=?", (target_id,))
            invalidate_cache(target_id)
            add_log(f"👍 Поставил лайк игроку {target['username']}", voter_id, voter['username'])
            update_achievement_progress(voter_id, 'liker', 1)
        else:
            cursor.execute("UPDATE users SET dislikes = dislikes + 1 WHERE user_id=?", (target_id,))
            invalidate_cache(target_id)
            add_log(f"👎 Поставил дизлайк игроку {target['username']}", voter_id, voter['username'])
            update_achievement_progress(voter_id, 'hater', 1)
    return jsonify({"success": True, "msg": "Голос учтён!"})

@app.route('/api/update_settings', methods=['POST'])
def api_update_settings():
    data = request.json
    if not data:
        return jsonify({"success": False}), 400
    user_id = data.get('user_id')
    setting = data.get('setting')
    value = data.get('value')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False}), 400
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
        settings[setting] = value
        cursor.execute("UPDATE users SET settings=? WHERE user_id=?", (json.dumps(settings), user_id))
        invalidate_cache(user_id)
    add_log(f"⚙️ Изменил настройки: {setting}={value}", user_id, user['username'])
    return jsonify({"success": True})

@app.route('/api/get_settings', methods=['POST'])
def api_get_settings():
    data = request.json
    if not data:
        return jsonify(
            {"notifications": True, "theme": "dark", "sounds": True, "vibration": True, "hideFromTop": False}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify(
            {"notifications": True, "theme": "dark", "sounds": True, "vibration": True, "hideFromTop": False}), 400
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
            if key not in settings:
                settings[key] = default_value
    return jsonify(settings)

@app.route('/api/lottery_all_tickets', methods=['GET'])
def api_lottery_all_tickets():
    with db.get_cursor() as cursor:
        cursor.execute("SELECT tickets FROM lottery LIMIT 1")
        row = cursor.fetchone()
        if row and row['tickets']:
            tickets = json.loads(row['tickets']) if row['tickets'] else []
            result = []
            for ticket in tickets:
                user_id = ticket.get('user_id')
                cursor.execute("SELECT username, first_name, role FROM users WHERE user_id=?", (user_id,))
                user_row = cursor.fetchone()
                username = user_row['username'] if user_row and user_row['username'] else (
                    user_row['first_name'] if user_row else 'Игрок')
                ticket['username'] = escape_html(username)
                ticket['role'] = user_row['role'] if user_row else 'player'
                result.append(ticket)
            return jsonify({"tickets": result})
    return jsonify({"tickets": []})

@app.route('/api/create_stars_invoice', methods=['POST'])
def api_create_stars_invoice():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    chat_id = data.get('chat_id', user_id)
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    user = get_user(user_id)
    if user['energy_upgrades'] >= 15:
        return jsonify({"success": False, "msg": "Максимум улучшений! (15/15)"})
    invoice_link = create_stars_invoice(chat_id, user_id)
    if invoice_link:
        return jsonify({"success": True, "invoice_link": invoice_link})
    return jsonify({"success": False, "msg": "Ошибка создания счёта"})

@app.route('/api/get_stars_balance', methods=['POST'])
def api_get_stars_balance():
    data = request.json
    if not data:
        return jsonify({"success": True, "balance": 0}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": True, "balance": 0}), 400
    try:
        verify_ssl = not DEBUG_MODE
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getStarBalance"
        response = requests.get(url, timeout=10, verify=verify_ssl)
        result = response.json()
        if result.get("ok"):
            balance = result.get("result", {}).get("balance", 0)
            with db.get_cursor() as cursor:
                cursor.execute("UPDATE users SET stars = ? WHERE user_id=?", (balance, user_id))
                invalidate_cache(user_id)
            return jsonify({"success": True, "balance": balance})
    except Exception as e:
        logger.error(f"Ошибка получения баланса звезд: {e}")
    user = get_user(user_id)
    return jsonify({"success": True, "balance": user['stars']})

@app.route('/api/get_available_prefixes', methods=['POST'])
def api_get_available_prefixes():
    data = request.json
    if not data:
        return jsonify({"success": True, "prefixes": [
            {"id": "player", "name": "Игрок", "icon": "🎮", "desc": "Выдаётся абсолютно всем игрокам",
             "color": "player"}], "current": "player"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": True, "prefixes": [
            {"id": "player", "name": "Игрок", "icon": "🎮", "desc": "Выдаётся абсолютно всем игрокам",
             "color": "player"}], "current": "player"}), 400
    user = get_user(user_id)
    unlocked = user.get('unlocked_prefixes', ['player'])
    all_prefixes = {
        "player": {"name": "Игрок", "icon": "🎮", "desc": "Выдаётся абсолютно всем игрокам", "color": "player"},
        "pioneer": {"name": "Первооткрыватель", "icon": "⭐", "desc": "За регистрацию в первый день",
                    "color": "pioneer"},
        "founder": {"name": "Основатель", "icon": "👑", "desc": "Основатель проекта", "color": "founder"},
        "legend": {"name": "Легенда", "icon": "👑", "desc": "Топ-5 по достижениям", "color": "legend"}
    }
    prefixes = [{"id": pid, **all_prefixes[pid]} for pid in unlocked if pid in all_prefixes]
    return jsonify({"success": True, "prefixes": prefixes, "current": user['role']})

@app.route('/api/change_prefix', methods=['POST'])
def api_change_prefix():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    new_prefix = data.get('prefix')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    user = get_user(user_id)
    unlocked = user.get('unlocked_prefixes', ['player'])
    if new_prefix not in unlocked:
        return jsonify({"success": False, "msg": "Префикс не разблокирован"})
    old_role = user['role']
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET role = ? WHERE user_id=?", (new_prefix, user_id))
        invalidate_cache(user_id)
    add_log(f"👑 Сменил префикс с {old_role} на {new_prefix}", user_id, user['username'])
    return jsonify({"success": True, "msg": f"Префикс изменён на {new_prefix}"})

@app.route('/api/create_withdrawal', methods=['POST'])
def api_create_withdrawal():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    amount = data.get('amount')
    address = data.get('address')
    network = data.get('network')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    if amount < 2:
        return jsonify({"success": False, "msg": "Минимальная сумма вывода: 2 USDT"})
    if amount > 1000:
        return jsonify({"success": False, "msg": "Максимальная сумма вывода: 1000 USDT"})
    user = get_user(user_id)
    if user['usdt'] < amount:
        return jsonify({"success": False, "msg": "Недостаточно USDT на балансе"})
    try:
        safe_update_user(user_id, usdt=user['usdt'] - amount)
        create_withdrawal_request_db(user_id, user['username'], amount, address, network)
        return jsonify({"success": True, "msg": "Заявка на вывод создана! Ожидайте обработки администратором."})
    except ValueError as e:
        return jsonify({"success": False, "msg": str(e)}), 400

@app.route('/api/get_withdrawals', methods=['POST'])
def api_get_withdrawals():
    data = request.json
    if not data:
        return jsonify({"success": True, "withdrawals": []}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": True, "withdrawals": []}), 400
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
    if not data:
        return jsonify({"current_day": 1, "can_claim": True, "next_claim_time": None, "recovered_count": 0,
                        "lost_streak": False}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"current_day": 1, "can_claim": True, "next_claim_time": None, "recovered_count": 0,
                        "lost_streak": False}), 400
    status = get_daily_status(user_id)
    return jsonify(status)

@app.route('/api/claim_daily', methods=['POST'])
def api_claim_daily():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    result = claim_daily_reward(user_id)
    return jsonify(result)

@app.route('/api/recover_daily', methods=['POST'])
def api_recover_daily():
    data = request.json
    if not data:
        return jsonify({"success": False, "msg": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "msg": "Invalid user_id"}), 400
    result = recover_streak_with_stars(user_id)
    return jsonify(result)

@app.route('/api/get_tutorial_status', methods=['POST'])
def api_get_tutorial_status():
    data = request.json
    if not data:
        return jsonify({"completed": False}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"completed": False}), 400
    with db.get_cursor() as cursor:
        cursor.execute("SELECT tutorial_completed FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        completed = row['tutorial_completed'] if row else 0
    return jsonify({"completed": completed == 1})

@app.route('/api/complete_tutorial', methods=['POST'])
def api_complete_tutorial():
    data = request.json
    if not data:
        return jsonify({"success": False}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False}), 400
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET tutorial_completed = 1 WHERE user_id=?", (user_id,))
        invalidate_cache(user_id)
    add_log(f"🎓 Завершил обучение", user_id, str(user_id))
    return jsonify({"success": True})

# ========== ЗАДАНИЯ API ==========
@app.route('/api/tasks', methods=['GET'])
def api_get_tasks():
    user_id = request.args.get('user_id')

    if not user_id:
        return jsonify({'success': False, 'error': 'user_id required'}), 400

    try:
        user_id = int(user_id)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid user_id'}), 400

    with db.get_cursor() as cursor:
        # Проверяем, есть ли колонки task_type и miniapp_url
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [col[1] for col in cursor.fetchall()]
        has_task_type = 'task_type' in columns
        has_miniapp_url = 'miniapp_url' in columns

        cursor.execute('''
            SELECT t.*, 
                   CASE WHEN ut.id IS NOT NULL THEN 1 ELSE 0 END as is_completed
            FROM tasks t
            LEFT JOIN user_tasks ut ON t.id = ut.task_id AND ut.user_id = ?
            WHERE t.is_active = 1
            ORDER BY is_completed ASC, t.created_at DESC
        ''', (user_id,))
        rows = cursor.fetchall()

        tasks = []
        for row in rows:
            task = {
                'id': row['id'],
                'title': row['title'],
                'channel_link': row['channel_link'] or '',
                'channel_username': row['channel_username'] or '',
                'channel_avatar': row['channel_avatar'] or '',
                'reward_amount': row['reward_amount'],
                'reward_type': row['reward_type'],
                'daily_limit': row['daily_limit'],
                'total_limit': row['total_limit'],
                'completed_count': row['completed_count'],
                'days_remaining': row['days_remaining'],
                'is_completed': bool(row['is_completed'])
            }

            # ✅ ИСПРАВЛЕНО: используем индексы, а не .get()
            if has_task_type:
                task['task_type'] = row['task_type'] if row['task_type'] is not None else 'channel'
            else:
                task['task_type'] = 'channel'

            if has_miniapp_url:
                task['miniapp_url'] = row['miniapp_url'] or ''
            else:
                task['miniapp_url'] = ''

            tasks.append(task)

        return jsonify({'success': True, 'tasks': tasks})


@app.route('/api/check_task_subscription', methods=['POST'])
def api_check_task_subscription():
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        user_id = data.get('user_id')
        task_id = data.get('task_id')

        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({'success': False, 'error': 'Invalid user_id'}), 400

        with db.get_cursor() as cursor:
            cursor.execute('SELECT * FROM tasks WHERE id = ? AND is_active = 1', (task_id,))
            task = cursor.fetchone()
            if not task:
                return jsonify({'success': False, 'error': 'Задание не найдено'}), 404

            if task['completed_count'] >= task['total_limit']:
                return jsonify({'success': False, 'error': 'Задание больше недоступно'}), 400

            cursor.execute('SELECT * FROM user_tasks WHERE user_id = ? AND task_id = ?', (user_id, task_id))
            if cursor.fetchone():
                return jsonify({'success': False, 'error': 'Вы уже получили награду за это задание'}), 400

            # ========== ПРОВЕРЯЕМ НАЛИЧИЕ КОЛОНОК ==========
            cursor.execute("PRAGMA table_info(tasks)")
            columns = [col[1] for col in cursor.fetchall()]
            has_task_type = 'task_type' in columns

            # ✅ ИСПРАВЛЕНО: используем прямой доступ к колонкам, а не .get()
            if has_task_type:
                task_type = task['task_type'] if task['task_type'] is not None else 'channel'
            else:
                task_type = 'channel'

            if task_type == 'channel':
                # Проверка подписки на канал
                channel_username = task['channel_username'].replace('@', '')
                if not channel_username:
                    return jsonify({'success': False, 'error': 'Не указан канал'}), 400

                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMember"
                try:
                    response = requests.get(url, params={
                        'chat_id': f'@{channel_username}',
                        'user_id': user_id
                    }, timeout=10)
                    data = response.json()
                    if data.get('ok'):
                        status = data.get('result', {}).get('status', '')
                        if status not in ['member', 'administrator', 'creator']:
                            return jsonify({'success': False, 'error': 'Вы не подписаны на канал'})
                    else:
                        return jsonify({'success': False, 'error': 'Не удалось проверить подписку'})
                except Exception as e:
                    logger.error(f"Ошибка проверки подписки: {e}")
                    return jsonify({'success': False, 'error': 'Ошибка при проверке'}), 500

            elif task_type == 'miniapp':
                # Проверка открытия Mini App
                cursor.execute('''
                    SELECT * FROM task_miniapp_clicks 
                    WHERE user_id = ? AND task_id = ? 
                    AND clicked_at > datetime('now', '-5 minutes')
                ''', (user_id, task_id))
                click = cursor.fetchone()

                if not click:
                    return jsonify({'success': False, 'error': 'Откройте Mini App по ссылке и вернитесь через 2-3 секунды'})

            else:
                return jsonify({'success': False, 'error': 'Неизвестный тип задания'}), 400

            # ========== ВЫДАЁМ НАГРАДУ ==========
            user = get_user(user_id)
            old_value = None
            new_value = None

            if task['reward_type'] == 'wg':
                old_value = user['wg']
                new_value = old_value + task['reward_amount']
                safe_update_user(user_id, wg=new_value)
            elif task['reward_type'] == 'lp':
                old_value = user['lp']
                new_value = old_value + task['reward_amount']
                safe_update_user(user_id, lp=new_value)
            elif task['reward_type'] == 'usdt':
                old_value = user['usdt']
                new_value = old_value + task['reward_amount']
                safe_update_user(user_id, usdt=new_value)
            elif task['reward_type'] == 'energy':
                current_energy, _ = calculate_energy(user)
                new_energy = min(user['max_energy'], current_energy + task['reward_amount'])
                update_energy_in_db(user_id, user, new_energy)

            # Записываем выполнение
            cursor.execute('INSERT INTO user_tasks (user_id, task_id) VALUES (?, ?)', (user_id, task_id))
            cursor.execute('UPDATE tasks SET completed_count = completed_count + 1 WHERE id = ?', (task_id,))

            # Обновляем достижение
            update_achievement_progress(user_id, 'task_master', 1)

            # Логируем
            reward_names = {'wg': 'WG', 'lp': 'LP', 'usdt': 'USDT', 'energy': 'энергии'}
            add_log(
                f"📋 Выполнил задание '{task['title']}' | +{task['reward_amount']} {reward_names.get(task['reward_type'], task['reward_type'])}",
                user_id,
                user.get('username') or f"User_{user_id}"
            )

            return jsonify({
                'success': True,
                'message': f'✅ Вы получили +{task["reward_amount"]} {task["reward_type"].upper()}!',
                'reward': {'type': task['reward_type'], 'amount': task['reward_amount']}
            })

    except Exception as e:
        logger.error(f"Ошибка в check_task_subscription: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/log_miniapp_click', methods=['POST'])
def api_log_miniapp_click():
    data = request.json
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    user_id = data.get('user_id')
    task_id = data.get('task_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({'success': False, 'error': 'Invalid user_id'}), 400

    with db.get_cursor() as cursor:
        # Проверяем, существует ли задание
        cursor.execute('SELECT id FROM tasks WHERE id = ? AND is_active = 1', (task_id,))
        if not cursor.fetchone():
            return jsonify({'success': False, 'error': 'Задание не найдено'}), 404

        # Проверяем, не выполнил ли уже задание
        cursor.execute('SELECT id FROM user_tasks WHERE user_id = ? AND task_id = ?', (user_id, task_id))
        if cursor.fetchone():
            return jsonify({'success': False, 'error': 'Вы уже выполнили это задание'}), 400

        # Записываем клик
        cursor.execute('''
            INSERT OR REPLACE INTO task_miniapp_clicks (user_id, task_id, clicked_at)
            VALUES (?, ?, datetime('now'))
        ''', (user_id, task_id))

    return jsonify({'success': True, 'message': 'Клик зафиксирован'})
# ========== ДОСТИЖЕНИЯ API ==========
@app.route('/api/achievements/list', methods=['POST'])
def api_achievements_list():
    data = request.json
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    achievements, completed_count = get_user_achievements(user_id)
    all_achievements = get_achievements_list()
    return jsonify({
        "success": True,
        "achievements": achievements,
        "completed_count": completed_count,
        "total_count": len(all_achievements)
    })

@app.route('/api/achievements/top', methods=['GET'])
def api_achievements_top():
    limit = int(request.args.get('limit', 50))
    top = get_achievements_top(limit)
    return jsonify({"success": True, "top": top})


@app.route('/api/check_miniapp_click', methods=['POST'])
def api_check_miniapp_click():
    data = request.json
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    user_id = data.get('user_id')
    task_id = data.get('task_id')

    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({'success': False, 'error': 'Invalid user_id'}), 400

    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT * FROM task_miniapp_clicks 
            WHERE user_id = ? AND task_id = ? 
            AND clicked_at > datetime('now', '-5 minutes')
        ''', (user_id, task_id))
        click = cursor.fetchone()

        if click:
            return jsonify({'success': True, 'message': 'Mini App открыт'})
        else:
            return jsonify({'success': False, 'error': 'Откройте Mini App по ссылке и вернитесь через 2-3 секунды'})

# ========== АДМИН-ЭНДПОИНТЫ ==========
@app.route('/api/admin/stats', methods=['GET'])
@require_admin
def api_admin_stats():
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
        with online_users_lock:
            online_count = len(online_users)
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
                        "lottery_pool": lottery_pool, "is_drawn": is_drawn, "online": online_count})

@app.route('/api/admin/lottery_participants', methods=['GET'])
@require_admin
def api_admin_lottery_participants():
    participants = []
    for ticket in lottery_tickets:
        participants.append({"user_id": ticket.get('user_id'), "ticket_number": ticket.get('number'),
                             "purchase_number": ticket.get('purchase_number'),
                             "revealed_count": sum(ticket.get('revealed', [])), "numbers": ticket.get('numbers', [])})
    return jsonify({"success": True, "participants": participants, "count": len(participants)})

@app.route('/api/admin/logs', methods=['GET'])
@require_admin
def api_admin_logs():
    log_type = request.args.get('type', 'all')
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))
    date = request.args.get('date', '')
    action_filter = request.args.get('action', '')
    user_id_filter = request.args.get('user_id', '')
    logs, total = get_logs(log_type, limit, offset, date, action_filter, user_id_filter)
    return jsonify({"success": True, "logs": logs, "total": total, "offset": offset, "limit": limit})

@app.route('/api/admin/withdrawals', methods=['GET'])
@require_admin
def api_admin_withdrawals():
    withdrawals = get_withdrawal_requests_db()
    return jsonify({"success": True, "withdrawals": withdrawals})

@app.route('/api/admin/reset_ad_limits', methods=['POST'])
@require_admin
def api_admin_reset_ad_limits():
    try:
        with db.get_cursor() as cursor:
            cursor.execute("DELETE FROM ad_watch_history WHERE date(watched_at) = date('now')")
            deleted_count = cursor.rowcount
            cursor.execute("DELETE FROM ad_watch_history WHERE watched_at < datetime('now', '-7 days')")
            old_deleted = cursor.rowcount
        admin_id = request.args.get('user_id', 'Admin')
        admin_name = "Admin"
        add_admin_log(
            f"🔄 СБРОС ЛИМИТОВ РЕКЛАМЫ: удалено {deleted_count} записей за сегодня, очищено старых записей: {old_deleted}",
            admin_id, admin_name)
        return jsonify({
            "success": True,
            "message": f"Лимиты рекламы сброшены для всех игроков! Удалено {deleted_count} записей за сегодня.",
            "deleted_today": deleted_count,
            "deleted_old": old_deleted
        })
    except Exception as e:
        logger.error(f"Ошибка сброса лимитов рекламы: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/reset_ad_limits_user', methods=['POST'])
@require_admin
def api_admin_reset_ad_limits_user():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON"}), 400
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    try:
        with db.get_cursor() as cursor:
            cursor.execute("DELETE FROM ad_watch_history WHERE user_id = ? AND date(watched_at) = date('now')", (user_id,))
            deleted_count = cursor.rowcount
        admin_id = request.args.get('user_id', 'Admin')
        admin_name = "Admin"
        user = get_user(user_id)
        username = user.get('username') or user.get('first_name') or str(user_id)
        add_admin_log(f"🔄 СБРОС ЛИМИТОВ РЕКЛАМЫ для игрока {username} (ID: {user_id}): удалено {deleted_count} записей",
                      admin_id, admin_name, user_id, username)
        return jsonify({
            "success": True,
            "message": f"Лимиты рекламы сброшены для игрока {username}! Удалено {deleted_count} записей за сегодня.",
            "deleted": deleted_count
        })
    except Exception as e:
        logger.error(f"Ошибка сброса лимитов рекламы для пользователя {user_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/search_users', methods=['POST'])
@require_admin
def api_admin_search_users():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON"}), 400
    query = data.get('query', '')
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT user_id, username, first_name, role, wg, lp, usdt, wins, total_clicks, stars, max_energy, energy_upgrades, unlocked_prefixes FROM users WHERE user_id LIKE ? OR username LIKE ? OR first_name LIKE ? LIMIT 50",
            (f'%{query}%', f'%{query}%', f'%{query}%'))
        rows = cursor.fetchall()
        users = []
        for row in rows:
            banned, _ = is_banned(row['user_id'])
            users.append({"user_id": row['user_id'],
                          "username": escape_html(row['username'] or row['first_name'] or str(row['user_id'])),
                          "role": row['role'] or 'player', "wg": round(row['wg'] or 0, 2), "lp": int(row['lp'] or 0),
                          "usdt": round(row['usdt'] or 0, 2), "wins": int(row['wins'] or 0),
                          "total_clicks": int(row['total_clicks'] or 0), "stars": int(row['stars'] or 0),
                          "max_energy": int(row['max_energy'] or 500),
                          "energy_upgrades": int(row['energy_upgrades'] or 0),
                          "unlocked_prefixes": json.loads(row['unlocked_prefixes']) if row['unlocked_prefixes'] else ["player"], "is_banned": banned})
    return jsonify({"success": True, "users": users})

@app.route('/api/admin/get_user', methods=['POST'])
@require_admin
def api_admin_get_user():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON"}), 400
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    user = get_user(user_id)
    banned, ban_info = is_banned(user_id)
    user['is_banned'] = banned
    if banned:
        user['ban_info'] = ban_info
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT r.username, r.first_name, r.created_at, r.total_spent_lp FROM referrals r WHERE r.referrer_id = ?",
            (user_id,))
        referrals = cursor.fetchall()
        user['referrals'] = [
            {"username": escape_html(r['username'] or r['first_name'] or 'Игрок'), "date": r['created_at'],
             "spent_lp": r['total_spent_lp'] or 0, "earned": round((r['total_spent_lp'] or 0) * 0.05, 2)} for r in referrals]
    user['personal_logs'], _ = get_logs('all', 100, 0, None, None, str(user_id))
    return jsonify({"success": True, "user": user})

@app.route('/api/admin/update_user', methods=['POST'])
@require_admin
def api_admin_update_user():
    if not check_rate_limit(f"admin_update", limit=60, window_seconds=60):
        return jsonify({"success": False, "error": "Too many admin requests"}), 429
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON"}), 400
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    action_type = data.get('action_type')
    amount = data.get('amount', 0)
    user = get_user(user_id)
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"
    if action_type == 'add_wg':
        old_value = user['wg']
        new_value = old_value + amount
        safe_update_user(user_id, wg=new_value)
        add_admin_log(f"💰 Добавил {amount} WG", admin_id, admin_name, user_id, user['username'],
                      f"Баланс WG: {old_value:.2f} → {new_value:.2f}")
    elif action_type == 'remove_wg':
        old_value = user['wg']
        new_value = max(0, old_value - amount)
        safe_update_user(user_id, wg=new_value)
        add_admin_log(f"📉 Отнял {amount} WG", admin_id, admin_name, user_id, user['username'],
                      f"Баланс WG: {old_value:.2f} → {new_value:.2f}")
    elif action_type == 'add_lp':
        old_value = user['lp']
        new_value = old_value + amount
        safe_update_user(user_id, lp=new_value)
        add_admin_log(f"🎯 Добавил {amount} LP", admin_id, admin_name, user_id, user['username'],
                      f"Баланс LP: {old_value} → {new_value}")
    elif action_type == 'remove_lp':
        old_value = user['lp']
        new_value = max(0, old_value - amount)
        safe_update_user(user_id, lp=new_value)
        add_admin_log(f"📉 Отнял {amount} LP", admin_id, admin_name, user_id, user['username'],
                      f"Баланс LP: {old_value} → {new_value}")
    elif action_type == 'add_usdt':
        old_value = user['usdt']
        new_value = old_value + amount
        safe_update_user(user_id, usdt=new_value)
        add_admin_log(f"💰 Добавил {amount} USDT", admin_id, admin_name, user_id, user['username'],
                      f"Баланс USDT: {old_value:.2f} → {new_value:.2f}")
    elif action_type == 'remove_usdt':
        old_value = user['usdt']
        new_value = max(0, old_value - amount)
        safe_update_user(user_id, usdt=new_value)
        add_admin_log(f"📉 Отнял {amount} USDT", admin_id, admin_name, user_id, user['username'],
                      f"Баланс USDT: {old_value:.2f} → {new_value:.2f}")
    elif action_type == 'add_stars':
        old_value = user['stars']
        new_value = old_value + amount
        safe_update_user(user_id, stars=new_value)
        add_admin_log(f"⭐ Добавил {amount} Stars", admin_id, admin_name, user_id, user['username'],
                      f"Баланс Stars: {old_value} → {new_value}")
    elif action_type == 'remove_stars':
        old_value = user['stars']
        new_value = max(0, old_value - amount)
        safe_update_user(user_id, stars=new_value)
        add_admin_log(f"⭐ Отнял {amount} Stars", admin_id, admin_name, user_id, user['username'],
                      f"Баланс Stars: {old_value} → {new_value}")
    elif action_type == 'add_energy':
        old_value = user['energy']
        new_value = min(user['max_energy'], old_value + amount)
        update_energy_in_db(user_id, user, new_value)
        add_admin_log(f"⚡ Добавил {amount} энергии", admin_id, admin_name, user_id, user['username'],
                      f"Энергия: {old_value} → {new_value}")
    elif action_type == 'remove_energy':
        old_value = user['energy']
        new_value = max(0, old_value - amount)
        update_energy_in_db(user_id, user, new_value)
        add_admin_log(f"⚡ Отнял {amount} энергии", admin_id, admin_name, user_id, user['username'],
                      f"Энергия: {old_value} → {new_value}")
    elif action_type == 'add_max_energy':
        old_value = user['max_energy']
        new_value = old_value + amount
        safe_update_user(user_id, max_energy=new_value)
        add_admin_log(f"⚡ Увеличил макс. энергию на {amount}", admin_id, admin_name, user_id, user['username'],
                      f"Макс. энергия: {old_value} → {new_value}")
    elif action_type == 'remove_max_energy':
        old_value = user['max_energy']
        new_value = max(100, old_value - amount)
        safe_update_user(user_id, max_energy=new_value)
        add_admin_log(f"⚡ Уменьшил макс. энергию на {amount}", admin_id, admin_name, user_id, user['username'],
                      f"Макс. энергия: {old_value} → {new_value}")
    elif action_type == 'add_clicks':
        old_value = user['total_clicks']
        new_value = old_value + amount
        safe_update_user(user_id, total_clicks=new_value)
        add_admin_log(f"👆 Добавил {amount} кликов", admin_id, admin_name, user_id, user['username'],
                      f"Клики: {old_value} → {new_value}")
    elif action_type == 'remove_clicks':
        old_value = user['total_clicks']
        new_value = max(0, old_value - amount)
        safe_update_user(user_id, total_clicks=new_value)
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
            old_role = user['role']
            safe_update_user(user_id, role=new_role)
            add_admin_log(f"👑 Изменил роль с {old_role} на {new_role}", admin_id, admin_name, user_id, user['username'])
    elif action_type == 'reset_energy':
        old_value = user['energy']
        new_value = user['max_energy']
        update_energy_in_db(user_id, user, new_value)
        add_admin_log(f"⚡ Сбросил энергию до максимума", admin_id, admin_name, user_id, user['username'],
                      f"Энергия: {old_value} → {new_value}")
    elif action_type == 'ban':
        days = data.get('days', 7)
        reason = data.get('reason', 'Нарушение правил')
        ban_user(user_id, days, reason, admin_id)
        add_admin_log(f"🔨 ЗАБАНИЛ игрока на {days} дней. Причина: {reason}", admin_id, admin_name, user_id, user['username'])
    elif action_type == 'unban':
        unban_user(user_id)
        add_admin_log(f"🔓 РАЗБАНИЛ игрока", admin_id, admin_name, user_id, user['username'])
    elif action_type == 'process_withdrawal':
        withdrawal_id = data.get('withdrawal_id')
        status = data.get('status')
        if withdrawal_id and status in ['completed', 'rejected']:
            process_withdrawal_db(withdrawal_id, status, admin_id, admin_name)
            add_admin_log(f"💸 Обработал заявку на вывод #{withdrawal_id} - {status}", admin_id, admin_name)
    return jsonify({"success": True, "msg": "Обновлено"})

@app.route('/api/admin/manage_energy_upgrades', methods=['POST'])
@require_admin
def api_admin_manage_energy_upgrades():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON"}), 400
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    action = data.get('action')
    amount = int(data.get('amount', 1))
    give_reward = data.get('give_reward', False)
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"
    try:
        user = get_user(user_id)
        current_upgrades = user.get('energy_upgrades', 0)
        if action == 'add':
            new_upgrades = min(current_upgrades + amount, 15)
            added = new_upgrades - current_upgrades
        elif action == 'remove':
            new_upgrades = max(0, current_upgrades - amount)
            added = new_upgrades - current_upgrades
        else:
            return jsonify({"success": False, "error": "Invalid action"}), 400
        if added == 0:
            if action == 'add' and current_upgrades >= 15:
                return jsonify({"success": False, "error": "У игрока уже максимум покупок (15/15)"}), 400
            elif action == 'remove' and current_upgrades <= 0:
                return jsonify({"success": False, "error": "У игрока уже 0 покупок"}), 400
        new_max_energy = 500 + (new_upgrades * 40)
        reward_applied = False
        reward_text = ""
        if action == 'add' and give_reward and added > 0:
            lp_reward = added * 50
            old_lp = user['lp']
            new_lp = old_lp + lp_reward
            safe_update_user(user_id, lp=new_lp, max_energy=new_max_energy, energy_upgrades=new_upgrades)
            reward_applied = True
            reward_text = f", выдано {lp_reward} LP"
            add_log(f"👑 Админ добавил {added} покупок усилителя (+{lp_reward} LP)", user_id,
                    user.get('username') or f"User_{user_id}", old_value=old_lp, new_value=new_lp, currency="lp")
        elif action == 'add' and not give_reward and added > 0:
            safe_update_user(user_id, max_energy=new_max_energy, energy_upgrades=new_upgrades)
            reward_text = " (без выдачи наград)"
            add_admin_log(f"➕ Добавил {added} покупок усилителя игроку (без выдачи наград)", admin_id, admin_name, user_id, user.get('username') or f"User_{user_id}")
        elif action == 'remove':
            safe_update_user(user_id, max_energy=new_max_energy, energy_upgrades=new_upgrades)
            add_admin_log(f"➖ Убавил {abs(added)} покупок усилителя у игрока", admin_id, admin_name, user_id, user.get('username') or f"User_{user_id}")
        invalidate_cache(user_id)
        updated_user = get_user(user_id, force_refresh=True)
        return jsonify({
            "success": True,
            "message": f"Покупки изменены: {current_upgrades} → {new_upgrades}/15{reward_text}",
            "old_upgrades": current_upgrades,
            "new_upgrades": new_upgrades,
            "new_max_energy": new_max_energy,
            "reward_applied": reward_applied
        })
    except Exception as e:
        logger.error(f"Ошибка управления покупками: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/clear_wallets', methods=['POST'])
@require_admin
def api_clear_wallets():
    try:
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET ton_wallet = ''")
            count = cursor.rowcount
        add_admin_log(f"🗑️ Очистил все TON кошельки игроков (удалено {count} записей)",
                      request.args.get('user_id', 'Admin'), "Admin")
        return jsonify({"success": True, "count": count})
    except Exception as e:
        logger.error(f"Ошибка очистки кошельков: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/clear_user_wallet', methods=['POST'])
@require_admin
def api_clear_user_wallet():
    data = request.json
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    try:
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET ton_wallet = '' WHERE user_id = ?", (user_id,))
        invalidate_cache(user_id)
        add_admin_log(f"🗑️ Отвязал TON кошелёк у игрока", request.args.get('user_id', 'Admin'), "Admin", user_id)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Ошибка очистки кошелька: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/reset_daily_clicks', methods=['POST'])
@require_admin
def api_admin_reset_daily_clicks():
    try:
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET daily_clicks = 0")
            count = cursor.rowcount
        add_admin_log(f"🔄 СБРОС ТОПА ДНЯ: обнулил daily_clicks у {count} игроков",
                      request.args.get('user_id', 'Admin'), "Admin")
        return jsonify({"success": True, "message": f"Топ дня сброшен! Обнулено {count} игроков", "count": count})
    except Exception as e:
        logger.error(f"Ошибка сброса daily_clicks: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/delete_user', methods=['POST'])
@require_admin
def api_admin_delete_user():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON"}), 400
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    confirm = data.get('confirm', False)
    if not confirm:
        return jsonify({"success": False, "error": "Подтвердите удаление (confirm=true)"}), 400
    try:
        user = get_user(user_id)
        username = user.get('username') or user.get('first_name') or str(user_id)
        delete_user(user_id)
        admin_id = request.args.get('user_id', 'Admin')
        admin_name = "Admin"
        add_admin_log(f"🗑️ ПОЛНОСТЬЮ УДАЛИЛ пользователя {username} (ID: {user_id}) из БД", admin_id, admin_name)
        return jsonify({"success": True, "message": f"Пользователь {username} удалён"})
    except Exception as e:
        logger.error(f"Ошибка удаления пользователя: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/lottery_action', methods=['POST'])
@require_admin
def api_admin_lottery_action():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON"}), 400
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
@require_admin
def api_admin_chart_data():
    period = request.args.get('period', 'week')
    metric = request.args.get('metric', 'clicks')
    result = get_stats_history(period, metric)
    return jsonify({"success": True, "labels": result["labels"], "data": result["data"], "metric": metric, "period": period})


@app.route('/api/admin/tasks', methods=['GET'])
@require_admin
def api_admin_get_tasks():
    try:
        with db.get_cursor() as cursor:
            # Проверяем, есть ли колонка task_type
            cursor.execute("PRAGMA table_info(tasks)")
            columns = [col[1] for col in cursor.fetchall()]
            has_task_type = 'task_type' in columns
            has_miniapp_url = 'miniapp_url' in columns

            cursor.execute('SELECT * FROM tasks ORDER BY created_at DESC')
            rows = cursor.fetchall()
            tasks = []
            for row in rows:
                # ✅ Преобразуем Row в словарь безопасно
                task = {
                    'id': row['id'],
                    'title': row['title'] or '',
                    'channel_link': row['channel_link'] or '',
                    'channel_username': row['channel_username'] or '',
                    'channel_avatar': row['channel_avatar'] or '',
                    'reward_amount': row['reward_amount'],
                    'reward_type': row['reward_type'],
                    'daily_limit': row['daily_limit'],
                    'total_limit': row['total_limit'],
                    'completed_count': row['completed_count'],
                    'days_remaining': row['days_remaining'],
                    'is_active': bool(row['is_active'])
                }

                # ✅ Безопасно добавляем новые поля
                if has_task_type:
                    task['task_type'] = row['task_type'] if row['task_type'] is not None else 'channel'
                else:
                    task['task_type'] = 'channel'

                if has_miniapp_url:
                    task['miniapp_url'] = row['miniapp_url'] or ''
                else:
                    task['miniapp_url'] = ''

                tasks.append(task)

            return jsonify({'success': True, 'tasks': tasks})

    except Exception as e:
        logger.error(f"Ошибка в api_admin_get_tasks: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/create_task', methods=['POST'])
@require_admin
def api_admin_create_task():
    data = request.json
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    title = data.get('title', '').strip()
    task_type = data.get('task_type', 'channel')
    miniapp_url = data.get('miniapp_url', '').strip()
    channel_link = data.get('channel_link', '').strip()
    channel_username = data.get('channel_username', '').strip()
    channel_avatar = data.get('channel_avatar', '').strip()
    reward_amount = int(data.get('reward_amount', 10))
    reward_type = data.get('reward_type', 'wg')
    daily_limit = int(data.get('daily_limit', 1))
    total_limit = int(data.get('total_limit', 100))
    days_remaining = int(data.get('days_remaining', 7))
    is_active = data.get('is_active', True)

    # ========== ВАЛИДАЦИЯ В ЗАВИСИМОСТИ ОТ ТИПА ==========
    if not title:
        return jsonify({'success': False, 'error': 'Название задания обязательно'}), 400

    if task_type == 'channel':
        if not channel_link or not channel_username:
            return jsonify({'success': False, 'error': 'Для задания "Канал" нужны ссылка и username'}), 400
    elif task_type == 'miniapp':
        if not miniapp_url:
            return jsonify({'success': False, 'error': 'Для задания "Mini App" нужна ссылка'}), 400
    else:
        return jsonify({'success': False, 'error': 'Неизвестный тип задания'}), 400

    if reward_amount <= 0:
        return jsonify({'success': False, 'error': 'Сумма награды должна быть больше 0'}), 400

    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"

    with db.get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO tasks (
                title, task_type, miniapp_url, channel_link, channel_username, channel_avatar,
                reward_amount, reward_type, daily_limit, total_limit, days_remaining, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            title, task_type, miniapp_url, channel_link, channel_username, channel_avatar,
            reward_amount, reward_type, daily_limit, total_limit, days_remaining, 1 if is_active else 0
        ))
        task_id = cursor.lastrowid

    add_admin_log(
        f"📋 Создал задание '{title}' (ID: {task_id}, тип: {task_type})",
        admin_id, admin_name
    )
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/admin/update_task', methods=['POST'])
@require_admin
def api_admin_update_task():
    data = request.json
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    task_id = data.get('task_id')
    if not task_id:
        return jsonify({'success': False, 'error': 'task_id required'}), 400

    title = data.get('title', '').strip()
    task_type = data.get('task_type', 'channel')
    miniapp_url = data.get('miniapp_url', '').strip()
    channel_link = data.get('channel_link', '').strip()
    channel_username = data.get('channel_username', '').strip()
    channel_avatar = data.get('channel_avatar', '').strip()
    reward_amount = int(data.get('reward_amount', 10))
    reward_type = data.get('reward_type', 'wg')
    daily_limit = int(data.get('daily_limit', 1))
    total_limit = int(data.get('total_limit', 100))
    days_remaining = int(data.get('days_remaining', 7))
    is_active = data.get('is_active', True)

    # ========== ВАЛИДАЦИЯ ==========
    if not title:
        return jsonify({'success': False, 'error': 'Название задания обязательно'}), 400

    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"

    with db.get_cursor() as cursor:
        # Проверяем, существует ли задание
        cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not cursor.fetchone():
            return jsonify({'success': False, 'error': 'Задание не найдено'}), 404

        cursor.execute('''
            UPDATE tasks SET 
                title = ?, task_type = ?, miniapp_url = ?, channel_link = ?, 
                channel_username = ?, channel_avatar = ?,
                reward_amount = ?, reward_type = ?, daily_limit = ?, 
                total_limit = ?, days_remaining = ?, is_active = ?
            WHERE id = ?
        ''', (
            title, task_type, miniapp_url, channel_link, channel_username, channel_avatar,
            reward_amount, reward_type, daily_limit, total_limit, days_remaining,
            1 if is_active else 0, task_id
        ))

    add_admin_log(f"📋 Обновил задание ID: {task_id}", admin_id, admin_name)
    return jsonify({'success': True})

@app.route('/api/admin/delete_task', methods=['POST'])
@require_admin
def api_admin_delete_task():
    data = request.json
    task_id = data.get('task_id')
    if not task_id:
        return jsonify({'success': False, 'error': 'task_id required'}), 400
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"
    with db.get_cursor() as cursor:
        cursor.execute('DELETE FROM user_tasks WHERE task_id = ?', (task_id,))
        cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
    add_admin_log(f"📋 Удалил задание ID: {task_id}", admin_id, admin_name)
    return jsonify({'success': True})

@app.route('/api/admin/promo_codes', methods=['GET'])
@require_admin
def api_admin_get_promo_codes():
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT p.*, COUNT(a.id) as used_count, GROUP_CONCAT(a.user_id || ':' || a.activated_at) as activations
            FROM promo_codes p
            LEFT JOIN promo_activations a ON p.id = a.promo_id
            GROUP BY p.id
            ORDER BY p.created_at DESC
        ''')
        rows = cursor.fetchall()
        promos = []
        for row in rows:
            activations_list = []
            if row['activations']:
                for act in row['activations'].split(','):
                    parts = act.split(':')
                    if len(parts) >= 2:
                        activations_list.append({"user_id": int(parts[0]), "activated_at": parts[1]})
            promos.append({"id": row['id'], "code": row['code'], "reward_type": row['reward_type'],
                           "reward_amount": row['reward_amount'], "max_uses": row['max_uses'],
                           "used_count": row['used_count'] or 0, "has_password": bool(row['password']),
                           "created_by": row['created_by'], "created_at": row['created_at'],
                           "expires_at": row['expires_at'], "is_active": row['is_active'],
                           "activations": activations_list})
        return jsonify({"success": True, "promo_codes": promos})

@app.route('/api/admin/create_promo', methods=['POST'])
@require_admin
def api_admin_create_promo():
    data = request.json
    code = data.get('code', '').upper().strip()
    reward_type = data.get('reward_type')
    reward_amount = int(data.get('reward_amount', 0))
    max_uses = int(data.get('max_uses', 1))
    password = data.get('password', '').strip() or None
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"
    if not code or not reward_type or reward_amount <= 0 or max_uses <= 0:
        return jsonify({"success": False, "error": "Invalid parameters"}), 400
    if reward_type not in ['wg', 'lp', 'energy_limit']:
        return jsonify({"success": False, "error": "Invalid reward type"}), 400
    with db.get_cursor() as cursor:
        cursor.execute(
            'INSERT INTO promo_codes (code, reward_type, reward_amount, max_uses, password, created_by) VALUES (?, ?, ?, ?, ?, ?)',
            (code, reward_type, reward_amount, max_uses, password, admin_id))
        promo_id = cursor.lastrowid
    add_admin_log(
        f"🎫 Создал промокод {code} | {reward_type}: {reward_amount} | Макс: {max_uses} | Пароль: {'Да' if password else 'Нет'}",
        admin_id, admin_name, details=f"ID: {promo_id}")
    telegram_url = f"https://t.me/WereGooodbot/WereGood?startapp=claim_{code}"
    web_url = f"https://weregood.ru/claim?code={code}"
    return jsonify({"success": True, "promo_id": promo_id, "code": code, "promo_url": telegram_url, "web_url": web_url})

@app.route('/api/admin/delete_promo', methods=['POST'])
@require_admin
def api_admin_delete_promo():
    data = request.json
    promo_id = data.get('promo_id')
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"
    with db.get_cursor() as cursor:
        cursor.execute("SELECT code FROM promo_codes WHERE id = ?", (promo_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Promo code not found"}), 404
        cursor.execute("DELETE FROM promo_activations WHERE promo_id = ?", (promo_id,))
        cursor.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
    add_admin_log(f"🗑️ Удалил промокод {row['code']}", admin_id, admin_name)
    return jsonify({"success": True})


@app.route('/api/admin/contest_stats', methods=['GET'])
@require_admin
def api_admin_contest_stats():
    """Полная статистика конкурса для админки"""
    try:
        contest_start = "2026-06-14 14:00:00"

        with db.get_cursor() as cursor:
            # Статистика
            cursor.execute('''
                SELECT 
                    COUNT(DISTINCT referrer_id) as participants,
                    COUNT(*) as total_referrals,
                    SUM(CASE WHEN r.created_at >= ? THEN 1 ELSE 0 END) as new_referrals
                FROM referrals r
            ''', (contest_start,))
            stats_row = cursor.fetchone()

            # Топ участников
            cursor.execute('''
                SELECT 
                    u.user_id,
                    u.username,
                    u.first_name,
                    u.avatar_url,
                    u.role,
                    COUNT(r.id) as total_referrals,
                    SUM(CASE WHEN r.created_at >= ? THEN 1 ELSE 0 END) as new_referrals,
                    SUM(CASE WHEN r.created_at >= ? AND u2.total_clicks >= 300 THEN 1 ELSE 0 END) as completed_referrals
                FROM users u
                LEFT JOIN referrals r ON r.referrer_id = u.user_id
                LEFT JOIN users u2 ON r.referred_id = u2.user_id
                GROUP BY u.user_id
                HAVING new_referrals > 0
                ORDER BY completed_referrals DESC, new_referrals DESC
                LIMIT 50
            ''', (contest_start, contest_start))
            top_rows = cursor.fetchall()

            top = []
            for idx, row in enumerate(top_rows, 1):
                name = row['username'] or row['first_name'] or f"Player_{row['user_id']}"
                completed = row['completed_referrals'] or 0
                tickets = completed // 3  # ← ТОЛЬКО ВЫПОЛНИВШИЕ!
                top.append({
                    "rank": idx,
                    "user_id": row['user_id'],
                    "username": name,
                    "new_referrals": row['new_referrals'],
                    "completed_referrals": completed,
                    "tickets": tickets,
                    "is_qualified": completed >= 3
                })

            # Игроки с билетами
            cursor.execute('''
                SELECT 
                    u.user_id,
                    u.username,
                    u.first_name,
                    COUNT(r.id) as total_referrals,
                    SUM(CASE WHEN r.created_at >= ? THEN 1 ELSE 0 END) as new_referrals
                FROM users u
                LEFT JOIN referrals r ON r.referrer_id = u.user_id
                GROUP BY u.user_id
                HAVING new_referrals >= 3
                ORDER BY new_referrals DESC
            ''', (contest_start,))
            ticket_rows = cursor.fetchall()

            ticket_holders = []
            for row in ticket_rows:
                name = row['username'] or row['first_name'] or f"Player_{row['user_id']}"
                tickets = row['new_referrals'] // 3
                ticket_holders.append({
                    "user_id": row['user_id'],
                    "username": name,
                    "tickets": tickets,
                    "new_referrals": row['new_referrals']
                })

            # Все рефералы конкурса
            cursor.execute('''
                SELECT 
                    r.referrer_id,
                    r.referred_id,
                    r.created_at,
                    u1.username as referrer_name,
                    u1.first_name as referrer_first,
                    u2.username as referred_name,
                    u2.first_name as referred_first,
                    u2.total_clicks
                FROM referrals r
                LEFT JOIN users u1 ON r.referrer_id = u1.user_id
                LEFT JOIN users u2 ON r.referred_id = u2.user_id
                WHERE r.created_at >= ?
                ORDER BY r.created_at DESC
            ''', (contest_start,))
            referral_rows = cursor.fetchall()

            referrals = []
            for row in referral_rows:
                referrer_name = row['referrer_name'] or row['referrer_first'] or f"User_{row['referrer_id']}"
                referred_name = row['referred_name'] or row['referred_first'] or f"User_{row['referred_id']}"
                referrals.append({
                    "referrer_id": row['referrer_id'],
                    "referrer_name": referrer_name,
                    "referred_id": row['referred_id'],
                    "referred_name": referred_name,
                    "created_at": row['created_at'],
                    "clicks": row['total_clicks'] or 0,
                    "is_completed": (row['total_clicks'] or 0) >= 300
                })

            # Время до окончания конкурса
            end_date = datetime.datetime(2026, 6, 21, 14, 0, 0)  # 7 дней = 168 часов
            now = datetime.datetime.now()
            remaining = end_date - now
            if remaining.total_seconds() > 0:
                days = remaining.days
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds % 3600) // 60
                time_left = f"{days}д {hours}ч {minutes}м"
            else:
                time_left = "Конкурс завершён"

            total_tickets = sum([p['tickets'] for p in ticket_holders])

            return jsonify({
                "success": True,
                "stats": {
                    "participants": stats_row['participants'] or 0,
                    "total_referrals": stats_row['total_referrals'] or 0,
                    "new_referrals": stats_row['new_referrals'] or 0,
                    "total_tickets": total_tickets,
                    "time_left": time_left
                },
                "top": top,
                "ticket_holders": ticket_holders,
                "referrals": referrals
            })
    except Exception as e:
        logger.error(f"Ошибка в admin contest stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/admin/reset_contest', methods=['POST'])
@require_admin
def api_admin_reset_contest():
    """Полный сброс конкурса"""
    try:
        # Очищаем данные конкурса у всех пользователей
        with db.get_cursor() as cursor:
            # Удаляем у пользователей поля, связанные с конкурсом
            # (если есть такие поля в users)
            pass

        # Сбрасываем локальные данные в localStorage через JS, поэтому здесь просто логируем
        add_admin_log("🏆 ПОЛНЫЙ СБРОС РЕФЕРАЛЬНОГО КОНКУРСА",
                      request.args.get('user_id', 'Admin'), "Admin")

        return jsonify({"success": True, "message": "Конкурс сброшен"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/admin/give_upgrade', methods=['POST'])
@require_admin
def api_admin_give_upgrade():
    """Выдать улучшение игроку (бесплатно)"""
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No JSON"}), 400

    user_id = data.get('user_id')
    upgrade_id = data.get('upgrade_id')
    amount = data.get('amount', 1)
    increase_price = data.get('increase_price', True)

    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400

    if upgrade_id not in [1, 2, 3, 4, 5]:
        return jsonify({"success": False, "error": "Неверный ID улучшения"}), 400

    try:
        amount = int(amount)
        if amount < 1 or amount > 100:
            return jsonify({"success": False, "error": "Количество должно быть от 1 до 100"}), 400
    except ValueError:
        return jsonify({"success": False, "error": "Неверный формат количества"}), 400

    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"

    user = get_user(user_id)
    upgrade_counts = user.get("upgrade_counts", {})
    free_upgrade_counts = user.get("free_upgrade_counts", {})

    current = upgrade_counts.get(upgrade_id, 0)
    free_current = free_upgrade_counts.get(upgrade_id, 0)

    if increase_price:
        # С повышением цены — добавляем в upgrade_counts
        new_count = current + amount
        upgrade_counts[upgrade_id] = new_count
        safe_update_user(user_id, upgrade_counts=upgrade_counts)
        price_action = "с повышением цены"
    else:
        # БЕЗ повышения цены — добавляем в free_upgrade_counts
        new_free_count = free_current + amount
        free_upgrade_counts[upgrade_id] = new_free_count
        safe_update_user(user_id, free_upgrade_counts=free_upgrade_counts)
        price_action = "БЕЗ повышения цены"

    upgrade_name = UPGRADE_CONFIG[upgrade_id]['name']

    add_admin_log(
        f"🎁 Выдал улучшение {upgrade_name} x{amount} ({price_action})",
        admin_id, admin_name,
        user_id, user.get('username') or f"User_{user_id}"
    )

    # Общий счёт улучшений (платные + бесплатные)
    total = current + free_current + amount

    return jsonify({
        "success": True,
        "message": f"✅ {upgrade_name} x{amount} выдано! Всего улучшений: {total}",
        "upgrade_id": upgrade_id,
        "total_count": total,
        "paid_count": upgrade_counts.get(upgrade_id, 0),
        "free_count": free_upgrade_counts.get(upgrade_id, 0),
        "increase_price": increase_price
    })

@app.route('/api/admin/export_contest', methods=['GET'])
@require_admin
def api_admin_export_contest():
    """Экспорт топа конкурса в JSON"""
    try:
        contest_start = "2026-06-14 14:00:00"

        with db.get_cursor() as cursor:
            cursor.execute('''
                SELECT 
                    u.user_id,
                    u.username,
                    u.first_name,
                    COUNT(r.id) as total_referrals,
                    SUM(CASE WHEN r.created_at >= ? THEN 1 ELSE 0 END) as new_referrals
                FROM users u
                LEFT JOIN referrals r ON r.referrer_id = u.user_id
                GROUP BY u.user_id
                HAVING new_referrals > 0
                ORDER BY new_referrals DESC
            ''', (contest_start,))
            rows = cursor.fetchall()

            top = []
            for idx, row in enumerate(rows, 1):
                name = row['username'] or row['first_name'] or f"Player_{row['user_id']}"
                tickets = row['new_referrals'] // 3
                top.append({
                    "rank": idx,
                    "user_id": row['user_id'],
                    "username": name,
                    "new_referrals": row['new_referrals'],
                    "tickets": tickets,
                    "is_qualified": row['new_referrals'] >= 3
                })

            return jsonify({"success": True, "top": top})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/admin/distribute_contest_prizes', methods=['POST'])
@require_admin
def api_admin_distribute_contest_prizes():
    """Ручная выдача призов победителям"""
    try:
        contest_start = "2026-06-14 14:00:00"

        with db.get_cursor() as cursor:
            # Получаем топ-20 победителей
            cursor.execute('''
                SELECT 
                    u.user_id,
                    u.username,
                    SUM(CASE WHEN r.created_at >= ? THEN 1 ELSE 0 END) as new_referrals
                FROM users u
                LEFT JOIN referrals r ON r.referrer_id = u.user_id
                GROUP BY u.user_id
                HAVING new_referrals >= 3
                ORDER BY new_referrals DESC
                LIMIT 20
            ''', (contest_start,))
            winners = cursor.fetchall()

            if not winners:
                return jsonify({"success": False, "error": "Нет победителей"}), 400

            # Призовой фонд 2000 LP
            prize_per_winner = 2000 // len(winners)

            awarded = 0
            for winner in winners:
                user = get_user(winner['user_id'])
                old_lp = user['lp']
                new_lp = old_lp + prize_per_winner
                safe_update_user(winner['user_id'], lp=new_lp)
                add_admin_log(f"🏆 ВЫДАЧА ПРИЗА КОНКУРСА: +{prize_per_winner} LP",
                              request.args.get('user_id', 'Admin'), "Admin",
                              winner['user_id'], winner['username'] or f"User_{winner['user_id']}")
                awarded += 1

                # Отправляем уведомление в Telegram
                send_telegram_message(winner['user_id'],
                                      f"🏆 **ПОЗДРАВЛЯЕМ!**\n\n"
                                      f"Вы вошли в ТОП-20 реферального конкурса!\n\n"
                                      f"💰 Вы получили +{prize_per_winner} LP\n\n"
                                      f"Спасибо за участие! 🎉")

            return jsonify({"success": True, "message": f"Выдано призов {awarded} игрокам по {prize_per_winner} LP"})
    except Exception as e:
        logger.error(f"Ошибка выдачи призов: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



# ========== РАССЫЛКА ==========
broadcast_active = False
broadcast_cancel = False


@app.route('/api/admin/users_list', methods=['GET'])
@require_admin
def api_admin_users_list():
    """Возвращает список всех пользователей для рассылки"""
    with db.get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) as total FROM users")
        row = cursor.fetchone()
        return jsonify({"success": True, "total": row['total']})


@app.route('/api/admin/send_broadcast', methods=['POST'])
@require_admin
def api_admin_send_broadcast():
    global broadcast_cancel, broadcast_active

    data = request.json
    message = data.get('message', '').strip()
    is_test = data.get('is_test', False)
    test_user_id = data.get('test_user_id')
    has_button = data.get('has_button', False)
    button_text = data.get('button_text', '')
    button_url = data.get('button_url', '')
    admin_id = request.args.get('user_id', 'Admin')
    admin_name = "Admin"

    if not message:
        return jsonify({"success": False, "error": "Сообщение не может быть пустым"}), 400

    broadcast_cancel = False
    broadcast_active = True

    def broadcast_worker():
        global broadcast_cancel, broadcast_active
        try:
            with db.get_cursor() as cursor:
                if is_test and test_user_id:
                    cursor.execute("SELECT user_id FROM users WHERE user_id = ? AND user_id > 0", (test_user_id,))
                else:
                    cursor.execute("SELECT user_id FROM users WHERE user_id > 0 AND user_id != 12345678")
                users = cursor.fetchall()

            if not users:
                add_admin_log("⚠️ РАССЫЛКА: нет пользователей для отправки", admin_id, admin_name)
                broadcast_active = False
                return

            keyboard = None
            if has_button and button_text and button_url:
                keyboard = {
                    "inline_keyboard": [[{"text": button_text, "web_app": {"url": button_url}}]]
                }

            sent = 0
            errors = 0

            for user in users:
                if broadcast_cancel:
                    add_admin_log(f"🛑 РАССЫЛКА ОСТАНОВЛЕНА. Отправлено: {sent}, ошибок: {errors}",
                                  admin_id, admin_name)
                    broadcast_cancel = False
                    broadcast_active = False
                    return

                try:
                    send_telegram_message(user['user_id'], message, keyboard)
                    sent += 1
                except Exception as e:
                    errors += 1
                    logger.error(f"Broadcast error to {user['user_id']}: {e}")
                time.sleep(0.05)

            add_admin_log(f"📢 РАССЫЛКА: отправлено {sent}, ошибок {errors}",
                          admin_id, admin_name)
        except Exception as e:
            logger.error(f"Broadcast worker error: {e}")
            add_admin_log(f"❌ ОШИБКА РАССЫЛКИ: {str(e)}", admin_id, admin_name)
        finally:
            broadcast_active = False
            broadcast_cancel = False

    thread = threading.Thread(target=broadcast_worker, daemon=True)
    thread.start()

    return jsonify({
        "success": True,
        "message": "Рассылка запущена в фоновом режиме"
    })


@app.route('/api/admin/broadcast/cancel', methods=['POST'])
@require_admin
def api_admin_broadcast_cancel():
    """Остановка текущей рассылки"""
    global broadcast_cancel
    broadcast_cancel = True
    return jsonify({"success": True, "message": "Рассылка остановлена"})

@app.route('/api/activate_promo', methods=['POST'])
def api_activate_promo():
    data = request.json
    user_id = data.get('user_id')
    code = data.get('code', '').upper().strip()
    password = data.get('password', '').strip()
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM promo_codes WHERE code = ? AND is_active = 1', (code,))
        promo = cursor.fetchone()
        if not promo:
            return jsonify({"success": False, "error": "Промокод не найден"}), 404
        if promo['expires_at']:
            expires = datetime.datetime.fromisoformat(promo['expires_at'])
            if datetime.datetime.now() > expires:
                return jsonify({"success": False, "error": "Срок действия промокода истёк"}), 400
        used_count = promo['used_count'] or 0
        if used_count >= promo['max_uses']:
            return jsonify({"success": False, "error": "Промокод больше не активен"}), 400
        if promo['password'] and promo['password'] != password:
            return jsonify({"success": False, "error": "Неверный пароль промокода"}), 400
        cursor.execute("SELECT id FROM promo_activations WHERE promo_id = ? AND user_id = ?", (promo['id'], user_id))
        if cursor.fetchone():
            return jsonify({"success": False, "error": "Вы уже активировали этот промокод"}), 400
        user = get_user(user_id)
        old_value = None
        new_value = None
        if promo['reward_type'] == 'wg':
            old_value = user['wg']
            new_value = old_value + promo['reward_amount']
            safe_update_user(user_id, wg=new_value)
        elif promo['reward_type'] == 'lp':
            old_value = user['lp']
            new_value = old_value + promo['reward_amount']
            safe_update_user(user_id, lp=new_value)
        elif promo['reward_type'] == 'energy_limit':
            old_value = user['max_energy']
            new_value = old_value + promo['reward_amount']
            safe_update_user(user_id, max_energy=new_value)
        new_used_count = used_count + 1
        cursor.execute("UPDATE promo_codes SET used_count = ? WHERE id = ?", (new_used_count, promo['id']))
        cursor.execute('INSERT INTO promo_activations (promo_id, user_id) VALUES (?, ?)', (promo['id'], user_id))
        add_log(f"🎁 Активировал промокод {code} | +{promo['reward_amount']} {promo['reward_type'].upper()}", user_id,
                user['username'], old_value=old_value, new_value=new_value, currency=promo['reward_type'])
        return jsonify(
            {"success": True, "message": f"Вы получили +{promo['reward_amount']} {promo['reward_type'].upper()}!",
             "reward_type": promo['reward_type'], "reward_amount": promo['reward_amount']})

@app.route('/api/promo/info', methods=['GET'])
def api_promo_info():
    code = request.args.get('code', '').upper().strip()
    if not code:
        return jsonify({"success": False, "error": "No code provided"}), 400
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM promo_codes WHERE code = ? AND is_active = 1', (code,))
        promo = cursor.fetchone()
        if not promo:
            return jsonify({"success": False, "error": "Promo code not found"}), 404
        if promo['expires_at']:
            expires = datetime.datetime.fromisoformat(promo['expires_at'])
            if datetime.datetime.now() > expires:
                return jsonify({"success": False, "error": "Promo code expired"}), 400
        used_count = promo['used_count'] or 0
        remaining = promo['max_uses'] - used_count
        if remaining <= 0:
            return jsonify({"success": False, "error": "Promo code is no longer active"}), 400
        return jsonify({"success": True, "code": promo['code'], "reward_type": promo['reward_type'],
                        "reward_amount": promo['reward_amount'], "remaining": remaining, "max_uses": promo['max_uses'],
                        "has_password": bool(promo['password'])})

@app.route('/api/set_referral', methods=['POST'])
def api_set_referral():
    data = request.json
    user_id = data.get('user_id')
    referral_code = data.get('referral_code', '').strip()
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"success": False, "error": "Invalid user_id"}), 400
    if not referral_code:
        return jsonify({"success": False, "error": "No referral code"}), 400
    with db.get_cursor() as cursor:
        cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404
        if user['referrer_id'] and user['referrer_id'] != 0:
            return jsonify({"success": True, "message": "Referrer already set"}), 200
        cursor.execute("SELECT user_id FROM users WHERE referral_code = ?", (referral_code,))
        referrer = cursor.fetchone()
        if referrer and referrer['user_id'] != user_id:
            cursor.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer['user_id'], user_id))
            cursor.execute(
                'INSERT INTO referrals (referrer_id, referred_id, username, first_name) SELECT ?, ?, username, first_name FROM users WHERE user_id = ?',
                (referrer['user_id'], user_id, user_id))
            add_log(f"👥 Реферал привязан! Код: {referral_code}", user_id, str(user_id))
            send_telegram_message(referrer['user_id'], f"🎉 Новый реферал присоединился по вашей ссылке!")
            return jsonify({"success": True, "message": "Referral attached"}), 200
    return jsonify({"success": False, "error": "Referrer not found"}), 404

# ========== ОПТИМИЗИРОВАННЫЙ SYNC ЭНДПОИНТ ==========
@app.route('/api/sync', methods=['POST'])
def api_sync():
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
    user_id = data.get('user_id')
    is_valid, user_id = validate_user_id(user_id)
    if not is_valid:
        return jsonify({"error": "Invalid user_id"}), 400
    if not check_rate_limit(f"sync_{user_id}", limit=180, window_seconds=60):
        return jsonify({"error": "Too many requests"}), 429
    banned, ban_info = is_banned(user_id)
    if banned:
        return jsonify({
            "error": f"Вы забанены!",
            "banned": True,
            "reason": ban_info['reason'],
            "until": ban_info['until_date']
        })
    user = get_user(user_id)
    current_energy, seconds_passed = calculate_energy(user)
    earning = get_total_earning(user["upgrade_counts"])
    regen_text = get_energy_regen_text(user["max_energy"], current_energy)
    with online_users_lock:
        online_users[user_id] = time.time()
    update_online_count()
    status_data = {
        "wg": user["wg"],
        "lp": user["lp"],
        "energy": current_energy,
        "total_clicks": user["total_clicks"],
        "daily_clicks": user.get("daily_clicks", 0),
        "earning_per_click": earning,
        "upgrade_counts": user["upgrade_counts"],
        "likes": user["likes"],
        "dislikes": user["dislikes"],
        "username": user["username"],
        "first_name": user["first_name"],
        "avatar_url": user["avatar_url"],
        "settings": user["settings"],
        "usdt": user["usdt"],
        "wins": user["wins"],
        "role": user["role"],
        "stars": user["stars"],
        "max_energy": user["max_energy"],
        "energy_upgrades": user["energy_upgrades"],
        "regen_text": regen_text
    }
    user_tickets = []
    for ticket in lottery_tickets:
        if ticket.get("user_id") == user_id:
            user_tickets.append(ticket)
    update_lottery_phase()
    lottery_data = {
        "prize_pool": lottery_pool,
        "user_tickets": len(user_tickets),
        "user_lp": user["lp"],
        "is_drawn": is_drawn,
        "winning_numbers": winning_numbers if is_drawn else [],
        "tickets": user_tickets,
        "lottery_phase": lottery_phase
    }
    leaderboard_data = get_daily_leaderboard_top_fast(limit=5)
    recent_players = get_recent_players_fast(limit=5)
    return jsonify({
        "success": True,
        "status": status_data,
        "lottery": lottery_data,
        "leaderboard": leaderboard_data,
        "recent_players": recent_players,
        "online_count": len(online_users),
        "server_time": time.time()
    })


@app.route('/api/contest/leaderboard', methods=['GET'])
def api_contest_leaderboard():
    """Возвращает топ участников конкурса"""
    try:
        contest_start_date = "2026-06-14 14:00:00"

        with db.get_cursor() as cursor:
            cursor.execute('''
                SELECT 
                    u.user_id,
                    u.username,
                    u.first_name,
                    u.avatar_url,
                    u.role,
                    COUNT(r.id) as total_referrals,
                    SUM(CASE WHEN r.created_at >= ? THEN 1 ELSE 0 END) as new_referrals,
                    SUM(CASE WHEN r.created_at >= ? AND u2.total_clicks >= 300 THEN 1 ELSE 0 END) as completed_referrals
                FROM users u
                LEFT JOIN referrals r ON r.referrer_id = u.user_id
                LEFT JOIN users u2 ON r.referred_id = u2.user_id
                GROUP BY u.user_id
                HAVING completed_referrals > 0
                ORDER BY completed_referrals DESC, new_referrals DESC
                LIMIT 100
            ''', (contest_start_date, contest_start_date))
            rows = cursor.fetchall()

            leaderboard = []
            for idx, row in enumerate(rows, 1):
                if row['username'] and row['username'] != '':
                    display_name = '@' + row['username']
                elif row['first_name'] and row['first_name'] != '':
                    display_name = row['first_name']
                else:
                    display_name = f"Player_{row['user_id']}"

                completed = row['completed_referrals'] or 0
                tickets = completed // 3

                leaderboard.append({
                    "rank": idx,
                    "user_id": row['user_id'],
                    "username": display_name,
                    "avatar_url": row['avatar_url'] or '',
                    "role": row['role'] or 'player',
                    "new_referrals": row['new_referrals'],
                    "completed_referrals": completed,
                    "tickets": tickets,
                    "is_qualified": completed >= 3
                })

            return jsonify({"success": True, "leaderboard": leaderboard})
    except Exception as e:
        logger.error(f"Ошибка получения топа конкурса: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

def get_daily_leaderboard_top_fast(limit=5):
    global leaderboard_cache, leaderboard_cache_time
    now = time.time()
    if now - leaderboard_cache_time < 5:
        if leaderboard_cache and len(leaderboard_cache) >= limit:
            return leaderboard_cache[:limit]
    with db.get_cursor() as cursor:
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'daily_clicks' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN daily_clicks INTEGER DEFAULT 0')
            return []
        cursor.execute("""
            SELECT user_id, daily_clicks, username, first_name, avatar_url, role, settings 
            FROM users 
            WHERE daily_clicks > 0
            ORDER BY daily_clicks DESC 
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        result = []
        for i, row in enumerate(rows):
            hide_from_top = False
            if row['settings']:
                try:
                    settings = json.loads(row['settings'])
                    hide_from_top = settings.get('hideFromTop', False)
                except:
                    pass
            if hide_from_top:
                display_name = 'Аноним'
                avatar = ''
            else:
                if row['username'] and row['username'] != '':
                    display_name = '@' + row['username']
                elif row['first_name'] and row['first_name'] != '':
                    display_name = row['first_name']
                else:
                    display_name = f"Player_{row['user_id']}"
                avatar = row['avatar_url'] or ''
            result.append({
                "rank": i + 1,
                "user_id": row['user_id'],
                "username": display_name,
                "daily_clicks": row['daily_clicks'] or 0,
                "total_clicks": 0,
                "avatar": avatar,
                "role": row['role'] if row['role'] else 'player',
                "hide_from_top": hide_from_top
            })
        leaderboard_cache = result
        leaderboard_cache_time = now
        return result

def get_recent_players_fast(limit=5):
    cache_key = "recent_players_cache"
    cache_time_key = "recent_players_time"
    if not hasattr(get_recent_players_fast, 'cache'):
        get_recent_players_fast.cache = {}
    now = time.time()
    if cache_key in get_recent_players_fast.cache:
        cache_time = get_recent_players_fast.cache.get(cache_time_key, 0)
        if now - cache_time < 10:
            return get_recent_players_fast.cache[cache_key]
    with db.get_cursor() as cursor:
        cursor.execute("""
            SELECT h.user_id, h.username, h.ticket_number, h.created_at, 
                   u.avatar_url, u.first_name, u.role 
            FROM lottery_tickets_history h 
            LEFT JOIN users u ON h.user_id = u.user_id 
            ORDER BY h.created_at DESC 
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        players = []
        for row in rows:
            created = datetime.datetime.strptime(row['created_at'], '%Y-%m-%d %H:%M:%S')
            diff = datetime.datetime.now() - created
            seconds = int(diff.total_seconds())
            if seconds < 60:
                time_ago = f"{seconds} сек назад"
            elif seconds < 3600:
                time_ago = f"{seconds // 60} мин назад"
            elif seconds < 86400:
                time_ago = f"{seconds // 3600} ч назад"
            else:
                time_ago = f"{seconds // 86400} дн назад"
            if row['username'] and row['username'] != '':
                display_name = '@' + row['username']
            elif row['first_name'] and row['first_name'] != '':
                display_name = row['first_name']
            else:
                display_name = f"Player_{row['user_id']}"
            players.append({
                "user_id": row['user_id'],
                "username": display_name,
                "avatar_url": row['avatar_url'] or '',
                "time_ago": time_ago,
                "ticket_number": row['ticket_number'],
                "role": row['role'] if row['role'] else 'player'
            })
        get_recent_players_fast.cache[cache_key] = players
        get_recent_players_fast.cache[cache_time_key] = now
        return players

def raw_to_user_friendly(raw_address: str) -> str:
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

def calculate_daily_top():
    with db.get_cursor() as cursor:
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'daily_clicks' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN daily_clicks INTEGER DEFAULT 0')
            add_admin_log("➕ Создана колонка daily_clicks для топа дня", 0, "System")
        cursor.execute('''
            SELECT user_id, daily_clicks, username, first_name, role
            FROM users 
            WHERE daily_clicks > 0
            ORDER BY daily_clicks DESC 
            LIMIT 5
        ''')
        rows = cursor.fetchall()
        if not rows:
            cursor.execute("UPDATE users SET daily_clicks = 0")
            add_admin_log("🏆 ТОП ДНЯ: нет данных для награждения, но daily_clicks обнулён", 0, "System")
            return []
        rewards = {
            1: {"lp": 70, "wg": 5000},
            2: {"lp": 50, "wg": 3000},
            3: {"lp": 35, "wg": 1500},
            4: {"lp": 25, "wg": 1000},
            5: {"lp": 15, "wg": 500}
        }
        awarded_count = 0
        for i, row in enumerate(rows, 1):
            if i in rewards and row['daily_clicks'] > 0:
                user = get_user(row['user_id'])
                old_lp = user['lp']
                old_wg = user['wg']
                new_lp = old_lp + rewards[i]["lp"]
                new_wg = old_wg + rewards[i]["wg"]
                safe_update_user(row['user_id'], lp=new_lp, wg=new_wg)
                add_log(f"🏆 ТОП ДНЯ #{i} место ({row['daily_clicks']} кликов) | +{rewards[i]['lp']} LP, +{rewards[i]['wg']} WG",
                        row['user_id'], row['username'] or row['first_name'] or str(row['user_id']),
                        old_lp, new_lp, "lp")
                add_log(f"🏆 ТОП ДНЯ #{i} место ({row['daily_clicks']} кликов) | +{rewards[i]['lp']} LP, +{rewards[i]['wg']} WG",
                        row['user_id'], row['username'] or row['first_name'] or str(row['user_id']),
                        old_wg, new_wg, "wg")
                try:
                    send_telegram_message(row['user_id'],
                        f"🏆 **ПОЗДРАВЛЯЕМ!**\n\nВы заняли #{i} место в **ТОПЕ ДНЯ**!\n\n📊 Кликов за день: {row['daily_clicks']}\n\n**Награда:**\n💎 +{rewards[i]['lp']} LP\n💰 +{rewards[i]['wg']} WG\n\nПродолжайте кликать! 🎉")
                except Exception as e:
                    logger.error(f"Ошибка отправки Telegram сообщения: {e}")
                awarded_count += 1
        cursor.execute("UPDATE users SET daily_clicks = 0")
        add_admin_log(f"🔄 ОБНУЛЕНИЕ daily_clicks выполнено в {datetime.datetime.now()}, выдано {awarded_count} наград", 0, "System")
        return rows

def schedule_daily_top_reset():
    def reset_and_reward():
        while True:
            now = datetime.datetime.now()
            next_reset = now.replace(hour=21, minute=0, second=0, microsecond=0)
            if now >= next_reset:
                next_reset += datetime.timedelta(days=1)
            wait_seconds = (next_reset - now).total_seconds()
            hours = int(wait_seconds // 3600)
            minutes = int((wait_seconds % 3600) // 60)
            add_admin_log(f"⏰ До следующего сброса топа: {hours}ч {minutes}мин", 0, "System")
            time.sleep(wait_seconds)
            calculate_daily_top()
            add_admin_log(f"🔄 ЕЖЕДНЕВНЫЙ СБРОС ТОПА выполнен в {datetime.datetime.now()}", 0, "System")
    threading.Thread(target=reset_and_reward, daemon=True).start()

schedule_daily_top_reset()


def handle_telegram_updates():
    """
    Обработка обновлений через Long Polling
    """
    last_update_id = 0
    verify_ssl = not DEBUG_MODE
    consecutive_errors = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {
                "offset": last_update_id + 1,
                "timeout": 10,  # Уменьшен с 30 до 10 секунд для более быстрого отклика
                "allowed_updates": ["message", "callback_query", "pre_checkout_query"]
            }

            response = requests.get(
                url,
                params=params,
                timeout=15,
                verify=verify_ssl
            )

            if response.status_code != 200:
                logger.warning(f"⚠️ Telegram API вернул код {response.status_code}")
                time.sleep(2)
                continue

            updates = response.json()

            if not updates.get("ok"):
                logger.warning(f"⚠️ Telegram API ошибка: {updates}")
                time.sleep(2)
                continue

            # Сброс счётчика ошибок при успешном ответе
            consecutive_errors = 0

            # ========== ОБРАБОТКА ОБНОВЛЕНИЙ ==========
            for update in updates.get("result", []):
                last_update_id = update["update_id"]

                # ---------- ОБРАБОТКА СООБЩЕНИЙ ----------
                if "message" in update:
                    message = update["message"]
                    chat_type = message["chat"]["type"]
                    chat_id = message["chat"]["id"]

                    # Игнорируем сообщения из групп и каналов
                    if chat_type != "private":
                        logger.debug(f"Игнорируем сообщение из чата {chat_type} (ID: {chat_id})")
                        continue

                    username = sanitize_string(message["chat"].get("username", ""))
                    first_name = sanitize_string(message["chat"].get("first_name", ""))
                    last_name = sanitize_string(message["chat"].get("last_name", ""))

                    # ---------- ТЕКСТОВЫЕ СООБЩЕНИЯ ----------
                    if "text" in message:
                        text = message["text"]

                        # === /START ===
                        if text.startswith("/start"):
                            parts = text.split()
                            ref_code = parts[1] if len(parts) > 1 else None

                            with db.get_cursor() as cursor:
                                cursor.execute("SELECT * FROM users WHERE user_id=?", (chat_id,))
                                existing = cursor.fetchone()

                                if not existing:
                                    now = time.time()
                                    ref_code_new = hashlib.md5(str(chat_id).encode()).hexdigest()[:8]
                                    role = "founder" if chat_id == 5264622363 else "player"
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
                                                'INSERT INTO referrals (referrer_id, referred_id, username, first_name) VALUES (?, ?, ?, ?)',
                                                (referrer_id, chat_id, username, first_name)
                                            )
                                            send_telegram_message(
                                                referrer_id,
                                                f"🎉 Новый реферал! {first_name or username} присоединился по вашей ссылке!"
                                            )

                                    cursor.execute('''
                                        INSERT INTO users (
                                            user_id, wg, lp, energy, last_energy_update, tickets, total_clicks, 
                                            upgrade_counts, ticket_counter, referral_code, referrer_id, likes, dislikes, 
                                            settings, username, first_name, last_name, avatar_url, usdt, wins, role, stars, 
                                            max_energy, energy_upgrades, energy_limit_upgrades, unlocked_prefixes, 
                                            tutorial_completed, ton_wallet, banned_until, ban_reason, banned_by, 
                                            completed_achievements
                                        ) VALUES (
                                            ?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, ?, 0, 0, 
                                            '{"theme":"dark"}', ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '', 0, '', 0, 0
                                        )
                                    ''', (
                                        chat_id, now, ref_code_new, referrer_id,
                                        username, first_name, last_name, "", role, unlocked
                                    ))

                            keyboard = {
                                "inline_keyboard": [[{
                                    "text": "💰 Открыть игру",
                                    "web_app": {"url": WEBHOOK_URL}
                                }]]
                            }
                            send_telegram_message(
                                chat_id,
                                "✨ Добро пожаловать в WereGood!\n\n"
                                "💰 Кликай по монете, улучшай заработок и участвуй в вызовах!\n\n"
                                "⬇️ Нажми на кнопку ниже, чтобы начать!",
                                keyboard
                            )

                        # === /HELP ===
                        elif text.startswith("/help"):
                            keyboard = {
                                "inline_keyboard": [[{
                                    "text": "💰 Открыть игру",
                                    "web_app": {"url": WEBHOOK_URL}
                                }]]
                            }
                            send_telegram_message(
                                chat_id,
                                "🎮 **WereGood - Помощь**\n\n"
                                "💰 **Клик по монете** - зарабатывай WG\n"
                                "⚡ **Энергия** - восстанавливается со временем\n"
                                "🎲 **Лотерея** - участвуй за 100 LP в 21:00\n"
                                "👥 **Рефералы** - приглашай друзей и получай 5%\n"
                                "⭐ **Stars** - покупай улучшения за Telegram Stars\n"
                                "💎 **TON** - покупай улучшения за TON\n\n"
                                "🔗 **Ссылка на игру:**",
                                keyboard
                            )

                        # === /ADMIN ===
                        elif text.startswith("/admin"):
                            if chat_id in ADMIN_IDS:
                                admin_url = f"{WEBHOOK_URL}/admin?key={ADMIN_SECRET}&user_id={chat_id}"
                                keyboard = {
                                    "inline_keyboard": [[{
                                        "text": "👑 Открыть админ-панель",
                                        "web_app": {"url": admin_url}
                                    }]]
                                }
                                send_telegram_message(
                                    chat_id,
                                    "👑 Админ-панель WereGood\n\n"
                                    "• 📊 Статистика\n"
                                    "• 💰 Выдача валюты\n"
                                    "• 🎲 Управление лотереей\n"
                                    "• 👑 Управление префиксами\n"
                                    "• 💸 Заявки на вывод\n"
                                    "• 🎫 Промокоды\n\n"
                                    "⬇️ Нажми на кнопку",
                                    keyboard
                                )
                            else:
                                send_telegram_message(chat_id, "⛔ У вас нет доступа к админ-панели")

                    # ---------- УСПЕШНЫЙ ПЛАТЁЖ ----------
                    elif "successful_payment" in message:
                        try:
                            handle_successful_payment(chat_id, message["successful_payment"])
                        except Exception as e:
                            logger.error(f"Ошибка обработки платежа: {e}")

                # ---------- ОБРАБОТКА PRE_CHECKOUT_QUERY ----------
                elif "pre_checkout_query" in update:
                    query = update["pre_checkout_query"]
                    try:
                        answer_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerPreCheckoutQuery"
                        requests.post(
                            answer_url,
                            json={"pre_checkout_query_id": query["id"], "ok": True},
                            timeout=5,
                            verify=verify_ssl
                        )
                    except Exception as e:
                        logger.error(f"Ошибка подтверждения платежа: {e}")

                # ---------- ОБРАБОТКА CALLBACK_QUERY ----------
                elif "callback_query" in update:
                    query = update["callback_query"]
                    data = query.get("data", "")
                    chat_id = query["message"]["chat"]["id"]

                    try:
                        # Здесь можно добавить обработку нажатий на кнопки
                        # Например, для админ-панели или игровых механик
                        pass
                    except Exception as e:
                        logger.error(f"Ошибка обработки callback: {e}")

                    # Обязательно отвечаем на callback, чтобы Telegram знал, что мы его обработали
                    try:
                        answer_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
                        requests.post(
                            answer_url,
                            json={"callback_query_id": query["id"]},
                            timeout=3,
                            verify=verify_ssl
                        )
                    except:
                        pass

            # Небольшая пауза перед следующим циклом
            time.sleep(0.1)

        except requests.exceptions.Timeout:
            logger.warning("⚠️ Таймаут при запросе к Telegram API")
            time.sleep(2)

        except requests.exceptions.ConnectionError:
            consecutive_errors += 1
            wait_time = min(60, 2 ** consecutive_errors)  # Экспоненциальная задержка
            logger.error(f"❌ Ошибка соединения с Telegram API (попытка {consecutive_errors}), ждём {wait_time}с")
            time.sleep(wait_time)

        except Exception as e:
            consecutive_errors += 1
            wait_time = min(60, 2 ** consecutive_errors)
            logger.error(f"❌ Неизвестная ошибка в polling: {e} (попытка {consecutive_errors})")
            time.sleep(wait_time)



restore_fortune_from_db()
start_fortune_timer_thread()

if __name__ == '__main__':
    # Запускаем polling для Telegram
    threading.Thread(target=handle_telegram_updates, daemon=True).start()

    # Запускаем Фортуну
    restore_fortune_from_db()
    start_fortune_timer_thread()

    print("\n" + "=" * 60)
    print("🔧 WereGood Bot - ПОЛНАЯ ВЕРСИЯ С ДОСТИЖЕНИЯМИ И ФОРТУНОЙ")
    print("=" * 60)
    print("✅ ВСЕ ФУНКЦИИ СОХРАНЕНЫ:")
    print("   • Лотерея с розыгрышами и новой логикой")
    print("   • Реферальная система")
    print("   • Ежедневные награды (24 часа)")
    print("   • TON Connect 2.0 с HEX payload")
    print("   • Stars оплата")
    print("   • Промокоды")
    print("   • Полная админ-панель")
    print("   • Система достижений с топом и префиксом ЛЕГЕНДА!")
    print("   • 10 достижений с авто-отслеживанием")
    print("   • Топ-50 по достижениям")
    print("   • Бесконечные логи с пагинацией (по 100 записей)")
    print("   • КУЛДАУН НА РЕКЛАМУ:")
    print("     • +150 энергии: 5 мин кулдаун, 40 раз в день")
    print("     • +1 к макс. энергии: 10 мин кулдаун, 15 раз в день")
    print("   • Энергия: отображение в минутах")
    print("   • ЗАДАНИЯ: подписка на каналы за награду")
    print("   • КОМАНДНАЯ ФОРТУНА: мини-игра с комиссией 7% и шансами победы")
    print("=" * 60)
    print(f"🌐 Игра: http://0.0.0.0:5000")
    print(f"👑 Админ-панель: http://0.0.0.0:5000/admin?key={ADMIN_SECRET}")
    print(f"🎫 Активация промокода: {WEBHOOK_URL}/claim?code=ВАШ_КОД")
    print("=" * 60)

    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)