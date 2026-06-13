# bot.py - ОФЛАЙН ВЕРСИЯ (без Telegram API)
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

set_socketio(socketio)

# === ПРОВЕРКИ ===
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не задан в .env файле!")
if not ADMIN_SECRET:
    raise ValueError("❌ ADMIN_SECRET не задан в .env файле!")

# === ИНИЦИАЛИЗАЦИЯ ===
repair_database()
init_db()
load_lottery()
restore_fortune_from_db()

# === ФОНОВЫЕ ЗАДАЧИ ===
threading.Thread(target=schedule_next_draw, daemon=True).start()
start_fortune_timer_thread()

# === РЕГИСТРАЦИЯ МАРШРУТОВ ===
register_routes(app, socketio)
register_admin_routes(app)


# === ОБРАБОТЧИКИ ДЛЯ БЕЗОПАСНОСТИ ===
@app.before_request
def before_request_func():
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


# === ГЛАВНЫЙ ЗАПУСК ===
if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("🔧 WereGood Bot - ОФЛАЙН ВЕРСИЯ (без Telegram API)")
    print("=" * 60)
    print("⚠️  Режим: только WebApp (команды /start не работают)")
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
    print("=" * 60)

    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)