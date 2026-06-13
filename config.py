# config.py
import os
import secrets
from dotenv import load_dotenv

load_dotenv()

# === ТЕЛЕГРАМ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", secrets.token_urlsafe(32))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_SECRET = os.getenv("ADMIN_SECRET")
BOT_USERNAME = os.getenv("BOT_USERNAME", "WereGooodbot")

# === ПЛАТЕЖИ ===
PROJECT_WALLET_ADDRESS = os.getenv("PROJECT_WALLET_ADDRESS")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
CRYPTO_PAY_TESTNET = os.getenv("CRYPTO_PAY_TESTNET", "false").lower() == "true"

# === БАЗА ДАННЫХ ===
DATABASE_PATH = os.getenv("DATABASE_PATH", "database.db")

# === АДМИНЫ ===
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "5264622363").split(",")]

# === РЕЖИМ ===
DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"

# === ПРОВЕРКИ ===
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не задан!")
if not ADMIN_SECRET:
    raise ValueError("❌ ADMIN_SECRET не задан!")

# === НАСТРОЙКИ ИГРЫ ===
UPGRADE_CONFIG = {
    1: {"base_cost": 1.5, "bonus": 0.01, "name": "Новичок"},
    2: {"base_cost": 10, "bonus": 0.03, "name": "Профессионал"},
    3: {"base_cost": 70, "bonus": 0.07, "name": "Мастер"}
}

DAILY_REWARDS = {
    1: {"wg": 15, "lp": 0, "energy_limit": 0, "description": "15 WG"},
    2: {"wg": 50, "lp": 0, "energy_limit": 0, "description": "50 WG"},
    3: {"wg": 0, "lp": 0, "energy_limit": 10, "description": "+10 к лимиту энергии"},
    4: {"wg": 0, "lp": 10, "energy_limit": 0, "description": "10 LP"},
    5: {"wg": 0, "lp": 0, "energy_limit": 15, "description": "+15 к лимиту энергии"},
    6: {"wg": 150, "lp": 0, "energy_limit": 0, "description": "150 WG"},
    7: {"wg": 0, "lp": 20, "energy_limit": 0, "description": "20 LP"}
}

FORTUNE_COMMISSION = 0.07
FORTUNE_ROUND_DURATION = 300