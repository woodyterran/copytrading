import sqlite3
import os
import pandas as pd
from datetime import datetime

DB_FILE = 'users.db'
HISTORY_DB_FILE = 'history.db'

def init_db():
    # --- 用户配置数据库 ---
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

    # 尝试添加 auto_refresh_interval 列（如果不存在）
    try:
        c.execute('ALTER TABLE users ADD COLUMN auto_refresh_interval INTEGER DEFAULT 10')
    except sqlite3.OperationalError:
        pass # 列已存在
        
    conn.commit()
    conn.close()

    # --- 历史记录数据库 ---
    init_history_db()

def init_history_db():
    conn = sqlite3.connect(HISTORY_DB_FILE)
    c = conn.cursor()
    
    # 挂单历史
    c.execute('''
        CREATE TABLE IF NOT EXISTS history_orders (
            oid TEXT PRIMARY KEY,
            timestamp INTEGER,
            target_address TEXT,
            coin TEXT,
            side TEXT,
            limit_px REAL,
            sz REAL,
            order_type TEXT,
            record_time TEXT
        )
    ''')
    
    # 成交历史
    c.execute('''
        CREATE TABLE IF NOT EXISTS history_trades (
            hash TEXT PRIMARY KEY,
            timestamp INTEGER,
            target_address TEXT,
            coin TEXT,
            side TEXT,
            px REAL,
            sz REAL,
            fee REAL,
            tid TEXT,
            record_time TEXT
        )
    ''')
    
    # 持仓历史 (快照)
    c.execute('''
        CREATE TABLE IF NOT EXISTS history_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            target_address TEXT,
            coin TEXT,
            size REAL,
            entry_px REAL,
            leverage REAL,
            record_time TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

# --- 历史记录写入函数 ---

def log_order(target_address, order):
    """记录挂单"""
    conn = sqlite3.connect(HISTORY_DB_FILE)
    c = conn.cursor()
    try:
        c.execute('''
            INSERT OR IGNORE INTO history_orders 
            (oid, timestamp, target_address, coin, side, limit_px, sz, order_type, record_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            str(order['oid']),
            int(order['timestamp']),
            target_address,
            order['coin'],
            order['side'],
            float(order['limitPx']),
            float(order['sz']),
            order.get('orderType', 'Limit'),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        conn.commit()
    except Exception as e:
        print(f"Log order error: {e}")
    finally:
        conn.close()

def log_trade(target_address, trade):
    """记录成交"""
    conn = sqlite3.connect(HISTORY_DB_FILE)
    c = conn.cursor()
    try:
        # trade 结构通常来自 info.user_fills
        # {'coin': 'ETH', 'px': '1800.5', 'sz': '0.1', 'side': 'B', 'time': 1234567890, 'hash': '...', 'fee': '0.05', 'tid': 123}
        c.execute('''
            INSERT OR IGNORE INTO history_trades 
            (hash, timestamp, target_address, coin, side, px, sz, fee, tid, record_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.get('hash') or f"{trade.get('tid')}_{trade.get('coin')}", # fallback if hash missing
            int(trade['time']),
            target_address,
            trade['coin'],
            trade['side'],
            float(trade['px']),
            float(trade['sz']),
            float(trade.get('fee', 0)),
            str(trade.get('tid', '')),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        conn.commit()
    except Exception as e:
        print(f"Log trade error: {e}")
    finally:
        conn.close()

def log_position(target_address, position):
    """记录持仓 (通常只在变化时调用)"""
    conn = sqlite3.connect(HISTORY_DB_FILE)
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO history_positions 
            (timestamp, target_address, coin, size, entry_px, leverage, record_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            int(datetime.now().timestamp() * 1000), # 使用当前时间作为快照时间
            target_address,
            position['coin'],
            float(position['szi']),
            float(position.get('entryPx', 0)),
            float(position.get('leverage', 0) if position.get('leverage') else 0), # leverage dict check
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        conn.commit()
    except Exception as e:
        print(f"Log position error: {e}")
    finally:
        conn.close()

def get_history_csv():
    """获取所有历史数据 CSV 内容 (返回字典: filename -> csv_string)"""
    conn = sqlite3.connect(HISTORY_DB_FILE)
    
    dfs = {}
    try:
        dfs['orders'] = pd.read_sql_query("SELECT * FROM history_orders ORDER BY timestamp DESC", conn)
        dfs['trades'] = pd.read_sql_query("SELECT * FROM history_trades ORDER BY timestamp DESC", conn)
        dfs['positions'] = pd.read_sql_query("SELECT * FROM history_positions ORDER BY timestamp DESC", conn)
    except Exception as e:
        print(f"Export csv error: {e}")
        return {}
    finally:
        conn.close()
        
    return {k: v.to_csv(index=False) for k, v in dfs.items()}

def get_user_config(email):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT private_key, target_address, copy_ratio, slippage, sync_mode, auto_refresh_interval FROM users WHERE email = ?', (email,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'private_key': row[0],
            'target_address': row[1],
            'copy_ratio': row[2],
            'slippage': row[3],
            'sync_mode': row[4] if len(row) > 4 else 'full',
            'auto_refresh_interval': row[5] if len(row) > 5 else 10
        }
    return None

def save_user_config(email, private_key, target_address, copy_ratio, slippage, sync_mode='full', auto_refresh_interval=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO users (email, private_key, target_address, copy_ratio, slippage, sync_mode, auto_refresh_interval)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (email, private_key, target_address, copy_ratio, slippage, sync_mode, auto_refresh_interval))
    conn.commit()
    conn.close()
