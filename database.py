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
            slippage REAL
        )
    ''')
    conn.commit()
    conn.close()

def get_user_config(email):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT private_key, target_address, copy_ratio, slippage FROM users WHERE email = ?', (email,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'private_key': row[0],
            'target_address': row[1],
            'copy_ratio': row[2],
            'slippage': row[3]
        }
    return None

def save_user_config(email, private_key, target_address, copy_ratio, slippage):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO users (email, private_key, target_address, copy_ratio, slippage)
        VALUES (?, ?, ?, ?, ?)
    ''', (email, private_key, target_address, copy_ratio, slippage))
    conn.commit()
    conn.close()
