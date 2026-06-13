# bot.py (НОВЫЙ ГЛАВНЫЙ ФАЙЛ)
import threading
import secrets
import os
from flask import Flask, render_template, send_from_directory, jsonify, request
from flask_socketio import SocketIO
from flask_cors import CORS
from dotenv import load_dotenv

from config import DEBUG_MODE, WEBHOOK_URL, ADMIN_SECRET, TELEGRAM_TOKEN
from database import init_db, repair_database, db
from lottery import load_lottery, schedule_next_draw
from fortune import restore_fortune_from_db, start_fortune_timer_thread, set_socketio
from api import register_routes
from admin import register_admin_routes
from models import add_admin_log
from utils import check_origin

# Загрузка переменных окружения
load_dotenv()

# === СОЗДАНИЕ ПРИЛОЖЕНИЯ ===
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = secrets.token_hex(32)
app.config['SESSION_COOKIE_SECURE'] = not DEBUG_MODE
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# === CORS ===
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

# === SOCKET.IO ===
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

# Передаём socketio в fortune.py
set_socketio(socketio)

# === ПРЕДЗАПУСКОВЫЕ ПРОВЕРКИ ===
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не задан в .env файле!")
if not ADMIN_SECRET:
    raise ValueError("❌ ADMIN_SECRET не задан в .env файле!")

# === ИНИЦИАЛИЗАЦИЯ ===
repair_database()
init_db()
load_lottery()
restore_fortune_from_db()

# === ЗАПУСК ФОНОВЫХ ЗАДАЧ ===
threading.Thread(target=schedule_next_draw, daemon=True).start()
start_fortune_timer_thread()

# === РЕГИСТРАЦИЯ МАРШРУТОВ ===
register_routes(app, socketio)
register_admin_routes(app)


# === ОБРАБОТЧИКИ ДЛЯ БЕЗОПАСНОСТИ ===
@app.before_request
def before_request():
    if request.path.startswith('/static') or request.path == '/health' or request.path.startswith(
            '/tonconnect') or request.path.startswith('/api/adsgram') or request.path.startswith(
            '/api/promo') or request.path.startswith('/claim') or request.path.startswith('/api/fortune'):
        return None
    if not check_origin():
        print(f"CSRF попытка с Origin: {request.headers.get('Origin')}")
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


# === ОБРАБОТЧИКИ TELEGRAM (ПОЛЛИНГ) ===
def handle_telegram_updates():
    import hashlib
    import json
    import time
    import requests
    from models import get_user, safe_update_user, add_log, update_achievement_progress
    from utils import send_telegram_message, sanitize_string
    from config import BOT_USERNAME, ADMIN_IDS

    last_update_id = 0
    verify_ssl = not DEBUG_MODE
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            response = requests.get(url, params=params, timeout=35, verify=verify_ssl)
            updates = response.json()
            if updates.get("ok"):
                for update in updates.get("result", []):
                    last_update_id = update["update_id"]
                    if "message" in update and "text" in update["message"]:
                        text = update["message"]["text"]
                        chat_id = update["message"]["chat"]["id"]
                        username = sanitize_string(update["message"]["chat"].get("username", ""))
                        first_name = sanitize_string(update["message"]["chat"].get("first_name", ""))
                        last_name = sanitize_string(update["message"]["chat"].get("last_name", ""))
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
                                                (referrer_id, chat_id, username, first_name))
                                            send_telegram_message(referrer_id,
                                                                  f"🎉 Новый реферал! {first_name or username} присоединился по вашей ссылке!")
                                    cursor.execute(
                                        '''INSERT INTO users (user_id, wg, lp, energy, last_energy_update, tickets, total_clicks, upgrade_counts, ticket_counter, referral_code, referrer_id, likes, dislikes, settings, username, first_name, last_name, avatar_url, usdt, wins, role, stars, max_energy, energy_upgrades, energy_limit_upgrades, unlocked_prefixes, tutorial_completed, ton_wallet, banned_until, ban_reason, banned_by, completed_achievements) VALUES (?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, ?, 0, 0, '{"theme":"dark"}', ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '', 0, '', 0, 0)''',
                                        (chat_id, now, ref_code_new, referrer_id, username, first_name, last_name, "",
                                         role, unlocked))
                            keyboard = {
                                "inline_keyboard": [[{"text": "💰 Открыть игру", "web_app": {"url": WEBHOOK_URL}}]]}
                            send_telegram_message(chat_id,
                                                  "✨ Добро пожаловать в WereGood!\n\n💰 Кликай по монете, улучшай заработок и участвуй в вызовах!\n\n⬇️ Нажми на кнопку ниже, чтобы начать!",
                                                  keyboard)
                        elif text.startswith("/help"):
                            keyboard = {
                                "inline_keyboard": [[{"text": "💰 Открыть игру", "web_app": {"url": WEBHOOK_URL}}]]}
                            send_telegram_message(chat_id,
                                                  "🎮 **WereGood - Помощь**\n\n💰 **Клик по монете** - зарабатывай WG\n⚡ **Энергия** - восстанавливается со временем\n🎲 **Лотерея** - участвуй за 100 LP в 21:00\n👥 **Рефералы** - приглашай друзей и получай 5%\n⭐ **Stars** - покупай улучшения за Telegram Stars\n💎 **TON** - покупай улучшения за TON\n\n🔗 **Ссылка на игру:**",
                                                  keyboard)
                        elif text.startswith("/admin"):
                            if chat_id in ADMIN_IDS:
                                admin_url = f"{WEBHOOK_URL}/admin?key={ADMIN_SECRET}&user_id={chat_id}"
                                keyboard = {"inline_keyboard": [
                                    [{"text": "👑 Открыть админ-панель", "web_app": {"url": admin_url}}]]}
                                send_telegram_message(chat_id,
                                                      "👑 Админ-панель WereGood\n\n• 📊 Статистика\n• 💰 Выдача валюты\n• 🎲 Управление лотереей\n• 👑 Управление префиксами\n• 💸 Заявки на вывод\n• 🎫 Промокоды\n\n⬇️ Нажми на кнопку",
                                                      keyboard)
                            else:
                                send_telegram_message(chat_id, "⛔ У вас нет доступа к админ-панели")
                    elif "pre_checkout_query" in update:
                        query = update["pre_checkout_query"]
                        answer_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerPreCheckoutQuery"
                        requests.post(answer_url, json={"pre_checkout_query_id": query["id"], "ok": True}, timeout=5,
                                      verify=verify_ssl)
                    elif "message" in update and "successful_payment" in update["message"]:
                        from models import handle_successful_payment
                        chat_id = update["message"]["chat"]["id"]
                        handle_successful_payment(chat_id, update["message"]["successful_payment"])
            time.sleep(1)
        except Exception as e:
            print(f"Ошибка в polling: {e}")
            time.sleep(5)


# === ЗАПУСК ПОТОКА TELEGRAM ===
threading.Thread(target=handle_telegram_updates, daemon=True).start()

# === ГЛАВНЫЙ ЗАПУСК ===
if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("🔧 WereGood Bot - ПОЛНАЯ ВЕРСИЯ С ДОСТИЖЕНИЯМИ И ФОРТУНОЙ")
    print("=" * 60)
    print("✅ ВСЕ ФУНКЦИИ РАЗДЕЛЕНЫ ПО ФАЙЛАМ:")
    print("   • config.py - настройки")
    print("   • database.py - работа с БД")
    print("   • models.py - пользователи")
    print("   • lottery.py - лотерея")
    print("   • fortune.py - Фортуна")
    print("   • api.py - игровые API")
    print("   • admin.py - админ-панель API")
    print("   • utils.py - вспомогательные функции")
    print("=" * 60)
    print(f"🌐 Игра: http://0.0.0.0:5000")
    print(f"👑 Админ-панель: http://0.0.0.0:5000/admin?key={ADMIN_SECRET}")
    print(f"🎫 Активация промокода: {WEBHOOK_URL}/claim?code=ВАШ_КОД")
    print("=" * 60)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)