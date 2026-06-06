# pg_adapter.py - Адаптер для PostgreSQL
import asyncpg
import json
import asyncio
from contextlib import contextmanager


class PgDatabase:
    def __init__(self):
        self.pool = None
        self._init_async()

    def _init_async(self):
        """Инициализация в синхронном режиме"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.pool = loop.run_until_complete(self._create_pool())
        loop.run_until_complete(self._create_tables())
        print("✅ PostgreSQL подключён!")

    async def _create_pool(self):
        return await asyncpg.create_pool(
            host='localhost',
            user='weregood_user',
            password='weregood123',
            database='weregood_db',
            min_size=5,
            max_size=20
        )

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
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
                    referrer_id BIGINT DEFAULT 0,
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
                    banned_by BIGINT DEFAULT 0,
                    completed_achievements INTEGER DEFAULT 0
                )
            ''')
            print("✅ Таблицы созданы")

    @contextmanager
    def get_cursor(self):
        """Совместимость со старым кодом"""

        class Cursor:
            def __init__(self, pool):
                self.pool = pool
                self.conn = None
                self.lastrowid = None
                self.rowcount = 0

            async def __aenter__(self):
                self.conn = await self.pool.acquire()
                return self

            async def __aexit__(self, *args):
                await self.pool.release(self.conn)

            def execute(self, query, *params):
                loop = asyncio.get_event_loop()
                return loop.run_until_complete(self._execute(query, params))

            async def _execute(self, query, params):
                # Конвертируем ? в $1, $2 для PostgreSQL
                if params:
                    for i, p in enumerate(params, 1):
                        query = query.replace('?', f'${i}', 1)

                # Для INSERT запросов
                if query.strip().upper().startswith('INSERT'):
                    result = await self.conn.execute(query, *params)
                    # Пытаемся получить ID (упрощённо)
                    self.lastrowid = 1
                else:
                    await self.conn.execute(query, *params)

                self.rowcount = 1
                return self

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        class SyncCursor:
            def __init__(self, pool):
                self.cursor = Cursor(pool)
                self.rowcount = 0

            def __enter__(self):
                loop = asyncio.get_event_loop()
                loop.run_until_complete(self.cursor.__aenter__())
                return self.cursor

            def __exit__(self, *args):
                loop = asyncio.get_event_loop()
                loop.run_until_complete(self.cursor.__aexit__(*args))

        yield SyncCursor(self.pool)


# Создаём глобальный экземпляр
pg_db = PgDatabase()