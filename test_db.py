import sqlite3
import json

conn = sqlite3.connect('local.db')
c = conn.cursor()
c.execute("SELECT id, nome, status_venda, created_at FROM leads WHERE status_venda = 'venda'")
vendas = c.fetchall()

print(f"Total Vendas encontradas: {len(vendas)}")
for v in vendas:
    print(v)

c.execute("SELECT id, nome, status_venda, created_at FROM leads ORDER BY id DESC LIMIT 5")
ultimos = c.fetchall()
print(f"Ultimos 5 leads:")
for u in ultimos:
    print(u)
