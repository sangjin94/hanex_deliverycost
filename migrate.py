import sqlite3, os

db_path = os.path.join(os.path.dirname(__file__), 'instance', 'delivery_pricing.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# transfer_rate 스키마 변경 (vehicle_type 추가, cost_per_plt 제거)
cur.execute('DROP TABLE IF EXISTS transfer_rate')
cur.execute('''CREATE TABLE transfer_rate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_center_code VARCHAR(20) NOT NULL,
    to_center_code   VARCHAR(20) NOT NULL,
    vehicle_type     VARCHAR(20) NOT NULL,
    unit_price       INTEGER NOT NULL,
    updated_at       DATETIME,
    UNIQUE(from_center_code, to_center_code, vehicle_type)
)''')

# our_center 초기화 (새 센터 데이터로 교체)
cur.execute('DELETE FROM our_center')
print('transfer_rate 재생성, our_center 초기화 완료')

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

# our_center 테이블 생성 (없으면)
cur.execute('PRAGMA table_info(our_center)')
if not cur.fetchall():
    cur.execute('''CREATE TABLE our_center (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        center_code VARCHAR(20) UNIQUE NOT NULL,
        center_name VARCHAR(100) NOT NULL,
        address VARCHAR(500),
        lat FLOAT,
        lon FLOAT,
        is_direct_hub BOOLEAN DEFAULT 1,
        memo VARCHAR(300),
        sort_order INTEGER DEFAULT 0
    )''')
    print('our_center 테이블 생성됨')

conn.commit()
cur.execute('SELECT * FROM joint_delivery_rate')
print('joint_delivery_rate:', cur.fetchall())
cur.execute('SELECT * FROM system_config')
print('system_config:', cur.fetchall())
cur.execute('SELECT count(*) FROM our_center')
print('our_center 건수:', cur.fetchone())
conn.close()
print('마이그레이션 완료')
