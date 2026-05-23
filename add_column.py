import sqlite3

print("Подключаюсь к database.db...")

conn = sqlite3.connect('database.db')
cursor = conn.cursor()

try:
    cursor.execute("ALTER TABLE users ADD COLUMN ton_wallet TEXT DEFAULT ''")
    print("✅ Колонка 'ton_wallet' успешно добавлена!")
except sqlite3.OperationalError as e:
    if "duplicate column name" in str(e):
        print("⚠️ Колонка 'ton_wallet' уже существует в таблице")
    else:
        print(f"❌ Ошибка: {e}")
except Exception as e:
    print(f"❌ Неожиданная ошибка: {e}")

conn.commit()
conn.close()

print("Готово!")