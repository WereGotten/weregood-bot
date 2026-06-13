# models.py
import json
import time
import hashlib
import threading
import datetime
import random
from collections import defaultdict
from database import db
from config import UPGRADE_CONFIG, DAILY_REWARDS, ADMIN_IDS, DEBUG_MODE, TELEGRAM_TOKEN
from utils import send_telegram_message, escape_html, validate_user_id

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
user_cache = {}
user_cache_time = {}
CACHE_TTL = 60
MAX_USER_CACHE = 5000
user_energy_locks = defaultdict(threading.Lock)
click_buffer = {}
click_buffer_lock = threading.Lock()
click_queue = []
online_users = {}
online_users_lock = threading.Lock()
used_ton_transactions = set()
used_transaction_lock = threading.Lock()
pending_invoices = {}

ALLOWED_UPDATE_FIELDS = {
    'wg', 'lp', 'energy', 'last_energy_update', 'tickets', 'total_clicks',
    'upgrade_counts', 'username', 'first_name', 'last_name', 'ticket_counter',
    'referral_code', 'referrer_id', 'likes', 'dislikes', 'settings',
    'avatar_url', 'usdt', 'wins', 'role', 'stars', 'max_energy',
    'energy_upgrades', 'energy_limit_upgrades', 'unlocked_prefixes',
    'tutorial_completed', 'ton_wallet', 'banned_until', 'ban_reason', 'banned_by',
    'completed_achievements', 'daily_clicks',
    'fortune_bets_count', 'fortune_wins_count', 'fortune_total_bet_amount'
}


# ========== ОСНОВНЫЕ ФУНКЦИИ ПОЛЬЗОВАТЕЛЕЙ ==========

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
                        banned_until, ban_reason, banned_by, completed_achievements
                    ) VALUES (
                        ?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, ?, 0, 0,
                        '{"theme":"dark"}', ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '', 0, '', 0, 0
                    )
                ''', (
                    user_id, now_time, ref_code, 0,
                    final_username, final_first_name, final_last_name, final_avatar_url,
                    role, unlocked
                ))
                cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
                row = cursor.fetchone()
            except Exception as e:
                print(f"Ошибка создания пользователя {user_id}: {e}")
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
                "completed_achievements": 0
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
                print(f"Попытка обновить запрещённое поле: {key}")
                continue
            if key in ['upgrade_counts', 'tickets', 'settings', 'unlocked_prefixes']:
                if value is None:
                    value = '{}' if key == 'upgrade_counts' else '[]'
                else:
                    value = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            cursor.execute(f'UPDATE users SET "{key}" = ? WHERE user_id = ?', (value, user_id))
    invalidate_cache(user_id)


# ========== ЭНЕРГИЯ ==========

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
            cursor.execute("UPDATE users SET energy=?, last_energy_update=? WHERE user_id=?",
                           (new_energy, now, user_id))
        user_data["energy"] = new_energy
        user_data["last_energy_update"] = now
        invalidate_cache(user_id)
        return True, new_energy


# ========== УЛУЧШЕНИЯ ==========

def get_total_earning(upgrade_counts):
    base = 0.01
    total_bonus = 0
    for uid, count in upgrade_counts.items():
        uid_int = int(uid) if isinstance(uid, str) else uid
        if uid_int in UPGRADE_CONFIG:
            total_bonus += UPGRADE_CONFIG[uid_int]["bonus"] * count
    return base + total_bonus


def get_upgrade_cost(upgrade_id, current_count):
    config = UPGRADE_CONFIG[upgrade_id]
    base_cost = config["base_cost"]
    if current_count == 0:
        return base_cost
    return base_cost * (1.65 ** current_count)


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


# ========== БАН И УДАЛЕНИЕ ==========

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
    add_log(f"🔨 ЗАБАНИЛ пользователя на {days} дней. Причина: {reason}", admin_id, "Admin")


def unban_user(user_id):
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET banned_until = 0, ban_reason = '', banned_by = 0 WHERE user_id = ?",
                       (user_id,))
    invalidate_cache(user_id)
    add_log(f"🔓 РАЗБАНИЛ пользователя", user_id, "Admin")


def update_online_count():
    """Обновляет количество онлайн-пользователей"""
    now = time.time()
    with online_users_lock:
        to_remove = [uid for uid, last_seen in online_users.items() if now - last_seen > 300]
        for uid in to_remove:
            del online_users[uid]


def delete_user(user_id):
    """Полностью удаляет пользователя из БД"""
    from lottery import lottery_tickets, save_lottery

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

        # Удаляем билеты пользователя из лотереи
        global lottery_tickets
        lottery_tickets = [t for t in lottery_tickets if t.get("user_id") != user_id]
        save_lottery()

    invalidate_cache(user_id)
    add_log(f"🗑️ ПОЛНОСТЬЮ УДАЛИЛ пользователя из БД", 0, "System")
    return True


# ========== ЛОГИ ==========

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


# ========== ВАЛЮТА ==========

def add_usdt(user_id, amount):
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET usdt = usdt + ? WHERE user_id=?", (amount, user_id))
    invalidate_cache(user_id)


def add_wins(user_id, amount=1):
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE users SET wins = wins + ? WHERE user_id=?", (amount, user_id))
    invalidate_cache(user_id)


# ========== STARTS ОПЛАТА ==========

def create_stars_invoice(chat_id, user_id):
    import requests
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
        verify_ssl = not DEBUG_MODE
        response = requests.post(url, json=data, timeout=10, verify=verify_ssl)
        result = response.json()
        if result.get("ok"):
            return result["result"]
        return None
    except Exception as e:
        print(f"Ошибка в create_stars_invoice: {e}")
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
            add_admin_log(
                f"⭐ Купил энергетический усилитель | +40 макс. энергии, +50 LP",
                user_id,
                user['username'] or f"User_{user_id}",
                details=f"Улучшений теперь: {new_upgrades}/15, макс. энергия: {new_max_energy}"
            )
            return True, "✨ Улучшение активировано! +40 макс. энергии и +50 LP!", {
                "energy_upgrades": new_upgrades,
                "max_energy": new_max_energy,
                "lp": new_lp,
                "energy": recalculated_energy
            }
    except Exception as e:
        print(f"Ошибка в grant_energy_upgrade: {e}")
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
        print(f"Ошибка в handle_successful_payment: {e}")
        return False


# ========== ДОСТИЖЕНИЯ ==========

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
        'autoclicker': 'Автокликер', 'investor': 'Инвестор', 'social': 'Общительный',
        'gambler': 'Азартный', 'lucky': 'Счастливчик', 'liker': 'Подписчик',
        'hater': 'Хейтер', 'ad_lover': 'Любитель TV', 'spender': 'Транжира',
        'task_master': 'Выполнитель', 'brave': 'Бесстрашный',
        'lucky_fortune': 'Везучий', 'gambler_fortune': 'Лудоман', 'crazy': 'Сумасшедший'
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
            cursor.execute('UPDATE users SET role = "legend" WHERE user_id = ? AND role != "legend"', (user_id,))
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
                'rank': i + 1, 'user_id': row['user_id'], 'username': display_name,
                'avatar_url': row['avatar_url'], 'completed': row['completed_achievements'], 'role': row['role']
            })
        return result

# ========== СТАТИСТИКА ==========

def update_stats_history(date, clicks=0, ad_views=0, stars=0, online=0, tickets=0, users=0):
    """Обновляет статистику в истории"""
    with db.get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO stats_history (date, clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users) 
            VALUES (?, ?, ?, ?, ?, ?, ?) 
            ON CONFLICT(date) DO UPDATE SET 
            clicks = clicks + ?, ad_views = ad_views + ?, stars_donated = stars_donated + ?, 
            online_peak = MAX(online_peak, ?), tickets_sold = tickets_sold + ?, new_users = new_users + ?
        ''', (date, clicks, ad_views, stars, online, tickets, users,
              clicks, ad_views, stars, online, tickets, users))

# ========== ДОСТИЖЕНИЯ ФОРТУНЫ ==========

def update_fortune_achievements(user_id, bet_amount=None, is_win=False, is_new_round=True):
    try:
        with db.get_cursor() as cursor:
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
            if bet_amount is not None and bet_amount > 0:
                new_total_bet = current_total_bet + bet_amount
                cursor.execute("UPDATE users SET fortune_total_bet_amount = ? WHERE user_id = ?",
                               (new_total_bet, user_id))
                if new_total_bet >= 200000:
                    update_achievement_progress(user_id, 'gambler_fortune', set_value=200000)
                else:
                    update_achievement_progress(user_id, 'gambler_fortune', int(bet_amount))
                if is_new_round:
                    new_bets = current_bets + 1
                    cursor.execute("UPDATE users SET fortune_bets_count = ? WHERE user_id = ?", (new_bets, user_id))
                    update_achievement_progress(user_id, 'brave', 1)
                    if new_bets >= 1000:
                        update_achievement_progress(user_id, 'crazy', set_value=1000)
                    else:
                        update_achievement_progress(user_id, 'crazy', 1)
            if is_win:
                new_wins = current_wins + 1
                cursor.execute("UPDATE users SET fortune_wins_count = ? WHERE user_id = ?", (new_wins, user_id))
                if new_wins >= 100:
                    update_achievement_progress(user_id, 'lucky_fortune', set_value=100)
                else:
                    update_achievement_progress(user_id, 'lucky_fortune', 1)
            invalidate_cache(user_id)
    except Exception as e:
        print(f"Ошибка обновления достижений Фортуны: {e}")