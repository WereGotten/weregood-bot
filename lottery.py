# lottery.py
import json
import random
import datetime
import threading
import time
from database import db
from models import (
    get_user, safe_update_user, add_log, invalidate_cache,
    add_usdt, add_wins, update_achievement_progress
)
from utils import send_telegram_message
from config import UPGRADE_CONFIG

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
lottery_pool = 0
lottery_tickets = []
global_ticket_counter = 0
winning_numbers = []
is_drawn = False
draw_time = None
lottery_phase = "buy"
lottery_lock = threading.Lock()


def load_lottery():
    global lottery_pool, lottery_tickets, global_ticket_counter, winning_numbers, is_drawn, draw_time, lottery_phase

    with db.get_cursor() as cursor:
        cursor.execute("""
            SELECT prize_pool, tickets, global_ticket_counter, winning_numbers, 
                   is_drawn, draw_time, lottery_phase 
            FROM lottery LIMIT 1
        """)
        row = cursor.fetchone()

        if row:
            lottery_pool = row['prize_pool'] or 0
            # ВАЖНО: всегда преобразуем в список, даже если в БД пустота
            tickets_data = row['tickets']
            if tickets_data and tickets_data != '[]':
                lottery_tickets = json.loads(tickets_data)
            else:
                lottery_tickets = []

            global_ticket_counter = row['global_ticket_counter'] or 0
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
    print(f"✅ Лотерея загружена: {len(lottery_tickets)} билетов, фонд {lottery_pool} USDT")

def save_lottery():
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE lottery SET prize_pool=?, tickets=?, global_ticket_counter=?, winning_numbers=?, is_drawn=?, draw_time=?, lottery_phase=?",
                       (lottery_pool, json.dumps(lottery_tickets), global_ticket_counter, json.dumps(winning_numbers),
                        1 if is_drawn else 0, draw_time.isoformat() if draw_time else None, lottery_phase))

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

def generate_ticket_numbers():
    return sorted(random.sample(range(1, 81), 12))

def generate_winning_numbers():
    return sorted(random.sample(range(1, 81), 12))


def buy_ticket(user_id, user_data):
    global lottery_pool, lottery_tickets, global_ticket_counter, is_drawn

    with lottery_lock:
        # Проверка на активную фазу стирания
        if is_drawn:
            return False, "Сейчас идёт стирание билетов! Новые билеты появятся в 00:00"

        # Проверка на наличие LP
        if user_data["lp"] < 100:
            return False, "Не хватает LP (нужно 100)"

        # Проверка на максимальное количество билетов у пользователя
        bought = len([t for t in lottery_tickets if t.get("user_id") == user_id])
        if bought >= 10:
            return False, "Уже куплено 10 билетов"

        # Списываем LP
        old_lp = user_data["lp"]
        user_data["lp"] -= 100
        safe_update_user(user_id, lp=user_data["lp"])

        # Увеличиваем глобальный счётчик билетов
        global_ticket_counter += 1
        ticket_num = global_ticket_counter

        # Формируем отображаемое имя
        if user_data.get('username') and user_data['username'] != '':
            display_name = '@' + user_data['username']
        elif user_data.get('first_name') and user_data['first_name'] != '':
            display_name = user_data['first_name']
        else:
            display_name = f"Player_{user_id}"

        # Сохраняем в историю
        with db.get_cursor() as cursor:
            cursor.execute("UPDATE lottery SET global_ticket_counter = ? WHERE id = 1", (global_ticket_counter,))
            cursor.execute("""
                INSERT INTO lottery_tickets_history (user_id, ticket_number, username, created_at) 
                VALUES (?, ?, ?, datetime('now', 'localtime'))
            """, (user_id, ticket_num, display_name))

        # Генерируем числа для билета
        ticket_numbers = generate_ticket_numbers()
        user_ticket_counter = user_data.get("ticket_counter", 0) + 1
        safe_update_user(user_id, ticket_counter=user_ticket_counter)

        # Создаём данные билета
        ticket_data = {
            "number": ticket_num,
            "purchase_number": user_ticket_counter,
            "numbers": ticket_numbers,
            "revealed": [False] * 12,
            "reward_claimed": False,
            "user_id": user_id
        }

        # Добавляем билет в список
        lottery_tickets.append(ticket_data)

        # Увеличиваем призовой фонд (0.40 USDT за билет)
        lottery_pool = round(lottery_pool + 0.40, 2)

        # Сохраняем состояние лотереи в БД
        save_lottery()

        # Логируем покупку
        add_log(
            f"🎫 Купил билет #{ticket_num}",
            user_id,
            user_data.get('username', str(user_id)),
            old_value=old_lp,
            new_value=user_data['lp'],
            currency="lp"
        )

        # Обновляем достижение "Азартный"
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
            send_telegram_message(winner["user_id"], f"🎉 ПОБЕДА! +{prize_per_winner} USDT! Совпадений: {winner['matches']}/12")
            update_achievement_progress(winner["user_id"], 'lucky', 1)
    save_lottery()
    add_log(f"🎰 Завершение розыгрыша. Призовой фонд {lottery_pool} USDT распределён между {len(winners)} победителями", 0, "System")

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