# api.py
import json
import time
import datetime
import hashlib
import random
import threading
import requests
import os
import secrets
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO, emit
from database import db
from models import (
    get_user, safe_update_user, add_log, add_admin_log, invalidate_cache,
    calculate_energy, get_energy_regen_text, update_energy_in_db, spend_energy,
    get_total_earning, get_upgrade_cost, is_banned, add_usdt, add_wins,
    create_stars_invoice, grant_energy_upgrade, get_achievements_list,
    get_user_achievements, update_achievement_progress, get_achievements_top,
    update_fortune_achievements, unlock_prefix, online_users, online_users_lock,
    used_ton_transactions, used_transaction_lock, click_buffer, click_buffer_lock,
    ALLOWED_UPDATE_FIELDS, user_cache, user_cache_time, CACHE_TTL
)
from lottery import (
    lottery_pool, lottery_tickets, is_drawn, winning_numbers, lottery_phase,
    buy_ticket, reveal_all_tickets, save_lottery, update_lottery_phase,
    load_lottery, generate_ticket_numbers, lottery_lock
)
from fortune import current_fortune_round, fortune_lock, restore_fortune_from_db, end_fortune_round
from utils import (
    check_rate_limit, validate_user_id, sanitize_string, escape_html,
    send_telegram_message, validate_ton_address, check_ton_transaction,
    record_ad_watch, check_ad_cooldown, string_to_hex_payload, raw_to_user_friendly,
    require_admin
)
from config import (
    TELEGRAM_TOKEN, UPGRADE_CONFIG, DEBUG_MODE, BOT_USERNAME,
    PROJECT_WALLET_ADDRESS, DAILY_REWARDS, FORTUNE_COMMISSION,
    FORTUNE_ROUND_DURATION, DATABASE_PATH
)

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ API ===
leaderboard_cache = []
leaderboard_cache_time = 0
LEADERBOARD_CACHE_TTL = 5


def register_routes(app, socketio):
    # === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
    def update_stats_history(date, clicks=0, ad_views=0, stars=0, online=0, tickets=0, users=0):
        with db.get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO stats_history (date, clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users) 
                VALUES (?, ?, ?, ?, ?, ?, ?) 
                ON CONFLICT(date) DO UPDATE SET 
                clicks = clicks + ?, ad_views = ad_views + ?, stars_donated = stars_donated + ?, 
                online_peak = MAX(online_peak, ?), tickets_sold = tickets_sold + ?, new_users = new_users + ?
            ''', (date, clicks, ad_views, stars, online, tickets, users, clicks, ad_views, stars, online, tickets,
                  users))

    def update_online_count():
        now = time.time()
        with online_users_lock:
            to_remove = [uid for uid, last_seen in online_users.items() if now - last_seen > 300]
            for uid in to_remove:
                del online_users[uid]

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

    # === ГЛАВНЫЕ СТРАНИЦЫ ===
    @app.route('/')
    def game_page():
        return render_template('game.html')

    @app.route('/admin')
    def admin_panel_page():
        client_ip = request.remote_addr
        if not DEBUG_MODE:
            from utils import check_admin_bruteforce, record_admin_failure
            if not check_admin_bruteforce(client_ip):
                return "Too many failed attempts", 429
        key = request.args.get('key')
        from config import ADMIN_SECRET
        if not key or not secrets.compare_digest(key, ADMIN_SECRET):
            if not DEBUG_MODE:
                record_admin_failure(client_ip)
            return "Доступ запрещён", 403
        from utils import admin_failures
        admin_failures.pop(client_ip, None)
        return render_template('admin.html')

    @app.route('/static/<path:filename>')
    def serve_static(filename):
        return send_from_directory('static', filename)

    @app.route('/health')
    def health_check():
        update_online_count()
        with online_users_lock:
            online_count = len(online_users)
        return jsonify({"status": "ok", "online_users": online_count, "threads": threading.active_count(),
                        "db_size": os.path.getsize(DATABASE_PATH) if os.path.exists(DATABASE_PATH) else 0,
                        "timestamp": time.time()})

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

    # === ОСНОВНЫЕ ИГРОВЫЕ API ===

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
        username = sanitize_string(data.get('username', ''), 50)
        first_name = sanitize_string(data.get('first_name', ''), 50)
        last_name = sanitize_string(data.get('last_name', ''), 50)
        avatar_url = sanitize_string(data.get('avatar_url', ''), 200)
        referral_code = sanitize_string(data.get('referral_code', ''), 50)
        with db.get_cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_name = ?, avatar_url = ? WHERE user_id = ?",
                    (username, first_name, last_name, avatar_url, user_id))
                invalidate_cache(user_id)
                add_log(f"✏️ Обновил профиль (username: {username or first_name})", user_id,
                        username or first_name or str(user_id))
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
                ''', (user_id, now, ref_code_new, referrer_id, username, first_name, last_name, avatar_url, role,
                      unlocked))
                add_log(f"✨ Новая регистрация! Добро пожаловать, {username or first_name}!", user_id,
                        username or first_name or str(user_id))
                today = datetime.datetime.now().strftime("%Y-%m-%d")
                update_stats_history(today, users=1)
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
            return jsonify(
                {"error": f"Вы забанены! Причина: {ban_info['reason']}. До: {ban_info['until_date']}", "banned": True})
        user = get_user(user_id)
        success, new_energy = spend_energy(user_id, user, 1)
        if not success:
            return jsonify({"error": "Нет энергии", "energy": new_energy, "wg": user["wg"], "lp": user["lp"]})
        earning = get_total_earning(user["upgrade_counts"])
        old_wg = user["wg"]
        new_wg = old_wg + earning
        with click_buffer_lock:
            click_buffer[user_id] = click_buffer.get(user_id, 0) + 1
        safe_update_user(user_id, wg=new_wg)
        referrer_id = user.get('referrer_id', 0)
        if referrer_id > 0:
            referrer_earning = earning * 0.1
            if referrer_earning > 0:
                referrer = get_user(referrer_id)
                old_referrer_wg = referrer['wg']
                new_referrer_wg = old_referrer_wg + referrer_earning
                safe_update_user(referrer_id, wg=new_referrer_wg)
                with db.get_cursor() as cursor:
                    cursor.execute(
                        'UPDATE referrals SET total_earned_wg = total_earned_wg + ? WHERE referrer_id = ? AND referred_id = ?',
                        (referrer_earning, referrer_id, user_id))
                add_log(f"👥 Получил 10% от WG реферала (+{referrer_earning:.4f} WG)", referrer_id,
                        referrer.get('username') or f"User_{referrer_id}", old_value=old_referrer_wg,
                        new_value=new_referrer_wg, currency="wg")
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE users SET daily_clicks = daily_clicks + 1 WHERE user_id = ?", (user_id,))
        invalidate_cache(user_id)
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        update_achievement_progress(user_id, 'autoclicker', 1)
        update_stats_history(today, clicks=1)
        if user["total_clicks"] == 0:
            add_log(f"🆕👆 ПЕРВЫЙ КЛИК в игре! +{earning:.4f} WG", user_id, user['username'], old_wg, new_wg, "wg")
        else:
            add_log(f"🖱️ Клик по монете +{earning:.4f} WG", user_id, user['username'], old_wg, new_wg, "wg")
        new_lp_value = user["lp"]
        lp_reward = False
        if random.random() < 0.0025:
            lp_reward = True
            new_lp_value = user["lp"] + 0.5
            safe_update_user(user_id, lp=new_lp_value)
            threading.Thread(target=add_log,
                             args=(f"🎲 Редкий дроп! +0.5 LP", user_id, user['username'], user["lp"], new_lp_value,
                                   "lp")).start()
        return jsonify({
            "energy": new_energy, "wg": new_wg, "lp": new_lp_value,
            "total_clicks": user["total_clicks"] + 1, "earned": earning,
            "lp_reward": lp_reward, "earning_per_click": earning
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
        return jsonify({
            "wg": user["wg"], "lp": user["lp"], "energy": current_energy,
            "total_clicks": user["total_clicks"], "earning_per_click": earning,
            "upgrade_counts": user["upgrade_counts"], "likes": user["likes"], "dislikes": user["dislikes"],
            "username": user["username"], "first_name": user["first_name"], "avatar_url": user["avatar_url"],
            "settings": user["settings"], "usdt": user["usdt"], "wins": user["wins"], "role": user["role"],
            "stars": user["stars"], "max_energy": user["max_energy"], "energy_upgrades": user["energy_upgrades"],
            "regen_text": regen_text
        })

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
        if upgrade_id not in [1, 2, 3]:
            return jsonify({"success": False, "msg": "Неверный ID улучшения"})
        user = get_user(user_id)
        current_count = user["upgrade_counts"].get(upgrade_id, 0)
        cost = get_upgrade_cost(upgrade_id, current_count)
        if user["wg"] < cost:
            return jsonify({"success": False, "msg": f"Не хватает WG! Нужно {cost:.2f} WG"})
        old_wg = user["wg"]
        new_wg = old_wg - cost
        new_count = current_count + 1
        user["upgrade_counts"][upgrade_id] = new_count
        safe_update_user(user_id, wg=new_wg, upgrade_counts=user["upgrade_counts"])
        update_achievement_progress(user_id, 'investor', 1)
        update_achievement_progress(user_id, 'spender', int(cost))
        if current_count == 0:
            add_log(f"🆕⭐ ПЕРВАЯ ПОКУПКА улучшения! {UPGRADE_CONFIG[upgrade_id]['name']} за {cost:.2f} WG", user_id,
                    user['username'], old_value=old_wg, new_value=new_wg, currency="wg")
        else:
            add_log(f"💰 Купил {UPGRADE_CONFIG[upgrade_id]['name']} #{new_count} за {cost:.2f} WG", user_id,
                    user['username'], old_value=old_wg, new_value=new_wg, currency="wg")
        return jsonify({"success": True, "msg": f"{UPGRADE_CONFIG[upgrade_id]['name']} #{new_count} куплено!",
                        "new_count": new_count, "next_cost": get_upgrade_cost(upgrade_id, new_count)})

    @app.route('/api/watch_ad', methods=['POST'])
    def api_watch_ad():
        data = request.json
        if not data:
            return jsonify({"success": False, "msg": "No data"}), 400
        user_id = data.get('user_id')
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"success": False, "msg": "Invalid user_id"}), 400
        can_watch, msg = check_ad_cooldown(user_id, "energy_200", 5, 40)
        if not can_watch:
            return jsonify({"success": False, "msg": msg}), 429
        banned, ban_info = is_banned(user_id)
        if banned:
            return jsonify({"success": False, "msg": f"Вы забанены! {ban_info['reason']}"})
        user = get_user(user_id)
        current_energy, _ = calculate_energy(user)
        max_energy = user.get("max_energy", 500)
        old_energy = current_energy
        new_energy = min(max_energy, current_energy + 150)
        update_energy_in_db(user_id, user, new_energy)
        record_ad_watch(user_id, "energy_200")
        update_achievement_progress(user_id, 'ad_lover', 1)
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        update_stats_history(today, ad_views=1)
        add_log(f"🎬 Просмотрел рекламу (+150 энергии)", user_id, user['username'], old_value=old_energy,
                new_value=new_energy, currency="energy")
        return jsonify({"success": True, "energy": 150})

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
        can_watch, msg = check_ad_cooldown(user_id, "energy_limit", 10, 15)
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
        new_max_energy = old_max_energy + 1
        new_upgrades = current_upgrades + 1
        safe_update_user(user_id, max_energy=new_max_energy, energy_limit_upgrades=new_upgrades)
        record_ad_watch(user_id, "energy_limit")
        update_achievement_progress(user_id, 'ad_lover', 1)
        add_log(f"🎬 Просмотрел рекламу (+1 к макс. энергии, теперь {new_max_energy})", user_id, user['username'],
                old_value=old_max_energy, new_value=new_max_energy, currency="energy")
        return jsonify({"success": True, "max_energy": new_max_energy, "upgrades": new_upgrades})

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
        if ad_type == 'energy_200':
            can_watch, msg = check_ad_cooldown(user_id, "energy_200", 5, 40)
            return jsonify({"can": can_watch, "message": msg if not can_watch else ""})
        elif ad_type == 'energy_limit':
            can_watch, msg = check_ad_cooldown(user_id, "energy_limit", 10, 15)
            return jsonify({"can": can_watch, "message": msg if not can_watch else ""})
        else:
            return jsonify({"can": False, "message": "Unknown ad type"})

    # === ЛОТЕРЕЯ API ===

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

        # ✅ ПРОВЕРЯЕМ СТАТУС РОЗЫГРЫША ПЕРЕД ПОКУПКОЙ
        from lottery import is_drawn, refresh_lottery_data
        refresh_lottery_data()  # Обновляем данные из БД

        if is_drawn:
            return jsonify({
                "success": False,
                "msg": "❌ Розыгрыш уже начался! Новые билеты появятся в 00:00",
                "is_drawn": True
            }), 400

        user = get_user(user_id)
        success, msg = buy_ticket(user_id, user)

        # ✅ Если покупка успешна, обновляем данные лотереи
        if success:
            refresh_lottery_data()
            # Получаем актуальное количество билетов пользователя
            from lottery import lottery_tickets
            user_tickets_count = len([t for t in lottery_tickets if t.get("user_id") == user_id])
            return jsonify({
                "success": success,
                "msg": msg,
                "lp": user["lp"],
                "user_tickets": user_tickets_count,
                "prize_pool": lottery_pool
            })

        return jsonify({"success": success, "msg": msg, "lp": user["lp"]})

    @app.route('/api/lottery_status', methods=['POST'])
    def api_lottery_status():
        from lottery import get_lottery_status

        data = request.json
        if not data:
            return jsonify({"error": "No data"}), 400

        user_id = data.get('user_id')
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"error": "Invalid user_id"}), 400

        result = get_lottery_status(user_id)
        return jsonify(result)

    @app.route('/api/lottery_all_tickets', methods=['GET'])
    def api_lottery_all_tickets():
        with db.get_cursor() as cursor:
            cursor.execute("SELECT tickets FROM lottery LIMIT 1")
            row = cursor.fetchone()
            if row and row['tickets']:
                tickets = json.loads(row['tickets']) if row['tickets'] else []
                result = []
                for ticket in tickets:
                    uid = ticket.get('user_id')
                    cursor.execute("SELECT username, first_name, role FROM users WHERE user_id=?", (uid,))
                    user_row = cursor.fetchone()
                    username = user_row['username'] if user_row and user_row['username'] else (
                        user_row['first_name'] if user_row else 'Игрок')
                    ticket['username'] = escape_html(username)
                    ticket['role'] = user_row['role'] if user_row else 'player'
                    result.append(ticket)
                return jsonify({"tickets": result})
        return jsonify({"tickets": []})

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
            return jsonify({"success": False, "message": "No data"}), 400

        user_id = data.get('user_id')
        ticket_number = data.get('ticket_number')
        cell_index = data.get('cell_index')  # ← ДОБАВЛЯЕМ ПОДДЕРЖКУ ОДНОЙ КЛЕТКИ

        if not user_id or ticket_number is None:
            return jsonify({"success": False, "message": "Missing parameters"}), 400

        from lottery import lottery_tickets, save_lottery, refresh_lottery_data, is_drawn

        refresh_lottery_data()

        # Если розыгрыш не начался — запрещаем открывать клетки
        if not is_drawn:
            return jsonify({"success": False, "message": "Розыгрыш ещё не начался!"}), 400

        for ticket in lottery_tickets:
            if ticket.get("number") == ticket_number and ticket.get("user_id") == user_id:

                # Если указан конкретный индекс клетки (стирание одной клетки)
                if cell_index is not None and 0 <= cell_index < 12:
                    if not ticket["revealed"][cell_index]:
                        ticket["revealed"][cell_index] = True
                        save_lottery()
                        print(f"🔓 Клетка {cell_index} билета {ticket_number} открыта игроком {user_id}")
                        return jsonify({"success": True, "message": "Клетка открыта", "cell_index": cell_index})
                    else:
                        return jsonify({"success": False, "message": "Клетка уже открыта"})

                # Иначе открываем все клетки билета
                revealed_count = 0
                for i in range(12):
                    if not ticket["revealed"][i]:
                        ticket["revealed"][i] = True
                        revealed_count += 1

                if revealed_count > 0:
                    save_lottery()
                    print(f"🔓 Открыто {revealed_count} клеток билета {ticket_number} игроком {user_id}")
                    return jsonify({"success": True, "message": f"Открыто {revealed_count} клеток",
                                    "revealed_count": revealed_count})
                else:
                    return jsonify({"success": False, "message": "Все клетки уже открыты"})

        return jsonify({"success": False, "message": "Ticket not found"})

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
                return jsonify(
                    {"success": True, "msg": f"Открыто {revealed_count} клеток в {tickets_revealed} билетах!",
                     "revealed": revealed_count})
            return jsonify({"success": False, "msg": "Нет неоткрытых клеток"})



    @app.route('/api/recent_players', methods=['GET'])
    def api_recent_players():
        if not check_rate_limit(f"recent_players_{request.remote_addr}", limit=20, window_seconds=60):
            return jsonify([]), 429
        with db.get_cursor() as cursor:
            cursor.execute('''SELECT h.user_id, h.username, h.ticket_number, h.created_at, u.avatar_url, u.first_name, u.role 
                              FROM lottery_tickets_history h 
                              LEFT JOIN users u ON h.user_id = u.user_id 
                              ORDER BY h.created_at DESC LIMIT 5''')
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
                players.append({
                    "user_id": row['user_id'],
                    "username": escape_html(display_name),
                    "avatar_url": row['avatar_url'] or '',
                    "time_ago": time_ago,
                    "ticket_number": row['ticket_number'],
                    "role": row['role'] if row['role'] else 'player'
                })
            return jsonify(players)

    # === ЛИДЕРБОРД API ===

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
                result.append({
                    "rank": i + 1,
                    "user_id": row['user_id'],
                    "username": escape_html(display_name),
                    "total_clicks": row['total_clicks'],
                    "avatar": avatar,
                    "role": row['role'] if row['role'] else 'player',
                    "hide_from_top": hide_from_top
                })
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
            rewards = {1: "🏆 70 LP + 200 WG", 2: "🥈 50 LP + 150 WG", 3: "🥉 35 LP + 120 WG",
                       4: "🎖️ 25 LP + 100 WG", 5: "🎖️ 15 LP + 75 WG"}
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

    # === ПРОФИЛЬ И НАСТРОЙКИ API ===

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
            cursor.execute('''SELECT wg, lp, total_clicks, likes, dislikes, username, first_name, upgrade_counts, avatar_url, usdt, wins, role, stars, max_energy, energy_upgrades, settings 
                              FROM users WHERE user_id=?''', (user_id,))
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
                return jsonify({
                    "wg": row['wg'], "lp": row['lp'], "total_clicks": row['total_clicks'],
                    "likes": row['likes'] or 0, "dislikes": row['dislikes'] or 0,
                    "username": escape_html(display_name), "avatar_url": row['avatar_url'] or "👤",
                    "usdt": row['usdt'] if 'usdt' in row.keys() else 0,
                    "wins": row['wins'] if 'wins' in row.keys() else 0,
                    "role": row['role'] if 'role' in row.keys() else 'player',
                    "stars": row['stars'] if 'stars' in row.keys() else 0,
                    "max_energy": row['max_energy'] if 'max_energy' in row.keys() else 500,
                    "energy_upgrades": row['energy_upgrades'] if 'energy_upgrades' in row.keys() else 0,
                    "hide_from_top": hide_from_top
                })
        return jsonify({"error": "Пользователь не найден"})

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
                    return jsonify(
                        {"success": False, "msg": f"Через {int(hours_left)}ч {int((hours_left % 1) * 60)}мин"})
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

    # === РЕФЕРАЛЫ API ===

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
                SELECT r.username, r.first_name, r.created_at, r.total_spent_lp, r.total_earned_wg
                FROM referrals r 
                WHERE r.referrer_id = ?
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
                    "username": escape_html(name),
                    "date": row['created_at'],
                    "spent_lp": row['total_spent_lp'] or 0,
                    "earned_lp": round(earned_lp, 2),
                    "earned_wg": round(earned_wg, 4)
                })
        return jsonify({
            "referrals": referrals,
            "total_earned_lp": round(total_earned_lp, 2),
            "total_earned_wg": round(total_earned_wg, 4)
        })

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

    # === TON КОШЕЛЬКИ И ПЛАТЕЖИ API ===

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
            invalidate_cache(user_id)
            user = get_user(user_id)
            add_log(f"🔗 Привязал TON кошелёк: {wallet_address[:6]}...{wallet_address[-4:]}", user_id,
                    user.get('username', 'Unknown'))
            return jsonify({"success": True, "wallet": wallet_address})
        except Exception as e:
            print(f"Ошибка при сохранении кошелька: {e}")
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
        proj_wallet = PROJECT_WALLET_ADDRESS
        if not proj_wallet:
            return jsonify({"success": False, "error": "Ошибка конфигурации платежного шлюза"}), 500
        payment_amount_ton = 0.18
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
            expected_amount = float(expected_amount)
            confirmed, amount_paid, tx_hash = check_ton_transaction(sender_wallet, expected_amount, user_id)
            if confirmed:
                success, message, upgrade_data = grant_energy_upgrade(user_id)
                if success:
                    user = get_user(user_id)
                    add_admin_log(
                        f"💎 Купил энергетический усилитель за TON ({expected_amount} TON) | +50 макс. энергии, +50 LP",
                        user_id, user.get('username') or f"User_{user_id}", details=f"Хэш транзакции: {tx_hash}")
                    send_telegram_message(user_id,
                                          f"✨ **Оплата через TON получена!**\n\n⚡️ +50 к максимальной энергии\n💎 +50 LP на баланс\n\n💪 Энергетический усилитель успешно активирован!")
                    return jsonify({'confirmed': True, 'tx_hash': tx_hash})
                else:
                    return jsonify({'confirmed': False, 'error': message}), 400
            return jsonify({'confirmed': False})
        except Exception as e:
            print(f"Ошибка проверки платежа: {e}")
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
            invalidate_cache(user_id)
            user = get_user(user_id)
            add_log(f"🔗 Отвязал TON кошелёк", user_id, user.get('username', 'Unknown'))
            return jsonify({"success": True, "message": "Кошелёк отвязан"})
        except Exception as e:
            print(f"Ошибка отвязки кошелька: {e}")
            return jsonify({"success": False, "error": "Ошибка базы данных"}), 500

    # === LP БУСТЕР API ===

    @app.route('/api/create_lp_boost_invoice', methods=['POST'])
    def api_create_lp_boost_invoice():
        data = request.json
        if not data:
            return jsonify({"success": False, "msg": "No data"}), 400
        user_id = data.get('user_id')
        chat_id = data.get('chat_id', user_id)
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"success": False, "msg": "Invalid user_id"}), 400
        try:
            title = "💎 LP Бустер"
            description = "Пополняет баланс на 50 LP!"
            payload = json.dumps({"user_id": user_id, "type": "lp_boost"})
            provider_token = ""
            currency = "XTR"
            prices = [{"label": "LP Бустер", "amount": 22}]
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createInvoiceLink"
            data = {"title": title, "description": description, "payload": payload, "provider_token": provider_token,
                    "currency": currency, "prices": prices}
            verify_ssl = not DEBUG_MODE
            response = requests.post(url, json=data, timeout=10, verify=verify_ssl)
            result = response.json()
            if result.get("ok"):
                return jsonify({"success": True, "invoice_link": result["result"]})
            return jsonify({"success": False, "msg": "Ошибка создания счёта"})
        except Exception as e:
            print(f"Ошибка создания LP счёта: {e}")
            return jsonify({"success": False, "msg": str(e)}), 500

    @app.route('/api/ton/create_lp_boost_payment', methods=['POST'])
    def api_ton_create_lp_boost_payment():
        data = request.json or {}
        user_id = data.get('user_id')
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"success": False, "error": "Неавторизованный запрос"}), 400
        proj_wallet = PROJECT_WALLET_ADDRESS
        if not proj_wallet:
            return jsonify({"success": False, "error": "Ошибка конфигурации платежного шлюза"}), 500
        payment_amount_ton = 0.13
        payment_amount_nano = int(payment_amount_ton * 1e9)
        return jsonify({
            "success": True,
            "wallet_address": proj_wallet,
            "amount": payment_amount_ton,
            "amount_nano": payment_amount_nano,
            "comment": f"WereGood_LP:{user_id}"
        })

    @app.route('/api/ton/check_lp_boost_payment', methods=['POST'])
    def check_lp_boost_payment():
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
                user = get_user(user_id)
                old_lp = user['lp']
                new_lp = old_lp + 50
                safe_update_user(user_id, lp=new_lp)
                add_admin_log(f"💎 Купил LP Бустер за TON ({expected_amount} TON) | +50 LP",
                              user_id, user.get('username') or f"User_{user_id}", details=f"Хэш транзакции: {tx_hash}")
                send_telegram_message(user_id,
                                      f"✨ **LP Бустер активирован!**\n\n💎 +50 LP на баланс!\n\nСпасибо за поддержку проекта!")
                return jsonify({'confirmed': True, 'tx_hash': tx_hash})
            return jsonify({'confirmed': False})
        except Exception as e:
            print(f"Ошибка проверки LP платежа: {e}")
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
                (user_id,))
            row = cursor.fetchone()
            count = row['count'] if row else 0
        return jsonify({"success": True, "count": count})

    # === STARS ОПЛАТА API ===

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
            print(f"Ошибка получения баланса звезд: {e}")
        user = get_user(user_id)
        return jsonify({"success": True, "balance": user['stars']})

    # === ПРОМОКОДЫ API ===

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
            cursor.execute("SELECT id FROM promo_activations WHERE promo_id = ? AND user_id = ?",
                           (promo['id'], user_id))
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
            add_log(f"🎁 Активировал промокод {code} | +{promo['reward_amount']} {promo['reward_type'].upper()}",
                    user_id,
                    user['username'], old_value=old_value, new_value=new_value, currency=promo['reward_type'])
            return jsonify(
                {"success": True, "message": f"Вы получили +{promo['reward_amount']} {promo['reward_type'].upper()}!",
                 "reward_type": promo['reward_type'], "reward_amount": promo['reward_amount']})

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
            cursor.execute("SELECT id FROM promo_activations WHERE promo_id = ? AND user_id = ?",
                           (promo['id'], user_id))
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
                            "reward_amount": promo['reward_amount'], "remaining": remaining,
                            "max_uses": promo['max_uses'],
                            "has_password": bool(promo['password'])})

    # === ПРЕФИКСЫ API ===

    @app.route('/api/get_available_prefixes', methods=['POST'])
    def api_get_available_prefixes():
        data = request.json
        if not data:
            return jsonify({"success": True, "prefixes": [
                {"id": "player", "name": "Игрок", "icon": "🎮", "desc": "Выдаётся абсолютно всем игрокам",
                 "color": "player"}
            ], "current": "player"}), 400

        user_id = data.get('user_id')
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"success": True, "prefixes": [
                {"id": "player", "name": "Игрок", "icon": "🎮", "desc": "Выдаётся абсолютно всем игрокам",
                 "color": "player"}
            ], "current": "player"}), 400

        user = get_user(user_id)
        unlocked = user.get('unlocked_prefixes', ['player'])

        # Убедись, что legend есть в unlocked если роль legend
        if user.get('role') == 'legend' and 'legend' not in unlocked:
            unlocked.append('legend')
            safe_update_user(user_id, unlocked_prefixes=unlocked)

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

    # === ВЫВОД СРЕДСТВ API ===

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
            # create_withdrawal_request_db
            created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with db.get_cursor() as cursor:
                cursor.execute(
                    'INSERT INTO withdrawal_requests (user_id, username, amount, address, network, status, created_at) VALUES (?, ?, ?, ?, ?, "pending", ?)',
                    (user_id, user['username'], amount, address, network, created_at))
            add_log(f"💸 Создал заявку на вывод {amount} USDT", user_id, user['username'], old_value=user['usdt'],
                    new_value=user['usdt'] - amount, currency="usdt")
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

    # === ЕЖЕДНЕВНЫЕ НАГРАДЫ API ===

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

        def check_and_reset_streak(uid):
            with db.get_cursor() as cursor:
                cursor.execute("SELECT current_day, last_claim_date FROM daily_rewards WHERE user_id=?", (uid,))
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
                        (now.isoformat(), uid))
                    add_log(f"🔄 Серия ежедневных наград сброшена (пропущено более 48ч)", uid, str(uid))
                    return True
            return False

        check_and_reset_streak(user_id)

        with db.get_cursor() as cursor:
            cursor.execute("SELECT * FROM daily_rewards WHERE user_id=?", (user_id,))
            row = cursor.fetchone()
            if not row:
                now = datetime.datetime.now().isoformat()
                cursor.execute(
                    'INSERT INTO daily_rewards (user_id, current_day, last_claim_date, streak_start_date, recovered_count) VALUES (?, 1, ?, ?, 0)',
                    (user_id, now, now))
                return jsonify({"current_day": 1, "can_claim": True, "next_claim_time": None, "recovered_count": 0,
                                "lost_streak": False})
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
            return jsonify({"current_day": current_day, "can_claim": can_claim,
                            "next_claim_time": next_claim_time.isoformat() if next_claim_time else None,
                            "recovered_count": recovered_count, "lost_streak": lost_streak})

    @app.route('/api/claim_daily', methods=['POST'])
    def api_claim_daily():
        data = request.json
        if not data:
            return jsonify({"success": False, "msg": "No data"}), 400
        user_id = data.get('user_id')
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"success": False, "msg": "Invalid user_id"}), 400

        def give_daily_reward(uid, day):
            reward = DAILY_REWARDS.get(day)
            if not reward:
                return False
            user = get_user(uid)
            if reward["wg"] > 0:
                safe_update_user(uid, wg=user["wg"] + reward["wg"])
                add_log(f"🎁 Ежедневная награда: +{reward['wg']} WG", uid, user['username'])
            if reward["lp"] > 0:
                safe_update_user(uid, lp=user["lp"] + reward["lp"])
                add_log(f"🎁 Ежедневная награда: +{reward['lp']} LP", uid, user['username'])
            if reward["energy_limit"] > 0:
                new_max_energy = user["max_energy"] + reward["energy_limit"]
                safe_update_user(uid, max_energy=new_max_energy)
                add_log(f"🎁 Ежедневная награда: +{reward['energy_limit']} к макс. энергии", uid, user['username'])
            return True

        with db.get_cursor() as cursor:
            cursor.execute("SELECT current_day, last_claim_date, recovered_count FROM daily_rewards WHERE user_id=?",
                           (user_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"success": False, "msg": "Ошибка"})
            last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
            now = datetime.datetime.now()
            time_diff = (now - last_claim).total_seconds()
            current_day = row['current_day']
            if current_day != 1 and time_diff < 86400:
                remaining = int(86400 - time_diff)
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                return jsonify({"success": False, "msg": f"Следующая награда через {hours} ч {minutes} мин"})
            recovered_count = row['recovered_count'] or 0
            give_daily_reward(user_id, current_day)
            new_day = current_day + 1
            cursor.execute(
                'UPDATE daily_rewards SET current_day = ?, last_claim_date = ?, recovered_count = ? WHERE user_id = ?',
                (new_day, now.isoformat(), recovered_count, user_id))
            return jsonify({"success": True, "msg": f"Награда за {current_day} день получена!", "new_day": new_day})

    @app.route('/api/recover_daily', methods=['POST'])
    def api_recover_daily():
        data = request.json
        if not data:
            return jsonify({"success": False, "msg": "No data"}), 400
        user_id = data.get('user_id')
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"success": False, "msg": "Invalid user_id"}), 400
        user = get_user(user_id)
        if user['stars'] < 20:
            return jsonify({"success": False, "msg": "Недостаточно Stars (нужно 20)"})
        with db.get_cursor() as cursor:
            cursor.execute("SELECT current_day, last_claim_date FROM daily_rewards WHERE user_id=?", (user_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"success": False, "msg": "Ошибка"})
            now = datetime.datetime.now()
            last_claim = datetime.datetime.fromisoformat(row['last_claim_date'])
            time_diff = (now - last_claim).total_seconds()
            current_day = row['current_day']
            if time_diff < 86400 or time_diff >= 172800:
                return jsonify({"success": False, "msg": "Сейчас нельзя восстановить серию"})
            safe_update_user(user_id, stars=user['stars'] - 20)
            cursor.execute(
                'UPDATE daily_rewards SET last_claim_date = ?, recovered_count = recovered_count + 1 WHERE user_id = ?',
                (now.isoformat(), user_id))
            add_log(f"⭐ Восстановил серию ежедневных наград за 20 Stars (день {current_day})", user_id,
                    user['username'])
            return jsonify(
                {"success": True, "msg": f"Серия восстановлена! Вы можете забрать награду за {current_day} день!",
                 "current_day": current_day})

    # === ЗАДАНИЯ API ===

    @app.route('/api/tasks', methods=['GET'])
    def api_get_tasks():
        uid = request.args.get('user_id')
        is_valid, uid = validate_user_id(uid)
        if not is_valid:
            return jsonify({'success': False, 'error': 'Invalid user_id'}), 400
        with db.get_cursor() as cursor:
            cursor.execute('''
                SELECT t.*, CASE WHEN ut.id IS NOT NULL THEN 1 ELSE 0 END as is_completed
                FROM tasks t
                LEFT JOIN user_tasks ut ON t.id = ut.task_id AND ut.user_id = ?
                WHERE t.is_active = 1
                ORDER BY is_completed ASC, t.created_at DESC
            ''', (uid,))
            rows = cursor.fetchall()
            tasks = []
            for row in rows:
                tasks.append({
                    'id': row['id'], 'title': row['title'], 'channel_link': row['channel_link'],
                    'channel_username': row['channel_username'], 'channel_avatar': row['channel_avatar'],
                    'reward_amount': row['reward_amount'], 'reward_type': row['reward_type'],
                    'daily_limit': row['daily_limit'], 'total_limit': row['total_limit'],
                    'completed_count': row['completed_count'], 'days_remaining': row['days_remaining'],
                    'is_completed': bool(row['is_completed'])
                })
            return jsonify({'success': True, 'tasks': tasks})

    @app.route('/api/check_task_subscription', methods=['POST'])
    def api_check_task_subscription():
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
                return jsonify({'success': False, 'error': 'Задание больше недоступно'})
            cursor.execute('SELECT * FROM user_tasks WHERE user_id = ? AND task_id = ?', (user_id, task_id))
            if cursor.fetchone():
                return jsonify({'success': False, 'error': 'Вы уже получили награду за это задание'})
            channel_username = task['channel_username'].replace('@', '')
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMember"
            try:
                response = requests.get(url, params={'chat_id': f'@{channel_username}', 'user_id': user_id}, timeout=10)
                data = response.json()
                if data.get('ok'):
                    status = data.get('result', {}).get('status', '')
                    if status in ['member', 'administrator', 'creator']:
                        user = get_user(user_id)
                        if task['reward_type'] == 'wg':
                            old_value = user['wg']
                            new_value = old_value + task['reward_amount']
                            safe_update_user(user_id, wg=new_value)
                            add_log(f"📋 Выполнил задание '{task['title']}' | +{task['reward_amount']} WG", user_id,
                                    user['username'])
                        elif task['reward_type'] == 'lp':
                            old_value = user['lp']
                            new_value = old_value + task['reward_amount']
                            safe_update_user(user_id, lp=new_value)
                            add_log(f"📋 Выполнил задание '{task['title']}' | +{task['reward_amount']} LP", user_id,
                                    user['username'])
                        elif task['reward_type'] == 'usdt':
                            old_value = user['usdt']
                            new_value = old_value + task['reward_amount']
                            safe_update_user(user_id, usdt=new_value)
                            add_log(f"📋 Выполнил задание '{task['title']}' | +{task['reward_amount']} USDT", user_id,
                                    user['username'])
                        elif task['reward_type'] == 'energy':
                            current_energy, _ = calculate_energy(user)
                            new_energy = min(user['max_energy'], current_energy + task['reward_amount'])
                            update_energy_in_db(user_id, user, new_energy)
                            add_log(f"📋 Выполнил задание '{task['title']}' | +{task['reward_amount']} энергии", user_id,
                                    user['username'])
                        cursor.execute('INSERT INTO user_tasks (user_id, task_id) VALUES (?, ?)', (user_id, task_id))
                        cursor.execute('UPDATE tasks SET completed_count = completed_count + 1 WHERE id = ?',
                                       (task_id,))
                        update_achievement_progress(user_id, 'task_master', 1)
                        return jsonify({'success': True,
                                        'message': f'✅ Вы получили +{task["reward_amount"]} {task["reward_type"].upper()}!',
                                        'reward': {'type': task['reward_type'], 'amount': task['reward_amount']}})
                    else:
                        return jsonify({'success': False, 'error': 'Вы не подписаны на канал'})
                else:
                    return jsonify({'success': False, 'error': 'Не удалось проверить подписку'})
            except Exception as e:
                print(f"Ошибка проверки подписки: {e}")
                return jsonify({'success': False, 'error': 'Ошибка при проверке'}), 500

    # === ДОСТИЖЕНИЯ API ===

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

    # === ТУТОРИАЛ API ===

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

    # === ФОРТУНА API ===

    @app.route('/api/fortune/status', methods=['GET'])
    def api_fortune_status():
        with fortune_lock:
            if not current_fortune_round.get('round_id'):
                restore_fortune_from_db()
            time_left = max(0, int(current_fortune_round.get('end_time', 0) - time.time()))
            yellow_bets_sorted = sorted(current_fortune_round.get('yellow_bets', []),
                                        key=lambda x: x.get('net_amount', 0), reverse=True)[:5]
            red_bets_sorted = sorted(current_fortune_round.get('red_bets', []), key=lambda x: x.get('net_amount', 0),
                                     reverse=True)[:5]
            return jsonify({
                "success": True,
                "round_id": current_fortune_round.get('round_id'),
                "time_left": time_left,
                "yellow_pool": current_fortune_round.get('yellow_pool', 0),
                "red_pool": current_fortune_round.get('red_pool', 0),
                "yellow_bets": [{
                    "userId": bet.get('user_id'),
                    "amount": bet.get('amount', 0),
                    "netAmount": bet.get('net_amount', 0),
                    "avatarUrl": bet.get('avatar_url', ''),
                    "username": bet.get('username', '')
                } for bet in yellow_bets_sorted],
                "red_bets": [{
                    "userId": bet.get('user_id'),
                    "amount": bet.get('amount', 0),
                    "netAmount": bet.get('net_amount', 0),
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
                new_end_time = time.time() + FORTUNE_ROUND_DURATION
                current_fortune_round['end_time'] = new_end_time
                with db.get_cursor() as cursor:
                    cursor.execute('UPDATE fortune_rounds SET end_time = ? WHERE round_id = ?',
                                   (datetime.datetime.fromtimestamp(new_end_time).isoformat(),
                                    current_fortune_round['round_id']))
            round_id = current_fortune_round['round_id']
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
            if existing_bet:
                new_total = existing_bet['amount'] + amount
                new_net_total = existing_bet['net_amount'] + net_amount
                existing_bet['amount'] = new_total
                existing_bet['net_amount'] = new_net_total
                with db.get_cursor() as cursor:
                    cursor.execute(
                        'UPDATE fortune_active_bets SET amount = ?, net_amount = ? WHERE round_id = ? AND user_id = ? AND team = ?',
                        (new_total, new_net_total, round_id, user_id, team))
                add_log(
                    f"🎲 ФОРТУНА: ДОБАВИЛ к ставке {amount} WG (всего {new_total} WG) на команду {'Жёлтые' if team == 'yellow' else 'Красные'}",
                    user_id, user['username'], old_value=user['wg'], new_value=user['wg'] - amount, currency="wg")
                result_amount = new_total
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
                    cursor.execute(
                        'INSERT INTO fortune_active_bets (round_id, user_id, team, amount, net_amount) VALUES (?, ?, ?, ?, ?)',
                        (round_id, user_id, team, amount, net_amount))
                add_log(f"🎲 ФОРТУНА: Новая ставка {amount} WG на команду {'Жёлтые' if team == 'yellow' else 'Красные'}",
                        user_id, user['username'], old_value=user['wg'], new_value=user['wg'] - amount, currency="wg")
                result_amount = amount
                update_fortune_achievements(user_id, bet_amount=amount, is_win=False, is_new_round=True)
            if team == 'yellow':
                current_fortune_round['yellow_pool'] += net_amount
            else:
                current_fortune_round['red_pool'] += net_amount
            with db.get_cursor() as cursor:
                cursor.execute('UPDATE fortune_rounds SET yellow_pool = ?, red_pool = ? WHERE round_id = ?',
                               (current_fortune_round['yellow_pool'], current_fortune_round['red_pool'], round_id))
            try:
                yellow_players_sorted = sorted(current_fortune_round['yellow_bets'], key=lambda x: x['net_amount'],
                                               reverse=True)[:5]
                red_players_sorted = sorted(current_fortune_round['red_bets'], key=lambda x: x['net_amount'],
                                            reverse=True)[:5]
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
                print(f"Socket emit error: {e}")
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
            cursor.execute('SELECT * FROM fortune_history WHERE user_id = ? ORDER BY id DESC LIMIT ?', (user_id, limit))
            rows = cursor.fetchall()
            history = []
            for row in rows:
                history.append({
                    "id": row['id'], "round_id": row['round_id'], "team": row['team'],
                    "amount": row['amount'], "result": row['result'], "win_amount": row['win_amount'],
                    "created_at": row['created_at']
                })
            return jsonify({"success": True, "history": history})

    @app.route('/api/fortune/end_round', methods=['POST'])
    def api_fortune_end_round():
        try:
            end_fortune_round()
            return jsonify({"status": "success", "message": "Раунд успешно завершен"})
        except Exception as e:
            print(f"Ошибка в роуте end_round: {e}")
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
                    "id": row['id'], "user_id": row['user_id'], "round_id": row['round_id'],
                    "team": row['team'], "amount": row['amount'], "win_amount": row['win_amount'],
                    "created_at": row['created_at'],
                    "username": row['username'] or row['first_name'] or f"Player_{row['user_id']}",
                    "avatar_url": row['avatar_url'] or ''
                })
            return jsonify({"success": True, "history": history})

    @app.route('/api/fortune/history_all', methods=['GET'])
    def api_fortune_history_all():
        limit = request.args.get('limit', 50, type=int)
        with db.get_cursor() as cursor:
            cursor.execute('SELECT * FROM fortune_history ORDER BY id DESC LIMIT ?', (limit,))
            rows = cursor.fetchall()
            history = []
            for row in rows:
                history.append({
                    "id": row['id'], "user_id": row['user_id'], "round_id": row['round_id'],
                    "team": row['team'], "amount": row['amount'], "result": row['result'],
                    "win_amount": row['win_amount'], "created_at": row['created_at']
                })
            return jsonify({"success": True, "history": history})

    @app.route('/api/fortune/stats', methods=['POST'])
    def api_fortune_stats():
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data"}), 400
        user_id = data.get('user_id')
        is_valid, user_id = validate_user_id(user_id)
        if not is_valid:
            return jsonify({"success": False, "error": "Invalid user_id"}), 400
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT fortune_bets_count, fortune_wins_count, fortune_total_bet_amount FROM users WHERE user_id = ?",
                (user_id,))
            row = cursor.fetchone()
            return jsonify({
                "success": True,
                "stats": {
                    "bets_count": row['fortune_bets_count'] or 0,
                    "wins_count": row['fortune_wins_count'] or 0,
                    "total_bet_amount": row['fortune_total_bet_amount'] or 0
                }
            })

    # === СИНХРОНИЗАЦИЯ API ===

    @app.route('/api/sync', methods=['POST'])
    def api_sync():
        from lottery import refresh_lottery_data
        refresh_lottery_data()  # ← ПРИНУДИТЕЛЬНО ОБНОВЛЯЕМ ДАННЫЕ ЛОТЕРЕИ ИЗ БД

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

        # Используем обновлённые глобальные переменные
        from lottery import lottery_pool, lottery_tickets, is_drawn, winning_numbers, lottery_phase

        user_tickets = [t for t in lottery_tickets if t.get("user_id") == user_id]
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

        # Добавляем заголовки для отключения кэширования
        response = jsonify({
            "success": True,
            "status": status_data,
            "lottery": lottery_data,
            "leaderboard": leaderboard_data,
            "recent_players": recent_players,
            "online_count": len(online_users),
            "server_time": time.time()
        })

        # Отключаем кэширование на клиенте
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'

        return response