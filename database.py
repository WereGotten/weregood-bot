# database.py
import sqlite3
import threading
import os
import shutil
import json
import datetime
from contextlib import contextmanager
from config import DATABASE_PATH


class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self.local = threading.local()

    @contextmanager
    def get_cursor(self):
        if not hasattr(self.local, 'conn') or self.local.conn is None:
            self.local.conn = sqlite3.connect(self.db_file, check_same_thread=False, timeout=10)
            self.local.conn.row_factory = sqlite3.Row
            self.local.conn.execute("PRAGMA journal_mode=WAL")
            self.local.conn.execute("PRAGMA busy_timeout=30000")
            self.local.conn.execute("PRAGMA synchronous=NORMAL")
            self.local.conn.execute("PRAGMA cache_size=-204800")
            self.local.conn.execute("PRAGMA mmap_size=536870912")
            self.local.conn.execute("PRAGMA temp_store=MEMORY")
            self.local.conn.execute("PRAGMA foreign_keys = ON;")
        cursor = self.local.conn.cursor()
        try:
            yield cursor
            self.local.conn.commit()
        except Exception as e:
            self.local.conn.rollback()
            print(f"Database error: {e}")
            raise
        finally:
            cursor.close()


db = Database(DATABASE_PATH)


def repair_database():
    if not os.path.exists(DATABASE_PATH):
        return True
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check")
        result = cursor.fetchone()
        if result[0] == "ok":
            conn.close()
            return True
        print(f"БД повреждена: {result[0]}. Удаляем...")
        conn.close()
        os.remove(DATABASE_PATH)
        for ext in ['-wal', '-shm']:
            if os.path.exists(DATABASE_PATH + ext):
                os.remove(DATABASE_PATH + ext)
        return True
    except Exception as e:
        print(f"Ошибка восстановления БД: {e}")
        return False


def init_db():
    with db.get_cursor() as cursor:
        # ========== ТАБЛИЦА USERS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                wg REAL DEFAULT 0,
                lp INTEGER DEFAULT 0,
                energy INTEGER DEFAULT 500,
                last_energy_update REAL,
                tickets TEXT DEFAULT '[]',
                total_clicks INTEGER DEFAULT 0,
                upgrade_counts TEXT DEFAULT '{"1":0,"2":0,"3":0}',
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                ticket_counter INTEGER DEFAULT 0,
                referral_code TEXT DEFAULT '',
                referrer_id INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                settings TEXT DEFAULT '{"theme":"dark"}',
                avatar_url TEXT DEFAULT '',
                usdt REAL DEFAULT 0,
                wins INTEGER DEFAULT 0,
                role TEXT DEFAULT 'player',
                stars INTEGER DEFAULT 0,
                max_energy INTEGER DEFAULT 500,
                energy_upgrades INTEGER DEFAULT 0,
                energy_limit_upgrades INTEGER DEFAULT 0,
                unlocked_prefixes TEXT DEFAULT '["player"]',
                tutorial_completed INTEGER DEFAULT 0,
                ton_wallet TEXT DEFAULT '',
                banned_until REAL DEFAULT 0,
                ban_reason TEXT DEFAULT '',
                banned_by INTEGER DEFAULT 0,
                completed_achievements INTEGER DEFAULT 0,
                daily_clicks INTEGER DEFAULT 0,
                fortune_bets_count INTEGER DEFAULT 0,
                fortune_wins_count INTEGER DEFAULT 0,
                fortune_total_bet_amount REAL DEFAULT 0
            )
        ''')

        # ========== ТАБЛИЦА LOTTERY ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS lottery (
                id INTEGER PRIMARY KEY,
                prize_pool REAL DEFAULT 0,
                tickets TEXT DEFAULT '[]',
                winning_numbers TEXT DEFAULT '',
                last_draw TIMESTAMP,
                global_ticket_counter INTEGER DEFAULT 0,
                is_drawn BOOLEAN DEFAULT 0,
                draw_time TIMESTAMP,
                lottery_phase TEXT DEFAULT 'buy'
            )
        ''')

        # ========== ТАБЛИЦА REFERRALS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                username TEXT,
                first_name TEXT,
                total_spent_lp INTEGER DEFAULT 0,
                total_earned_wg REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ========== ТАБЛИЦА VOTES ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voter_id INTEGER,
                target_id INTEGER,
                vote_type TEXT,
                last_vote_time TEXT,
                UNIQUE(voter_id, target_id)
            )
        ''')

        # ========== ТАБЛИЦА LOTTERY_TICKETS_HISTORY ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS lottery_tickets_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ticket_number INTEGER,
                username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ========== ТАБЛИЦА SUCCESSFUL_PAYMENTS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS successful_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                telegram_payment_charge_id TEXT UNIQUE,
                payload TEXT,
                amount INTEGER,
                granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ========== ТАБЛИЦА USED_TON_TRANSACTIONS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS used_ton_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT UNIQUE,
                user_id INTEGER,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ========== ТАБЛИЦА SYSTEM_LOGS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                action TEXT,
                user_id INTEGER,
                username TEXT,
                details TEXT,
                log_type TEXT DEFAULT 'user'
            )
        ''')

        # ========== ТАБЛИЦА WITHDRAWAL_REQUESTS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                amount REAL,
                address TEXT,
                network TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                processed_at TEXT
            )
        ''')

        # ========== ТАБЛИЦА STATS_HISTORY ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stats_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE,
                clicks INTEGER DEFAULT 0,
                ad_views INTEGER DEFAULT 0,
                stars_donated INTEGER DEFAULT 0,
                online_peak INTEGER DEFAULT 0,
                tickets_sold INTEGER DEFAULT 0,
                new_users INTEGER DEFAULT 0
            )
        ''')

        # ========== ТАБЛИЦА DAILY_REWARDS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_rewards (
                user_id INTEGER PRIMARY KEY,
                current_day INTEGER DEFAULT 0,
                last_claim_date TEXT,
                streak_start_date TEXT,
                recovered_count INTEGER DEFAULT 0
            )
        ''')

        # ========== ТАБЛИЦА PROMO_CODES ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                reward_type TEXT NOT NULL,
                reward_amount INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                used_count INTEGER DEFAULT 0,
                password TEXT,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                is_active BOOLEAN DEFAULT 1
            )
        ''')

        # ========== ТАБЛИЦА PROMO_ACTIVATIONS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promo_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                promo_id INTEGER,
                user_id INTEGER,
                activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (promo_id) REFERENCES promo_codes(id)
            )
        ''')

        # ========== ТАБЛИЦА AD_WATCH_HISTORY ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ad_watch_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ad_type TEXT,
                watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ========== ТАБЛИЦА TASKS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                channel_link TEXT NOT NULL,
                channel_username TEXT NOT NULL,
                channel_avatar TEXT DEFAULT '',
                reward_amount INTEGER DEFAULT 10,
                reward_type TEXT DEFAULT 'wg',
                daily_limit INTEGER DEFAULT 1,
                total_limit INTEGER DEFAULT 100,
                completed_count INTEGER DEFAULT 0,
                days_remaining INTEGER DEFAULT 7,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ========== ТАБЛИЦА USER_TASKS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                task_id INTEGER,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reward_claimed BOOLEAN DEFAULT 1,
                UNIQUE(user_id, task_id)
            )
        ''')

        # ========== ТАБЛИЦА ACHIEVEMENTS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                description TEXT NOT NULL,
                icon TEXT NOT NULL,
                target_count INTEGER NOT NULL
            )
        ''')

        # ========== ТАБЛИЦА USER_ACHIEVEMENTS ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                achievement_id INTEGER NOT NULL,
                current_count INTEGER DEFAULT 0,
                is_completed BOOLEAN DEFAULT 0,
                completed_at TIMESTAMP,
                UNIQUE(user_id, achievement_id)
            )
        ''')

        # ========== ТАБЛИЦЫ ФОРТУНЫ ==========
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fortune_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                team TEXT NOT NULL,
                amount REAL NOT NULL,
                net_amount REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fortune_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL,
                winner_team TEXT,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                yellow_pool REAL DEFAULT 0,
                red_pool REAL DEFAULT 0
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fortune_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                round_id TEXT NOT NULL,
                team TEXT NOT NULL,
                amount REAL NOT NULL,
                result TEXT NOT NULL,
                win_amount REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fortune_active_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                team TEXT NOT NULL,
                amount REAL NOT NULL,
                net_amount REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ========== ИНДЕКСЫ ==========
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_fortune_bets_round ON fortune_bets(round_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_fortune_bets_user ON fortune_bets(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_fortune_rounds_id ON fortune_rounds(round_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_fortune_history_user ON fortune_history(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_achievements_user ON user_achievements(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_achievements_completed ON user_achievements(is_completed)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ad_watch_user_type ON ad_watch_history(user_id, ad_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ad_watch_date ON ad_watch_history(watched_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON system_logs(timestamp DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_user_id ON system_logs(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_tasks ON user_tasks(user_id, task_id)')

        # ========== МИГРАЦИИ (добавление колонок если нет) ==========
        for col in ['banned_until', 'ban_reason', 'banned_by']:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} DEFAULT 0")
            except:
                pass

        for col in ['completed_achievements', 'daily_clicks']:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            except:
                pass

        for col in ['is_drawn', 'draw_time', 'global_ticket_counter', 'lottery_phase']:
            try:
                cursor.execute(f"ALTER TABLE lottery ADD COLUMN {col} DEFAULT 0")
            except:
                pass

        for col in ['fortune_bets_count', 'fortune_wins_count', 'fortune_total_bet_amount']:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            except:
                pass

        # ========== ЗАПОЛНЕНИЕ ДОСТИЖЕНИЙ ==========
        achievements_list = [
            ('autoclicker', '🏆 Автокликер', 'Сделать 50 000 кликов по монете', '🖱️', 50000),
            ('investor', '💰 Инвестор', 'Купить 30 улучшений в магазине', '📈', 30),
            ('social', '👥 Общительный', 'Пригласить 10 рефералов', '🤝', 10),
            ('gambler', '🎲 Азартный', 'Купить 100 билетов в Вызове', '🎫', 100),
            ('lucky', '🍀 Счастливчик', 'Выиграть Вызов 5 раз', '🏆', 5),
            ('liker', '👍 Подписчик', 'Поставить 200 лайков', '❤️', 200),
            ('hater', '👎 Хейтер', 'Поставить 200 дизлайков', '💔', 200),
            ('ad_lover', '📺 Любитель TV', 'Просмотреть 100 реклам', '🎬', 100),
            ('spender', '💸 Транжира', 'Потратить 50 000 WG Coin', '💎', 50000),
            ('task_master', '📋 Выполнитель', 'Выполнить 10 заданий', '✅', 10),
            ('brave', '⚔️ Бесстрашный', 'Сделать 100 ставок в Командной Фортуне', '🎲', 100),
            ('lucky_fortune', '🍀 Везучий', 'Выиграть 100 раз в Командной Фортуне', '🏆', 100),
            ('gambler_fortune', '🎰 Лудоман', 'Поставить 200 000 WG в Командной Фортуне', '💰', 200000),
            ('crazy', '🤪 Сумасшедший', 'Сделать 1000 ставок в Командной Фортуне', '🔥', 1000)
        ]

        for ach in achievements_list:
            cursor.execute('''
                INSERT OR IGNORE INTO achievements (name, display_name, description, icon, target_count)
                VALUES (?, ?, ?, ?, ?)
            ''', ach)

        # ========== ИНИЦИАЛИЗАЦИЯ ЛОТЕРЕИ ==========
        cursor.execute("SELECT * FROM lottery LIMIT 1")
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO lottery (prize_pool, tickets, winning_numbers, is_drawn, lottery_phase) VALUES (0, '[]', '', 0, 'buy')")