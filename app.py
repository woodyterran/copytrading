# -*- coding: utf-8 -*-
import streamlit as st
import os
import subprocess
import time
import signal
import sys
import pandas as pd
import hashlib
import zipfile
import io
import re
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants
import database as db

from streamlit_autorefresh import st_autorefresh
import extra_streamlit_components as stx

# --- 配置 ---
st.set_page_config(page_title='Hyperliquid 跟单机器人', layout='wide')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, '.env')
SCRIPT_PATH = os.path.join(BASE_DIR, 'hyperliquid_copy_trader.py')

# 初始化数据库
db.init_db()

# @st.cache_resource
def get_cookie_manager():
    return stx.CookieManager()

cookie_manager = get_cookie_manager()

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

DEFAULT_USER_EMAIL = "admin@remote"

# --- 侧边栏逻辑 (登录/配置/控制) ---
def sidebar_logic():
    st.sidebar.title('🤖 Hyperliquid 跟单')
    
    # 检查登录状态
    is_logged_in = 'user_email' in st.session_state
    
    if is_logged_in:
        email = st.session_state['user_email']
        st.sidebar.success(f"已登录: {email}")
        
        # --- 登出按钮 ---
        if st.sidebar.button("登出"):
            try:
                cookie_manager.delete('user_email', key="del_email")
            except KeyError:
                pass # Cookie already deleted or not found
                
            if 'user_email' in st.session_state:
                del st.session_state['user_email']
            st.rerun()
            
        st.sidebar.divider()
        st.sidebar.header('⚙️ 参数配置')
        
        # 加载用户配置
        user_config = db.get_user_config(email) or {}
        
        with st.sidebar.form('config_form'):
            market_type_options = {'perps': '合约 (Perpetuals)', 'spot': '现货 (Spot)'}
            market_type_val = user_config.get('market_type', 'perps')
            
            # 兼容旧配置：如果是单字符串，转换为列表
            default_types = []
            if market_type_val:
                # 检查是否包含逗号
                if ',' in market_type_val:
                    default_types = [x.strip() for x in market_type_val.split(',')]
                elif market_type_val in market_type_options:
                    default_types = [market_type_val]
            if not default_types:
                default_types = ['perps'] # 默认选中合约

            market_types = st.multiselect(
                '交易类型',
                options=list(market_type_options.keys()),
                format_func=lambda x: market_type_options[x],
                default=default_types,
                help="可同时选择现货和合约进行跟单"
            )
            
            private_key = st.text_input('私钥 (MY_PRIVATE_KEY)', value=user_config.get('private_key', ''), type='password')
            
            # 新增：主账户地址配置
            my_address = st.text_input('主账户地址 (MY_ADDRESS)', value=user_config.get('my_address', ''), help="如果您的私钥是Agent私钥，请在此填写您的主账户地址。如果私钥即为主账户私钥，可留空。")
            
            target_address = st.text_input('目标地址', value=user_config.get('target_address', '0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637'))
            copy_ratio = st.number_input('跟单比例', value=float(user_config.get('copy_ratio', 1.0)), min_value=0.01, step=0.01, format='%.2f')
            slippage = st.number_input('最大滑点', value=float(user_config.get('slippage', 0.02)), min_value=0.01, step=0.01)
            
            sync_mode_options = {'full': '同步持仓 (Full Sync)', 'order': '仅同步下单 (Orders Only)'}
            sync_mode_val = user_config.get('sync_mode', 'full')
            sync_mode = st.radio(
                '跟单模式',
                options=list(sync_mode_options.keys()),
                format_func=lambda x: sync_mode_options[x],
                index=0 if sync_mode_val == 'full' else 1,
                help="同步持仓: 初始时将仓位调整至目标一致。\n仅同步下单: 初始不调整仓位，仅跟随后续的挂单和市价单。"
            )

            st.markdown("#### 挂单同步选项")
            sync_perp_orders = st.checkbox('同步合约挂单', value=bool(user_config.get('sync_perp_orders', True)))
            sync_spot_orders = st.checkbox('同步现货挂单', value=bool(user_config.get('sync_spot_orders', False)))
            
            auto_refresh_interval = st.number_input('监控自动刷新间隔 (秒)', value=int(user_config.get('auto_refresh_interval', 10)), min_value=1, step=1, help="设置监控目标用户数据的自动刷新时间间隔")

            submitted = st.form_submit_button('保存配置')
            
            if submitted:
                # 将列表转换为逗号分隔的字符串
                market_type_str = ",".join(market_types)
                db.save_user_config(email, private_key, target_address, copy_ratio, slippage, sync_mode, auto_refresh_interval, market_type=market_type_str, my_address=my_address, sync_perp_orders=sync_perp_orders, sync_spot_orders=sync_spot_orders)
                st.sidebar.success('✅配置已保存')

        st.sidebar.divider()
        st.sidebar.subheader('状态控制')
        
        # 获取机器人状态
        user_files = get_user_files(email)
        PID_FILE = user_files['pid']
        LOG_FILE = user_files['log']
        
        pid = get_bot_pid(PID_FILE)
        is_running = pid is not None
        
        if is_running:
            st.sidebar.success(f'🟢 运行中 (PID: {pid})')
            if st.sidebar.button('🔴 停止机器人'):
                try:
                    os.kill(pid, signal.SIGTERM)
                    if os.path.exists(PID_FILE): os.remove(PID_FILE)
                    st.rerun()
                except Exception as e:
                    st.sidebar.error(f'停止失败: {e}')
        else:
            st.sidebar.warning('⚪ 已停止')
            if st.sidebar.button('🟢 启动机器人'):
                cfg = db.get_user_config(email)
                if not cfg:
                    st.sidebar.error("请先保存配置")
                else:
                    env = os.environ.copy()
                    # 允许私钥为空（模拟模式）
                    env['MY_PRIVATE_KEY'] = cfg.get('private_key', '')
                    env['TARGET_ADDRESS'] = cfg['target_address']
                    env['COPY_RATIO'] = str(cfg['copy_ratio'])
                    env['SLIPPAGE'] = str(cfg['slippage'])
                    env['SYNC_MODE'] = str(cfg.get('sync_mode', 'full'))
                    env['AUTO_REFRESH_INTERVAL'] = str(cfg.get('auto_refresh_interval', 10))
                    env['MARKET_TYPE'] = str(cfg.get('market_type', 'perps'))
                    env['MY_ADDRESS'] = str(cfg.get('my_address', ''))
                    env['SYNC_PERP_ORDERS'] = '1' if cfg.get('sync_perp_orders', True) else '0'
                    env['SYNC_SPOT_ORDERS'] = '1' if cfg.get('sync_spot_orders', False) else '0'
                    
                    with open(LOG_FILE, 'a') as log_f:
                        proc = subprocess.Popen(
                            [sys.executable, '-u', SCRIPT_PATH],
                            stdout=log_f, stderr=log_f, cwd=BASE_DIR, env=env
                        )
                    with open(PID_FILE, 'w') as f: f.write(str(proc.pid))
                    st.rerun()
                    
        # --- 修改密码 ---
        with st.sidebar.expander("🔑 修改管理员密码"):
            with st.form("change_pwd_form"):
                old_pwd = st.text_input("原密码", type="password")
                new_pwd = st.text_input("新密码", type="password")
                confirm_pwd = st.text_input("确认新密码", type="password")
                
                if st.form_submit_button("修改密码"):
                    current_db_pwd = db.get_admin_password()
                    # 如果数据库没有密码（理论上登录时已同步），则使用默认逻辑
                    actual_current_pwd = current_db_pwd if current_db_pwd else st.secrets.get("admin_password", "admin123")
                    
                    if old_pwd != actual_current_pwd:
                        st.error("原密码错误")
                    elif new_pwd != confirm_pwd:
                        st.error("两次输入的新密码不一致")
                    elif not new_pwd:
                        st.error("新密码不能为空")
                    else:
                        db.set_admin_password(new_pwd)
                        st.success("密码修改成功！请重新登录。")
                        # 登出
                        try:
                            cookie_manager.delete('user_email', key="del_email_chpwd")
                        except KeyError:
                            pass # Cookie already deleted or not found
                            
                        if 'user_email' in st.session_state:
                            del st.session_state['user_email']
                        time.sleep(1)
                        st.rerun()
            
    else:
        # 未登录状态：显示登录表单
        st.sidebar.info("登录后可修改配置和控制机器人")
        with st.sidebar.form("login_form"):
            st.markdown("### 🔐 管理员登录")
            pwd = st.text_input("访问密码", type="password")
            if st.form_submit_button("登录"):
                # 优先从数据库获取密码，如果为空则使用 secrets 或默认值
                db_pwd = db.get_admin_password()
                correct_pwd = db_pwd if db_pwd else st.secrets.get("admin_password", "admin123")
                
                if pwd == correct_pwd:
                    # 如果数据库中没有密码，自动同步当前使用的正确密码到数据库
                    if not db_pwd:
                        db.set_admin_password(correct_pwd)
                        
                    expires_at = datetime.now() + timedelta(days=30)
                    user_email = DEFAULT_USER_EMAIL
                    cookie_manager.set('user_email', user_email, key="set_pwd_email", expires_at=expires_at)
                    st.session_state['user_email'] = user_email
                    st.session_state['user_name'] = "Admin"
                    st.rerun()
                else:
                    st.error("密码错误")

# --- 辅助函数 ---
def retry_api_call(func, *args, retries=3, delay=1, **kwargs):
    """带重试机制的 API 调用 helper"""
    last_exception = None
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if i < retries - 1:
                time.sleep(delay)
    raise last_exception

# --- 主内容区域 (公开数据) ---
def main_content():
    st.title('📊 市场数据监控')
    
    # 确定要展示的目标地址
    # 1. 优先使用已登录用户的配置
    # 2. 如果未登录，尝试获取默认管理员配置
    # 3. 允许用户在主界面手动输入/覆盖
    
    default_target = '0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637'
    default_refresh = 10
    
    # 尝试获取系统默认配置 (DEFAULT_USER_EMAIL)
    system_config = db.get_user_config(DEFAULT_USER_EMAIL)
    if system_config:
        default_target = system_config.get('target_address', default_target)
        default_refresh = system_config.get('auto_refresh_interval', 10)
        
    # 如果已登录，优先显示登录用户的配置
    if 'user_email' in st.session_state:
        user_config = db.get_user_config(st.session_state['user_email'])
        if user_config:
            default_target = user_config.get('target_address', default_target)
            default_refresh = user_config.get('auto_refresh_interval', default_refresh)

    # 允许用户临时修改监控目标 (不影响配置)
    col_t1, col_t2 = st.columns([3, 1])
    with col_t1:
        current_target = st.text_input("监控目标地址", value=default_target, help="此处修改仅用于临时查看数据，不会修改后台跟单配置")
    with col_t2:
        if st.button("🔄 立即刷新"):
            st.rerun()
            
    # 自动刷新
    st_autorefresh(interval=default_refresh * 1000, key="data_refresh")
    
    if current_target:
        try:
            info = get_hl_info()
            # 获取用户状态 (包含持仓) - 增加重试
            user_state = retry_api_call(info.user_state, current_target)
            
            # 显式获取挂单 - 增加重试
            try:
                raw_open_orders = retry_api_call(info.open_orders, current_target)
            except Exception as e_orders:
                raw_open_orders = []
                # st.warning(f"获取挂单失败: {e_orders}") # 保持界面整洁，忽略非关键错误

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
                        display_orders['time'] = format_time_with_label(display_orders['time'])
                        st.dataframe(
                            display_orders, 
                            width='stretch', 
                            height=800
                        )
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
                            "币种": core.get('coin'),
                            "持仓量": szi,
                            "入场价": float(core.get('entryPx', 0)),
                            "未实现盈亏": float(core.get('unrealizedPnl', 0)),
                            "杠杆": core.get('leverage', {}).get('value', 0),
                            "类型": "多" if szi > 0 else "空"
                        })
                
                if pos_data:
                    df_pos = pd.DataFrame(pos_data)
                    df_pos.index = df_pos.index + 1
                    st.dataframe(df_pos, width='stretch', height=800)
                else:
                    st.info("当前无持仓")
                    
            with tab_trades:
                try:
                    # 注意: user_fills 对于非本人地址可能返回空或报错，视 API 权限而定
                    fills = retry_api_call(info.user_fills, current_target)
                    if fills:
                        df_fills = pd.DataFrame(fills)
                        df_fills['price'] = df_fills['px'].astype(float)
                        df_fills['size'] = df_fills['sz'].astype(float)
                        df_fills['time'] = pd.to_datetime(df_fills['time'], unit='ms')
                        
                        display_fills = df_fills[['time', 'coin', 'side', 'price', 'size', 'fee', 'closedPnl']].head(50)
                        display_fills.index = display_fills.index + 1
                        display_fills['time'] = format_time_with_label(display_fills['time'])
                        st.dataframe(
                            display_fills, 
                            width='stretch', 
                            height=800
                        )
                    else:
                        st.info("暂无可见成交记录")
                except Exception as e:
                    st.warning(f"无法获取成交历史 (可能仅限私有读取): {e}")

        except Exception as e:
            st.error(f"获取链上数据失败: {e}")

    # --- 历史数据下载 (公开) ---
    st.divider()
    with st.expander("📥 历史数据下载 (点击展开)"):
        st.info("说明: 只有在跟单程序运行时才会持续记录历史数据。")
        
        if st.button("生成历史数据 CSV"):
            csvs = db.get_history_csv()
            if csvs:
                # 创建 ZIP 文件
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for name, data in csvs.items():
                        zf.writestr(f"history_{name}.csv", data)
                
                st.download_button(
                    label="📦 下载全部历史数据 (ZIP)",
                    data=buffer.getvalue(),
                    file_name="all_history.zip",
                    mime="application/zip"
                )
            else:
                st.warning("暂无历史数据或读取失败")

    # --- 实时日志 (仅显示默认用户的日志) ---
    # 虽然未登录，但既然是单用户系统，展示运行日志也是一种数据监控
    st.divider()
    with st.expander("📜 运行日志 (点击展开)"):
        # 始终读取默认用户的日志
        log_files = get_user_files(DEFAULT_USER_EMAIL)
        LOG_FILE = log_files['log']
        
        if os.path.exists(LOG_FILE):
            col_l1, col_l2, col_l3 = st.columns([2, 2, 2])
            with col_l1:
                log_lines_count = st.selectbox("显示行数", [20, 50, 100, 200, 500, 1000, 5000], index=0)
            
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                total_lines = len(lines)
                display_lines = lines[-log_lines_count:]
                st.code(''.join(display_lines), language='text')
                st.caption(f"共 {total_lines} 行日志，当前显示最后 {len(display_lines)} 行")

            with col_l2:
                c1, c2 = st.columns([1, 1])
                with c1:
                    if st.button('🔄 刷新日志'):
                        st.rerun()
                with c2:
                    if st.button('🗑️ 清空日志'):
                        try:
                            # 清空文件内容
                            open(LOG_FILE, 'w').close()
                            st.success("已清空")
                            time.sleep(0.5)
                            st.rerun()
                        except Exception as e:
                            st.error(f"失败: {e}")
            
            # --- 下载功能 ---
            with col_l3:
                # 1. 下载原始日志
                with open(LOG_FILE, 'rb') as f:
                    st.download_button(
                        label="📥 下载原始日志 (.log)",
                        data=f,
                        file_name=f"bot_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
                        mime="text/plain"
                    )
            
            # 2. 导出 CSV
            def parse_log_to_csv(log_path):
                data = []
                pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - (\w+) - (.*)$')
                try:
                    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            match = pattern.match(line.strip())
                            if match:
                                data.append({
                                    'Time': match.group(1),
                                    'Level': match.group(2),
                                    'Message': match.group(3)
                                })
                            elif data: # Append non-matching lines (like tracebacks) to previous message
                                data[-1]['Message'] += " | " + line.strip()
                except Exception as e:
                    return None
                
                if not data:
                    return None
                
                df = pd.DataFrame(data)
                return df.to_csv(index=False).encode('utf-8-sig')

            csv_data = parse_log_to_csv(LOG_FILE)
            if csv_data:
                st.download_button(
                    label="📊 导出日志 CSV",
                    data=csv_data,
                    file_name=f"bot_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv"
                )
        else:
            st.info('暂无日志')

@st.cache_resource
def get_hl_info():
    return Info(constants.MAINNET_API_URL, skip_ws=True)

def format_time_with_label(dt_series):
    """将时间转换为北京时间字符串，保留ISO格式以支持排序，并附加友好标签"""
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
        base_str = t.strftime('%Y-%m-%d %H:%M:%S')
        if d == today:
            return f"{base_str} (今天)"
        elif d == yesterday:
            return f"{base_str} (昨天)"
        else:
            return base_str
            
    return dt_series.apply(fmt)

# --- 入口逻辑 ---
# 本地测试版本：直接使用默认账户，移除 Google 登录
# if 'user_email' not in st.session_state:
#     st.session_state['user_email'] = 'admin@local'
#     st.session_state['user_name'] = 'Admin'

# 检查 Cookie 是否存在已登录用户
if 'user_email' not in st.session_state:
    cookie_email = cookie_manager.get('user_email')
    if cookie_email:
        st.session_state['user_email'] = cookie_email

sidebar_logic()
main_content()
