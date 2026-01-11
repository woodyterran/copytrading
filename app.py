# -*- coding: utf-8 -*-
import streamlit as st
import os
import subprocess
import time
import signal
import sys
import pandas as pd
import hashlib
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import database as db

# --- 配置 ---
st.set_page_config(page_title='Hyperliquid 跟单机器人', layout='wide')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, '.env')
SCRIPT_PATH = os.path.join(BASE_DIR, 'hyperliquid_copy_trader.py')
CLIENT_SECRETS_FILE = os.path.join(BASE_DIR, 'client_secret.json')
SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile']

# 初始化数据库
db.init_db()

# --- Google Auth 辅助函数 ---
def get_auth_flow():
    # 优先尝试从 Streamlit Secrets 读取配置
    if "web" in st.secrets:
        # 动态获取 redirect_uri: 优先从 secrets 读取, 否则默认 localhost
        secrets_config = dict(st.secrets)
        redirect_uri = secrets_config["web"].get("redirect_uris", ["http://localhost:8501"])[0]
        
        return Flow.from_client_config(
            client_config=secrets_config,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
    
    # 回退到读取本地文件
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri='http://localhost:8501' 
    )

def verify_google_token(token):
    try:
        id_info = id_token.verify_oauth2_token(
            token, 
            google_requests.Request(), 
            audience=None  # 可以指定 Client ID 增加安全性
        )
        return id_info
    except ValueError:
        return None

# --- 用户隔离辅助函数 ---
def get_user_files(email):
    """根据邮箱生成唯一的文件路径"""
    # 使用邮箱哈希作为文件名的一部分，避免文件名过长或非法字符
    email_hash = hashlib.md5(email.encode()).hexdigest()
    return {
        'pid': os.path.join(BASE_DIR, f'bot_{email_hash}.pid'),
        'log': os.path.join(BASE_DIR, f'bot_{email_hash}.log')
    }

def get_bot_pid(pid_file):
    if os.path.exists(pid_file):
        try:
            with open(pid_file, 'r') as f:
                content = f.read().strip()
                if not content: return None
                pid = int(content)
            try:
                os.kill(pid, 0)
                return pid
            except OSError:
                return None
        except:
            return None
    return None

# --- 登录页面 ---
def login_page():
    st.title('🔐 登录')
    
    # 检查配置是否存在 (优先检查 st.secrets, 其次检查文件)
    has_secrets = "web" in st.secrets
    has_file = os.path.exists(CLIENT_SECRETS_FILE)
    
    if not has_secrets and not has_file:
        st.error("未找到 Google OAuth 配置。")
        st.info("请配置 `.streamlit/secrets.toml` 或上传 `client_secret.json`。")
        return

    # 处理 OAuth 回调
    if 'code' in st.query_params:
        try:
            code = st.query_params['code']
            flow = get_auth_flow()
            flow.fetch_token(code=code)
            credentials = flow.credentials
            
            # 验证 Token 获取用户信息
            user_info = verify_google_token(credentials.id_token)
            
            if user_info:
                st.session_state['user_email'] = user_info['email']
                st.session_state['user_name'] = user_info.get('name', 'User')
                # 清除 URL 参数
                st.query_params.clear()
                st.rerun()
        except Exception as e:
            st.error(f"登录失败: {e}")
            
    if st.button('使用 Google 账号登录'):
        flow = get_auth_flow()
        auth_url, _ = flow.authorization_url(prompt='consent')
        st.link_button("👉 点击跳转 Google 登录", auth_url)

# --- 主应用 ---
def main_app(email):
    st.sidebar.success(f"已登录: {email}")
    if st.sidebar.button("登出"):
        del st.session_state['user_email']
        st.rerun()

    st.title('🤖 Hyperliquid 跟单机器人控制台')

    # 获取用户文件路径
    user_files = get_user_files(email)
    PID_FILE = user_files['pid']
    LOG_FILE = user_files['log']

    # 加载用户配置
    user_config = db.get_user_config(email) or {}
    
    st.sidebar.header('⚙️ 参数配置')
    with st.sidebar.form('config_form'):
        private_key = st.text_input('私钥 (MY_PRIVATE_KEY)', value=user_config.get('private_key', ''), type='password')
        target_address = st.text_input('目标地址', value=user_config.get('target_address', '0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637'))
        copy_ratio = st.number_input('跟单比例', value=float(user_config.get('copy_ratio', 0.1)), min_value=0.01, step=0.01, format='%.2f')
        slippage = st.number_input('最大滑点', value=float(user_config.get('slippage', 0.02)), min_value=0.01, step=0.01)
        
        submitted = st.form_submit_button('保存配置')
        
        if submitted:
            db.save_user_config(email, private_key, target_address, copy_ratio, slippage)
            st.sidebar.success('配置已保存！')
            # 重新加载以更新界面
            st.rerun()

    # 如果没有配置，提示先配置
    current_target = user_config.get('target_address')
    
    # --- 机器人控制 ---
    pid = get_bot_pid(PID_FILE)
    is_running = pid is not None
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.subheader('状态控制')
        if is_running:
            st.success(f'🟢 运行中 (PID: {pid})')
            if st.button('🔴 停止机器人'):
                try:
                    os.kill(pid, signal.SIGTERM)
                    if os.path.exists(PID_FILE): os.remove(PID_FILE)
                    st.rerun()
                except Exception as e:
                    st.error(f'停止失败: {e}')
        else:
            st.warning('⚪ 已停止')
            if st.button('🟢 启动机器人'):
                # 获取最新配置用于启动进程
                cfg = db.get_user_config(email)
                if not cfg or not cfg.get('private_key'):
                    st.error("请先保存配置（特别是私钥）再启动机器人")
                else:
                    # 准备环境变量
                    env = os.environ.copy()
                    env['MY_PRIVATE_KEY'] = cfg['private_key']
                    env['TARGET_ADDRESS'] = cfg['target_address']
                    env['COPY_RATIO'] = str(cfg['copy_ratio'])
                    env['SLIPPAGE'] = str(cfg['slippage'])
                    
                    with open(LOG_FILE, 'a') as log_f:
                        proc = subprocess.Popen(
                            [sys.executable, '-u', SCRIPT_PATH],
                            stdout=log_f, stderr=log_f, cwd=BASE_DIR, env=env
                        )
                    with open(PID_FILE, 'w') as f: f.write(str(proc.pid))
                    st.rerun()

    with col2:
        st.subheader('实时日志')
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                st.code(''.join(lines[-20:]), language='text')
            if st.button('清空日志'):
                open(LOG_FILE, 'w').close()
                st.rerun()
        else:
            st.info('暂无日志')

    # --- 目标用户数据监控 ---
    st.divider()
    
    if current_target:
        col_mon, col_refresh = st.columns([3, 1])
        with col_mon:
            st.subheader(f"📊 目标用户监控: {current_target}")
        with col_refresh:
            if st.button("🔄 刷新数据"):
                st.rerun()

        try:
            info = get_hl_info()
            # 获取用户状态 (包含持仓)
            user_state = info.user_state(current_target)
            
            # 显式获取挂单
            try:
                raw_open_orders = info.open_orders(current_target)
            except Exception as e_orders:
                raw_open_orders = []
                st.warning(f"获取挂单失败: {e_orders}")

            # 调试: 显示原始数据结构以便排查
            with st.expander("🔍 查看原始 API 响应 (调试用)"):
                st.write("User State:", user_state)
                st.write("Open Orders:", raw_open_orders)
            
            tab_orders, tab_trades, tab_positions = st.tabs(["实时挂单", "近期成交", "持仓状态"])
            
            with tab_orders:
                if raw_open_orders:
                    df_orders = pd.DataFrame(raw_open_orders)
                    # 提取关键字段
                    if not df_orders.empty:
                        df_orders = df_orders[['coin', 'side', 'limitPx', 'sz', 'timestamp']]
                        df_orders['limitPx'] = df_orders['limitPx'].astype(float)
                        df_orders['sz'] = df_orders['sz'].astype(float)
                        df_orders['time'] = pd.to_datetime(df_orders['timestamp'], unit='ms')
                        
                        display_orders = df_orders[['time', 'coin', 'side', 'limitPx', 'sz']].sort_values('time', ascending=False).reset_index(drop=True)
                        display_orders.index = display_orders.index + 1
                        display_orders['time'] = format_beijing_time(display_orders['time'])
                        st.dataframe(display_orders, width='stretch', height=1050)
                else:
                    st.info("当前无挂单")
                    
            with tab_positions:
                positions = user_state.get('assetPositions', [])
                pos_data = []
                for p in positions:
                    core = p.get('position', {})
                    szi = float(core.get('szi', 0))
                    if szi != 0:
                        pos_data.append({
                            "币种": p.get('coin'),
                            "持仓量": szi,
                            "入场价": float(core.get('entryPx', 0)),
                            "未实现盈亏": float(core.get('unrealizedPnl', 0)),
                            "杠杆": core.get('leverage', {}).get('value', 0),
                            "类型": "多" if szi > 0 else "空"
                        })
                
                if pos_data:
                    df_pos = pd.DataFrame(pos_data)
                    df_pos.index = df_pos.index + 1
                    st.dataframe(df_pos, width='stretch', height=1050)
                else:
                    st.info("当前无持仓")
                    
            with tab_trades:
                try:
                    # 注意: user_fills 对于非本人地址可能返回空或报错，视 API 权限而定
                    fills = info.user_fills(current_target)
                    if fills:
                        df_fills = pd.DataFrame(fills)
                        df_fills['price'] = df_fills['px'].astype(float)
                        df_fills['size'] = df_fills['sz'].astype(float)
                        df_fills['time'] = pd.to_datetime(df_fills['time'], unit='ms')
                        
                        display_fills = df_fills[['time', 'coin', 'side', 'price', 'size', 'fee', 'closedPnl']].head(50)
                        display_fills.index = display_fills.index + 1
                        display_fills['time'] = format_beijing_time(display_fills['time'])
                        st.dataframe(display_fills, width='stretch', height=1050)
                    else:
                        st.info("暂无可见成交记录")
                except Exception as e:
                    st.warning(f"无法获取成交历史 (可能仅限私有读取): {e}")

        except Exception as e:
            st.error(f"获取链上数据失败: {e}")

@st.cache_resource
def get_hl_info():
    return Info(constants.MAINNET_API_URL, skip_ws=True)

def format_beijing_time(dt_series):
    """将时间序列转换为北京时间并格式化显示"""
    beijing_tz = pytz.timezone('Asia/Shanghai')
    
    if not pd.api.types.is_datetime64_any_dtype(dt_series):
        dt_series = pd.to_datetime(dt_series)
        
    if dt_series.dt.tz is None:
        dt_series = dt_series.dt.tz_localize('UTC')
    
    dt_series = dt_series.dt.tz_convert(beijing_tz)
    
    now = datetime.now(beijing_tz)
    today = now.date()
    yesterday = today - timedelta(days=1)
    
    def fmt(t):
        d = t.date()
        time_str = t.strftime('%H:%M:%S')
        if d == today:
            return f"今天 {time_str}"
        elif d == yesterday:
            return f"昨天 {time_str}"
        else:
            return t.strftime('%Y-%m-%d %H:%M:%S')
            
    return dt_series.apply(fmt)

# --- 入口逻辑 ---
if 'user_email' in st.session_state:
    main_app(st.session_state['user_email'])
else:
    login_page()
