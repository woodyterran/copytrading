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
import gzip
import shutil
import hashlib
import subprocess
import fcntl

import database as db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(BASE_DIR, 'hyperliquid_copy_trader.py')
WATCHDOG_LOG = os.path.join(BASE_DIR, 'watchdog.log')
LOCK_FILE = os.path.join(BASE_DIR, '.watchdog.lock')

# 飞书查询机器人(双向应答, 单例只读服务)相关路径。
#   期望状态 = .env 已配置飞书自建应用凭证, 且不存在关停标记文件。
#   停止方法 = 创建 .feishu_bot.off(或清掉 .env 里的凭证), 看门狗即不再拉起。
ENV_FILE = os.path.join(BASE_DIR, '.env')
FEISHU_SCRIPT_PATH = os.path.join(BASE_DIR, 'feishu_bot.py')
FEISHU_PID_FILE = os.path.join(BASE_DIR, 'feishu_bot.pid')
FEISHU_LOG_FILE = os.path.join(BASE_DIR, 'feishu_bot.log')
FEISHU_OFF_FILE = os.path.join(BASE_DIR, '.feishu_bot.off')

# 日志轮转：单个日志超过 MAX_LOG_BYTES 则 copytruncate 并保留 LOG_KEEP 份 gzip 备份。
# 机器人以 append 模式写日志，truncate(0) 对正在写入的 fd 安全(下次写入从 0 开始，无空洞)。
MAX_LOG_BYTES = 50 * 1024 * 1024   # 50MB
LOG_KEEP = 3


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    print(line, flush=True)
    try:
        with open(WATCHDOG_LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def rotate_log_if_needed(path):
    """copytruncate 式轮转：超阈值则 gzip 归档为 .1.gz 并就地清空，保留 LOG_KEEP 份。"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < MAX_LOG_BYTES:
            return
        # 丢弃最旧，依次下移 .1.gz -> .2.gz -> ...
        oldest = f"{path}.{LOG_KEEP}.gz"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(LOG_KEEP - 1, 0, -1):
            src = f"{path}.{i}.gz"
            if os.path.exists(src):
                os.replace(src, f"{path}.{i + 1}.gz")
        # 当前日志压缩为 .1.gz
        with open(path, 'rb') as f_in, gzip.open(f"{path}.1.gz", 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        # 就地截断(append 写入安全)
        with open(path, 'r+') as f:
            f.truncate(0)
        log(f"日志已轮转: {os.path.basename(path)} (> {MAX_LOG_BYTES // 1024 // 1024}MB) -> .1.gz")
    except Exception as e:
        log(f"日志轮转失败 {os.path.basename(path)}: {e}")


def pid_alive_and_is_script(pid, needle):
    """判断 pid 是否存活，且命令行确实包含 needle(避免重启后 PID 复用误判)。"""
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
    return needle in out


def pid_alive_and_is_bot(pid):
    """判断 pid 是否存活，且确实是本跟单脚本。"""
    return pid_alive_and_is_script(pid, 'hyperliquid_copy_trader.py')


def _env_has_feishu_creds():
    """检查 .env 是否配置了飞书自建应用凭证(App ID / Secret 均非空)。"""
    try:
        app_id = app_secret = ''
        with open(ENV_FILE) as f:
            for line in f:
                s = line.strip()
                if s.startswith('FEISHU_APP_ID='):
                    app_id = s.split('=', 1)[1].strip()
                elif s.startswith('FEISHU_APP_SECRET='):
                    app_secret = s.split('=', 1)[1].strip()
        return bool(app_id) and bool(app_secret)
    except Exception:
        return False


def check_feishu_bot():
    """守护飞书查询机器人(单例, 只读应答服务)。
    期望状态 = 已配置凭证 且 无关停标记; 否则不干预。"""
    if os.path.exists(FEISHU_OFF_FILE):
        return  # 用户主动关停
    if not _env_has_feishu_creds():
        return  # 未配置凭证, 不启动

    pid = 0
    if os.path.exists(FEISHU_PID_FILE):
        try:
            with open(FEISHU_PID_FILE) as f:
                pid = int((f.read() or '0').strip() or 0)
        except Exception:
            pid = 0
    if pid and pid_alive_and_is_script(pid, 'feishu_bot.py'):
        return  # 健康运行中

    # (重)启动: 独立会话, 日志追加写入
    with open(FEISHU_LOG_FILE, 'a') as log_f:
        proc = subprocess.Popen(
            [sys.executable, '-u', FEISHU_SCRIPT_PATH],
            stdout=log_f, stderr=log_f, cwd=BASE_DIR, env=os.environ.copy(),
            start_new_session=True,
        )
    with open(FEISHU_PID_FILE, 'w') as f:
        f.write(str(proc.pid))
    log(f"已启动飞书查询机器人 (陈旧 PID={pid or '无'}) -> PID={proc.pid}")


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
                # 先轮转该用户的机器人/监控日志，再做存活检查
                h = hashlib.md5(email.encode()).hexdigest()
                rotate_log_if_needed(os.path.join(BASE_DIR, f'bot_{h}.log'))
                rotate_log_if_needed(os.path.join(BASE_DIR, f'monitor_{h}.log'))
                check_user(email)
            except Exception as e:
                log(f"[{email}] 检查异常: {e}")
        # 守护飞书查询机器人(单例, 与各用户无关)
        try:
            rotate_log_if_needed(FEISHU_LOG_FILE)
            check_feishu_bot()
        except Exception as e:
            log(f"飞书机器人检查异常: {e}")
        # 轮转看门狗自身日志
        rotate_log_if_needed(WATCHDOG_LOG)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == '__main__':
    main()
