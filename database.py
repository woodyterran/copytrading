import sqlite3
import os

DB_FILE = 'users.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            private_key TEXT,
            target_address TEXT,
            copy_ratio REAL,
            slippage REAL,
            sync_mode TEXT DEFAULT 'full'
        )
    ''')
    # 尝试添加 sync_mode 列（如果不存在）
    try:
        c.execute('ALTER TABLE users ADD COLUMN sync_mode TEXT DEFAULT "full"')
    except sqlite3.OperationalError:
        pass # 列已存在
        
    conn.commit()
    conn.close()

def get_user_config(email):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT private_key, target_address, copy_ratio, slippage, sync_mode FROM users WHERE email = ?', (email,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'private_key': row[0],
            'target_address': row[1],
            'copy_ratio': row[2],
            'slippage': row[3],
            'sync_mode': row[4] if len(row) > 4 else 'full'
        }
    return None

def save_user_config(email, private_key, target_address, copy_ratio, slippage, sync_mode='full'):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO users (email, private_key, target_address, copy_ratio, slippage, sync_mode)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (email, private_key, target_address, copy_ratio, slippage, sync_mode))
    conn.commit()
    conn.close()
