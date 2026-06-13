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
    """Загрузка лотереи из БД при старте сервера"""
    global lottery_pool, lottery_tickets, global_ticket_counter, winning_numbers, is_drawn, draw_time, lottery_phase
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT prize_pool, tickets, global_ticket_counter, winning_numbers, 
                       is_drawn, draw_time, lottery_phase 
                FROM lottery LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                lottery_pool = row['prize_pool'] if row['prize_pool'] is not None else 0

                tickets_data = row['tickets']
                if tickets_data and tickets_data != '[]':
                    try:
                        lottery_tickets = json.loads(tickets_data)
                    except:
                        lottery_tickets = []
                else:
                    lottery_tickets = []

                global_ticket_counter = row['global_ticket_counter'] if row['global_ticket_counter'] is not None else 0

                winning_data = row['winning_numbers']
                if winning_data and winning_data != '[]':
                    try:
                        winning_numbers = json.loads(winning_data)
                    except:
                        winning_numbers = []
                else:
                    winning_numbers = []

                is_drawn = row['is_drawn'] == 1 if row['is_drawn'] is not None else False
                lottery_phase = row['lottery_phase'] if row['lottery_phase'] else 'buy'

                if row['draw_time']:
                    try:
                        draw_time = datetime.datetime.fromisoformat(row['draw_time'])
                    except:
                        draw_time = None
                else:
                    draw_time = None

        print(f"✅ Лотерея загружена: {len(lottery_tickets)} билетов, фонд {lottery_pool} USDT, is_drawn={is_drawn}")
        update_lottery_phase()
    except Exception as e:
        print(f"❌ Ошибка загрузки лотереи: {e}")


def refresh_lottery_data():
    """Принудительное обновление глобальных переменных из БД"""
    global lottery_pool, lottery_tickets, global_ticket_counter, winning_numbers, is_drawn, draw_time, lottery_phase
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT prize_pool, tickets, global_ticket_counter, winning_numbers, 
                       is_drawn, draw_time, lottery_phase 
                FROM lottery LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                lottery_pool = float(row['prize_pool']) if row['prize_pool'] is not None else 0

                tickets_data = row['tickets']
                if tickets_data and tickets_data != '[]':
                    try:
                        lottery_tickets = json.loads(tickets_data)
                        # ✅ КОНВЕРТИРУЕМ revealed в булевы значения
                        for ticket in lottery_tickets:
                            if 'revealed' in ticket:
                                ticket['revealed'] = [bool(r) for r in ticket['revealed']]
                    except Exception as e:
                        print(f"Ошибка парсинга tickets: {e}")
                        lottery_tickets = []
                else:
                    lottery_tickets = []

                global_ticket_counter = int(row['global_ticket_counter']) if row[
                                                                                 'global_ticket_counter'] is not None else 0

                winning_data = row['winning_numbers']
                if winning_data and winning_data != '[]':
                    try:
                        winning_numbers = json.loads(winning_data)
                    except:
                        winning_numbers = []
                else:
                    winning_numbers = []

                is_drawn = bool(row['is_drawn']) if row['is_drawn'] is not None else False
                lottery_phase = row['lottery_phase'] if row['lottery_phase'] else 'buy'

                print(f"🔄 [REFRESH] is_drawn={is_drawn}, pool={lottery_pool}, tickets={len(lottery_tickets)}")
                return True
        return False
    except Exception as e:
        print(f"❌ Ошибка синхронизации: {e}")
        import traceback
        traceback.print_exc()
        return False


def save_lottery():
    """Сохранение лотереи в БД"""
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                UPDATE lottery 
                SET prize_pool=?, tickets=?, global_ticket_counter=?, 
                    winning_numbers=?, is_drawn=?, draw_time=?, lottery_phase=?
                WHERE id=1
            """, (
                lottery_pool,
                json.dumps(lottery_tickets),
                global_ticket_counter,
                json.dumps(winning_numbers),
                1 if is_drawn else 0,
                draw_time.isoformat() if draw_time else None,
                lottery_phase
            ))
        print(f"💾 Лотерея сохранена: билетов={len(lottery_tickets)}, фонд={lottery_pool} USDT")
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения лотереи: {e}")
        return False


def update_lottery_phase():
    """Обновление фазы лотереи (покупка/раскрытие)"""
    global lottery_phase
    try:
        now = datetime.datetime.now()
        current_hour = now.hour
        # Розыгрыш в 21:00, раскрытие до 00:00
        if current_hour >= 21 or current_hour < 0:
            new_phase = "reveal"
        else:
            new_phase = "buy"

        if lottery_phase != new_phase:
            lottery_phase = new_phase
            with db.get_cursor() as cursor:
                cursor.execute("UPDATE lottery SET lottery_phase = ? WHERE id = 1", (lottery_phase,))
            print(f"🔄 Смена фазы лотереи: {lottery_phase}")
    except Exception as e:
        print(f"❌ Ошибка update_lottery_phase: {e}")


def generate_ticket_numbers():
    """Генерация 12 случайных чисел для билета (1-80)"""
    return sorted(random.sample(range(1, 81), 12))


def generate_winning_numbers():
    """Генерация 12 выигрышных чисел (1-80)"""
    return sorted(random.sample(range(1, 81), 12))


def buy_ticket(user_id, user_data):
    """Покупка билета пользователем"""
    global lottery_pool, lottery_tickets, global_ticket_counter, is_drawn

    with lottery_lock:
        print(f"🎫 [buy_ticket] Начало: user={user_id}, is_drawn={is_drawn}, tickets={len(lottery_tickets)}")

        # Проверка: розыгрыш не должен идти
        if is_drawn:
            return False, "Сейчас идёт розыгрыш! Новые билеты появятся после 00:00"

        # Проверка: достаточно ли LP
        if user_data["lp"] < 100:
            return False, f"Не хватает LP! Нужно 100, у вас {user_data['lp']}"

        # Проверка: не больше 10 билетов на игрока
        bought = len([t for t in lottery_tickets if t.get("user_id") == user_id])
        if bought >= 10:
            return False, "У вас уже 10 билетов! Нельзя купить больше."

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

        # Создаём билет
        ticket_data = {
            "number": ticket_num,
            "purchase_number": user_ticket_counter,
            "numbers": ticket_numbers,
            "revealed": [False] * 12,
            "reward_claimed": False,
            "user_id": user_id
        }
        lottery_tickets.append(ticket_data)

        # Добавляем 0.40 USDT в призовой фонд
        lottery_pool = round(lottery_pool + 0.40, 2)

        # ✅ СОХРАНЯЕМ В БД
        save_lottery()

        print(f"🎫 [buy_ticket] Билет #{ticket_num} куплен! Всего билетов={len(lottery_tickets)}, фонд={lottery_pool}")

        add_log(
            f"🎫 Купил билет #{ticket_num}",
            user_id,
            user_data.get('username', str(user_id)),
            old_value=old_lp,
            new_value=user_data['lp'],
            currency="lp"
        )
        update_achievement_progress(user_id, 'gambler', 1)

        return True, f"Билет #{ticket_num} куплен!"


def reveal_cell(user_id, ticket_number, cell_index):
    """Открытие конкретной клетки билета"""
    global lottery_tickets, lottery_pool, global_ticket_counter, winning_numbers, is_drawn, draw_time, lottery_phase

    print(f"🔍 [reveal_cell] НАЧАЛО: user={user_id}, ticket={ticket_number}, cell={cell_index}")

    # Принудительно обновляем данные из БД перед проверкой
    refresh_lottery_data()

    print(f"🔍 [reveal_cell] is_drawn={is_drawn}")

    if not is_drawn:
        return False, "Розыгрыш ещё не начался!"

    # Ищем билет
    ticket_index = -1
    for idx, ticket in enumerate(lottery_tickets):
        if ticket.get("user_id") == user_id and ticket.get("number") == ticket_number:
            ticket_index = idx
            break

    if ticket_index == -1:
        return False, "Билет не найден"

    ticket = lottery_tickets[ticket_index]

    if cell_index < 0 or cell_index >= 12:
        return False, "Неверный индекс клетки"

    if ticket["revealed"][cell_index]:
        return False, "Клетка уже открыта"

    # Открываем клетку
    ticket["revealed"][cell_index] = True

    # Сохраняем в БД
    save_lottery()

    # Обновляем глобальные переменные
    refresh_lottery_data()

    revealed_count = sum(ticket["revealed"])
    if revealed_count == 12:
        update_achievement_progress(user_id, 'brave', 1)

    print(f"✅ [reveal_cell] Клетка {cell_index} открыта, всего открыто: {revealed_count}/12")

    return True, f"Клетка {cell_index + 1} открыта!"


def reveal_all_tickets(user_id):
    """Открытие всех клеток всех билетов пользователя"""
    with lottery_lock:
        if not is_drawn:
            return False, "Розыгрыш ещё не начался!"

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
            return True, f"Открыто {revealed_count} клеток в {tickets_revealed} билетах!"

        return False, "Нет неоткрытых клеток"


def perform_draw():
    """Проведение розыгрыша лотереи"""
    global winning_numbers, is_drawn, draw_time, lottery_phase

    with lottery_lock:
        if not lottery_tickets:
            print("⚠️ Попытка розыгрыша без билетов")
            return

        winning_numbers = generate_winning_numbers()
        is_drawn = True
        draw_time = datetime.datetime.now()
        lottery_phase = "reveal"
        save_lottery()

        refresh_lottery_data()

        add_log(f"🎲 РОЗЫГРЫШ ЛОТЕРЕИ! Выигрышные номера: {winning_numbers}", 0, "System")
        print(f"🎲 РОЗЫГРЫШ: winning_numbers={winning_numbers}, is_drawn={is_drawn}, билетов={len(lottery_tickets)}")

        threading.Timer(10800, auto_reveal_and_distribute).start()


def auto_reveal_and_distribute():
    """Автоматическое раскрытие всех билетов через 3 часа после розыгрыша"""
    print("⏰ Прошло 3 часа, автоматическое раскрытие билетов...")
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
    """Распределение призов между победителями"""
    global lottery_pool

    if not lottery_tickets:
        print("⚠️ Нет билетов для распределения призов")
        return

    results = []
    for ticket in lottery_tickets:
        if all(ticket.get("revealed", [])):
            matches = sum(1 for i in range(12) if ticket["numbers"][i] in winning_numbers)
            results.append({
                "user_id": ticket["user_id"],
                "matches": matches,
                "ticket": ticket
            })

    if not results:
        print("⚠️ Нет полностью открытых билетов для распределения")
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

            add_log(
                f"🏆 ПОБЕДА в лотерее! +{prize_per_winner} USDT (совпадений: {winner['matches']}/12)",
                winner["user_id"],
                user.get('username', str(winner["user_id"])),
                old_value=old_usdt,
                new_value=user.get('usdt', 0) + prize_per_winner,
                currency="usdt"
            )

            send_telegram_message(
                winner["user_id"],
                f"🎉 ПОБЕДА В ЛОТЕРЕЕ!\n\n💰 Вы выиграли {prize_per_winner} USDT!\n🎯 Совпадений: {winner['matches']}/12\n\nПоздравляем! 🎊"
            )

            update_achievement_progress(winner["user_id"], 'lucky', 1)

    save_lottery()
    add_log(
        f"🎰 Завершение розыгрыша. Призовой фонд {lottery_pool} USDT распределён между {len(winners)} победителями",
        0, "System"
    )
    print(f"✅ Призы распределены: {len(winners)} победителей, каждый получил {prize_per_winner} USDT")


def reset_lottery():
    """Сброс лотереи для нового дня"""
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

        print("🔄 ЛОТЕРЕЯ СБРОШЕНА для нового розыгрыша")


def schedule_next_draw():
    """Планирование следующего розыгрыша (каждый день в 21:00)"""

    def wait_and_draw():
        while True:
            now = datetime.datetime.now()
            next_draw = now.replace(hour=21, minute=0, second=0, microsecond=0)
            if now >= next_draw:
                next_draw += datetime.timedelta(days=1)
            wait_seconds = (next_draw - now).total_seconds()
            print(f"⏰ Следующий розыгрыш через {wait_seconds / 3600:.1f} часов (в 21:00)")
            time.sleep(wait_seconds)

            perform_draw()

            time.sleep(14400)
            reset_lottery()

    threading.Thread(target=wait_and_draw, daemon=True).start()


def get_lottery_status(user_id=None):
    """Получение текущего статуса лотереи"""
    refresh_lottery_data()

    result = {
        "prize_pool": lottery_pool,
        "is_drawn": is_drawn,
        "winning_numbers": winning_numbers if is_drawn else [],
        "lottery_phase": lottery_phase,
        "user_tickets": 0,
        "user_lp": 0,
        "tickets": []
    }

    if user_id:
        user = get_user(user_id)
        result["user_lp"] = user.get("lp", 0)
        user_tickets = [t for t in lottery_tickets if t.get("user_id") == user_id]
        result["user_tickets"] = len(user_tickets)
        result["tickets"] = user_tickets
        print(f"📊 Статус для user {user_id}: билетов={len(user_tickets)}, is_drawn={is_drawn}")

    return result