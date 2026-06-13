# fortune.py
import time
import uuid
import threading
import random
import datetime
from database import db
from models import get_user, safe_update_user, add_log, invalidate_cache, update_fortune_achievements
from utils import send_telegram_message
from config import FORTUNE_COMMISSION, FORTUNE_ROUND_DURATION

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
current_fortune_round = {
    "round_id": None,
    "yellow_pool": 0,
    "red_pool": 0,
    "yellow_bets": [],
    "red_bets": [],
    "end_time": None
}
fortune_lock = threading.Lock()
fortune_timer_thread_started = False

# Глобальная ссылка на socketio (будет установлена из bot.py)
socketio_instance = None

def set_socketio(socketio):
    global socketio_instance
    socketio_instance = socketio

def create_new_fortune_round():
    global current_fortune_round
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT round_id FROM fortune_rounds 
            WHERE winner_team IS NULL 
            LIMIT 1
        ''')
        existing_round = cursor.fetchone()
    if existing_round:
        print(f"⚠️ Активный раунд {existing_round['round_id']} уже существует, восстанавливаю...")
        restore_fortune_from_db()
        return
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

def update_fortune_timer():
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
                    if socketio_instance:
                        socketio_instance.emit('fortune_timer_update', {'time_left': time_left})
                    if time_left == 0 and not current_fortune_round.get('is_ending', False):
                        should_end = True
            if should_end:
                print("⏰ [ТАЙМЕР] Время вышло! Автоматически завершаем раунд...")
                end_fortune_round()
        except Exception as e:
            print(f"Таймер Фортуны ошибка: {e}")

def end_fortune_round():
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
    yellow_pool = current_fortune_round['yellow_pool']
    red_pool = current_fortune_round['red_pool']
    total_pool = yellow_pool + red_pool
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM fortune_active_bets WHERE round_id = ?', (round_id,))
        all_bets = cursor.fetchall()
    has_yellow = any(b['team'] == 'yellow' for b in all_bets)
    has_red = any(b['team'] == 'red' for b in all_bets)
    winner_team = None
    sector_factor = random.uniform(0.15, 0.85)
    if not (has_yellow and has_red):
        winner_team = 'refund'
    else:
        yellow_weight = yellow_pool / total_pool if total_pool > 0 else 0.5
        winner_team = 'yellow' if random.random() < yellow_weight else 'red'
    if socketio_instance:
        socketio_instance.emit('fortune_round_ending_immediate', {
            'winner': winner_team,
            'sector_factor': sector_factor,
            'yellow_pool': yellow_pool,
            'red_pool': red_pool,
            'round_id': round_id
        })
    time.sleep(0.1)

    def process_prizes_async():
        try:
            with db.get_cursor() as cursor:
                if winner_team == 'refund':
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
                    winner_bets = [b for b in all_bets if b['team'] == winner_team]
                    winner_total = sum(b['net_amount'] for b in winner_bets)
                    for bet in winner_bets:
                        share = bet['net_amount'] / winner_total if winner_total > 0 else 0
                        win_amount = round(total_pool * share, 2)
                        user = get_user(bet['user_id'])
                        safe_update_user(bet['user_id'], wg=user['wg'] + win_amount)
                        update_fortune_achievements(bet['user_id'], is_win=True)
                        cursor.execute('''
                            INSERT INTO fortune_history (user_id, round_id, team, amount, result, win_amount)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (bet['user_id'], round_id, winner_team, bet['amount'], 'win', win_amount))
                        add_log(f"🎲 ФОРТУНА: ПОБЕДА! +{win_amount} WG", bet['user_id'], user['username'])
                        team_name = "Жёлтых 🟡" if winner_team == 'yellow' else "Красных 🔴"
                        send_telegram_message(bet['user_id'],
                                              f"🎉 **ПОБЕДА В КОМАНДНОЙ ФОРТУНЕ!**\n\n"
                                              f"Команда {team_name} победила!\n"
                                              f"💰 Вы выиграли {win_amount} WG!\n\n"
                                              f"Поздравляем! 🎊")
                    for bet in all_bets:
                        if bet['team'] != winner_team:
                            cursor.execute('''
                                INSERT INTO fortune_history (user_id, round_id, team, amount, result, win_amount)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (bet['user_id'], round_id, bet['team'], bet['amount'], 'lose', 0))
                    cursor.execute('''
                        UPDATE fortune_rounds 
                        SET winner_team = ?, end_time = ?, yellow_pool = ?, red_pool = ?
                        WHERE round_id = ?
                    ''', (winner_team, datetime.datetime.now().isoformat(), yellow_pool, red_pool, round_id))
                cursor.execute("DELETE FROM fortune_active_bets WHERE round_id = ?", (round_id,))
            create_new_fortune_round()
            print(f"✅ [ФОРТУНА] Призы распределены, создан новый раунд")
        except Exception as e:
            print(f"Ошибка при выдаче призов Фортуны: {e}")
            create_new_fortune_round()
        finally:
            with fortune_lock:
                if 'current_fortune_round' in globals() and current_fortune_round:
                    current_fortune_round['is_ending'] = False
    threading.Thread(target=process_prizes_async, daemon=True).start()

def start_fortune_timer_thread():
    global fortune_timer_thread_started
    if fortune_timer_thread_started:
        return
    fortune_timer_thread_started = True
    threading.Thread(target=update_fortune_timer, daemon=True).start()