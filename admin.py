import json
import time
import datetime
import threading
import secrets
from flask import request, jsonify
from database import db
from models import (
    get_user, safe_update_user, add_admin_log, invalidate_cache,
    is_banned, ban_user, unban_user, delete_user, unlock_prefix,
    get_logs, add_log, update_energy_in_db, get_achievements_top,
    online_users, online_users_lock, update_online_count, send_telegram_message
)
from utils import require_admin, check_rate_limit
from config import ADMIN_SECRET

# Импортируем из lottery все необходимые переменные и функции
from lottery import (
    lottery_pool, lottery_tickets, global_ticket_counter,
    winning_numbers, is_drawn, draw_time, lottery_phase,
    perform_draw, reset_lottery, save_lottery, refresh_lottery_data
)


# ========== ДОПОЛНИТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С ВЫВОДАМИ И СТАТИСТИКОЙ ==========

def get_withdrawal_requests_db():
    """Получает все заявки на вывод из БД"""
    with db.get_cursor() as cursor:
        cursor.execute("SELECT * FROM withdrawal_requests ORDER BY id DESC")
        rows = cursor.fetchall()
        withdrawals = []
        for row in rows:
            withdrawals.append({
                "id": row['id'],
                "user_id": row['user_id'],
                "username": row['username'],
                "amount": row['amount'],
                "address": row['address'],
                "network": row['network'],
                "status": row['status'],
                "created_at": row['created_at'],
                "processed_at": row['processed_at']
            })
        return withdrawals


def process_withdrawal_db(withdrawal_id, status, admin_id, admin_name):
    """Обрабатывает заявку на вывод (одобрить/отклонить)"""
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


def get_stats_history(period='week', metric='clicks'):
    """Получает статистику для графиков"""
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
                val = row[metric] if row and metric in row.keys() else 0
                data.append(val or 0)
        elif period == 'week':
            for i in range(6, -1, -1):
                date = (now - datetime.timedelta(days=i)).strftime("%d.%m")
                labels.append(date)
                date_key = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                cursor.execute(
                    "SELECT clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users FROM stats_history WHERE date = ?",
                    (date_key,))
                row = cursor.fetchone()
                val = row[metric] if row and metric in row.keys() else 0
                data.append(val or 0)
        elif period == 'month':
            for i in range(29, -1, -1):
                date = (now - datetime.timedelta(days=i)).strftime("%d.%m")
                labels.append(date)
                date_key = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                cursor.execute(
                    "SELECT clicks, ad_views, stars_donated, online_peak, tickets_sold, new_users FROM stats_history WHERE date = ?",
                    (date_key,))
                row = cursor.fetchone()
                val = row[metric] if row and metric in row.keys() else 0
                data.append(val or 0)
        else:  # year
            for i in range(11, -1, -1):
                month_date = now - datetime.timedelta(days=30 * i)
                labels.append(month_date.strftime("%b %Y"))
                month_start = month_date.strftime("%Y-%m")
                cursor.execute(
                    "SELECT SUM(clicks) as clicks, SUM(ad_views) as ad_views, SUM(stars_donated) as stars_donated, MAX(online_peak) as online_peak, SUM(tickets_sold) as tickets_sold, SUM(new_users) as new_users FROM stats_history WHERE date LIKE ?",
                    (f'{month_start}%',))
                row = cursor.fetchone()
                val = row[metric] if row and metric in row.keys() else 0
                data.append(val or 0)
    return {"labels": labels, "data": data}


def register_admin_routes(app):
    # === СТАТИСТИКА ===
    @app.route('/api/admin/stats', methods=['GET'])
    @require_admin
    def api_admin_stats():
        refresh_lottery_data()  # Обновляем данные лотереи
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
            total_current_tickets = len(lottery_tickets)
            players_in_lottery = len(set([t.get('user_id') for t in lottery_tickets if t.get('user_id')]))
            with online_users_lock:
                online_count = len(online_users)
            return jsonify({
                "success": True,
                "total_users": total_users,
                "total_wg": round(stats['total_wg'] or 0, 2),
                "total_lp": int(stats['total_lp'] or 0),
                "total_usdt": round(stats['total_usdt'] or 0, 2),
                "total_wins": int(stats['total_wins'] or 0),
                "total_clicks": int(stats['total_clicks'] or 0),
                "upgrade_1": int(upgrade_stats['upgrade_1'] or 0),
                "upgrade_2": int(upgrade_stats['upgrade_2'] or 0),
                "upgrade_3": int(upgrade_stats['upgrade_3'] or 0),
                "total_stars": int(star_stats['total_stars'] or 0),
                "total_energy_upgrades": int(star_stats['total_energy_upgrades'] or 0),
                "total_tickets_history": int(ticket_history['total_tickets'] or 0),
                "total_current_tickets": total_current_tickets,
                "players_in_lottery": players_in_lottery,
                "lottery_pool": lottery_pool,
                "is_drawn": is_drawn,
                "online": online_count
            })

    # === ЛОТЕРЕЯ АДМИН ===
    @app.route('/api/admin/lottery_participants', methods=['GET'])
    @require_admin
    def api_admin_lottery_participants():
        refresh_lottery_data()  # Обновляем данные
        participants = []
        for ticket in lottery_tickets:
            # Получаем имя пользователя
            user = get_user(ticket.get('user_id'))
            username = user.get('username') or user.get('first_name') or f"Player_{ticket.get('user_id')}"
            participants.append({
                "user_id": ticket.get('user_id'),
                "username": username,
                "ticket_number": ticket.get('number'),
                "purchase_number": ticket.get('purchase_number'),
                "revealed_count": sum(ticket.get('revealed', [])),
                "numbers": ticket.get('numbers', [])
            })
        participants.sort(key=lambda x: x['ticket_number'])
        return jsonify({
            "success": True,
            "participants": participants,
            "count": len(participants),
            "prize_pool": lottery_pool,
            "is_drawn": is_drawn,
            "winning_numbers": winning_numbers if is_drawn else []
        })

    @app.route('/api/admin/lottery_action', methods=['POST'])
    @require_admin
    def api_admin_lottery_action():
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON"}), 400

        action = data.get('action')
        admin_id = request.args.get('user_id', 'Admin')
        admin_name = "Admin"

        # Получаем реальное имя админа
        if admin_id != 'Admin' and str(admin_id).isdigit():
            admin_user = get_user(int(admin_id))
            if admin_user:
                admin_name = admin_user.get('username') or admin_user.get('first_name') or str(admin_id)

        if action == 'force_draw':
            print("👑 Админ: принудительный розыгрыш лотереи")
            try:
                perform_draw()

                # Отправляем уведомление через Socket.IO всем игрокам
                try:
                    from bot import socketio
                    socketio.emit('draw_completed', {
                        'is_drawn': True,
                        'winning_numbers': winning_numbers,
                        'message': '🎲 Администратор запустил розыгрыш! Стирайте билеты в течение 3 часов!',
                        'end_time': draw_time.isoformat() if draw_time else None
                    })
                except Exception as e:
                    print(f"Socket emit error: {e}")

                add_admin_log(f"🎲 Принудительный розыгрыш лотереи", admin_id, admin_name)
                return jsonify({"success": True, "msg": "Розыгрыш запущен! Выигрышные номера сгенерированы."})
            except Exception as e:
                print(f"Ошибка при розыгрыше: {e}")
                return jsonify({"success": False, "error": str(e)}), 500

        elif action == 'reset_lottery':
            print("👑 Админ: принудительный сброс лотереи")
            try:
                reset_lottery()

                # Отправляем уведомление через Socket.IO всем игрокам
                try:
                    from bot import socketio
                    socketio.emit('draw_reset', {
                        'is_drawn': False,
                        'message': '🔄 Лотерея сброшена администратором! Можно покупать новые билеты.'
                    })
                except Exception as e:
                    print(f"Socket emit error: {e}")

                add_admin_log(f"🔄 Сброс лотереи", admin_id, admin_name)
                return jsonify({"success": True, "msg": "Лотерея сброшена! Все билеты удалены."})
            except Exception as e:
                print(f"Ошибка при сбросе: {e}")
                return jsonify({"success": False, "error": str(e)}), 500

        elif action == 'set_pool':
            try:
                global lottery_pool
                old_pool = lottery_pool
                new_pool = float(data.get('amount', 0))
                lottery_pool = round(new_pool, 2)
                save_lottery()
                add_admin_log(f"💰 Изменил призовой фонд с {old_pool} на {lottery_pool} USDT", admin_id, admin_name)
                return jsonify(
                    {"success": True, "msg": f"Фонд изменён на {lottery_pool} USDT", "new_pool": lottery_pool})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

        elif action == 'get_status':
            refresh_lottery_data()
            return jsonify({
                "success": True,
                "prize_pool": lottery_pool,
                "total_tickets": len(lottery_tickets),
                "is_drawn": is_drawn,
                "winning_numbers": winning_numbers if is_drawn else [],
                "lottery_phase": lottery_phase,
                "global_ticket_counter": global_ticket_counter
            })

        return jsonify({"success": False, "msg": "Неизвестное действие"})

    # === СПИСОК ПОЛЬЗОВАТЕЛЕЙ ДЛЯ РАССЫЛКИ ===
    @app.route('/api/admin/users_list', methods=['GET'])
    @require_admin
    def api_admin_users_list():
        with db.get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) as total FROM users")
            row = cursor.fetchone()
            return jsonify({"success": True, "total": row['total'] if row else 0})

    # === ПОИСК ИГРОКОВ ===
    @app.route('/api/admin/search_users', methods=['POST'])
    @require_admin
    def api_admin_search_users():
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON"}), 400
        query = data.get('query', '')
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT user_id, username, first_name, role, wg, lp, usdt, wins, total_clicks, stars, max_energy, energy_upgrades, unlocked_prefixes, settings FROM users WHERE user_id LIKE ? OR username LIKE ? OR first_name LIKE ? LIMIT 50",
                (f'%{query}%', f'%{query}%', f'%{query}%'))
            rows = cursor.fetchall()
            users = []
            for row in rows:
                hide_from_top = False
                if row['settings']:
                    try:
                        settings = json.loads(row['settings'])
                        hide_from_top = settings.get('hideFromTop', False)
                    except:
                        pass
                banned, _ = is_banned(row['user_id'])
                username_display = 'Аноним' if hide_from_top else (
                        row['username'] or row['first_name'] or str(row['user_id']))
                users.append({
                    "user_id": row['user_id'],
                    "username": username_display,
                    "role": row['role'] or 'player',
                    "wg": round(row['wg'] or 0, 2),
                    "lp": int(row['lp'] or 0),
                    "usdt": round(row['usdt'] or 0, 2),
                    "wins": int(row['wins'] or 0),
                    "total_clicks": int(row['total_clicks'] or 0),
                    "stars": int(row['stars'] or 0),
                    "max_energy": int(row['max_energy'] or 500),
                    "energy_upgrades": int(row['energy_upgrades'] or 0),
                    "unlocked_prefixes": json.loads(row['unlocked_prefixes']) if row['unlocked_prefixes'] else [
                        "player"],
                    "is_banned": banned,
                    "hide_from_top": hide_from_top
                })
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
                {"username": r['username'] or r['first_name'] or 'Игрок', "date": r['created_at'],
                 "spent_lp": r['total_spent_lp'] or 0, "earned": round((r['total_spent_lp'] or 0) * 0.05, 2)} for r in
                referrals]
        user['personal_logs'], _ = get_logs('all', 100, 0, None, None, str(user_id))
        return jsonify({"success": True, "user": user})

    # === РЕДАКТИРОВАНИЕ ИГРОКА ===
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
                add_admin_log(f"👑 Изменил роль с {old_role} на {new_role}", admin_id, admin_name, user_id,
                              user['username'])
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
            add_admin_log(f"🔨 ЗАБАНИЛ игрока на {days} дней. Причина: {reason}", admin_id, admin_name, user_id,
                          user['username'])
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

    # === УПРАВЛЕНИЕ ЭНЕРГЕТИЧЕСКИМИ УЛУЧШЕНИЯМИ ===
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
                add_admin_log(f"➕ Добавил {added} покупок усилителя игроку (без выдачи наград)", admin_id, admin_name,
                              user_id, user.get('username') or f"User_{user_id}")
            elif action == 'remove':
                safe_update_user(user_id, max_energy=new_max_energy, energy_upgrades=new_upgrades)
                add_admin_log(f"➖ Убавил {abs(added)} покупок усилителя у игрока", admin_id, admin_name, user_id,
                              user.get('username') or f"User_{user_id}")
            invalidate_cache(user_id)
            return jsonify({
                "success": True,
                "message": f"Покупки изменены: {current_upgrades} → {new_upgrades}/15{reward_text}",
                "old_upgrades": current_upgrades,
                "new_upgrades": new_upgrades,
                "new_max_energy": new_max_energy,
                "reward_applied": reward_applied
            })
        except Exception as e:
            print(f"Ошибка управления покупками: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # === УДАЛЕНИЕ ИГРОКА ===
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
            print(f"Ошибка удаления пользователя: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # === ЛОГИ ===
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

    # === ВЫВОД СРЕДСТВ ===
    @app.route('/api/admin/withdrawals', methods=['GET'])
    @require_admin
    def api_admin_withdrawals():
        withdrawals = get_withdrawal_requests_db()
        return jsonify({"success": True, "withdrawals": withdrawals})

    # === СБРОС ЛИМИТОВ ===
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
                "message": f"Лимиты рекламы сброшены! Удалено {deleted_count} записей за сегодня.",
                "deleted_today": deleted_count,
                "deleted_old": old_deleted
            })
        except Exception as e:
            print(f"Ошибка сброса лимитов рекламы: {e}")
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
                cursor.execute("DELETE FROM ad_watch_history WHERE user_id = ? AND date(watched_at) = date('now')",
                               (user_id,))
                deleted_count = cursor.rowcount
            admin_id = request.args.get('user_id', 'Admin')
            admin_name = "Admin"
            user = get_user(user_id)
            username = user.get('username') or user.get('first_name') or str(user_id)
            add_admin_log(
                f"🔄 СБРОС ЛИМИТОВ РЕКЛАМЫ для игрока {username} (ID: {user_id}): удалено {deleted_count} записей",
                admin_id, admin_name, user_id, username)
            return jsonify({
                "success": True,
                "message": f"Лимиты рекламы сброшены для игрока {username}! Удалено {deleted_count} записей за сегодня.",
                "deleted": deleted_count
            })
        except Exception as e:
            print(f"Ошибка сброса лимитов рекламы для пользователя {user_id}: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # === СБРОС ТОПА ДНЯ ===
    @app.route('/api/admin/reset_daily_clicks', methods=['POST'])
    @require_admin
    def api_admin_reset_daily_clicks():
        try:
            with db.get_cursor() as cursor:
                cursor.execute("UPDATE users SET daily_clicks = 0")
                count = cursor.rowcount
            admin_id = request.args.get('user_id', 'Admin')
            admin_name = "Admin"
            add_admin_log(f"🔄 СБРОС ТОПА ДНЯ: обнулил daily_clicks у {count} игроков", admin_id, admin_name)
            return jsonify({"success": True, "message": f"Топ дня сброшен! Обнулено {count} игроков", "count": count})
        except Exception as e:
            print(f"Ошибка сброса daily_clicks: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # === ОЧИСТКА КОШЕЛЬКОВ ===
    @app.route('/api/admin/clear_wallets', methods=['POST'])
    @require_admin
    def api_clear_wallets():
        try:
            with db.get_cursor() as cursor:
                cursor.execute("UPDATE users SET ton_wallet = ''")
                count = cursor.rowcount
            admin_id = request.args.get('user_id', 'Admin')
            admin_name = "Admin"
            add_admin_log(f"🗑️ Очистил все TON кошельки игроков (удалено {count} записей)", admin_id, admin_name)
            return jsonify({"success": True, "count": count})
        except Exception as e:
            print(f"Ошибка очистки кошельков: {e}")
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
            admin_id = request.args.get('user_id', 'Admin')
            admin_name = "Admin"
            add_admin_log(f"🗑️ Отвязал TON кошелёк у игрока", admin_id, admin_name, user_id)
            return jsonify({"success": True})
        except Exception as e:
            print(f"Ошибка очистки кошелька: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # === ГРАФИКИ ===
    @app.route('/api/admin/chart_data', methods=['GET'])
    @require_admin
    def api_admin_chart_data():
        period = request.args.get('period', 'week')
        metric = request.args.get('metric', 'clicks')
        result = get_stats_history(period, metric)
        return jsonify(
            {"success": True, "labels": result["labels"], "data": result["data"], "metric": metric, "period": period})

    # === ЗАДАНИЯ АДМИН ===
    @app.route('/api/admin/tasks', methods=['GET'])
    @require_admin
    def api_admin_get_tasks():
        with db.get_cursor() as cursor:
            cursor.execute('SELECT * FROM tasks ORDER BY created_at DESC')
            rows = cursor.fetchall()
            tasks = []
            for row in rows:
                tasks.append({
                    'id': row['id'], 'title': row['title'], 'channel_link': row['channel_link'],
                    'channel_username': row['channel_username'], 'channel_avatar': row['channel_avatar'],
                    'reward_amount': row['reward_amount'], 'reward_type': row['reward_type'],
                    'daily_limit': row['daily_limit'], 'total_limit': row['total_limit'],
                    'completed_count': row['completed_count'], 'days_remaining': row['days_remaining'],
                    'is_active': bool(row['is_active'])
                })
            return jsonify({'success': True, 'tasks': tasks})

    @app.route('/api/admin/create_task', methods=['POST'])
    @require_admin
    def api_admin_create_task():
        data = request.json
        title = data.get('title', '').strip()
        channel_link = data.get('channel_link', '').strip()
        channel_username = data.get('channel_username', '').strip()
        channel_avatar = data.get('channel_avatar', '').strip()
        reward_amount = int(data.get('reward_amount', 10))
        reward_type = data.get('reward_type', 'wg')
        daily_limit = int(data.get('daily_limit', 1))
        total_limit = int(data.get('total_limit', 100))
        days_remaining = int(data.get('days_remaining', 7))
        if not title or not channel_link or not channel_username:
            return jsonify({'success': False, 'error': 'Заполните все поля'}), 400
        if reward_amount <= 0:
            return jsonify({'success': False, 'error': 'Сумма награды должна быть больше 0'}), 400
        admin_id = request.args.get('user_id', 'Admin')
        admin_name = "Admin"
        with db.get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO tasks (title, channel_link, channel_username, channel_avatar, reward_amount, reward_type, daily_limit, total_limit, days_remaining)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (title, channel_link, channel_username, channel_avatar, reward_amount, reward_type, daily_limit,
                  total_limit, days_remaining))
            task_id = cursor.lastrowid
        add_admin_log(f"📋 Создал задание '{title}' (ID: {task_id})", admin_id, admin_name)
        return jsonify({'success': True, 'task_id': task_id})

    @app.route('/api/admin/update_task', methods=['POST'])
    @require_admin
    def api_admin_update_task():
        data = request.json
        task_id = data.get('task_id')
        title = data.get('title', '').strip()
        channel_link = data.get('channel_link', '').strip()
        channel_username = data.get('channel_username', '').strip()
        channel_avatar = data.get('channel_avatar', '').strip()
        reward_amount = int(data.get('reward_amount', 10))
        reward_type = data.get('reward_type', 'wg')
        daily_limit = int(data.get('daily_limit', 1))
        total_limit = int(data.get('total_limit', 100))
        days_remaining = int(data.get('days_remaining', 7))
        is_active = data.get('is_active', True)
        if not task_id:
            return jsonify({'success': False, 'error': 'task_id required'}), 400
        admin_id = request.args.get('user_id', 'Admin')
        admin_name = "Admin"
        with db.get_cursor() as cursor:
            cursor.execute('''
                UPDATE tasks SET title = ?, channel_link = ?, channel_username = ?, channel_avatar = ?,
                reward_amount = ?, reward_type = ?, daily_limit = ?, total_limit = ?,
                days_remaining = ?, is_active = ? WHERE id = ?
            ''', (title, channel_link, channel_username, channel_avatar, reward_amount, reward_type,
                  daily_limit, total_limit, days_remaining, is_active, task_id))
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

    # === ПРОМОКОДЫ АДМИН ===
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
                promos.append({
                    "id": row['id'], "code": row['code'], "reward_type": row['reward_type'],
                    "reward_amount": row['reward_amount'], "max_uses": row['max_uses'],
                    "used_count": row['used_count'] or 0, "has_password": bool(row['password']),
                    "created_by": row['created_by'], "created_at": row['created_at'],
                    "expires_at": row['expires_at'], "is_active": row['is_active'],
                    "activations": activations_list
                })
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
        return jsonify(
            {"success": True, "promo_id": promo_id, "code": code, "promo_url": telegram_url, "web_url": web_url})

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