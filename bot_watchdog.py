#!/usr/bin/env python3
"""跟单机器人看门狗：保证"应当运行"的机器人在重启/崩溃后自动恢复。

设计原则——PID 文件即"期望状态"，与 Streamlit 界面的启停按钮和谐共处：
  - app.py 点"启动机器人"会写 bot_<md5(email)>.pid；点"停止"会删除它。
  - 因此 PID 文件存在 == 用户希望它运行。
  - 看门狗只在"PID 文件存在但进程已死(或不是本机器人)"时才重新拉起。
  - 用户在界面主动停止(文件被删) → 看门狗不会擅自复活。

由 LaunchAgent 每 60s 及开机时调用一次。逻辑幂等，快速退出。
"""
import os
import sys
import time
import hashlib
import subprocess
import fcntl

import database as db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(BASE_DIR, 'hyperliquid_copy_trader.py')
WATCHDOG_LOG = os.path.join(BASE_DIR, 'watchdog.log')
LOCK_FILE = os.path.join(BASE_DIR, '.watchdog.lock')


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    print(line, flush=True)
    try:
        with open(WATCHDOG_LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def pid_alive_and_is_bot(pid):
    """判断 pid 是否存活，且确实是本跟单脚本(避免重启后 PID 复用误判)。"""
    try:
        os.kill(pid, 0)  # 不发信号，仅探测存活
    except (OSError, ProcessLookupError):
        return False
    try:
        out = subprocess.run(
            ['ps', '-o', 'command=', '-p', str(pid)],
            capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return True  # 探测不了就保守地认为活着，避免重复拉起
    return 'hyperliquid_copy_trader.py' in out


def bot_env(cfg):
    """按 app.py 完全一致的方式，从 DB 配置构造子进程环境变量。"""
    env = os.environ.copy()
    env['MY_PRIVATE_KEY'] = cfg.get('private_key', '') or ''
    env['TARGET_ADDRESS'] = cfg['target_address']
    env['COPY_RATIO'] = str(cfg['copy_ratio'])
    env['SLIPPAGE'] = str(cfg['slippage'])
    env['SYNC_MODE'] = str(cfg.get('sync_mode', 'full'))
    env['AUTO_REFRESH_INTERVAL'] = str(cfg.get('auto_refresh_interval', 10))
    env['MARKET_TYPE'] = str(cfg.get('market_type', 'perps'))
    env['MY_ADDRESS'] = str(cfg.get('my_address', ''))
    env['SYNC_PERP_ORDERS'] = '1' if cfg.get('sync_perp_orders', True) else '0'
    env['SYNC_SPOT_ORDERS'] = '1' if cfg.get('sync_spot_orders', False) else '0'
    env['COPY_COINS'] = str(cfg.get('copy_coins', ''))
    return env


def relaunch(email, cfg, pid_file, log_file):
    env = bot_env(cfg)
    with open(log_file, 'a') as log_f:
        proc = subprocess.Popen(
            [sys.executable, '-u', SCRIPT_PATH],
            stdout=log_f, stderr=log_f, cwd=BASE_DIR, env=env,
            start_new_session=True,
        )
    with open(pid_file, 'w') as f:
        f.write(str(proc.pid))
    has_key = bool((cfg.get('private_key') or '').strip())
    log(f"已恢复机器人 [{email}] PID={proc.pid} mode={env['SYNC_MODE']} "
        f"coins={env['COPY_COINS'] or '全部'} 私钥={'有' if has_key else '无(模拟)'}")


def check_user(email):
    h = hashlib.md5(email.encode()).hexdigest()
    pid_file = os.path.join(BASE_DIR, f'bot_{h}.pid')
    log_file = os.path.join(BASE_DIR, f'bot_{h}.log')

    # PID 文件不存在 == 用户未启动 / 已主动停止 → 不干预
    if not os.path.exists(pid_file):
        return

    try:
        with open(pid_file) as f:
            pid = int((f.read() or '0').strip() or 0)
    except Exception:
        pid = 0

    if pid and pid_alive_and_is_bot(pid):
        return  # 健康运行中

    cfg = db.get_user_config(email)
    if not cfg:
        log(f"[{email}] PID 文件存在但 DB 无配置，跳过")
        return
    log(f"[{email}] 检测到应运行但已停止 (陈旧 PID={pid})，正在恢复...")
    relaunch(email, cfg, pid_file, log_file)


def list_bot_users():
    """从 DB 枚举配置了机器人的用户邮箱。"""
    import sqlite3
    emails = []
    try:
        conn = sqlite3.connect(os.path.join(BASE_DIR, db.DB_FILE))
        for (email,) in conn.execute('SELECT email FROM users'):
            emails.append(email)
        conn.close()
    except Exception as e:
        log(f"读取用户列表失败: {e}")
    return emails


def main():
    # 单实例锁，避免 launchd 调度重叠导致重复拉起
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        return  # 上一轮还在跑，直接退出

    try:
        for email in list_bot_users():
            try:
                check_user(email)
            except Exception as e:
                log(f"[{email}] 检查异常: {e}")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == '__main__':
    main()
