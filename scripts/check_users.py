import sqlite3
c = sqlite3.connect('crm_atacama.db')
tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", tables)
for t in tables:
    if 'user' in t[0].lower() or 'login' in t[0].lower() or 'admin' in t[0].lower():
        rows = c.execute(f"SELECT * FROM {t[0]} LIMIT 5").fetchall()
        print(f"\n{t[0]}:", rows)
c.close()
