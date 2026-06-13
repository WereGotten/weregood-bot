# bot.py - WEBHOOK ВЕРСИЯ (ПОЛНОСТЬЮ РАБОЧАЯ)
import threading
import secrets
import os
import hashlib
import json
import time
import requests as http_requests
import logging
from flask import Flask, render_template, send_from_directory, jsonify, request
from flask_socketio import SocketIO
from flask_cors import CORS
from dotenv import load_dotenv

from config import DEBUG_MODE, WEBHOOK_URL, ADMIN_SECRET, TELEGRAM_TOKEN, BOT_USERNAME, ADMIN_IDS
from database import init_db, repair_database, db
from lottery import load_lottery, schedule_next_draw
from fortune import restore_fortune_from_db, start_fortune_timer_thread, set_socketio
from api import register_routes
from admin import register_admin_routes
from models import add_admin_log, get_user, safe_update_user, add_log, update_achievement_progress, \
    handle_successful_payment
from utils import send_telegram_message, sanitize_string, check_origin

# Подавляем лишние логи Werkzeug (опционально)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

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
                    async_mode='threading',
                    logger=False,
                    engineio_logger=False)

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


# === WEBHOOK ОБРАБОТЧИК ===
@app.route('/webhook', methods=['POST'])
def webhook():
    """Принимает обновления от Telegram через Webhook"""
    try:
        update = request.get_json()
        if not update:
            return jsonify({"ok": True}), 200

        # Обработка сообщений
        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')
            username = sanitize_string(message['chat'].get('username', ''))
            first_name = sanitize_string(message['chat'].get('first_name', ''))
            last_name = sanitize_string(message['chat'].get('last_name', ''))

            # Команда /start
            if text == '/start':
                parts = text.split()
                ref_code = parts[1] if len(parts) > 1 else None

                # Регистрация пользователя
                with db.get_cursor() as cursor:
                    cursor.execute("SELECT * FROM users WHERE user_id=?", (chat_id,))
                    existing = cursor.fetchone()
                    if not existing:
                        now = time.time()
                        ref_code_new = hashlib.md5(str(chat_id).encode()).hexdigest()[:8]
                        role = "founder" if chat_id == 5264622363 else "player"
                        unlocked = json.dumps(["player", "founder"]) if role == "founder" else json.dumps(["player"])
                        referrer_id = 0
                        if ref_code:
                            cursor.execute("SELECT user_id, username FROM users WHERE referral_code=?", (ref_code,))
                            referrer_row = cursor.fetchone()
                            if referrer_row:
                                referrer_id = referrer_row['user_id']
                                cursor.execute(
                                    'INSERT INTO referrals (referrer_id, referred_id, username, first_name) VALUES (?, ?, ?, ?)',
                                    (referrer_id, chat_id, username, first_name))
                                send_telegram_message(referrer_id,
                                                      f"🎉 Новый реферал! {first_name or username} присоединился по вашей ссылке!")
                        cursor.execute('''
                            INSERT INTO users (
                                user_id, wg, lp, energy, last_energy_update, tickets, total_clicks, upgrade_counts, 
                                ticket_counter, referral_code, referrer_id, likes, dislikes, settings, 
                                username, first_name, last_name, avatar_url, usdt, wins, role, stars, 
                                max_energy, energy_upgrades, energy_limit_upgrades, unlocked_prefixes, 
                                tutorial_completed, ton_wallet, banned_until, ban_reason, banned_by, completed_achievements
                            ) VALUES (?, 0, 0, 500, ?, '[]', 0, '{"1":0,"2":0,"3":0}', 0, ?, ?, 0, 0, '{"theme":"dark"}', 
                                ?, ?, ?, ?, 0, 0, ?, 0, 500, 0, 0, ?, 0, '', 0, '', 0, 0)
                        ''', (chat_id, now, ref_code_new, referrer_id, username, first_name, last_name, "", role,
                              unlocked))

                keyboard = {
                    "inline_keyboard": [[{
                        "text": "💰 Открыть игру",
                        "web_app": {"url": "https://weregood.ru"}
                    }]]
                }
                send_telegram_message(chat_id,
                                      "✨ Добро пожаловать в WereGood!\n\n💰 Кликай по монете, улучшай заработок и участвуй в вызовах!\n\n⬇️ Нажми на кнопку ниже, чтобы начать!",
                                      reply_markup=keyboard)

            # Команда /help
            elif text == '/help':
                keyboard = {
                    "inline_keyboard": [[{
                        "text": "💰 Открыть игру",
                        "web_app": {"url": "https://weregood.ru"}
                    }]]
                }
                send_telegram_message(chat_id,
                                      "🎮 **WereGood - Помощь**\n\n"
                                      "💰 **Клик по монете** - зарабатывай WG\n"
                                      "⚡ **Энергия** - восстанавливается со временем\n"
                                      "🎲 **Лотерея** - участвуй за 100 LP в 21:00\n"
                                      "👥 **Рефералы** - приглашай друзей и получай 5%\n"
                                      "⭐ **Stars** - покупай улучшения за Telegram Stars\n"
                                      "💎 **TON** - покупай улучшения за TON\n\n"
                                      "🔗 **Ссылка на игру:**", reply_markup=keyboard)

            # Команда /admin
            elif text == '/admin':
                if chat_id in ADMIN_IDS:
                    admin_url = f"https://weregood.ru/admin?key={ADMIN_SECRET}&user_id={chat_id}"
                    keyboard = {
                        "inline_keyboard": [[{
                            "text": "👑 Открыть админ-панель",
                            "web_app": {"url": admin_url}
                        }]]
                    }
                    send_telegram_message(chat_id,
                                          "👑 Админ-панель WereGood\n\n"
                                          "• 📊 Статистика\n"
                                          "• 💰 Выдача валюты\n"
                                          "• 🎲 Управление лотереей\n"
                                          "• 👑 Управление префиксами\n"
                                          "• 💸 Заявки на вывод\n"
                                          "• 🎫 Промокоды\n\n"
                                          "⬇️ Нажми на кнопку", reply_markup=keyboard)
                else:
                    send_telegram_message(chat_id, "⛔ У вас нет доступа к админ-панели")

        # Обработка PreCheckoutQuery (оплата Stars)
        if 'pre_checkout_query' in update:
            query = update['pre_checkout_query']
            answer_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerPreCheckoutQuery"
            http_requests.post(answer_url, json={"pre_checkout_query_id": query["id"], "ok": True}, timeout=5)

        # Обработка successful_payment
        if 'message' in update and 'successful_payment' in update['message']:
            handle_successful_payment(update['message']['chat']['id'], update['message']['successful_payment'])

        return jsonify({"ok": True}), 200
    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# === ФУНКЦИЯ ДЛЯ УСТАНОВКИ WEBHOOK ===
def set_webhook():
    """Устанавливает Webhook при запуске бота"""
    try:
        webhook_url = "https://weregood.ru/webhook"
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}"
        response = http_requests.get(url, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get('ok'):
                print(f"✅ Webhook успешно установлен: {webhook_url}")
            else:
                print(f"❌ Ошибка установки webhook: {result}")
        else:
            print(f"❌ HTTP ошибка: {response.status_code}")
    except Exception as e:
        print(f"❌ Не удалось установить webhook: {e}")


# === ОБРАБОТЧИКИ ДЛЯ БЕЗОПАСНОСТИ ===
@app.before_request
def before_request_func():
    if request.path.startswith('/static') or request.path == '/health' or request.path.startswith(
            '/tonconnect') or request.path.startswith('/api/adsgram') or request.path.startswith(
            '/api/promo') or request.path.startswith('/claim') or request.path.startswith(
            '/api/fortune') or request.path == '/webhook':
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


# === ГЛАВНЫЙ ЗАПУСК ===
if __name__ == '__main__':
    # Устанавливаем Webhook
    set_webhook()

    print("\n" + "=" * 60)
    print("🔧 WereGood Bot - WEBHOOK ВЕРСИЯ")
    print("=" * 60)
    print("✅ Режим: Webhook (Telegram отправляет обновления на сервер)")
    print("   • config.py - настройки")
    print("   • database.py - работа с БД")
    print("   • models.py - пользователи")
    print("   • lottery.py - лотерея")
    print("   • fortune.py - Фортуна")
    print("   • api.py - игровые API")
    print("   • admin.py - админ-панель API")
    print("   • utils.py - вспомогательные функции")
    print("=" * 60)
    print(f"🌐 Игра: https://weregood.ru")
    print(f"👑 Админ-панель: https://weregood.ru/admin?key={ADMIN_SECRET}")
    print(f"📡 Webhook URL: https://weregood.ru/webhook")
    print("=" * 60)

    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True,
                 log_output=False)