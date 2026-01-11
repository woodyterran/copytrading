import streamlit as st
import os
import subprocess
import time
import signal
import sys
import pandas as pd
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

st.set_page_config(page_title='Hyperliquid 跟单机器人', layout='wide')
st.title('🤖 Hyperliquid 跟单机器人控制台')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, '.env')
SCRIPT_PATH = os.path.join(BASE_DIR, 'hyperliquid_copy_trader.py')
PID_FILE = os.path.join(BASE_DIR, 'bot.pid')
LOG_FILE = os.path.join(BASE_DIR, 'bot.log')

load_dotenv(ENV_PATH)

st.sidebar.header('⚙️ 参数配置')

with st.sidebar.form('config_form'):
    private_key = st.text_input('私钥 (MY_PRIVATE_KEY)', value=os.getenv('MY_PRIVATE_KEY', ''), type='password')
    target_address = st.text_input('目标地址', value=os.getenv('TARGET_ADDRESS', '0xdAe4DF7207feB3B350e4284C8eFe5f7DAc37f637'))
    copy_ratio = st.number_input('跟单比例', value=float(os.getenv('COPY_RATIO', '0.1')), min_value=0.01, step=0.01, format='%.2f')
    slippage = st.number_input('最大滑点', value=float(os.getenv('SLIPPAGE', '0.02')), min_value=0.01, step=0.01)
    
    submitted = st.form_submit_button('保存配置')
    
    if submitted:
        with open(ENV_PATH, 'w') as f:
            f.write(f'MY_PRIVATE_KEY={private_key}\n')
            f.write(f'TARGET_ADDRESS={target_address}\n')
            f.write(f'COPY_RATIO={copy_ratio}\n')
            f.write(f'SLIPPAGE={slippage}\n')
        st.sidebar.success('配置已保存！请重启机器人以生效。')

def get_bot_pid():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
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

pid = get_bot_pid()
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
            with open(LOG_FILE, 'a') as log_f:
                proc = subprocess.Popen(
                    [sys.executable, '-u', SCRIPT_PATH],
                    stdout=log_f, stderr=log_f, cwd=BASE_DIR, env=os.environ.copy()
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
col_mon, col_refresh = st.columns([3, 1])
with col_mon:
    st.subheader(f"📊 目标用户监控: {target_address}")
with col_refresh:
    if st.button("🔄 刷新数据"):
        st.rerun()

@st.cache_resource
def get_hl_info():
    return Info(constants.MAINNET_API_URL, skip_ws=True)

if target_address:
    try:
        info = get_hl_info()
        # 获取用户状态 (包含持仓)
        user_state = info.user_state(target_address)
        
        # 显式获取挂单 (有时 user_state 不包含 openOrders)
        try:
            raw_open_orders = info.open_orders(target_address)
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
                    st.dataframe(display_orders, use_container_width=True, height=1050)
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
                st.dataframe(df_pos, use_container_width=True, height=1050)
            else:
                st.info("当前无持仓")
                
        with tab_trades:
            try:
                # 注意: user_fills 对于非本人地址可能返回空或报错，视 API 权限而定
                fills = info.user_fills(target_address)
                if fills:
                    df_fills = pd.DataFrame(fills)
                    df_fills['price'] = df_fills['px'].astype(float)
                    df_fills['size'] = df_fills['sz'].astype(float)
                    df_fills['time'] = pd.to_datetime(df_fills['time'], unit='ms')
                    
                    display_fills = df_fills[['time', 'coin', 'side', 'price', 'size', 'fee', 'closedPnl']].head(50)
                    display_fills.index = display_fills.index + 1
                    st.dataframe(display_fills, use_container_width=True, height=1050)
                else:
                    st.info("暂无可见成交记录")
            except Exception as e:
                st.warning(f"无法获取成交历史 (可能仅限私有读取): {e}")

    except Exception as e:
        st.error(f"获取链上数据失败: {e}")

if is_running:
    time.sleep(2)
    st.rerun()
