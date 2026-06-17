import sqlite3, os

db_path = os.path.join(os.path.dirname(__file__), 'instance', 'delivery_pricing.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# joint_delivery_rate 재생성
cur.execute('DROP TABLE IF EXISTS joint_delivery_rate')
cur.execute('''CREATE TABLE joint_delivery_rate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    destination VARCHAR(100) NOT NULL UNIQUE,
    price_per_box FLOAT NOT NULL,
    memo VARCHAR(200),
    updated_at DATETIME
)''')

# system_config 없으면 생성
cur.execute('PRAGMA table_info(system_config)')
if not cur.fetchall():
    cur.execute('''CREATE TABLE system_config (
        key VARCHAR(50) PRIMARY KEY,
        value VARCHAR(500) NOT NULL,
        description VARCHAR(200),
        updated_at DATETIME
    )''')

cur.execute("INSERT OR IGNORE INTO system_config (key, value, description) VALUES ('direct_plt_threshold', '3', '직송 전환 기준 PLT 수')")
cur.execute("INSERT OR IGNORE INTO joint_delivery_rate (destination, price_per_box, memo) VALUES ('기본', 1200.0, '기본 공동배송 단가')")

conn.commit()
cur.execute('SELECT * FROM joint_delivery_rate')
print('joint_delivery_rate:', cur.fetchall())
cur.execute('SELECT * FROM system_config')
print('system_config:', cur.fetchall())
conn.close()
print('마이그레이션 완료')
